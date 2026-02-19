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
    "红包",
    "抽奖",
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


def swipe_to_next_room(driver: webdriver.Remote) -> None:
    size = driver.get_window_size()
    x = int(size["width"] * 0.5)
    start_y = int(size["height"] * 0.75)
    end_y = int(size["height"] * 0.25)
    driver.execute_script(
        "mobile: dragFromToForDuration",
        {
            "duration": 0.25,
            "fromX": x,
            "fromY": start_y,
            "toX": x,
            "toY": end_y,
        },
    )


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


def run_task_panel_actions(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR], rounds: int = 5) -> int:
    taps = 0
    clicked_points: list[tuple[int, int]] = []
    for _ in range(rounds):
        size = driver.get_window_size()
        min_y = int(size["height"] * 0.30)

        n_hits = [
            h
            for h in native_candidates(driver, TASK_KEYWORDS)
            if len(h.text) <= 14 and h.y >= min_y and not _is_noise_task_text(h.text)
        ]
        o_hits: list[Hit] = []
        if ocr_engine is not None:
            o_hits = [
                h
                for h in ocr_candidates(driver, ocr_engine, TASK_KEYWORDS)
                if len(h.text) <= 14 and h.y >= min_y and not _is_noise_task_text(h.text)
            ]
        all_hits = _dedup_hits(n_hits + o_hits)
        # Secondary path: text buttons (red-shape is primary).
        all_hits.sort(
            key=lambda h: (
                0 if any(k in h.text for k in ("一键发表评论", "一键参与", "去完成", "去参与", "立即参与")) else 1,
                h.y,
            )
        )

        tapped_this_round = False

        # Primary path: red background + white text CTA shape.
        img = screenshot_np_safe(driver)
        reds = find_red_button_centers(img)
        for rx, ry, _ in reds:
            if any(abs(rx - px) <= 15 and abs(ry - py) <= 15 for px, py in clicked_points):
                continue
            log(f"Tap TASK (red-shape) @ ({rx},{ry})")
            tap(driver, rx, ry)
            clicked_points.append((rx, ry))
            taps += 1
            tapped_this_round = True
            time.sleep(0.8)
            break

        if not tapped_this_round:
            for h in all_hits:
                if any(abs(h.x - px) <= 12 and abs(h.y - py) <= 12 for px, py in clicked_points):
                    continue
                log(f"Tap TASK ({h.source}) -> '{h.text}' @ ({h.x},{h.y})")
                tap(driver, h.x, h.y)
                clicked_points.append((h.x, h.y))
                taps += 1
                tapped_this_round = True
                time.sleep(0.8)
                break

        if not tapped_this_round:
            break
    return taps


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--appium", default="http://127.0.0.1:4723")
    parser.add_argument("--udid", required=True)
    parser.add_argument("--bundle-id", default="com.ss.iphone.ugc.Aweme")
    parser.add_argument("--max-minutes", type=int, default=20)
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
    )
    end_at = time.time() + args.max_minutes * 60
    last_click_key = None
    last_click_ts = 0.0
    last_swipe_ts = 0.0
    open_retry_count = 0
    no_open_rounds = 0

    try:
        log("Started. Please manually stay in target live room.")
        while time.time() < end_at:
            if contains_blocked(driver, ocr_engine):
                if time.time() - last_swipe_ts >= args.blocked_swipe_cooldown:
                    log("Detected blocked text, swiping to next live room...")
                    swipe_to_next_room(driver)
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    time.sleep(1.2)
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
                    return 0
                # join click happened but no success, continue scanning
                continue

            # B) If no join button, try opening lucky-bag panel.
            n_open = filter_short_hits(native_candidates(driver, OPEN_KEYWORDS), max_len=6)
            open_hit = pick_best_hit(n_open, OPEN_KEYWORDS)
            if open_hit is None and ocr_engine is not None:
                o_open = filter_short_hits(ocr_candidates(driver, ocr_engine, OPEN_KEYWORDS), max_len=6)
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
                task_taps = run_task_panel_actions(driver, ocr_engine, rounds=6)
                if task_taps > 0:
                    log(f"Task panel taps: {task_taps}")
                if contains_success(driver, ocr_engine):
                    log("Detected success text: joined lucky draw.")
                    return 0
                if open_retry_count >= args.open_retry_before_swipe and time.time() - last_swipe_ts >= args.blocked_swipe_cooldown:
                    log("OPEN retries exceeded without JOIN, swiping to next live room...")
                    swipe_to_next_room(driver)
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    time.sleep(1.2)
            else:
                no_open_rounds += 1
                if no_open_rounds >= 1 and time.time() - last_swipe_ts >= args.blocked_swipe_cooldown:
                    log("No lucky-bag button in current room, swiping to next live room...")
                    swipe_to_next_room(driver)
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    time.sleep(1.2)

            time.sleep(random.uniform(args.interval_min, args.interval_max))

        log("Timeout reached: no confirmed join signal.")
        return 2
    finally:
        driver.quit()


if __name__ == "__main__":
    sys.exit(main())
