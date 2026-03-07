#!/usr/bin/env python3
"""
Simple multi-device CLI manager for iOS lucky-bag bot.

Features:
- Start one Appium + one bot process per device (UDID).
- Isolated per-device Appium/WDA/MJPEG ports.
- Persist process metadata in a local state file.
- status/stop/logs/discover commands.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
BOT_SCRIPT = ROOT / "ios_douyin_fudai_bot.py"

DEFAULT_STATE_DIR = ROOT / ".runtime" / "multi-device-manager"
DEFAULT_BUNDLE_ID = "com.ss.iphone.ugc.Aweme"
DEFAULT_XCODE_ORG_ID = "997XR67PRS"
DEFAULT_UPDATED_WDA_BUNDLE_ID = "com.see2see.livecontainer"
EXCLUDED_DEVICE_MODEL_NAMES = {"iphone 13 pro max"}
EXCLUDED_DEVICE_PRODUCT_TYPES = {"iphone14,3"}


def _now_ts() -> float:
    return time.time()


def _load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {"version": 1, "devices": {}}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "devices": {}}
    if not isinstance(data, dict):
        return {"version": 1, "devices": {}}
    if "devices" not in data or not isinstance(data["devices"], dict):
        data["devices"] = {}
    data.setdefault("version", 1)
    return data


def _save_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _terminate_process_group(pid: int | None, grace_seconds: float = 5.0) -> bool:
    if pid is None or pid <= 0:
        return True
    if not _is_pid_alive(pid):
        return True
    try:
        pgid = os.getpgid(pid)
    except Exception:
        pgid = None

    def _send(sig: int) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
        except Exception:
            pass

    _send(signal.SIGTERM)
    deadline = time.time() + max(0.1, grace_seconds)
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.2)

    _send(signal.SIGKILL)
    time.sleep(0.3)
    return not _is_pid_alive(pid)


def _collect_pids_by_pattern(pattern: str) -> list[int]:
    cmd = ["pgrep", "-if", pattern]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=8)
    except subprocess.CalledProcessError as e:
        if e.returncode == 1:
            return []
        return []
    except Exception:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            pid = int(s)
        except Exception:
            continue
        if pid > 0 and pid != os.getpid():
            pids.append(pid)
    return sorted(set(pids))


def _terminate_pids(pids: list[int], grace_seconds: float = 2.0) -> bool:
    alive = [pid for pid in pids if _is_pid_alive(pid)]
    if not alive:
        return True
    for pid in alive:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    deadline = time.time() + max(0.1, grace_seconds)
    while time.time() < deadline:
        alive = [pid for pid in alive if _is_pid_alive(pid)]
        if not alive:
            return True
        time.sleep(0.15)
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    time.sleep(0.2)
    return all(not _is_pid_alive(pid) for pid in alive)


def _cleanup_wda_build_processes(udid: str) -> bool:
    pids = _collect_pids_by_pattern(f"xcodebuild.*{udid}")
    return _terminate_pids(pids, grace_seconds=2.0)


def _is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.35)
    try:
        return sock.connect_ex((host, int(port))) == 0
    finally:
        sock.close()


def _wait_port_up(port: int, timeout_seconds: float = 20.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _is_port_listening(port):
            return True
        time.sleep(0.25)
    return False


def _allocate_port(start_port: int, reserved: set[int]) -> int:
    port = max(1, int(start_port))
    while True:
        if port not in reserved and not _is_port_listening(port):
            reserved.add(port)
            return port
        port += 1


def _parse_devices_arg(raw: str | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for part in raw.split(","):
        v = part.strip()
        if v:
            out.append(v)
    return out


def _normalize_model_text(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _is_excluded_device_model(*name_like_fields: str, product_type: str = "") -> bool:
    normalized_product = _normalize_model_text(product_type)
    if normalized_product in EXCLUDED_DEVICE_PRODUCT_TYPES:
        return True
    for raw in name_like_fields:
        normalized = _normalize_model_text(raw)
        if not normalized:
            continue
        for model_name in EXCLUDED_DEVICE_MODEL_NAMES:
            if model_name in normalized:
                return True
    return False


def _discover_connected_udids_from_devicectl(only_wired: bool = True) -> list[str]:
    if shutil.which("xcrun") is None:
        return []
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="ios-devices-", suffix=".json", delete=False) as tf:
            tmp_path = tf.name
        subprocess.check_output(
            ["xcrun", "devicectl", "list", "devices", "--json-output", tmp_path],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
        raw = Path(tmp_path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return []
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    result = data.get("result", {}) if isinstance(data, dict) else {}
    devices = result.get("devices", []) if isinstance(result, dict) else []
    out: list[str] = []
    for item in devices:
        if not isinstance(item, dict):
            continue
        hw = item.get("hardwareProperties") or {}
        conn = item.get("connectionProperties") or {}
        dev = item.get("deviceProperties") or {}
        if not isinstance(hw, dict) or not isinstance(conn, dict) or not isinstance(dev, dict):
            continue

        udid = str(hw.get("udid") or "").strip()
        if not udid:
            continue
        if str(hw.get("reality") or "").lower() not in ("", "physical"):
            continue

        platform = str(hw.get("platform") or "").strip().lower()
        if platform != "ios":
            continue
        device_type = str(hw.get("deviceType") or "").strip().lower()
        if device_type not in ("iphone", "ipad"):
            continue

        pairing_state = str(conn.get("pairingState") or "").strip().lower()
        if pairing_state and pairing_state != "paired":
            continue

        product_type = str(hw.get("productType") or hw.get("thinningProductType") or "").strip()
        marketing_name = str(hw.get("marketingName") or "").strip()
        device_name = str(dev.get("name") or "").strip()
        if _is_excluded_device_model(
            device_name,
            marketing_name,
            product_type=product_type,
        ):
            continue

        transport_type = str(conn.get("transportType") or "").strip().lower()
        if only_wired and transport_type and transport_type != "wired":
            continue
        out.append(udid)
    return sorted(set(out))


def _discover_connected_udids_from_xctrace() -> list[str]:
    cmd = ["xcrun", "xctrace", "list", "devices"]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=15)
    except Exception:
        return []
    udids: list[str] = []
    p = re.compile(r"\(([0-9A-Fa-f-]{20,})\)\s*$")
    for line in out.splitlines():
        s = line.strip()
        if (
            not s
            or "Simulator" in s
            or "Mac" in s
            or "Watch" in s
            or "Apple Watch" in s
            or "vision" in s
            or " - Connecting " in f" {s} "
        ):
            continue
        if _is_excluded_device_model(s):
            continue
        m = p.search(s)
        if m:
            udids.append(m.group(1))
    return udids


def _discover_connected_udids(only_wired: bool = True) -> list[str]:
    udids = _discover_connected_udids_from_devicectl(only_wired=only_wired)
    if udids:
        return udids
    # Fallback for environments where devicectl metadata is unavailable.
    return _discover_connected_udids_from_xctrace()


def _tail_lines(path: Path, lines: int) -> str:
    if not path.exists():
        return f"[missing] {path}"
    dq: deque[str] = deque(maxlen=max(1, lines))
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                dq.append(ln.rstrip("\n"))
    except Exception as e:
        return f"[read-error] {path}: {e}"
    return "\n".join(dq)


def _build_bot_cmd(
    args: argparse.Namespace,
    udid: str,
    appium_port: int,
    wda_local_port: int,
    mjpeg_server_port: int,
    derived_data_path: Path,
) -> list[str]:
    cmd = [
        sys.executable,
        str(BOT_SCRIPT),
        "--udid",
        udid,
        "--appium",
        f"http://127.0.0.1:{appium_port}",
        "--bundle-id",
        args.bundle_id,
        "--max-minutes",
        str(args.max_minutes),
        "--xcode-org-id",
        args.xcode_org_id,
        "--updated-wda-bundle-id",
        args.updated_wda_bundle_id,
        "--blocked-swipe-cooldown",
        str(args.blocked_swipe_cooldown),
        "--open-retry-before-swipe",
        str(args.open_retry_before_swipe),
        "--post-swipe-wait",
        str(args.post_swipe_wait),
        "--draw-countdown-grace",
        str(args.draw_countdown_grace),
        "--draw-poll-interval",
        str(args.draw_poll_interval),
        "--draw-result-max-wait",
        str(args.draw_result_max_wait),
        "--wda-launch-timeout-ms",
        str(args.wda_launch_timeout_ms),
        "--wda-connection-timeout-ms",
        str(args.wda_connection_timeout_ms),
        "--wda-startup-retries",
        str(args.wda_startup_retries),
        "--wda-startup-retry-interval-ms",
        str(args.wda_startup_retry_interval_ms),
        "--wait-for-idle-timeout",
        str(args.wait_for_idle_timeout),
        "--wda-local-port",
        str(wda_local_port),
        "--mjpeg-server-port",
        str(mjpeg_server_port),
        "--derived-data-path",
        str(derived_data_path),
    ]
    if args.allow_provisioning_updates:
        cmd.append("--allow-provisioning-updates")
    if args.allow_provisioning_device_registration:
        cmd.append("--allow-provisioning-device-registration")
    if args.use_new_wda:
        cmd.append("--use-new-wda")
    if args.wait_for_quiescence:
        cmd.append("--wait-for-quiescence")
    if args.show_xcode_log:
        cmd.append("--show-xcode-log")

    for token in args.bot_extra_arg:
        if token:
            cmd.append(token)
    if args.bot_extra:
        cmd.extend(shlex.split(args.bot_extra))
    return cmd


def _spawn_logged_process(cmd: list[str], log_file: Path) -> subprocess.Popen[bytes]:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    f = log_file.open("ab")
    try:
        return subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        f.close()


def cmd_discover(args: argparse.Namespace) -> int:
    udids = _discover_connected_udids(only_wired=not args.allow_network_devices)
    if not udids:
        if args.allow_network_devices:
            print("No Appium-ready iOS devices detected (iPhone 13 Pro Max is excluded by model policy).")
        else:
            print("No Appium-ready wired iOS devices detected (iPhone 13 Pro Max is excluded by model policy).")
        return 1
    for u in udids:
        print(u)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state_file = Path(args.state_dir).resolve() / "state.json"
    state = _load_state(state_file)
    devices = state.get("devices", {})
    if not devices:
        print("No managed devices in state.")
        return 0

    print("UDID | STATUS | APPIUM(port,pid) | BOT(pid) | WDA/MJPEG")
    for udid in sorted(devices.keys()):
        item = devices.get(udid, {})
        appium = item.get("appium", {})
        bot = item.get("bot", {})
        appium_pid = int(appium.get("pid", 0) or 0)
        bot_pid = int(bot.get("pid", 0) or 0)
        appium_alive = _is_pid_alive(appium_pid)
        bot_alive = _is_pid_alive(bot_pid)
        if appium_alive and bot_alive:
            status = "RUNNING"
        elif appium_alive or bot_alive:
            status = "PARTIAL"
        else:
            status = "STOPPED"
        print(
            f"{udid} | {status} | "
            f"{appium.get('port','-')},{appium_pid} | "
            f"{bot_pid} | "
            f"{item.get('wda_local_port','-')}/{item.get('mjpeg_server_port','-')}"
        )
    return 0


def _stop_one_device(state: dict[str, Any], udid: str) -> tuple[bool, str]:
    item = state.get("devices", {}).get(udid)
    if not item:
        return False, f"{udid}: not found in state."
    appium_pid = int(item.get("appium", {}).get("pid", 0) or 0)
    bot_pid = int(item.get("bot", {}).get("pid", 0) or 0)

    bot_ok = _terminate_process_group(bot_pid, grace_seconds=4.0)
    appium_ok = _terminate_process_group(appium_pid, grace_seconds=5.0)
    wda_ok = _cleanup_wda_build_processes(udid)
    state["devices"].pop(udid, None)
    return bot_ok and appium_ok and wda_ok, f"{udid}: stop bot={bot_ok}, appium={appium_ok}, wda_build={wda_ok}"


def cmd_stop(args: argparse.Namespace) -> int:
    state_file = Path(args.state_dir).resolve() / "state.json"
    state = _load_state(state_file)
    devices = state.get("devices", {})
    if not devices:
        print("No managed devices in state.")
        return 0

    targets = _parse_devices_arg(args.devices)
    if not targets:
        targets = sorted(devices.keys())

    ok = True
    for udid in targets:
        success, msg = _stop_one_device(state, udid)
        ok = ok and success
        print(msg)
    _save_state(state_file, state)
    return 0 if ok else 1


def cmd_logs(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    state_file = state_dir / "state.json"
    state = _load_state(state_file)
    item = state.get("devices", {}).get(args.device)
    if not item:
        print(f"Device not found in state: {args.device}")
        return 1

    if args.kind in ("appium", "both"):
        appium_log = Path(item.get("appium", {}).get("log", ""))
        print("===== APPium LOG =====")
        print(_tail_lines(appium_log, args.lines))

    if args.kind in ("bot", "both"):
        bot_log = Path(item.get("bot", {}).get("log", ""))
        print("===== BOT LOG =====")
        print(_tail_lines(bot_log, args.lines))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    if not BOT_SCRIPT.exists():
        print(f"Bot script not found: {BOT_SCRIPT}")
        return 2
    if shutil.which("appium") is None:
        print("appium command not found in PATH.")
        return 2

    state_dir = Path(args.state_dir).resolve()
    logs_dir = state_dir / "logs"
    derived_dir = state_dir / "derived_data"
    state_file = state_dir / "state.json"
    state = _load_state(state_file)
    devices_state: dict[str, Any] = state.setdefault("devices", {})

    appium_ready_udids = _discover_connected_udids(only_wired=not args.allow_network_devices)
    appium_ready_set = set(appium_ready_udids)

    requested_udids = _parse_devices_arg(args.devices)
    if requested_udids:
        if appium_ready_set:
            skipped = [u for u in requested_udids if u not in appium_ready_set]
            for u in skipped:
                hint = "wired iOS device required" if not args.allow_network_devices else "device not appium-ready"
                print(f"[skip] {u}: {hint}")
            udids = [u for u in requested_udids if u in appium_ready_set]
        else:
            # If discovery fails unexpectedly, keep explicit user targets.
            udids = requested_udids
    else:
        udids = appium_ready_udids

    if not udids:
        if args.allow_network_devices:
            print("No target devices. Pass --devices or connect appium-ready iOS devices (iPhone 13 Pro Max excluded).")
        else:
            print("No target devices. Pass --devices or connect wired appium-ready iOS devices (iPhone 13 Pro Max excluded).")
        return 2

    reserved_appium: set[int] = set()
    reserved_wda: set[int] = set()
    reserved_mjpeg: set[int] = set()
    for item in devices_state.values():
        if isinstance(item, dict):
            appium = item.get("appium", {})
            if isinstance(appium, dict):
                p = appium.get("port")
                if isinstance(p, int):
                    reserved_appium.add(p)
            p = item.get("wda_local_port")
            if isinstance(p, int):
                reserved_wda.add(p)
            p = item.get("mjpeg_server_port")
            if isinstance(p, int):
                reserved_mjpeg.add(p)

    appium_seed = int(args.appium_base_port)
    wda_seed = int(args.wda_base_port)
    mjpeg_seed = int(args.mjpeg_base_port)

    any_fail = False
    for udid in udids:
        existing = devices_state.get(udid, {})
        existing_bot_pid = int(existing.get("bot", {}).get("pid", 0) or 0) if isinstance(existing, dict) else 0
        existing_appium_pid = int(existing.get("appium", {}).get("pid", 0) or 0) if isinstance(existing, dict) else 0

        if _is_pid_alive(existing_bot_pid) and _is_pid_alive(existing_appium_pid):
            if not args.restart:
                print(f"[skip] {udid} is already running.")
                continue
            success, msg = _stop_one_device(state, udid)
            print(msg)
            if not success:
                any_fail = True
                continue

        # Clean up stale WDA xcodebuild processes before relaunch for this UDID.
        _cleanup_wda_build_processes(udid)

        appium_port = _allocate_port(appium_seed, reserved_appium)
        wda_port = _allocate_port(wda_seed, reserved_wda)
        mjpeg_port = _allocate_port(mjpeg_seed, reserved_mjpeg)
        appium_seed = appium_port + 1
        wda_seed = wda_port + 1
        mjpeg_seed = mjpeg_port + 1

        appium_log = logs_dir / f"{udid}.appium.log"
        bot_log = logs_dir / f"{udid}.bot.log"
        derived_path = derived_dir / udid
        derived_path.mkdir(parents=True, exist_ok=True)

        appium_cmd = ["appium", "-p", str(appium_port)]
        appium_proc = _spawn_logged_process(appium_cmd, appium_log)
        if not _wait_port_up(appium_port, timeout_seconds=25.0):
            _terminate_process_group(appium_proc.pid, grace_seconds=2.0)
            print(f"[fail] {udid}: appium did not listen on {appium_port}")
            any_fail = True
            continue

        bot_cmd = _build_bot_cmd(
            args=args,
            udid=udid,
            appium_port=appium_port,
            wda_local_port=wda_port,
            mjpeg_server_port=mjpeg_port,
            derived_data_path=derived_path,
        )
        bot_proc = _spawn_logged_process(bot_cmd, bot_log)
        time.sleep(1.8)
        if bot_proc.poll() is not None:
            _terminate_process_group(appium_proc.pid, grace_seconds=2.0)
            print(f"[fail] {udid}: bot exited early (code={bot_proc.returncode})")
            print(_tail_lines(bot_log, 50))
            any_fail = True
            continue

        devices_state[udid] = {
            "udid": udid,
            "started_ts": _now_ts(),
            "appium": {
                "pid": int(appium_proc.pid),
                "port": int(appium_port),
                "log": str(appium_log),
            },
            "bot": {
                "pid": int(bot_proc.pid),
                "log": str(bot_log),
                "cmd": bot_cmd,
            },
            "wda_local_port": int(wda_port),
            "mjpeg_server_port": int(mjpeg_port),
            "derived_data_path": str(derived_path),
        }
        _save_state(state_file, state)
        print(
            f"[ok] {udid}: appium={appium_port} (pid={appium_proc.pid}), "
            f"wda={wda_port}, mjpeg={mjpeg_port}, bot_pid={bot_proc.pid}"
        )

    _save_state(state_file, state)
    return 1 if any_fail else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage multi-device iOS lucky-bag bot processes.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="List connected physical iOS device UDIDs.")
    p_discover.add_argument(
        "--allow-network-devices",
        action="store_true",
        help="Include non-wired iOS devices if discoverable.",
    )
    p_discover.set_defaults(func=cmd_discover)

    p_status = sub.add_parser("status", help="Show managed device process status.")
    p_status.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop", help="Stop managed processes.")
    p_stop.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    p_stop.add_argument("--devices", help="Comma-separated UDIDs. Empty means stop all.")
    p_stop.set_defaults(func=cmd_stop)

    p_logs = sub.add_parser("logs", help="Show recent logs of one device.")
    p_logs.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    p_logs.add_argument("--device", required=True)
    p_logs.add_argument("--kind", choices=("bot", "appium", "both"), default="both")
    p_logs.add_argument("--lines", type=int, default=80)
    p_logs.set_defaults(func=cmd_logs)

    p_start = sub.add_parser("start", help="Start appium+bot for multiple devices.")
    p_start.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    p_start.add_argument(
        "--devices",
        help="Comma-separated UDIDs. Empty means auto-discover appium-ready iOS devices.",
    )
    p_start.add_argument(
        "--allow-network-devices",
        action="store_true",
        help="Include non-wired iOS devices if discoverable.",
    )
    p_start.add_argument("--restart", action="store_true", help="Restart devices already running in manager state.")
    p_start.add_argument("--appium-base-port", type=int, default=4723)
    p_start.add_argument("--wda-base-port", type=int, default=8100)
    p_start.add_argument("--mjpeg-base-port", type=int, default=9100)

    p_start.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID)
    p_start.add_argument("--max-minutes", type=int, default=0)
    p_start.add_argument("--xcode-org-id", default=DEFAULT_XCODE_ORG_ID)
    p_start.add_argument("--updated-wda-bundle-id", default=DEFAULT_UPDATED_WDA_BUNDLE_ID)

    p_start.add_argument("--blocked-swipe-cooldown", type=float, default=3.0)
    p_start.add_argument("--open-retry-before-swipe", type=int, default=4)
    p_start.add_argument("--post-swipe-wait", type=float, default=5.0)
    p_start.add_argument("--draw-countdown-grace", type=float, default=2.0)
    p_start.add_argument("--draw-poll-interval", type=float, default=1.0)
    p_start.add_argument("--draw-result-max-wait", type=int, default=240)

    p_start.add_argument("--wda-launch-timeout-ms", type=int, default=120000)
    p_start.add_argument("--wda-connection-timeout-ms", type=int, default=120000)
    p_start.add_argument("--wda-startup-retries", type=int, default=4)
    p_start.add_argument("--wda-startup-retry-interval-ms", type=int, default=25000)
    p_start.add_argument("--wait-for-idle-timeout", type=float, default=0.0)

    p_start.add_argument("--wait-for-quiescence", action="store_true", default=False)
    p_start.add_argument("--show-xcode-log", action="store_true", default=False)

    p_start.add_argument("--allow-provisioning-updates", dest="allow_provisioning_updates", action="store_true")
    p_start.add_argument(
        "--allow-provisioning-device-registration",
        dest="allow_provisioning_device_registration",
        action="store_true",
    )
    p_start.add_argument("--use-new-wda", dest="use_new_wda", action="store_true")
    p_start.set_defaults(
        allow_provisioning_updates=True,
        allow_provisioning_device_registration=True,
        use_new_wda=True,
    )

    p_start.add_argument("--bot-extra-arg", action="append", default=[], help="Append one raw extra bot arg.")
    p_start.add_argument("--bot-extra", default="", help="Extra bot args string, parsed by shell rules.")
    p_start.set_defaults(func=cmd_start)

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
