#!/usr/bin/env python3
"""
iOS Douyin lucky-bag auto helper (Appium + OCR fallback).

Usage example:
  python3 scripts/ios_douyin_fudai_bot.py \
    --udid <YOUR_UDID> \
    --appium http://127.0.0.1:4723 \
    --max-minutes 20

Install deps:
  pip install Appium-Python-Client pillow numpy rapidocr-onnxruntime
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
from appium import webdriver
from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy
from PIL import Image

try:
    from rapidocr_onnxruntime import RapidOCR
except Exception:
    RapidOCR = None


JOIN_KEYWORDS = [
    "去参与",
    "立即参与",
    "参与抽奖",
    "马上参与",
    "立刻参与",
]

OPEN_KEYWORDS = [
    "福袋",
]

TASK_KEYWORDS = [
    "一键发表评论",
    "一键参与",
    "去完成",
    "去参与",
    "立即参与",
    "参与",
    "去评论",
    "发表评论",
    "去抢",
    "去领取",
    "参与任务",
]

SUCCESS_KEYWORDS = [
    "已参与",
    "参与成功",
]

RESULT_WIN_KEYWORDS = [
    "恭喜抽中",
    "恭喜你抽中",
    "恭喜你中奖了",
    "已中奖",
    "抽中福袋",
]

RESULT_LOSE_KEYWORDS = [
    "未中奖",
    "很遗憾",
    "下次再来",
    "擦肩而过",
]

RESULT_WIN_PATTERNS = [
    re.compile(r"恭喜.*抽中"),
    re.compile(r"恭喜.*中奖"),
    re.compile(r"已中奖"),
    re.compile(r"抽中.*福袋"),
]

RESULT_LOSE_PATTERNS = [
    re.compile(r"未中奖"),
    re.compile(r"很遗憾"),
    re.compile(r"下次再来"),
    re.compile(r"擦肩而过"),
]

DIAMOND_BAG_KEYWORDS = [
    "钻石",
    "抖币",
    "音浪",
]

PHYSICAL_BAG_HINT_KEYWORDS = [
    "实物",
    "商品",
    "礼品",
    "包邮",
]

TASK_UNFINISHED_KEYWORDS = [
    "未达成",
    "未完成",
]

TASK_ACTION_TEXT_KEYWORDS = [
    "一键发表评论",
    "一键参与",
    "去完成",
    "去参与",
    "立即参与",
    "去领取",
    "去抢",
]

TASK_ACTION_BLOCKLIST = [
    "参与条件",
    "福袋规则",
    "任务说明",
]

TASK_TEXT_BLOCKLIST = [
    "人已参与",
    "已参与人数",
    "共",
    "剩余",
    "开奖",
]

BLOCKED_KEYWORDS = [
    "暂未开始",
    "活动已结束",
    "已结束",
    "不可参与",
    "无法参与",
    "不在活动时间",
    "资格不足",
    "已抢完",
    "人数已满",
]

CLOSE_KEYWORDS = [
    "关闭",
    "取消",
    "我知道了",
    "稍后再说",
    "返回",
]

PANEL_HINT_KEYWORDS = [
    "福袋规则",
    "参与条件",
    "去发表评论",
    "倒计时",
    "参考价值",
]

OPEN_TEXT_BLOCKLIST = [
    "没有抽中",
    "未抽中",
    "抽中福袋",
    "已开奖",
    "开奖结果",
]

ELEMENT_TYPES = (
    "XCUIElementTypeButton",
    "XCUIElementTypeStaticText",
    "XCUIElementTypeOther",
)


@dataclass
class Hit:
    text: str
    x: int
    y: int
    source: str


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_driver(
    appium_url: str,
    udid: str,
    bundle_id: str,
    no_reset: bool,
    xcode_org_id: Optional[str] = None,
    updated_wda_bundle_id: Optional[str] = None,
    show_xcode_log: bool = False,
    allow_provisioning_updates: bool = False,
    allow_provisioning_device_registration: bool = False,
    wda_launch_timeout_ms: int = 120000,
    wda_connection_timeout_ms: int = 120000,
    use_new_wda: bool = False,
) -> webdriver.Remote:
    opts = XCUITestOptions()
    opts.platform_name = "iOS"
    opts.automation_name = "XCUITest"
    opts.udid = udid
    opts.bundle_id = bundle_id
    opts.set_capability("noReset", no_reset)
    if xcode_org_id:
        opts.set_capability("xcodeOrgId", xcode_org_id)
        opts.set_capability("xcodeSigningId", "Apple Development")
    if updated_wda_bundle_id:
        opts.set_capability("updatedWDABundleId", updated_wda_bundle_id)
    if show_xcode_log:
        opts.set_capability("showXcodeLog", True)
    if allow_provisioning_updates:
        opts.set_capability("allowProvisioningUpdates", True)
    if allow_provisioning_device_registration:
        opts.set_capability("allowProvisioningDeviceRegistration", True)
    opts.set_capability("wdaLaunchTimeout", int(wda_launch_timeout_ms))
    opts.set_capability("wdaConnectionTimeout", int(wda_connection_timeout_ms))
    opts.set_capability("useNewWDA", bool(use_new_wda))
    return webdriver.Remote(appium_url, options=opts)


def tap(driver: webdriver.Remote, x: int, y: int) -> None:
    driver.execute_script("mobile: tap", {"x": int(x), "y": int(y)})


def native_candidates(driver: webdriver.Remote, keywords: Iterable[str]) -> list[Hit]:
    expr_kw = " OR ".join(
        [f"name CONTAINS '{k}' OR label CONTAINS '{k}' OR value CONTAINS '{k}'" for k in keywords]
    )
    expr_type = " OR ".join([f"type == '{t}'" for t in ELEMENT_TYPES])
    predicate = f"({expr_type}) AND ({expr_kw})"
    elements = driver.find_elements(AppiumBy.IOS_PREDICATE, predicate)

    hits: list[Hit] = []
    for el in elements:
        try:
            rect = el.rect
            x = int(rect["x"] + rect["width"] / 2)
            y = int(rect["y"] + rect["height"] / 2)
            text = (el.get_attribute("label") or el.get_attribute("name") or "").strip()
            if rect["width"] < 8 or rect["height"] < 8:
                continue
            hits.append(Hit(text=text, x=x, y=y, source="native"))
        except Exception:
            continue
    return hits


def pick_best_hit(hits: list[Hit], keyword_priority: list[str]) -> Optional[Hit]:
    if not hits:
        return None
    best: Optional[Hit] = None
    best_rank = 10**9
    for h in hits:
        rank = 10**9
        for i, k in enumerate(keyword_priority):
            if k in h.text:
                rank = i
                break
        if rank < best_rank:
            best_rank = rank
            best = h
    return best or hits[0]


def filter_short_hits(hits: list[Hit], max_len: int) -> list[Hit]:
    out: list[Hit] = []
    for h in hits:
        t = (h.text or "").strip()
        if not t:
            continue
        if len(t) <= max_len:
            out.append(h)
    return out


def screenshot_np_safe(driver: webdriver.Remote) -> np.ndarray:
    png = driver.get_screenshot_as_png()
    from io import BytesIO

    img = Image.open(BytesIO(png))
    return np.array(img.convert("RGB"))


def ocr_candidates(driver: webdriver.Remote, ocr_engine: RapidOCR, keywords: Iterable[str]) -> list[Hit]:
    image = screenshot_np_safe(driver)
    result, _ = ocr_engine(image)
    if not result:
        return []

    hits: list[Hit] = []
    for item in result:
        # item: [box, text, score]
        box, text, score = item
        try:
            s = float(score)
        except Exception:
            s = 0.0
        if not text or s < 0.45:
            continue
        txt = str(text).strip()
        if not any(k in txt for k in keywords):
            continue
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
        x = int(sum(xs) / len(xs))
        y = int(sum(ys) / len(ys))
        hits.append(Hit(text=txt, x=x, y=y, source="ocr"))
    return hits


def contains_success(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR]) -> bool:
    native = native_candidates(driver, SUCCESS_KEYWORDS)
    native = [h for h in native if _is_valid_success_text(h.text)]
    if native:
        return True
    if ocr_engine is None:
        return False
    ocr_hits = ocr_candidates(driver, ocr_engine, SUCCESS_KEYWORDS)
    ocr_hits = [h for h in ocr_hits if _is_valid_success_text(h.text)]
    return bool(ocr_hits)


def visible_texts(driver: webdriver.Remote) -> list[str]:
    texts: list[str] = []
    elements = driver.find_elements(
        AppiumBy.IOS_PREDICATE,
        "type == 'XCUIElementTypeStaticText' OR type == 'XCUIElementTypeButton'",
    )
    for el in elements:
        try:
            t = (el.get_attribute("label") or el.get_attribute("name") or "").strip()
        except Exception:
            t = ""
        if t:
            texts.append(t)
    return texts


def parse_countdown_seconds(texts: list[str]) -> Optional[int]:
    best: Optional[int] = None
    mmss = re.compile(r"(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)")
    sec_only = re.compile(r"(?<!\d)(\d{1,3})\s*秒")
    min_sec = re.compile(r"(?<!\d)(\d{1,2})\s*分(?:钟)?\s*(\d{1,2})\s*秒")
    for t in texts:
        for m in mmss.finditer(t):
            sec = int(m.group(1)) * 60 + int(m.group(2))
            if 0 <= sec <= 3600 and (best is None or sec < best):
                best = sec
        for m in min_sec.finditer(t):
            sec = int(m.group(1)) * 60 + int(m.group(2))
            if 0 <= sec <= 3600 and (best is None or sec < best):
                best = sec
        for m in sec_only.finditer(t):
            sec = int(m.group(1))
            if 0 <= sec <= 3600 and (best is None or sec < best):
                best = sec
    return best


def detect_draw_result(texts: list[str]) -> Optional[str]:
    joined = " | ".join(texts)
    if any(k in joined for k in RESULT_LOSE_KEYWORDS):
        return "lose"
    if any(p.search(joined) for p in RESULT_LOSE_PATTERNS):
        return "lose"
    if any(k in joined for k in RESULT_WIN_KEYWORDS):
        return "win"
    if any(p.search(joined) for p in RESULT_WIN_PATTERNS):
        return "win"
    return None


def handle_post_join_draw_flow(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    draw_countdown_grace: float,
    draw_poll_interval: float,
    draw_result_max_wait: int,
    post_swipe_wait: float,
) -> bool:
    draw_result = wait_countdown_and_check_result(
        driver,
        grace_seconds=draw_countdown_grace,
        poll_interval=draw_poll_interval,
        max_wait_seconds=draw_result_max_wait,
    )
    if draw_result == "win":
        log("Winner detected in draw result popup. Finish task.")
        return True

    log("Draw result is not win. Close popup(s) and switch to next room.")
    switch_room_hard(driver, ocr_engine, post_wait=post_swipe_wait)
    return False


def is_diamond_luckybag_popup(texts: list[str]) -> bool:
    joined = " | ".join(texts)
    has_diamond = any(k in joined for k in DIAMOND_BAG_KEYWORDS)
    has_physical_hint = any(k in joined for k in PHYSICAL_BAG_HINT_KEYWORDS)
    return has_diamond and not has_physical_hint


def is_popup_countdown_zero(texts: list[str]) -> bool:
    left = parse_countdown_seconds(texts)
    if left == 0:
        return True
    joined = " | ".join(texts)
    zero_patterns = [
        re.compile(r"00:00"),
        re.compile(r"0:00"),
        re.compile(r"00\s*分\s*00\s*秒"),
        re.compile(r"00\s*时\s*00\s*分\s*00\s*秒"),
    ]
    return any(p.search(joined) for p in zero_patterns)


def parse_reference_value_yuan(texts: list[str]) -> Optional[float]:
    value_patterns = [
        re.compile(r"参考价值[^0-9]{0,6}([0-9]+(?:\.[0-9]+)?)\s*元"),
        re.compile(r"参考价值[^0-9]{0,6}¥\s*([0-9]+(?:\.[0-9]+)?)"),
        re.compile(r"参考价值[^0-9]{0,6}([0-9]+(?:\.[0-9]+)?)"),
    ]
    for t in texts:
        for p in value_patterns:
            m = p.search(t)
            if not m:
                continue
            try:
                return float(m.group(1))
            except Exception:
                continue
    return None


def is_low_value_long_countdown_popup(texts: list[str]) -> bool:
    left = parse_countdown_seconds(texts)
    if left is None or left <= 300:
        return False
    ref_value = parse_reference_value_yuan(texts)
    if ref_value is None:
        return False
    return ref_value < 100.0


def wait_countdown_and_check_result(
    driver: webdriver.Remote,
    grace_seconds: float = 2.0,
    poll_interval: float = 1.0,
    max_wait_seconds: int = 240,
    timer_offset_seconds: float = 1.0,
    result_probe_seconds: float = 8.0,
    reopen_interval_seconds: float = 6.0,
) -> str:
    log("Join success, entering draw-result wait flow...")
    log(
        f"Draw wait config: max_wait={max_wait_seconds}s, timer_offset={timer_offset_seconds:.1f}s, probe={result_probe_seconds:.1f}s."
    )
    deadline = time.time() + max_wait_seconds
    dynamic_deadline = deadline
    countdown_found = False
    last_reported_left: Optional[int] = None
    countdown_target_ts: Optional[float] = None
    last_reopen_ts = 0.0
    last_no_countdown_log_ts = 0.0

    while time.time() < dynamic_deadline:
        texts = visible_texts(driver)
        result = detect_draw_result(texts)
        if result is not None:
            log(f"Draw result detected: {result}")
            return result

        left = parse_countdown_seconds(texts)
        if left is not None:
            countdown_found = True
            target = time.time() + left + timer_offset_seconds
            if countdown_target_ts is None or target < countdown_target_ts:
                countdown_target_ts = target
            if countdown_target_ts < dynamic_deadline:
                dynamic_deadline = countdown_target_ts
            if last_reported_left is None or abs(left - last_reported_left) >= 3:
                log(f"Countdown detected: {left}s, timer set to +{timer_offset_seconds:.1f}s.")
                last_reported_left = left
        else:
            now = time.time()
            if now - last_no_countdown_log_ts >= 8.0:
                log("No countdown text detected in draw popup; trying to refresh popup state.")
                last_no_countdown_log_ts = now
            if now - last_reopen_ts >= reopen_interval_seconds:
                open_hits = filter_short_hits(native_candidates(driver, OPEN_KEYWORDS), max_len=8)
                open_hit = pick_best_hit(open_hits, OPEN_KEYWORDS)
                if open_hit is not None:
                    log(f"Re-open lucky-bag popup -> '{open_hit.text}' @ ({open_hit.x},{open_hit.y})")
                    tap(driver, open_hit.x, open_hit.y)
                    last_reopen_ts = now
                    time.sleep(0.7)

        if countdown_target_ts is not None and time.time() >= countdown_target_ts:
            log("Countdown timer reached, probing draw result...")
            probe_end = time.time() + max(2.0, result_probe_seconds + grace_seconds)
            while time.time() < probe_end:
                probe_texts = visible_texts(driver)
                probe_result = detect_draw_result(probe_texts)
                if probe_result is not None:
                    log(f"Draw result detected after timer: {probe_result}")
                    return probe_result
                time.sleep(max(0.3, poll_interval))
            log("Countdown timer reached but draw result text not found.")
            return "unknown_after_countdown"

        time.sleep(max(0.3, poll_interval))

    texts = visible_texts(driver)
    result = detect_draw_result(texts)
    if result is not None:
        log(f"Draw result detected at deadline: {result}")
        return result
    if countdown_found:
        log("Countdown finished but draw result text not found.")
        return "unknown_after_countdown"
    log("No countdown found in wait window; draw result unknown.")
    return "unknown_no_countdown"


def _is_noise_task_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if any(k in t for k in TASK_TEXT_BLOCKLIST):
        # allow explicit action text even if it contains "共"
        if any(x in t for x in ("去参与", "立即参与", "一键", "去完成", "参与任务", "发表评论")):
            return False
        return True
    return False


def _is_valid_success_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "人已参与" in t:
        return False
    if "已参与" in t and len(t) > 8:
        return False
    return any(k in t for k in SUCCESS_KEYWORDS)


def find_red_button_centers(image: np.ndarray) -> list[tuple[int, int, int]]:
    h, w, _ = image.shape
    # Focus on lower half where task panel usually appears.
    crop_top = int(h * 0.35)
    roi = image[crop_top:, :, :]
    # Downsample to reduce component search cost.
    step = 3
    small = roi[::step, ::step, :]
    r = small[:, :, 0].astype(np.int16)
    g = small[:, :, 1].astype(np.int16)
    b = small[:, :, 2].astype(np.int16)
    mask = (r > 180) & (g < 120) & (b < 120) & ((r - g) > 55) & ((r - b) > 55)
    hh, ww = mask.shape
    visited = np.zeros_like(mask, dtype=np.uint8)
    centers: list[tuple[int, int, int]] = []
    dirs = ((1, 0), (-1, 0), (0, 1), (0, -1))

    for y in range(hh):
        for x in range(ww):
            if not mask[y, x] or visited[y, x]:
                continue
            q: deque[tuple[int, int]] = deque()
            q.append((x, y))
            visited[y, x] = 1
            minx = maxx = x
            miny = maxy = y
            area = 0
            while q:
                cx, cy = q.popleft()
                area += 1
                if cx < minx:
                    minx = cx
                if cx > maxx:
                    maxx = cx
                if cy < miny:
                    miny = cy
                if cy > maxy:
                    maxy = cy
                for dx, dy in dirs:
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < ww and 0 <= ny < hh and mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = 1
                        q.append((nx, ny))

            bw = maxx - minx + 1
            bh = maxy - miny + 1
            # Heuristic for red horizontal CTA buttons.
            if area < 120 or bw < 24 or bh < 10:
                continue
            ratio = bw / max(1, bh)
            if ratio < 1.4 or ratio > 10:
                continue
            cx = int(((minx + maxx) / 2) * step)
            cy = int(((miny + maxy) / 2) * step + crop_top)
            centers.append((cx, cy, area))

    centers.sort(key=lambda t: t[2], reverse=True)
    return centers[:6]


def contains_blocked(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR]) -> bool:
    native = native_candidates(driver, BLOCKED_KEYWORDS)
    if native:
        return True
    if ocr_engine is None:
        return False
    ocr_hits = ocr_candidates(driver, ocr_engine, BLOCKED_KEYWORDS)
    return bool(ocr_hits)


def swipe_to_next_room(driver: webdriver.Remote, duration: float = 0.35) -> None:
    size = driver.get_window_size()
    x = int(size["width"] * 0.5)
    start_y = int(size["height"] * 0.82)
    end_y = int(size["height"] * 0.18)
    driver.execute_script(
        "mobile: dragFromToForDuration",
        {
            "duration": duration,
            "fromX": x,
            "fromY": start_y,
            "toX": x,
            "toY": end_y,
        },
    )


def switch_room_hard(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    post_wait: float,
) -> None:
    # Ensure focus is back to live canvas before swipe.
    dismiss_overlays(driver, ocr_engine, rounds=4)
    size = driver.get_window_size()
    tap(driver, int(size["width"] * 0.5), int(size["height"] * 0.14))
    time.sleep(0.25)
    swipe_to_next_room(driver, duration=0.40)
    time.sleep(0.35)
    swipe_to_next_room(driver, duration=0.40)
    wait_seconds = 3.0 + random.uniform(0.0, 2.0)
    log(f"Post-swipe wait: {wait_seconds:.2f}s")
    time.sleep(wait_seconds)


def dismiss_overlays(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR], rounds: int = 4) -> int:
    """Try best-effort close for popups/panels before room switch."""
    closed = 0
    for _ in range(rounds):
        hits = filter_short_hits(native_candidates(driver, CLOSE_KEYWORDS), max_len=8)
        if not hits and ocr_engine is not None:
            hits = filter_short_hits(ocr_candidates(driver, ocr_engine, CLOSE_KEYWORDS), max_len=8)
        hit = pick_best_hit(hits, CLOSE_KEYWORDS)
        if hit is not None:
            log(f"Dismiss overlay ({hit.source}) -> '{hit.text}' @ ({hit.x},{hit.y})")
            tap(driver, hit.x, hit.y)
            closed += 1
            time.sleep(0.45)
            continue

        # If lucky-bag panel still visible but no explicit close button, tap blank area.
        panel_hints = native_candidates(driver, PANEL_HINT_KEYWORDS)
        if not panel_hints and ocr_engine is not None:
            panel_hints = ocr_candidates(driver, ocr_engine, PANEL_HINT_KEYWORDS)
        if panel_hints:
            size = driver.get_window_size()
            x = int(size["width"] * 0.5)
            y = int(size["height"] * 0.14)
            log(f"Dismiss overlay (blank area) @ ({x},{y})")
            tap(driver, x, y)
            closed += 1
            time.sleep(0.45)
            continue
        break
    return closed


def _dedup_hits(hits: list[Hit], dist: int = 18) -> list[Hit]:
    out: list[Hit] = []
    for h in hits:
        ok = True
        for x in out:
            if abs(h.x - x.x) <= dist and abs(h.y - x.y) <= dist and h.text == x.text:
                ok = False
                break
        if ok:
            out.append(h)
    return out


def has_unfinished_task_text(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR]) -> bool:
    native = native_candidates(driver, TASK_UNFINISHED_KEYWORDS)
    if native:
        return True
    if ocr_engine is None:
        return False
    ocr_hits = ocr_candidates(driver, ocr_engine, TASK_UNFINISHED_KEYWORDS)
    return bool(ocr_hits)


def pick_task_text_buttons(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR]) -> list[Hit]:
    size = driver.get_window_size()
    min_y = int(size["height"] * 0.42)
    max_y = int(size["height"] * 0.96)
    hits = native_candidates(driver, TASK_ACTION_TEXT_KEYWORDS)
    if not hits and ocr_engine is not None:
        hits = ocr_candidates(driver, ocr_engine, TASK_ACTION_TEXT_KEYWORDS)
    out: list[Hit] = []
    for h in hits:
        t = (h.text or "").strip()
        if not t:
            continue
        if any(k in t for k in TASK_ACTION_BLOCKLIST):
            continue
        if not any(k in t for k in TASK_ACTION_TEXT_KEYWORDS):
            continue
        if h.y < min_y or h.y > max_y:
            continue
        out.append(h)
    return _dedup_hits(out, dist=14)


def run_task_panel_actions(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    rounds: int = 6,
) -> tuple[int, bool]:
    taps = 0
    clicked_points: list[tuple[int, int]] = []
    still_unfinished = has_unfinished_task_text(driver, ocr_engine)

    for round_idx in range(rounds):
        if not still_unfinished and round_idx > 0:
            break

        # Only click red CTA buttons in lucky-bag popup to avoid invalid text taps.
        img = screenshot_np_safe(driver)
        reds = find_red_button_centers(img)
        tapped_this_round = 0
        for rx, ry, _ in reds:
            if any(abs(rx - px) <= 15 and abs(ry - py) <= 15 for px, py in clicked_points):
                continue
            log(f"Tap TASK (red-shape) @ ({rx},{ry})")
            tap(driver, rx, ry)
            clicked_points.append((rx, ry))
            taps += 1
            tapped_this_round += 1
            time.sleep(0.65)

        if tapped_this_round == 0:
            # Strict fallback for red CTA text buttons (e.g. "一键发表评论").
            text_hits = pick_task_text_buttons(driver, ocr_engine)
            for h in text_hits:
                if any(abs(h.x - px) <= 12 and abs(h.y - py) <= 12 for px, py in clicked_points):
                    continue
                log(f"Tap TASK (text-fallback:{h.source}) -> '{h.text}' @ ({h.x},{h.y})")
                tap(driver, h.x, h.y)
                clicked_points.append((h.x, h.y))
                taps += 1
                tapped_this_round += 1
                time.sleep(0.65)

        if tapped_this_round == 0:
            break

        time.sleep(0.7)
        still_unfinished = has_unfinished_task_text(driver, ocr_engine)
        if still_unfinished:
            log("Task status still contains '未达成', continue tapping red buttons...")

    still_unfinished = has_unfinished_task_text(driver, ocr_engine)
    return taps, still_unfinished


def _is_valid_open_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if any(k in t for k in OPEN_TEXT_BLOCKLIST):
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--appium", default="http://127.0.0.1:4723")
    parser.add_argument("--udid", required=True)
    parser.add_argument("--bundle-id", default="com.ss.iphone.ugc.Aweme")
    parser.add_argument("--max-minutes", type=int, default=0)
    parser.add_argument("--no-reset", action="store_true", default=True)
    parser.add_argument("--interval-min", type=float, default=0.7)
    parser.add_argument("--interval-max", type=float, default=1.2)
    parser.add_argument("--xcode-org-id", default=None)
    parser.add_argument("--updated-wda-bundle-id", default=None)
    parser.add_argument("--show-xcode-log", action="store_true")
    parser.add_argument("--allow-provisioning-updates", action="store_true")
    parser.add_argument("--allow-provisioning-device-registration", action="store_true")
    parser.add_argument("--blocked-swipe-cooldown", type=float, default=4.0)
    parser.add_argument("--open-retry-before-swipe", type=int, default=5)
    parser.add_argument("--post-swipe-wait", type=float, default=3.0)
    parser.add_argument("--draw-countdown-grace", type=float, default=2.0)
    parser.add_argument("--draw-poll-interval", type=float, default=1.0)
    parser.add_argument("--draw-result-max-wait", type=int, default=240)
    parser.add_argument("--wda-launch-timeout-ms", type=int, default=120000)
    parser.add_argument("--wda-connection-timeout-ms", type=int, default=120000)
    parser.add_argument("--use-new-wda", action="store_true")
    args = parser.parse_args()

    ocr_engine = RapidOCR() if RapidOCR is not None else None
    if ocr_engine is None:
        log("OCR engine unavailable, running native-only.")

    log("Connecting to Appium...")
    driver = build_driver(
        args.appium,
        args.udid,
        args.bundle_id,
        args.no_reset,
        xcode_org_id=args.xcode_org_id,
        updated_wda_bundle_id=args.updated_wda_bundle_id,
        show_xcode_log=args.show_xcode_log,
        allow_provisioning_updates=args.allow_provisioning_updates,
        allow_provisioning_device_registration=args.allow_provisioning_device_registration,
        wda_launch_timeout_ms=args.wda_launch_timeout_ms,
        wda_connection_timeout_ms=args.wda_connection_timeout_ms,
        use_new_wda=args.use_new_wda,
    )
    end_at: Optional[float]
    if args.max_minutes > 0:
        end_at = time.time() + args.max_minutes * 60
        log(f"Run with max-minutes={args.max_minutes}, but process exits only on confirmed win.")
    else:
        end_at = None
        log("Run without max time limit; process exits only on confirmed win.")
    last_click_key = None
    last_click_ts = 0.0
    last_swipe_ts = 0.0
    open_retry_count = 0
    no_open_rounds = 0

    try:
        log("Started. Please manually stay in target live room.")
        while True:
            if end_at is not None and time.time() >= end_at:
                # Keep running: user requires exit only when draw result is confirmed win.
                end_at = None
                log("Max-minutes reached; continue running until confirmed win.")

            if contains_blocked(driver, ocr_engine):
                if time.time() - last_swipe_ts >= args.blocked_swipe_cooldown:
                    log("Detected blocked text, swiping to next live room...")
                    switch_room_hard(driver, ocr_engine, post_wait=args.post_swipe_wait)
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    continue

            # A) Strictly try join button first.
            n_join = filter_short_hits(native_candidates(driver, JOIN_KEYWORDS), max_len=10)
            join_hit = pick_best_hit(n_join, JOIN_KEYWORDS)
            if join_hit is None and ocr_engine is not None:
                o_join = filter_short_hits(ocr_candidates(driver, ocr_engine, JOIN_KEYWORDS), max_len=10)
                join_hit = pick_best_hit(o_join, JOIN_KEYWORDS)

            if join_hit is not None:
                click_key = join_hit.text
                if click_key == last_click_key and time.time() - last_click_ts < 5:
                    time.sleep(0.4)
                    continue
                log(f"Tap JOIN ({join_hit.source}) -> '{join_hit.text}' @ ({join_hit.x},{join_hit.y})")
                tap(driver, join_hit.x, join_hit.y)
                last_click_key = click_key
                last_click_ts = time.time()
                open_retry_count = 0
                no_open_rounds = 0
                time.sleep(1.5)
                if contains_success(driver, ocr_engine):
                    log("Detected success text: joined lucky draw.")
                    if handle_post_join_draw_flow(
                        driver,
                        ocr_engine,
                        draw_countdown_grace=args.draw_countdown_grace,
                        draw_poll_interval=args.draw_poll_interval,
                        draw_result_max_wait=args.draw_result_max_wait,
                        post_swipe_wait=args.post_swipe_wait,
                    ):
                        return 0
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    continue
                # join click happened but no success, continue scanning
                continue

            # B) If no join button, try opening lucky-bag panel.
            n_open = [
                h
                for h in filter_short_hits(native_candidates(driver, OPEN_KEYWORDS), max_len=8)
                if _is_valid_open_text(h.text)
            ]
            open_hit = pick_best_hit(n_open, OPEN_KEYWORDS)
            if open_hit is None and ocr_engine is not None:
                o_open = [
                    h
                    for h in filter_short_hits(ocr_candidates(driver, ocr_engine, OPEN_KEYWORDS), max_len=8)
                    if _is_valid_open_text(h.text)
                ]
                open_hit = pick_best_hit(o_open, OPEN_KEYWORDS)
            if open_hit is not None:
                no_open_rounds = 0
                click_key = f"open:{open_hit.text}"
                if click_key == last_click_key and time.time() - last_click_ts < 3:
                    time.sleep(0.3)
                    continue
                log(f"Tap OPEN ({open_hit.source}) -> '{open_hit.text}' @ ({open_hit.x},{open_hit.y})")
                tap(driver, open_hit.x, open_hit.y)
                last_click_key = click_key
                last_click_ts = time.time()
                open_retry_count += 1
                time.sleep(1.0)

                popup_texts = visible_texts(driver)
                if is_diamond_luckybag_popup(popup_texts):
                    log("Diamond lucky-bag popup detected, skip and switch to next room.")
                    switch_room_hard(driver, ocr_engine, post_wait=args.post_swipe_wait)
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    continue
                if is_popup_countdown_zero(popup_texts):
                    log("Lucky-bag popup countdown is 0, skip and switch to next room.")
                    switch_room_hard(driver, ocr_engine, post_wait=args.post_swipe_wait)
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    continue
                if is_low_value_long_countdown_popup(popup_texts):
                    ref_val = parse_reference_value_yuan(popup_texts)
                    left = parse_countdown_seconds(popup_texts)
                    log(
                        f"Physical bag filtered (countdown={left}s, ref={ref_val}元 < 99), switch to next room."
                    )
                    switch_room_hard(driver, ocr_engine, post_wait=args.post_swipe_wait)
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    continue

                task_taps, still_unfinished = run_task_panel_actions(
                    driver,
                    ocr_engine,
                    rounds=6,
                )
                if task_taps > 0:
                    log(f"Task panel taps: {task_taps}")
                    open_retry_count = 0
                if still_unfinished:
                    log("Task still unfinished in popup, will keep trying in current room.")
                if contains_success(driver, ocr_engine):
                    log("Detected success text: joined lucky draw.")
                    if handle_post_join_draw_flow(
                        driver,
                        ocr_engine,
                        draw_countdown_grace=args.draw_countdown_grace,
                        draw_poll_interval=args.draw_poll_interval,
                        draw_result_max_wait=args.draw_result_max_wait,
                        post_swipe_wait=args.post_swipe_wait,
                    ):
                        return 0
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    continue
                if open_retry_count >= args.open_retry_before_swipe and time.time() - last_swipe_ts >= args.blocked_swipe_cooldown:
                    log("OPEN retries exceeded without JOIN, swiping to next live room...")
                    switch_room_hard(driver, ocr_engine, post_wait=args.post_swipe_wait)
                    last_swipe_ts = time.time()
                    open_retry_count = 0
            else:
                no_open_rounds += 1
                if no_open_rounds >= 1 and time.time() - last_swipe_ts >= args.blocked_swipe_cooldown:
                    log("No lucky-bag button in current room, swiping to next live room...")
                    switch_room_hard(driver, ocr_engine, post_wait=args.post_swipe_wait)
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0

            time.sleep(random.uniform(args.interval_min, args.interval_max))

        return 2
    finally:
        driver.quit()


if __name__ == "__main__":
    sys.exit(main())
