#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
MANAGER="$ROOT/ios_multi_device_manager.py"
STATE_DIR="${STATE_DIR:-$ROOT/.runtime/multi-device-manager}"
STATE_FILE="$STATE_DIR/state.json"

# Target devices:
# - empty => auto discover all connected physical devices
# - comma-separated UDIDs => only start these devices
DEVICES="${DEVICES:-}"
RESTART="${RESTART:-1}"
ALLOW_NETWORK_DEVICES="${ALLOW_NETWORK_DEVICES:-0}"

# Port seeds
APPIUM_BASE_PORT="${APPIUM_BASE_PORT:-4723}"
WDA_BASE_PORT="${WDA_BASE_PORT:-8100}"
MJPEG_BASE_PORT="${MJPEG_BASE_PORT:-9100}"

# Bot runtime knobs
POST_SWIPE_WAIT="${POST_SWIPE_WAIT:-5.0}"
BLOCKED_SWIPE_COOLDOWN="${BLOCKED_SWIPE_COOLDOWN:-3.0}"
OPEN_RETRY_BEFORE_SWIPE="${OPEN_RETRY_BEFORE_SWIPE:-4}"
DRAW_COUNTDOWN_GRACE="${DRAW_COUNTDOWN_GRACE:-2.0}"
DRAW_POLL_INTERVAL="${DRAW_POLL_INTERVAL:-1.0}"
DRAW_RESULT_MAX_WAIT="${DRAW_RESULT_MAX_WAIT:-240}"
SHOW_XCODE_LOG="${SHOW_XCODE_LOG:-0}"

# Logs UI:
# - 1 => open one Terminal window per running device with live tail
# - 0 => do not open windows
OPEN_LOG_WINDOWS="${OPEN_LOG_WINDOWS:-1}"

# Per-device window log source: bot | appium | both
LOG_KIND="${LOG_KIND:-bot}"

if [[ ! -f "$MANAGER" ]]; then
  echo "manager not found: $MANAGER"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found in PATH."
  exit 1
fi

START_ARGS=(
  start
  --state-dir "$STATE_DIR"
  --appium-base-port "$APPIUM_BASE_PORT"
  --wda-base-port "$WDA_BASE_PORT"
  --mjpeg-base-port "$MJPEG_BASE_PORT"
  --post-swipe-wait "$POST_SWIPE_WAIT"
  --blocked-swipe-cooldown "$BLOCKED_SWIPE_COOLDOWN"
  --open-retry-before-swipe "$OPEN_RETRY_BEFORE_SWIPE"
  --draw-countdown-grace "$DRAW_COUNTDOWN_GRACE"
  --draw-poll-interval "$DRAW_POLL_INTERVAL"
  --draw-result-max-wait "$DRAW_RESULT_MAX_WAIT"
)

if [[ -n "$DEVICES" ]]; then
  START_ARGS+=(--devices "$DEVICES")
fi
if [[ "$ALLOW_NETWORK_DEVICES" == "1" ]]; then
  START_ARGS+=(--allow-network-devices)
fi
if [[ "$RESTART" == "1" ]]; then
  START_ARGS+=(--restart)
fi
if [[ "$SHOW_XCODE_LOG" == "1" ]]; then
  START_ARGS+=(--show-xcode-log)
fi

echo "[run] starting multi-device manager..."
set +e
python3 "$MANAGER" "${START_ARGS[@]}"
START_RC=$?
set -e
echo
echo "[run] manager status:"
python3 "$MANAGER" status --state-dir "$STATE_DIR" || true

if [[ "$OPEN_LOG_WINDOWS" != "1" ]]; then
  exit "$START_RC"
fi

if [[ ! -f "$STATE_FILE" ]]; then
  echo "[warn] state file not found: $STATE_FILE"
  exit "$START_RC"
fi

RUNNING_UDIDS=()
while IFS= read -r udid; do
  [[ -n "$udid" ]] || continue
  RUNNING_UDIDS+=("$udid")
done < <(python3 - "$STATE_FILE" <<'PY'
import json
import os
import signal
import sys

state_file = sys.argv[1]
try:
    data = json.load(open(state_file, "r", encoding="utf-8"))
except Exception:
    sys.exit(0)

for udid, item in sorted((data.get("devices") or {}).items()):
    try:
        appium_pid = int(((item.get("appium") or {}).get("pid")) or 0)
        bot_pid = int(((item.get("bot") or {}).get("pid")) or 0)
    except Exception:
        continue

    def alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    if alive(appium_pid) and alive(bot_pid):
        print(udid)
PY
)

if [[ "${#RUNNING_UDIDS[@]}" -eq 0 ]]; then
  echo "[warn] no running devices in state, skip opening log windows."
  exit "$START_RC"
fi

if ! command -v osascript >/dev/null 2>&1; then
  echo "[warn] osascript not found; print manual tail commands:"
  for udid in "${RUNNING_UDIDS[@]}"; do
    bot_log="$STATE_DIR/logs/${udid}.bot.log"
    appium_log="$STATE_DIR/logs/${udid}.appium.log"
    case "$LOG_KIND" in
      bot)
        echo "tail -F \"$bot_log\""
        ;;
      appium)
        echo "tail -F \"$appium_log\""
        ;;
      both)
        echo "tail -F \"$bot_log\" \"$appium_log\""
        ;;
      *)
        echo "tail -F \"$bot_log\""
        ;;
    esac
  done
  exit "$START_RC"
fi

for udid in "${RUNNING_UDIDS[@]}"; do
  bot_log="$STATE_DIR/logs/${udid}.bot.log"
  appium_log="$STATE_DIR/logs/${udid}.appium.log"

  case "$LOG_KIND" in
    bot)
      log_cmd="cd $(printf '%q' "$ROOT"); echo '[${udid}] bot log'; tail -F $(printf '%q' "$bot_log")"
      ;;
    appium)
      log_cmd="cd $(printf '%q' "$ROOT"); echo '[${udid}] appium log'; tail -F $(printf '%q' "$appium_log")"
      ;;
    both)
      log_cmd="cd $(printf '%q' "$ROOT"); echo '[${udid}] bot+appium logs'; tail -F $(printf '%q' "$bot_log") $(printf '%q' "$appium_log")"
      ;;
    *)
      log_cmd="cd $(printf '%q' "$ROOT"); echo '[${udid}] bot log'; tail -F $(printf '%q' "$bot_log")"
      ;;
  esac

  osascript -e "tell application \"Terminal\" to do script \"${log_cmd}\"" >/dev/null
done

exit "$START_RC"
