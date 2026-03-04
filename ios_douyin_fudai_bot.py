#!/usr/bin/env python3
"""
iOS Douyin lucky-bag bot (Appium + optional OCR).

Design goals:
- Deterministic state flow (scan -> open -> task -> wait draw/result -> switch room)
- Avoid false positives from hidden accessibility text
- Keep running until confirmed win (or explicit red-packet exit signal)
"""

from __future__ import annotations

import argparse
import random
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
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


# ---- Text rules ----

JOIN_KEYWORDS = [
    "去参与",
    "立即参与",
    "参与抽奖",
    "马上参与",
    "立刻参与",
]

OPEN_KEYWORDS = ["福袋"]

TASK_ACTION_TEXT_KEYWORDS = [
    "一键发表评论",
    "一键加入粉丝团",
    "去加入粉丝团",
    "立即加入粉丝团",
    "加入粉丝团",
    "加入粉丝",
    "去加入",
    "立即加入",
    "一键参与",
    "去完成",
    "去参与",
    "立即参与",
    "参与",
    "去评论",
    "发表评论",
    "去抢",
    "去领取",
]

FANS_GROUP_CTA_KEYWORDS = [
    "一键加入粉丝团",
    "去加入粉丝团",
    "立即加入粉丝团",
    "加入粉丝团",
]

FANS_GROUP_CONFIRM_KEYWORDS = [
    "确认加入",
    "加入并关注",
    "立即加入",
    "去加入",
    "加入粉丝团",
    "加入",
]

FANS_GROUP_IGNORE_TEXT = [
    "将同步关注",
    "粉丝团规则",
    "购物粉丝团权益",
    "团成员",
    "待解锁",
    "酷炫勋章",
    "专属优惠",
]

STRICT_TASK_ACTION_TEXT_KEYWORDS = [
    "一键发表评论",
    "一键加入粉丝团",
    "去加入粉丝团",
    "立即加入粉丝团",
    "加入粉丝团",
    "加入粉丝",
    "去加入",
    "立即加入",
    "一键参与",
    "去完成",
    "去参与",
    "立即参与",
]

TASK_ACTION_BLOCKLIST = [
    "参与条件",
    "福袋规则",
    "任务说明",
    "参与任务",
    "人气榜",
    "榜单",
    "热榜",
]

TASK_TEXT_BLOCKLIST = [
    "人已参与",
    "已参与人数",
    "共",
    "剩余",
    "开奖",
    "将同步关注",
]

TASK_UNFINISHED_KEYWORDS = ["未达成", "未完成"]

SUCCESS_KEYWORDS = ["已参与", "参与成功"]

RESULT_WIN_KEYWORDS = [
    "恭喜抽中",
    "恭喜你抽中",
    "恭喜你中奖了",
    "抽中福袋",
]

RESULT_LOSE_KEYWORDS = [
    "未中奖",
    "未中签",
    "没有抽中福袋",
    "很遗憾",
    "下次再来",
    "擦肩而过",
]

RESULT_WIN_PATTERNS = [
    re.compile(r"恭喜.*抽中"),
    re.compile(r"恭喜.*中奖"),
    re.compile(r"抽中.*福袋"),
]

RESULT_LOSE_PATTERNS = [
    re.compile(r"未中奖"),
    re.compile(r"未中签"),
    re.compile(r"没有抽中福袋"),
    re.compile(r"很遗憾"),
    re.compile(r"下次再来"),
    re.compile(r"擦肩而过"),
]

DIAMOND_BAG_KEYWORDS = ["钻石", "抖币", "音浪"]

NON_PHYSICAL_BAG_KEYWORDS = [
    "红包",
    "金币",
    "福气",
    "福气值",
    "任务红包",
    "现金红包",
    "抖币",
    "音浪",
    "钻石",
]

PHYSICAL_BAG_HINT_KEYWORDS = ["实物", "商品", "礼品", "包邮"]

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

RED_PACKET_EXIT_KEYWORDS = ["的红包"]

CLOSE_KEYWORDS = ["关闭", "取消", "我知道了", "稍后再说"]

RISKY_EXIT_KEYWORDS = [
    "返回推荐",
    "返回直播",
    "退出直播",
    "退出直播间",
    "离开直播",
    "关闭直播",
    "结束观看",
    "返回",
]

PANEL_HINT_KEYWORDS = [
    "福袋规则",
    "参与条件",
    "去发表评论",
    "查看中奖观众",
    "我知道了",
    "没有抽中福袋",
    "倒计时",
    "参考价值",
    "后开奖",
    "参与任务",
]

LOSE_POPUP_HINT_KEYWORDS = [
    "没有抽中福袋",
    "未中签",
    "我知道了",
    "查看中奖观众",
    "开奖结果",
    "已开奖",
]

POPULARITY_POPUP_KEYWORDS = [
    "人气榜",
    "带货榜",
    "销量榜",
    "商品榜",
    "热销榜",
    "榜单",
    "本场",
    "小时榜",
    "热榜",
    "贡献榜",
    "粉丝榜",
    "查看完整榜单",
    "本场榜",
]

OPEN_TEXT_BLOCKLIST = ["没有抽中", "未抽中", "抽中福袋", "已开奖", "开奖结果"]

ELEMENT_TYPES = (
    "XCUIElementTypeButton",
    "XCUIElementTypeStaticText",
    "XCUIElementTypeOther",
)

OPEN_ENTRY_ELEMENT_TYPES = (
    "XCUIElementTypeButton",
    "XCUIElementTypeStaticText",
)
OPEN_ENTRY_REGION_X_MAX_RATIO = 0.52
OPEN_ENTRY_REGION_Y_MAX_RATIO = 0.52
OPEN_ENTRY_REGION_X_MIN_RATIO = 0.09
OPEN_ENTRY_REGION_Y_MIN_RATIO = 0.12
OPEN_ENTRY_MAX_WIDTH_RATIO = 0.24
OPEN_ENTRY_MAX_HEIGHT_RATIO = 0.18
OPEN_ENTRY_MAX_ASPECT_RATIO = 1.85
OPEN_ENTRY_MIN_SIDE_PX = 12
POPUP_REGION_Y_MIN_RATIO = 0.50
OPEN_ENTRY_CACHE_TTL_SECONDS = 20.0
OPEN_ENTRY_OCR_COOLDOWN_SECONDS = 2.5
OPEN_FALLBACK_MAX_POPULARITY_HITS = 3


# ---- Models ----

@dataclass
class Hit:
    text: str
    x: int
    y: int
    source: str
    w: int = 0
    h: int = 0


# ---- Core helpers ----

_open_entry_cache_hit: Optional[Hit] = None
_open_entry_cache_ts: float = 0.0
_open_entry_next_ocr_ts: float = 0.0
_open_entry_cache_ttl_seconds: float = OPEN_ENTRY_CACHE_TTL_SECONDS
_open_entry_ocr_cooldown_seconds: float = OPEN_ENTRY_OCR_COOLDOWN_SECONDS

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def auto_detect_udid() -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["xcrun", "xctrace", "list", "devices"],
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception:
        return None

    udids: list[str] = []
    p = re.compile(r"\(([0-9A-Fa-f-]{20,})\)\s*$")
    for line in out.splitlines():
        s = line.strip()
        if not s or "Simulator" in s or "Mac" in s:
            continue
        m = p.search(s)
        if m:
            udids.append(m.group(1))
    return udids[0] if udids else None


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
    wda_startup_retries: int = 2,
    wda_startup_retry_interval_ms: int = 15000,
    wait_for_idle_timeout: float = 0.0,
    wait_for_quiescence: bool = False,
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
    opts.set_capability("wdaStartupRetries", int(wda_startup_retries))
    opts.set_capability("wdaStartupRetryInterval", int(wda_startup_retry_interval_ms))
    opts.set_capability("waitForIdleTimeout", float(wait_for_idle_timeout))
    opts.set_capability("waitForQuiescence", bool(wait_for_quiescence))

    return webdriver.Remote(appium_url, options=opts)


def tap(driver: webdriver.Remote, x: int, y: int) -> None:
    driver.execute_script("mobile: tap", {"x": int(x), "y": int(y)})


def native_candidates(
    driver: webdriver.Remote,
    keywords: Iterable[str],
    element_types: Iterable[str] = ELEMENT_TYPES,
) -> list[Hit]:
    expr_kw = " OR ".join(
        [f"name CONTAINS '{k}' OR label CONTAINS '{k}' OR value CONTAINS '{k}'" for k in keywords]
    )
    expr_type = " OR ".join([f"type == '{t}'" for t in element_types])
    predicate = f"({expr_type}) AND ({expr_kw})"

    elements = driver.find_elements(AppiumBy.IOS_PREDICATE, predicate)
    out: list[Hit] = []
    for el in elements:
        try:
            visible_attr = str(el.get_attribute("visible") or "").strip().lower()
            if visible_attr and visible_attr not in ("1", "true", "yes"):
                continue
            rect = el.rect
            if rect["width"] < 8 or rect["height"] < 8:
                continue
            x = int(rect["x"] + rect["width"] / 2)
            y = int(rect["y"] + rect["height"] / 2)
            text = (el.get_attribute("name") or el.get_attribute("label") or "").strip()
            out.append(
                Hit(
                    text=text,
                    x=x,
                    y=y,
                    source="native",
                    w=int(rect["width"]),
                    h=int(rect["height"]),
                )
            )
        except Exception:
            continue
    return out


def _to_int(v: object, default: int = 0) -> int:
    try:
        return int(float(str(v)))
    except Exception:
        return default


def source_hits(
    driver: webdriver.Remote,
    element_types: Iterable[str],
    keywords: Optional[Iterable[str]] = None,
    upper_left_only: bool = False,
    lower_half_only: bool = False,
) -> list[Hit]:
    try:
        source = driver.page_source
        root = ET.fromstring(source)
    except Exception:
        return []

    screen_w = 0
    screen_h = 0
    for el in root.iter():
        w = _to_int(el.attrib.get("width"), 0)
        h = _to_int(el.attrib.get("height"), 0)
        if w > 0 and h > 0:
            screen_w = w
            screen_h = h
            break
    if screen_w <= 0:
        screen_w = 414
    if screen_h <= 0:
        screen_h = 896

    x_max = int(screen_w * OPEN_ENTRY_REGION_X_MAX_RATIO)
    y_max = int(screen_h * OPEN_ENTRY_REGION_Y_MAX_RATIO)
    y_min = int(screen_h * POPUP_REGION_Y_MIN_RATIO)
    keyword_list = list(keywords) if keywords is not None else None
    type_set = set(element_types)

    out: list[Hit] = []
    for el in root.iter():
        t = (el.attrib.get("type") or "").strip()
        if t not in type_set:
            continue
        visible_attr = str(el.attrib.get("visible") or "").strip().lower()
        if visible_attr and visible_attr not in ("1", "true", "yes"):
            continue

        x = _to_int(el.attrib.get("x"), 0)
        y = _to_int(el.attrib.get("y"), 0)
        w = _to_int(el.attrib.get("width"), 0)
        h = _to_int(el.attrib.get("height"), 0)
        if w < 4 or h < 4:
            continue
        cx = int(x + w / 2)
        cy = int(y + h / 2)

        if upper_left_only and (cx > x_max or cy > y_max):
            continue
        if lower_half_only and cy < y_min:
            continue

        text = (el.attrib.get("name") or el.attrib.get("label") or el.attrib.get("value") or "").strip()
        if not text:
            continue
        if keyword_list is not None and not any(k in text for k in keyword_list):
            continue

        out.append(Hit(text=text, x=cx, y=cy, source="src", w=w, h=h))
    return out


def pick_best_hit(hits: list[Hit], keyword_priority: list[str]) -> Optional[Hit]:
    if not hits:
        return None
    best = hits[0]
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
    return best


def pick_best_open_entry_hit(hits: list[Hit], keyword_priority: list[str]) -> Optional[Hit]:
    if not hits:
        return None

    def _keyword_rank(text: str) -> int:
        for i, k in enumerate(keyword_priority):
            if k in text:
                return i
        return 10**9

    def _shape_penalty(h: Hit) -> float:
        if h.w <= 0 or h.h <= 0:
            return 0.35
        return abs(h.w - h.h) / max(1.0, float(max(h.w, h.h)))

    return min(hits, key=lambda h: (_keyword_rank(h.text), _shape_penalty(h), h.y, h.x))


def filter_short_hits(hits: list[Hit], max_len: int) -> list[Hit]:
    out: list[Hit] = []
    for h in hits:
        t = (h.text or "").strip()
        if t and len(t) <= max_len:
            out.append(h)
    return out


def filter_hits_to_open_entry_region(driver: webdriver.Remote, hits: list[Hit]) -> list[Hit]:
    size = driver.get_window_size()
    x_min = int(size["width"] * OPEN_ENTRY_REGION_X_MIN_RATIO)
    y_min = int(size["height"] * OPEN_ENTRY_REGION_Y_MIN_RATIO)
    x_max = int(size["width"] * OPEN_ENTRY_REGION_X_MAX_RATIO)
    y_max = int(size["height"] * OPEN_ENTRY_REGION_Y_MAX_RATIO)
    return [h for h in hits if x_min <= h.x <= x_max and y_min <= h.y <= y_max]


def is_safe_open_entry_point(driver: webdriver.Remote, x: int, y: int) -> bool:
    try:
        size = driver.get_window_size()
        x_min = int(size["width"] * OPEN_ENTRY_REGION_X_MIN_RATIO)
        y_min = int(size["height"] * OPEN_ENTRY_REGION_Y_MIN_RATIO)
        x_max = int(size["width"] * OPEN_ENTRY_REGION_X_MAX_RATIO)
        y_max = int(size["height"] * OPEN_ENTRY_REGION_Y_MAX_RATIO)
    except Exception:
        return False
    if x < x_min or y < y_min or x > x_max or y > y_max:
        return False
    # Extra protection: avoid top-left navigation zone.
    if x <= int(size["width"] * 0.25) and y <= int(size["height"] * 0.16):
        return False
    return True


def filter_hits_by_open_entry_shape(driver: webdriver.Remote, hits: list[Hit]) -> list[Hit]:
    if not hits:
        return []
    size = driver.get_window_size()
    max_w = int(size["width"] * OPEN_ENTRY_MAX_WIDTH_RATIO)
    max_h = int(size["height"] * OPEN_ENTRY_MAX_HEIGHT_RATIO)
    out: list[Hit] = []
    for h in hits:
        if h.w <= 0 or h.h <= 0:
            out.append(h)
            continue
        if h.w < OPEN_ENTRY_MIN_SIDE_PX or h.h < OPEN_ENTRY_MIN_SIDE_PX:
            continue
        if h.w > max_w or h.h > max_h:
            continue
        ratio = max(h.w, h.h) / max(1, min(h.w, h.h))
        if ratio > OPEN_ENTRY_MAX_ASPECT_RATIO:
            continue
        out.append(h)
    return out


def configure_open_entry_lookup(cache_ttl_seconds: float, ocr_cooldown_seconds: float) -> None:
    global _open_entry_cache_ttl_seconds, _open_entry_ocr_cooldown_seconds
    _open_entry_cache_ttl_seconds = max(1.0, float(cache_ttl_seconds))
    _open_entry_ocr_cooldown_seconds = max(0.0, float(ocr_cooldown_seconds))


def invalidate_open_entry_cache() -> None:
    global _open_entry_cache_hit, _open_entry_cache_ts
    _open_entry_cache_hit = None
    _open_entry_cache_ts = 0.0


def _cache_open_entry_hit(hit: Hit) -> None:
    global _open_entry_cache_hit, _open_entry_cache_ts
    _open_entry_cache_hit = Hit(text=hit.text, x=hit.x, y=hit.y, source=hit.source, w=hit.w, h=hit.h)
    _open_entry_cache_ts = time.time()


def _get_cached_open_entry_hit() -> Optional[Hit]:
    if _open_entry_cache_hit is None:
        return None
    if time.time() - _open_entry_cache_ts > _open_entry_cache_ttl_seconds:
        return None
    return Hit(
        text=_open_entry_cache_hit.text or "福袋",
        x=_open_entry_cache_hit.x,
        y=_open_entry_cache_hit.y,
        source="cache",
        w=_open_entry_cache_hit.w,
        h=_open_entry_cache_hit.h,
    )


def find_open_entry_hit(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR]) -> Optional[Hit]:
    global _open_entry_next_ocr_ts
    cached_hit = _get_cached_open_entry_hit()
    if cached_hit is not None and is_safe_open_entry_point(driver, cached_hit.x, cached_hit.y):
        return cached_hit

    source_open_hits = filter_short_hits(
        source_hits(
            driver,
            OPEN_ENTRY_ELEMENT_TYPES,
            keywords=OPEN_KEYWORDS,
            upper_left_only=True,
        ),
        max_len=16,
    )
    source_open_hits = [h for h in source_open_hits if _is_valid_open_text(h.text)]
    source_open_hits = filter_hits_to_open_entry_region(driver, source_open_hits)
    source_open_hits = filter_hits_by_open_entry_shape(driver, source_open_hits)
    hit = pick_best_open_entry_hit(source_open_hits, OPEN_KEYWORDS)
    if hit is not None:
        _cache_open_entry_hit(hit)
        return hit

    native_hits = filter_short_hits(
        native_candidates(driver, OPEN_KEYWORDS, element_types=OPEN_ENTRY_ELEMENT_TYPES),
        max_len=16,
    )
    native_hits = [h for h in native_hits if _is_valid_open_text(h.text)]
    native_hits = filter_hits_to_open_entry_region(driver, native_hits)
    native_hits = filter_hits_by_open_entry_shape(driver, native_hits)
    hit = pick_best_open_entry_hit(native_hits, OPEN_KEYWORDS)
    if hit is not None:
        _cache_open_entry_hit(hit)
        return hit

    if ocr_engine is None:
        return None
    if time.time() < _open_entry_next_ocr_ts:
        return None

    ocr_hits = filter_short_hits(ocr_candidates(driver, ocr_engine, OPEN_KEYWORDS), max_len=16)
    ocr_hits = [h for h in ocr_hits if _is_valid_open_text(h.text)]
    ocr_hits = filter_hits_to_open_entry_region(driver, ocr_hits)
    ocr_hits = filter_hits_by_open_entry_shape(driver, ocr_hits)
    _open_entry_next_ocr_ts = time.time() + _open_entry_ocr_cooldown_seconds
    hit = pick_best_open_entry_hit(ocr_hits, OPEN_KEYWORDS)
    if hit is not None:
        _cache_open_entry_hit(hit)
    return hit


def screenshot_np(driver: webdriver.Remote) -> np.ndarray:
    from io import BytesIO

    png = driver.get_screenshot_as_png()
    img = Image.open(BytesIO(png))
    return np.array(img.convert("RGB"))


def ocr_candidates(driver: webdriver.Remote, ocr_engine: RapidOCR, keywords: Iterable[str]) -> list[Hit]:
    img = screenshot_np(driver)
    result, _ = ocr_engine(img)
    if not result:
        return []

    out: list[Hit] = []
    for box, text, score in result:
        s = float(score) if score is not None else 0.0
        t = str(text).strip() if text is not None else ""
        if not t or s < 0.45:
            continue
        if not any(k in t for k in keywords):
            continue
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
        out.append(
            Hit(
                text=t,
                x=int(sum(xs) / len(xs)),
                y=int(sum(ys) / len(ys)),
                source="ocr",
                w=max(xs) - min(xs),
                h=max(ys) - min(ys),
            )
        )
    return out


def ocr_texts(driver: webdriver.Remote, ocr_engine: RapidOCR, min_score: float = 0.45) -> list[str]:
    img = screenshot_np(driver)
    result, _ = ocr_engine(img)
    if not result:
        return []
    out: list[str] = []
    for _, text, score in result:
        s = float(score) if score is not None else 0.0
        t = str(text).strip() if text is not None else ""
        if t and s >= min_score:
            out.append(t)
    return out


def unique_texts(texts: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in texts:
        s = (t or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def visible_texts_raw(driver: webdriver.Remote, lower_half_only: bool = False) -> list[str]:
    source_text_hits = source_hits(
        driver,
        ("XCUIElementTypeStaticText", "XCUIElementTypeButton"),
        keywords=None,
        upper_left_only=False,
        lower_half_only=lower_half_only,
    )
    if source_text_hits:
        return [h.text for h in source_text_hits if (h.text or "").strip()]

    try:
        elements = driver.find_elements(
            AppiumBy.IOS_PREDICATE,
            "type == 'XCUIElementTypeStaticText' OR type == 'XCUIElementTypeButton'",
        )
    except Exception:
        return []

    y_min = 0
    if lower_half_only:
        size = driver.get_window_size()
        y_min = int(size["height"] * POPUP_REGION_Y_MIN_RATIO)

    out: list[str] = []
    for el in elements:
        try:
            visible_attr = str(el.get_attribute("visible") or "").strip().lower()
            if visible_attr and visible_attr not in ("1", "true", "yes"):
                continue
            if lower_half_only:
                rect = el.rect
                if rect["width"] < 4 or rect["height"] < 4:
                    continue
                cy = int(rect["y"] + rect["height"] / 2)
                if cy < y_min:
                    continue
            t = (el.get_attribute("name") or el.get_attribute("label") or el.get_attribute("value") or "").strip()
            if t:
                out.append(t)
        except Exception:
            continue
    return out


def visible_texts(driver: webdriver.Remote) -> list[str]:
    return unique_texts(visible_texts_raw(driver))


def merged_scene_texts(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    prefer_ocr_for_popup: bool = False,
    popup_lower_half: bool = False,
) -> list[str]:
    if prefer_ocr_for_popup:
        texts = visible_texts_raw(driver, lower_half_only=popup_lower_half)
    else:
        texts = visible_texts(driver)
    if ocr_engine is None:
        return texts
    need_ocr = prefer_ocr_for_popup
    if not need_ocr:
        need_ocr = (parse_popup_draw_countdown_seconds(texts) is None) or (parse_reference_value_yuan(texts) is None)
    if not need_ocr:
        return texts

    ocr_ts = ocr_texts(driver, ocr_engine)
    if prefer_ocr_for_popup:
        return texts + ocr_ts
    return unique_texts(texts + ocr_ts)


# ---- Text parsing ----

def _is_valid_success_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "人已参与" in t:
        return False
    if "已参与" in t and len(t) > 8:
        return False
    return any(k in t for k in SUCCESS_KEYWORDS)


def contains_success_in_texts(texts: list[str]) -> bool:
    return any(_is_valid_success_text(t) for t in texts)


def has_popup_draw_anchor(texts: list[str]) -> bool:
    return any(("后开奖" in (t or "")) or ("后开" in (t or "")) for t in texts)


def is_popularity_board_popup(texts: list[str]) -> bool:
    joined = " | ".join(texts)
    if not any(k in joined for k in POPULARITY_POPUP_KEYWORDS):
        return False
    # Lucky-bag popup signals take precedence over board-like words.
    if has_popup_draw_anchor(texts):
        return False
    if any(k in joined for k in ("福袋规则", "参与条件", "参与任务", "参考价值", "实物福袋")):
        return False
    return True


def has_luckybag_context(texts: list[str]) -> bool:
    if is_popularity_board_popup(texts):
        return False
    joined = " | ".join(texts)
    if has_popup_draw_anchor(texts):
        return True
    if any(k in joined for k in PANEL_HINT_KEYWORDS):
        return True
    if "福袋" in joined and any(k in joined for k in ("开奖", "参与任务", "参考价值", "未达成", "已达成")):
        return True
    return False


def contains_success_with_context(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR]) -> bool:
    texts = merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=True)
    return contains_success_in_texts(texts) and has_luckybag_context(texts)


def has_task_success_cta_text(texts: list[str]) -> bool:
    joined = " | ".join(texts)
    return ("参与成功" in joined) or ("等待开奖" in joined)


def has_active_draw_countdown(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR]) -> bool:
    texts = merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=True)
    left = parse_popup_draw_countdown_seconds(texts)
    if left is None:
        left = parse_countdown_seconds(texts)
    if left is None or left <= 0:
        return False
    return has_popup_draw_anchor(texts) or has_task_success_cta_text(texts)


def extract_countdown_seconds(texts: list[str]) -> list[int]:
    values: list[int] = []
    mmss = re.compile(r"(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)")
    sec_only = re.compile(r"(?<!\d)(\d{1,3})\s*秒(?!\s*(?:后开奖|后开))")
    min_sec = re.compile(r"(?<!\d)(\d{1,2})\s*分(?:钟)?\s*(\d{1,2})\s*秒")

    for t in texts:
        has_compound = False
        for m in mmss.finditer(t):
            sec = int(m.group(1)) * 60 + int(m.group(2))
            if 0 <= sec <= 3600:
                values.append(sec)
                has_compound = True
        for m in min_sec.finditer(t):
            sec = int(m.group(1)) * 60 + int(m.group(2))
            if 0 <= sec <= 3600:
                values.append(sec)
                has_compound = True
        if not has_compound:
            for m in sec_only.finditer(t):
                sec = int(m.group(1))
                if 0 <= sec <= 3600:
                    values.append(sec)

    joined = " | ".join(texts)
    p = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*([0-5]\d)\s*(?:后开奖|后开)")
    for m in p.finditer(joined):
        sec = int(m.group(1)) * 60 + int(m.group(2))
        if 0 <= sec <= 3600:
            values.append(sec)

    p2 = re.compile(r"(?<!\d)(\d{1,2})\s*分(?:钟)?\s*([0-5]?\d)\s*秒?\s*(?:后开奖|后开)")
    for m in p2.finditer(joined):
        sec = int(m.group(1)) * 60 + int(m.group(2))
        if 0 <= sec <= 3600:
            values.append(sec)

    p3 = re.compile(r"(?<!分)(?<!\d)(\d{1,3})\s*秒\s*(?:后开奖|后开)")
    for m in p3.finditer(joined):
        sec = int(m.group(1))
        if 0 <= sec <= 3600:
            values.append(sec)

    return values


def parse_countdown_seconds(texts: list[str]) -> Optional[int]:
    vals = extract_countdown_seconds(texts)
    return min(vals) if vals else None


def parse_popup_draw_countdown_seconds(texts: list[str]) -> Optional[int]:
    candidates: list[int] = []
    mmss_after_open = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*([0-5]\d)\s*(?:后开奖|后开)")
    minsec_after_open = re.compile(r"(?<!\d)(\d{1,2})\s*分(?:钟)?\s*([0-5]?\d)\s*秒?\s*(?:后开奖|后开)")
    sec_after_open = re.compile(r"(?<!分)(?<!\d)(\d{1,3})\s*秒\s*(?:后开奖|后开)")

    for t in texts:
        for m in mmss_after_open.finditer(t):
            sec = int(m.group(1)) * 60 + int(m.group(2))
            if 0 <= sec <= 3600:
                candidates.append(sec)
        for m in minsec_after_open.finditer(t):
            sec = int(m.group(1)) * 60 + int(m.group(2))
            if 0 <= sec <= 3600:
                candidates.append(sec)
        for m in sec_after_open.finditer(t):
            sec = int(m.group(1))
            if 0 <= sec <= 3600:
                candidates.append(sec)

    for i, token in enumerate(texts):
        t = (token or "").strip()
        if "后开奖" not in t and "后开" not in t:
            continue
        window = [((x or "").strip()) for x in texts[max(0, i - 6): i + 1]]
        nums: list[int] = []
        for w in window:
            m = re.fullmatch(r"(\d{1,2})", w)
            if m:
                nums.append(int(m.group(1)))
        if len(nums) >= 2:
            mm, ss = nums[-2], nums[-1]
            if 0 <= mm <= 59 and 0 <= ss <= 59:
                candidates.append(mm * 60 + ss)

    return min(candidates) if candidates else None


def parse_open_entry_countdown_seconds(text: str) -> Optional[int]:
    t = (text or "").strip()
    if not t:
        return None
    m = re.search(r"(?<!\d)(\d{1,2})\s*[:：]\s*([0-5]\d)(?!\d)", t)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(?<!\d)(\d{1,2})\s*分(?:钟)?\s*([0-5]?\d)\s*秒", t)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(?<!\d)(\d{1,2})\s*分(?:钟)?(?!\s*\d)", t)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"(?<!\d)(\d{1,3})\s*秒", t)
    if m:
        return int(m.group(1))
    return None


def is_popup_countdown_zero(texts: list[str]) -> bool:
    left = parse_popup_draw_countdown_seconds(texts)
    if left == 0:
        return True
    if left is not None and left <= 1 and has_popup_draw_anchor(texts):
        return True

    joined = " | ".join(texts)
    patterns = [
        re.compile(r"00:00"),
        re.compile(r"0:00"),
        re.compile(r"0{1,2}\s*[:：]\s*0{1,2}\s*(?:后开奖|后开)"),
        re.compile(r"00\s*分\s*00\s*秒"),
    ]
    return any(p.search(joined) for p in patterns)


def parse_reference_value_yuan(texts: list[str]) -> Optional[float]:
    patterns = [
        re.compile(r"(?:参考)?(?:价值|价)[^0-9¥￥]{0,12}[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万)?\s*元?"),
        re.compile(r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]+)?)"),
    ]
    for t in texts:
        for p in patterns:
            m = p.search(t)
            if not m:
                continue
            try:
                v = float(m.group(1).replace(",", ""))
                if len(m.groups()) >= 2 and m.group(2):
                    v *= 10000.0
                return v
            except Exception:
                continue
    return None


def detect_draw_result(texts: list[str]) -> Optional[str]:
    joined = " | ".join(texts)
    left = parse_popup_draw_countdown_seconds(texts)
    if left is None:
        left = parse_countdown_seconds(texts)
    # Guard: do not settle win/lose while countdown is still running.
    if left is not None and left > 1:
        return None
    # Guard: do not treat lose as final result while countdown is still running.
    lose_allowed = (left is None) or (left <= 1)
    if lose_allowed and any(k in joined for k in RESULT_LOSE_KEYWORDS):
        return "lose"
    if lose_allowed and any(p.search(joined) for p in RESULT_LOSE_PATTERNS):
        return "lose"
    if any(k in joined for k in RESULT_WIN_KEYWORDS):
        return "win"
    if any(p.search(joined) for p in RESULT_WIN_PATTERNS):
        return "win"
    return None


def is_diamond_luckybag_popup(texts: list[str]) -> bool:
    joined = " | ".join(texts)
    has_diamond = any(k in joined for k in DIAMOND_BAG_KEYWORDS)
    has_physical = any(k in joined for k in PHYSICAL_BAG_HINT_KEYWORDS)
    return has_diamond and not has_physical


def is_non_physical_luckybag_popup(texts: list[str]) -> bool:
    joined = " | ".join(texts)
    has_non_physical = any(k in joined for k in NON_PHYSICAL_BAG_KEYWORDS)
    has_physical = any(k in joined for k in PHYSICAL_BAG_HINT_KEYWORDS)
    return has_non_physical and not has_physical


def classify_reference_value_filter(texts: list[str]) -> Optional[str]:
    val = parse_reference_value_yuan(texts)
    if val is None:
        return None
    # Hard floor: ultra-low value bags are always skipped.
    if val < 10.0:
        return "low-value-under-10"
    left = parse_popup_draw_countdown_seconds(texts)
    if left is None:
        left = parse_countdown_seconds(texts)
    if left is not None and left > 300 and val < 100.0:
        return "low-value-long-countdown"
    return None


def is_luckybag_popup_visible(texts: list[str]) -> bool:
    if is_popularity_board_popup(texts):
        return False
    joined = " | ".join(texts)
    if has_popup_draw_anchor(texts):
        return True
    if any(k in joined for k in PANEL_HINT_KEYWORDS):
        return True
    if any(k in joined for k in TASK_UNFINISHED_KEYWORDS):
        return True
    if any(k in joined for k in TASK_ACTION_TEXT_KEYWORDS):
        return ("福袋" in joined) or ("参与任务" in joined) or ("参考价值" in joined)
    return False


def contains_blocked(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR]) -> bool:
    hits = native_candidates(driver, BLOCKED_KEYWORDS)
    if hits:
        return True
    if ocr_engine is None:
        return False
    return bool(ocr_candidates(driver, ocr_engine, BLOCKED_KEYWORDS))


def contains_lose_popup(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR]) -> bool:
    texts = merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=True)
    if detect_draw_result(texts) == "lose":
        return True
    joined = " | ".join(texts)
    return any(k in joined for k in LOSE_POPUP_HINT_KEYWORDS)


def contains_red_packet_exit_signal(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR]) -> bool:
    texts = merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=True)
    joined = " | ".join(texts)
    return any(k in joined for k in RED_PACKET_EXIT_KEYWORDS)


def has_popularity_popup(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
) -> bool:
    if native_candidates(driver, POPULARITY_POPUP_KEYWORDS):
        return True
    if ocr_engine is not None and ocr_candidates(driver, ocr_engine, POPULARITY_POPUP_KEYWORDS):
        return True
    texts = merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=False)
    return is_popularity_board_popup(texts)


def dismiss_popularity_popup(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
) -> bool:
    if not has_popularity_popup(driver, ocr_engine):
        return False

    size = driver.get_window_size()
    # Single safe dismiss point: middle area, slightly above center.
    tap_points = [(0.50, 0.38), (0.50, 0.38), (0.50, 0.38)]
    for idx, (rx, ry) in enumerate(tap_points, start=1):
        x = int(size["width"] * rx)
        y = int(size["height"] * ry)
        log(f"Dismiss popularity popup (probe#{idx}) @ ({x},{y})")
        tap(driver, x, y)
        time.sleep(0.25)
        if not has_popularity_popup(driver, ocr_engine):
            return True
    return True


def is_risky_overlay_close_hit(driver: webdriver.Remote, hit: Hit) -> bool:
    t = (hit.text or "").strip()
    if any(k in t for k in RISKY_EXIT_KEYWORDS):
        return True
    try:
        size = driver.get_window_size()
        x_limit = int(size["width"] * 0.46)
        y_limit = int(size["height"] * 0.20)
        if hit.x <= x_limit and hit.y <= y_limit:
            return True
    except Exception:
        pass
    return False


# ---- UI actions ----

def swipe_to_next_room(driver: webdriver.Remote, duration: float = 0.35) -> None:
    size = driver.get_window_size()
    # Strict vertical swipe on center line (fromX == toX) to avoid any
    # horizontal edge-gesture ambiguity.
    x = int(size["width"] * 0.50)
    start_y = int(size["height"] * 0.78)
    end_y = int(size["height"] * 0.22)
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


def room_fingerprint(driver: webdriver.Remote) -> tuple[str, ...]:
    try:
        size = driver.get_window_size()
        h = int(size["height"])
    except Exception:
        h = 844

    top_min = int(h * 0.04)
    top_max = int(h * 0.44)
    ignore_contains = (
        "福袋",
        "关闭",
        "关注",
        "分享",
        "更多",
        "评论",
        "说点什么",
        "人气榜",
        "带货榜",
        "榜单",
        "直播中",
        "小时榜",
    )

    hits = source_hits(
        driver,
        ("XCUIElementTypeStaticText", "XCUIElementTypeButton"),
        keywords=None,
        upper_left_only=False,
        lower_half_only=False,
    )
    tokens: list[str] = []
    for hit in hits:
        if hit.y < top_min or hit.y > top_max:
            continue
        t = re.sub(r"\s+", "", (hit.text or "").strip())
        if not t or len(t) < 2 or len(t) > 28:
            continue
        if any(k in t for k in ignore_contains):
            continue
        if re.fullmatch(r"[0-9:：]+", t):
            continue
        if re.fullmatch(r"[0-9]+", t):
            continue
        if t not in tokens:
            tokens.append(t)
        if len(tokens) >= 12:
            break
    return tuple(tokens)


def is_room_switched(before_fp: tuple[str, ...], after_fp: tuple[str, ...]) -> bool:
    if not before_fp or not after_fp:
        return False
    b = set(before_fp)
    a = set(after_fp)
    common = len(a & b)
    baseline = max(1, min(len(a), len(b)))
    similarity = common / baseline
    return similarity < 0.45


def dismiss_overlays(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR], rounds: int = 4) -> int:
    closed = 0
    for _ in range(rounds):
        if dismiss_popularity_popup(driver, ocr_engine):
            invalidate_open_entry_cache()
            closed += 1
            time.sleep(0.20)
            continue

        hits = filter_short_hits(native_candidates(driver, CLOSE_KEYWORDS), max_len=8)
        if not hits and ocr_engine is not None:
            hits = filter_short_hits(ocr_candidates(driver, ocr_engine, CLOSE_KEYWORDS), max_len=8)
        if hits:
            safe_hits = [h for h in hits if not is_risky_overlay_close_hit(driver, h)]
            if len(safe_hits) != len(hits):
                log("Skip risky close hit(s) near live-room exit area.")
            hits = safe_hits
        hit = pick_best_hit(hits, CLOSE_KEYWORDS)
        if hit is not None:
            log(f"Dismiss overlay ({hit.source}) -> '{hit.text}' @ ({hit.x},{hit.y})")
            tap(driver, hit.x, hit.y)
            closed += 1
            time.sleep(0.25)
            continue

        panel_hits = native_candidates(driver, PANEL_HINT_KEYWORDS)
        if not panel_hits and ocr_engine is not None:
            panel_hits = ocr_candidates(driver, ocr_engine, PANEL_HINT_KEYWORDS)
        if panel_hits:
            size = driver.get_window_size()
            x = int(size["width"] * 0.5)
            y = int(size["height"] * 0.38)
            log(f"Dismiss overlay (blank area) @ ({x},{y})")
            tap(driver, x, y)
            closed += 1
            time.sleep(0.25)
            continue
        break
    return closed


def switch_room_hard(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    post_wait: float,
    precheck_reason: Optional[str] = None,
) -> bool:
    if precheck_reason:
        if try_open_popup_recheck_before_switch(driver, ocr_engine, reason=f"final-{precheck_reason}"):
            log(f"Final pre-switch recheck kept current room ({precheck_reason}).")
            return False

    before_fp = room_fingerprint(driver)
    invalidate_open_entry_cache()
    dismiss_overlays(driver, ocr_engine, rounds=4)
    size = driver.get_window_size()
    tap(driver, int(size["width"] * 0.5), int(size["height"] * 0.38))
    time.sleep(0.15)
    for idx, duration in enumerate((0.40, 0.46, 0.52), start=1):
        swipe_to_next_room(driver, duration=duration)
        time.sleep(0.20)
        swipe_to_next_room(driver, duration=duration)
        wait_seconds = 3.0 + random.uniform(0.0, 2.0)
        log(f"Post-swipe wait: {wait_seconds:.2f}s (attempt {idx})")
        time.sleep(wait_seconds)
        after_fp = room_fingerprint(driver)
        if is_room_switched(before_fp, after_fp):
            log(f"Room switch verified (attempt {idx}).")
            return True
        log(f"Room switch not verified (attempt {idx}), retry swipe.")
        dismiss_overlays(driver, ocr_engine, rounds=2)
        size = driver.get_window_size()
        tap(driver, int(size["width"] * 0.5), int(size["height"] * 0.38))
        time.sleep(0.12)

    log("Room switch failed to verify after retries; stay in current room.")
    return False


def close_overlays_and_reopen_luckybag(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    post_tap_wait: float = 0.55,
) -> bool:
    closed = dismiss_overlays(driver, ocr_engine, rounds=6)
    if closed > 0:
        log(f"Post-fans-confirm dismiss overlays: {closed}")
    size = driver.get_window_size()
    tap(driver, int(size["width"] * 0.5), int(size["height"] * 0.38))
    time.sleep(0.20)

    open_hit = find_open_entry_hit(driver, ocr_engine)
    if open_hit is None:
        log("Post-fans-confirm reopen: no lucky-bag entry found.")
        return False
    if not is_safe_open_entry_point(driver, open_hit.x, open_hit.y):
        log(f"Post-fans-confirm reopen skipped unsafe OPEN hit @ ({open_hit.x},{open_hit.y})")
        invalidate_open_entry_cache()
        return False

    log(f"Post-fans-confirm reopen OPEN ({open_hit.source}) -> '{open_hit.text}' @ ({open_hit.x},{open_hit.y})")
    tap(driver, open_hit.x, open_hit.y)
    time.sleep(post_tap_wait)
    return True


def try_open_popup_recheck_before_switch(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    reason: str,
) -> bool:
    def _fallback_probe_points() -> list[tuple[int, int]]:
        size = driver.get_window_size()
        w, h = int(size["width"]), int(size["height"])
        points: list[tuple[int, int]] = []

        # Prefer last known open-entry location (even if cache TTL expired).
        if _open_entry_cache_hit is not None:
            x0, y0 = int(_open_entry_cache_hit.x), int(_open_entry_cache_hit.y)
            points.extend(
                [
                    (x0, y0),
                    (x0 + 22, y0),
                    (x0 - 22, y0),
                    (x0, y0 + 18),
                ]
            )

        # Expanded deterministic probes across upper-left area.
        ratio_points = [
            (0.14, 0.14),
            (0.20, 0.14),
            (0.26, 0.14),
            (0.32, 0.14),
            (0.16, 0.20),
            (0.22, 0.20),
            (0.28, 0.20),
            (0.34, 0.20),
            (0.20, 0.26),
            (0.28, 0.26),
        ]
        for rx, ry in ratio_points:
            points.append((int(w * rx), int(h * ry)))

        x_min = int(w * OPEN_ENTRY_REGION_X_MIN_RATIO)
        y_min = int(h * OPEN_ENTRY_REGION_Y_MIN_RATIO)
        x_max = int(w * min(0.40, OPEN_ENTRY_REGION_X_MAX_RATIO + 0.06))
        y_max = int(h * min(0.44, OPEN_ENTRY_REGION_Y_MAX_RATIO + 0.06))

        dedup: list[tuple[int, int]] = []
        for x, y in points:
            if x < x_min or y < y_min or x > x_max or y > y_max:
                continue
            if any(abs(x - px) <= 12 and abs(y - py) <= 12 for px, py in dedup):
                continue
            dedup.append((x, y))
        return dedup

    def _collect_popup_after_tap(x: int, y: int, tag: str) -> list[str]:
        if not is_safe_open_entry_point(driver, x, y):
            log(f"Pre-switch recheck skip unsafe OPEN tap ({tag}) @ ({x},{y})")
            return []
        log(f"Pre-switch recheck tap OPEN ({tag}) @ ({x},{y})")
        tap(driver, x, y)
        time.sleep(0.55)
        return merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=True)

    log(f"Pre-switch recheck ({reason}): try opening lucky-bag popup once.")
    open_hit = find_open_entry_hit(driver, ocr_engine)
    popup_texts: list[str] = []
    if open_hit is not None:
        popup_texts = _collect_popup_after_tap(
            open_hit.x,
            open_hit.y,
            f"{open_hit.source}:{open_hit.text}",
        )
        if is_popularity_board_popup(popup_texts):
            log("Pre-switch recheck: popularity-board popup detected after open-hit tap, dismiss and continue probes.")
            invalidate_open_entry_cache()
            dismiss_overlays(driver, ocr_engine, rounds=3)
            popup_texts = []
        if not is_luckybag_popup_visible(popup_texts):
            log("Pre-switch recheck: popup not visible after open-hit tap.")
            popup_texts = []
    else:
        log("Pre-switch recheck: no lucky-bag entry found from detector.")

    # Fallback: when entry detection fails, probe a denser set of upper-left points.
    if not popup_texts:
        fallback_points = _fallback_probe_points()
        popularity_hits = 0
        for idx, (fx, fy) in enumerate(fallback_points, start=1):
            candidate_texts = _collect_popup_after_tap(fx, fy, f"fallback#{idx}")
            if is_popularity_board_popup(candidate_texts):
                log("Pre-switch recheck: popularity-board popup detected in fallback probe, dismiss and continue.")
                invalidate_open_entry_cache()
                dismiss_overlays(driver, ocr_engine, rounds=3)
                popularity_hits += 1
                if popularity_hits >= OPEN_FALLBACK_MAX_POPULARITY_HITS:
                    log("Pre-switch recheck: popularity-board hits exceeded threshold, stop fallback probes and keep switch.")
                    return False
                continue
            if is_luckybag_popup_visible(candidate_texts):
                popup_texts = candidate_texts
                log(f"Pre-switch recheck: popup visible via fallback tap#{idx}.")
                break
        # One delayed retry pass to handle just-refreshed entrance animations.
        if not popup_texts:
            time.sleep(0.65)
            open_hit_retry = find_open_entry_hit(driver, ocr_engine)
            if open_hit_retry is not None:
                candidate_texts = _collect_popup_after_tap(
                    open_hit_retry.x,
                    open_hit_retry.y,
                    f"retry:{open_hit_retry.source}:{open_hit_retry.text}",
                )
                if is_popularity_board_popup(candidate_texts):
                    log("Pre-switch recheck: popularity-board popup detected in delayed retry, dismiss and keep switch.")
                    invalidate_open_entry_cache()
                    dismiss_overlays(driver, ocr_engine, rounds=3)
                    candidate_texts = []
                if is_luckybag_popup_visible(candidate_texts):
                    popup_texts = candidate_texts
                    log("Pre-switch recheck: popup visible via delayed detector retry.")
        if not popup_texts:
            log("Pre-switch recheck: popup still not visible after fallback taps.")
            return False

    if is_diamond_luckybag_popup(popup_texts):
        log("Pre-switch recheck: diamond popup detected, keep switch decision.")
        return False
    if is_non_physical_luckybag_popup(popup_texts):
        log("Pre-switch recheck: non-physical popup detected, keep switch decision.")
        return False
    if is_popup_countdown_zero(popup_texts):
        log("Pre-switch recheck: popup countdown is 0, keep switch decision.")
        return False
    value_filter_reason = classify_reference_value_filter(popup_texts)
    if value_filter_reason:
        ref_val = parse_reference_value_yuan(popup_texts)
        left = parse_popup_draw_countdown_seconds(popup_texts)
        if left is None:
            left = parse_countdown_seconds(popup_texts)
        if value_filter_reason == "low-value-under-10":
            log(f"Pre-switch recheck: low reference value popup (ref={ref_val}元 < 10), keep switch.")
        else:
            log(f"Pre-switch recheck: low-value long countdown popup (countdown={left}s, ref={ref_val}元), keep switch.")
        return False

    # Run one mini main-flow task pass in recheck mode so we don't miss
    # actionable tasks (e.g. 一键发表评论 / 加入粉丝团) before switching rooms.
    joined = " | ".join(popup_texts)
    should_try_tasks = has_unfinished_task_text(driver, ocr_engine) or any(
        k in joined for k in ("一键发表评论", "加入粉丝团", "加入粉丝", "去参与", "立即参与")
    )
    if should_try_tasks:
        taps, still_unfinished, fans_confirmed = run_task_panel_actions(driver, ocr_engine, rounds=4)
        if taps > 0:
            log(f"Pre-switch recheck task taps: {taps}")
        if fans_confirmed:
            log("Pre-switch recheck: fans-group confirm succeeded, reopen lucky-bag panel.")
            close_overlays_and_reopen_luckybag(driver, ocr_engine)
        popup_texts = merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=True)
        if still_unfinished:
            log("Pre-switch recheck: task still unfinished after recheck taps; keep current room.")
            return True

    if contains_success_in_texts(popup_texts) and has_luckybag_context(popup_texts):
        log("Pre-switch recheck detected success text in lucky-bag context; keep current room.")
        return True
    if has_task_success_cta_text(popup_texts):
        left2 = parse_popup_draw_countdown_seconds(popup_texts)
        if left2 is None:
            left2 = parse_countdown_seconds(popup_texts)
        if left2 is not None and left2 > 0:
            log(f"Pre-switch recheck detected active draw countdown ({left2}s); keep current room.")
            return True

    left = parse_popup_draw_countdown_seconds(popup_texts)
    if left is None:
        left = parse_countdown_seconds(popup_texts)
    if left is not None:
        log(f"Pre-switch recheck hit valid popup, keep current room (countdown={left}s).")
    else:
        log("Pre-switch recheck hit popup, keep current room and continue processing.")
    return True


# ---- Task actions ----

def _dedup_hits(hits: list[Hit], dist: int = 18) -> list[Hit]:
    out: list[Hit] = []
    for h in hits:
        keep = True
        for x in out:
            if abs(h.x - x.x) <= dist and abs(h.y - x.y) <= dist and h.text == x.text:
                keep = False
                break
        if keep:
            out.append(h)
    return out


def has_unfinished_task_text(driver: webdriver.Remote, ocr_engine: Optional[RapidOCR]) -> bool:
    if native_candidates(driver, TASK_UNFINISHED_KEYWORDS):
        return True
    if ocr_engine is None:
        return False
    return bool(ocr_candidates(driver, ocr_engine, TASK_UNFINISHED_KEYWORDS))


def find_red_button_centers(image: np.ndarray) -> list[tuple[int, int, int]]:
    h, w, _ = image.shape
    crop_top = int(h * 0.35)
    roi = image[crop_top:, :, :]

    step = 3
    small = roi[::step, ::step, :]
    r = small[:, :, 0].astype(np.int16)
    g = small[:, :, 1].astype(np.int16)
    b = small[:, :, 2].astype(np.int16)
    mask = (r > 180) & (g < 120) & (b < 120) & ((r - g) > 55) & ((r - b) > 55)

    hh, ww = mask.shape
    visited = np.zeros_like(mask, dtype=np.uint8)
    out: list[tuple[int, int, int]] = []
    dirs = ((1, 0), (-1, 0), (0, 1), (0, -1))

    for y in range(hh):
        for x in range(ww):
            if not mask[y, x] or visited[y, x]:
                continue
            q: deque[tuple[int, int]] = deque([(x, y)])
            visited[y, x] = 1
            minx = maxx = x
            miny = maxy = y
            area = 0

            while q:
                cx, cy = q.popleft()
                area += 1
                minx = min(minx, cx)
                maxx = max(maxx, cx)
                miny = min(miny, cy)
                maxy = max(maxy, cy)
                for dx, dy in dirs:
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < ww and 0 <= ny < hh and mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = 1
                        q.append((nx, ny))

            bw = maxx - minx + 1
            bh = maxy - miny + 1
            if area < 120 or bw < 24 or bh < 10:
                continue
            ratio = bw / max(1, bh)
            if ratio < 1.4 or ratio > 10:
                continue
            cx = int(((minx + maxx) / 2) * step)
            cy = int(((miny + maxy) / 2) * step + crop_top)
            out.append((cx, cy, area))

    x_min = int(w * 0.12)
    x_max = int(w * 0.88)
    y_min = int(h * 0.55)
    y_max = int(h * 0.95)
    out = [c for c in out if x_min <= c[0] <= x_max and y_min <= c[1] <= y_max]
    out.sort(key=lambda t: t[2], reverse=True)
    return out[:6]


def pick_task_text_buttons(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    strict_only: bool = False,
) -> list[Hit]:
    size = driver.get_window_size()
    min_y = int(size["height"] * 0.55)
    max_y = int(size["height"] * 0.96)
    keys = STRICT_TASK_ACTION_TEXT_KEYWORDS if strict_only else TASK_ACTION_TEXT_KEYWORDS

    hits = native_candidates(driver, keys)
    if not hits and ocr_engine is not None:
        hits = ocr_candidates(driver, ocr_engine, keys)

    out: list[Hit] = []
    for h in hits:
        t = (h.text or "").strip()
        if not t:
            continue
        if any(k in t for k in TASK_ACTION_BLOCKLIST):
            continue
        if not any(k in t for k in keys):
            continue
        if h.y < min_y or h.y > max_y:
            continue
        if any(k in t for k in TASK_TEXT_BLOCKLIST):
            continue
        out.append(h)
    return _dedup_hits(out, dist=14)


def pick_fans_group_button(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
) -> Optional[Hit]:
    size = driver.get_window_size()
    min_y = int(size["height"] * 0.55)
    max_y = int(size["height"] * 0.96)

    hits = native_candidates(driver, FANS_GROUP_CTA_KEYWORDS)
    if not hits and ocr_engine is not None:
        hits = ocr_candidates(driver, ocr_engine, FANS_GROUP_CTA_KEYWORDS)

    out: list[Hit] = []
    for h in hits:
        t = (h.text or "").strip()
        if not t:
            continue
        if h.y < min_y or h.y > max_y:
            continue
        if any(k in t for k in TASK_ACTION_BLOCKLIST):
            continue
        if any(k in t for k in FANS_GROUP_IGNORE_TEXT):
            continue
        if not any(k in t for k in FANS_GROUP_CTA_KEYWORDS):
            continue
        out.append(h)

    out = _dedup_hits(out, dist=14)
    if not out:
        return None
    return pick_best_hit(out, FANS_GROUP_CTA_KEYWORDS)


def pick_fans_group_confirm_button(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
) -> Optional[Hit]:
    size = driver.get_window_size()
    min_y = int(size["height"] * 0.45)
    max_y = int(size["height"] * 0.96)

    hits = native_candidates(driver, FANS_GROUP_CONFIRM_KEYWORDS)
    if not hits and ocr_engine is not None:
        hits = ocr_candidates(driver, ocr_engine, FANS_GROUP_CONFIRM_KEYWORDS)

    out: list[Hit] = []
    for h in hits:
        t = (h.text or "").strip()
        if not t:
            continue
        if h.y < min_y or h.y > max_y:
            continue
        if any(k in t for k in TASK_ACTION_BLOCKLIST):
            continue
        if any(k in t for k in FANS_GROUP_IGNORE_TEXT):
            continue
        if not any(k in t for k in FANS_GROUP_CONFIRM_KEYWORDS):
            continue
        out.append(h)

    out = _dedup_hits(out, dist=14)
    if not out:
        return None
    return pick_best_hit(out, FANS_GROUP_CONFIRM_KEYWORDS)


def tap_fans_group_confirm_popup(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    max_wait_seconds: float = 3.2,
    max_taps: int = 2,
) -> int:
    tapped = 0
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline and tapped < max_taps:
        hit = pick_fans_group_confirm_button(driver, ocr_engine)
        if hit is None:
            time.sleep(0.30)
            continue

        log(f"Tap TASK (fans-confirm:{hit.source}) -> '{hit.text}' @ ({hit.x},{hit.y})")
        tap(driver, hit.x, hit.y)
        tapped += 1
        time.sleep(0.45)

        # Confirm popup is often a second-layer join button; if it disappears, stop retrying.
        if pick_fans_group_confirm_button(driver, ocr_engine) is None:
            break

    return tapped


def run_task_panel_actions(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    rounds: int = 6,
) -> tuple[int, bool, bool]:
    taps = 0
    clicked_points: list[tuple[int, int]] = []
    still_unfinished = has_unfinished_task_text(driver, ocr_engine)
    fans_confirmed = False

    panel_texts = merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=True)
    panel_confirmed = any(k in " | ".join(panel_texts) for k in PANEL_HINT_KEYWORDS)
    strict_text_only_mode = not panel_confirmed
    if strict_text_only_mode:
        log("Lucky-bag panel hints not found; fallback to strict text-task taps only.")

    for round_idx in range(rounds):
        if not still_unfinished and round_idx > 0:
            break

        tapped_this_round = 0

        if not strict_text_only_mode:
            img = screenshot_np(driver)
            for rx, ry, _ in find_red_button_centers(img):
                if any(abs(rx - px) <= 15 and abs(ry - py) <= 15 for px, py in clicked_points):
                    continue
                log(f"Tap TASK (red-shape) @ ({rx},{ry})")
                tap(driver, rx, ry)
                clicked_points.append((rx, ry))
                taps += 1
                tapped_this_round += 1
                time.sleep(0.35)

        fans_hit = pick_fans_group_button(driver, ocr_engine)
        if fans_hit is not None and not any(abs(fans_hit.x - px) <= 12 and abs(fans_hit.y - py) <= 12 for px, py in clicked_points):
            log(f"Tap TASK (fans-group:{fans_hit.source}) -> '{fans_hit.text}' @ ({fans_hit.x},{fans_hit.y}) [#1]")
            tap(driver, fans_hit.x, fans_hit.y)
            clicked_points.append((fans_hit.x, fans_hit.y))
            taps += 1
            tapped_this_round += 1
            time.sleep(0.35)

            confirm_taps = tap_fans_group_confirm_popup(driver, ocr_engine, max_wait_seconds=3.2, max_taps=2)
            if confirm_taps > 0:
                taps += confirm_taps
                tapped_this_round += confirm_taps
                fans_confirmed = True

        for h in pick_task_text_buttons(driver, ocr_engine, strict_only=strict_text_only_mode):
            if any(abs(h.x - px) <= 12 and abs(h.y - py) <= 12 for px, py in clicked_points):
                continue
            log(f"Tap TASK (text-fallback:{h.source}) -> '{h.text}' @ ({h.x},{h.y})")
            tap(driver, h.x, h.y)
            clicked_points.append((h.x, h.y))
            taps += 1
            tapped_this_round += 1
            time.sleep(0.35)

        if tapped_this_round == 0:
            break

        time.sleep(0.35)
        still_unfinished = has_unfinished_task_text(driver, ocr_engine)
        if still_unfinished:
            log("Task status still contains '未达成', continue tapping...")

    still_unfinished = has_unfinished_task_text(driver, ocr_engine)
    return taps, still_unfinished, fans_confirmed


def try_task_actions_during_active_countdown(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    last_try_ts: float,
    min_interval: float = 8.0,
) -> tuple[int, float]:
    now = time.time()
    if now - last_try_ts < min_interval:
        return 0, last_try_ts

    popup_texts = merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=True)
    if not (has_popup_draw_anchor(popup_texts) or has_task_success_cta_text(popup_texts)):
        return 0, last_try_ts

    joined = " | ".join(popup_texts)
    should_try = has_unfinished_task_text(driver, ocr_engine) or any(
        k in joined for k in ("一键发表评论", "加入粉丝团", "加入粉丝")
    )
    if not should_try:
        return 0, now

    taps, _, fans_confirmed = run_task_panel_actions(driver, ocr_engine, rounds=4)
    if fans_confirmed:
        log("Fans-group confirm succeeded during draw-wait task actions; reopen lucky-bag panel for extra tasks.")
        close_overlays_and_reopen_luckybag(driver, ocr_engine)
    return taps, now


# ---- Draw wait ----

def wait_countdown_and_check_result(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    grace_seconds: float,
    poll_interval: float,
    sample_interval: float,
    max_wait_seconds: int,
    timer_offset_seconds: float = 1.0,
    result_probe_seconds: float = 8.0,
    reopen_interval_seconds: float = 6.0,
    max_no_bag_reopen_rounds: int = 3,
) -> str:
    log("Join success, entering draw-result wait flow...")
    log(
        f"Draw wait config: max_wait={max_wait_seconds}s, timer_offset={timer_offset_seconds:.1f}s, probe={result_probe_seconds:.1f}s."
    )

    deadline = time.time() + max_wait_seconds
    dynamic_deadline = deadline
    countdown_found = False
    countdown_target_ts: Optional[float] = None
    last_reported_left: Optional[int] = None

    last_reopen_ts = 0.0
    last_no_countdown_log_ts = 0.0
    no_bag_reopen_rounds = 0

    sample_step = max(0.5, float(sample_interval))
    next_sample_ts = time.time()

    while time.time() < dynamic_deadline:
        now = time.time()
        if now < next_sample_ts:
            time.sleep(min(0.2, next_sample_ts - now))
            continue
        next_sample_ts = now + sample_step

        texts = merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=True)
        result = detect_draw_result(texts)
        if result == "win":
            confirm_texts = unique_texts(visible_texts_raw(driver, lower_half_only=True))
            confirm_result = detect_draw_result(confirm_texts)
            if confirm_result == "win":
                log("Draw result detected: win (confirmed by native texts).")
                return "win"
            log("Win signal not confirmed by native texts, continue waiting.")
        elif result is not None:
            log(f"Draw result detected: {result}")
            return result

        left = parse_popup_draw_countdown_seconds(texts)
        if left is None:
            left = parse_countdown_seconds(texts)
        if left is not None:
            countdown_found = True
            target = time.time() + left + timer_offset_seconds
            if left <= 1:
                # Countdown reaches zero frequently before result text appears.
                # Force an immediate probe instead of waiting for an older, longer target.
                countdown_target_ts = time.time()
            elif countdown_target_ts is None or target > countdown_target_ts:
                countdown_target_ts = target
            # Once countdown is known, extend wait window to at least countdown target.
            if countdown_target_ts > dynamic_deadline:
                dynamic_deadline = countdown_target_ts
            if last_reported_left is None or abs(left - last_reported_left) >= 3:
                log(f"Countdown detected: {left}s, timer set to +{timer_offset_seconds:.1f}s.")
                last_reported_left = left
        else:
            if now - last_no_countdown_log_ts >= 8.0:
                log("No countdown text detected in draw popup; trying to refresh popup state.")
                last_no_countdown_log_ts = now

            if now - last_reopen_ts >= reopen_interval_seconds:
                open_hit = find_open_entry_hit(driver, ocr_engine)
                if open_hit is not None:
                    log(f"Re-open lucky-bag popup -> '{open_hit.text}' @ ({open_hit.x},{open_hit.y})")
                    tap(driver, open_hit.x, open_hit.y)
                    no_bag_reopen_rounds = 0
                    time.sleep(0.7)
                else:
                    no_bag_reopen_rounds += 1
                    log(
                        f"No lucky-bag entry while waiting for draw result ({no_bag_reopen_rounds}/{max_no_bag_reopen_rounds})."
                    )
                    if no_bag_reopen_rounds >= max_no_bag_reopen_rounds:
                        log("Draw popup appears stale/no-bag; keep waiting for countdown/result.")
                last_reopen_ts = now

        if countdown_target_ts is not None and time.time() >= countdown_target_ts:
            log("Countdown timer reached, probing draw result...")
            probe_end = time.time() + max(2.0, result_probe_seconds + grace_seconds)
            while time.time() < probe_end:
                probe_now = time.time()
                if probe_now < next_sample_ts:
                    time.sleep(min(0.2, next_sample_ts - probe_now))
                    continue
                next_sample_ts = probe_now + sample_step

                probe_texts = visible_texts_raw(driver, lower_half_only=True)
                probe_result = detect_draw_result(probe_texts)
                if probe_result == "win":
                    confirm_texts = unique_texts(visible_texts_raw(driver, lower_half_only=True))
                    confirm_result = detect_draw_result(confirm_texts)
                    if confirm_result == "win":
                        log("Draw result detected after timer: win (confirmed by native texts).")
                        return "win"
                    log("Post-timer win signal not confirmed by native texts.")
                elif probe_result is not None:
                    log(f"Draw result detected after timer: {probe_result}")
                    return probe_result
                time.sleep(max(0.3, poll_interval))
            log("Countdown timer reached but draw result text not found.")
            return "unknown_after_countdown"

    texts = visible_texts_raw(driver, lower_half_only=True)
    result = detect_draw_result(texts)
    if result is not None:
        log(f"Draw result detected at deadline: {result}")
        return result
    if countdown_found:
        log("Countdown finished but draw result text not found.")
        return "unknown_after_countdown"
    log("No countdown found in wait window; draw result unknown.")
    return "unknown_no_countdown"


def handle_post_join_draw_flow(
    driver: webdriver.Remote,
    ocr_engine: Optional[RapidOCR],
    draw_countdown_grace: float,
    draw_poll_interval: float,
    draw_sample_interval: float,
    draw_result_max_wait: int,
    post_swipe_wait: float,
) -> str:
    result = wait_countdown_and_check_result(
        driver,
        ocr_engine=ocr_engine,
        grace_seconds=draw_countdown_grace,
        poll_interval=draw_poll_interval,
        sample_interval=draw_sample_interval,
        max_wait_seconds=draw_result_max_wait,
    )
    if result == "win":
        log("Winner detected in draw result popup. Finish task.")
        return "win"
    if result == "lose":
        log("Draw result is lose. Close popup(s) and switch to next room.")
        switched = switch_room_hard(driver, ocr_engine, post_wait=post_swipe_wait, precheck_reason="draw-lose")
        if switched:
            return "lose"
        return "unknown"
    if result == "unknown_after_countdown":
        log("Countdown ended but no explicit result text. Treat as stale popup and switch to next room.")
        switched = switch_room_hard(
            driver,
            ocr_engine,
            post_wait=post_swipe_wait,
            precheck_reason="draw-unknown-after-countdown",
        )
        if switched:
            return "switched"
        return "unknown"
    log("Draw result unknown. Stay in current room and keep monitoring.")
    return "unknown"


# ---- Main loop ----

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
    parser.add_argument("--udid", default="auto")
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
    parser.add_argument("--draw-sample-interval", type=float, default=2.0)
    parser.add_argument("--draw-result-max-wait", type=int, default=240)

    parser.add_argument("--room-stall-seconds", type=float, default=45.0)
    parser.add_argument("--max-collapse-reopen-rounds", type=int, default=2)
    parser.add_argument("--max-unfinished-rounds", type=int, default=2)
    parser.add_argument("--open-entry-cache-ttl-seconds", type=float, default=20.0)
    parser.add_argument("--open-entry-ocr-cooldown-seconds", type=float, default=2.5)

    parser.add_argument("--wda-launch-timeout-ms", type=int, default=120000)
    parser.add_argument("--wda-connection-timeout-ms", type=int, default=120000)
    parser.add_argument("--use-new-wda", action="store_true")
    parser.add_argument("--wda-startup-retries", type=int, default=2)
    parser.add_argument("--wda-startup-retry-interval-ms", type=int, default=15000)
    parser.add_argument("--wait-for-idle-timeout", type=float, default=0.0)
    parser.add_argument("--wait-for-quiescence", action="store_true", default=False)

    args = parser.parse_args()

    if args.udid == "auto":
        detected = auto_detect_udid()
        if not detected:
            raise RuntimeError("Cannot auto-detect iOS real-device UDID. Pass --udid explicitly.")
        args.udid = detected
        log(f"Auto-detected UDID: {args.udid}")

    configure_open_entry_lookup(
        cache_ttl_seconds=args.open_entry_cache_ttl_seconds,
        ocr_cooldown_seconds=args.open_entry_ocr_cooldown_seconds,
    )

    ocr_engine = RapidOCR() if RapidOCR is not None else None
    if ocr_engine is None:
        log("OCR engine unavailable, running native-only.")

    log("Connecting to Appium...")
    driver: Optional[webdriver.Remote] = None
    try:
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
            wda_startup_retries=args.wda_startup_retries,
            wda_startup_retry_interval_ms=args.wda_startup_retry_interval_ms,
            wait_for_idle_timeout=args.wait_for_idle_timeout,
            wait_for_quiescence=args.wait_for_quiescence,
        )
    except Exception as e:
        msg = str(e)
        should_retry = ("WebDriverAgent" in msg) or ("xcodebuild failed with code 70" in msg)
        if not should_retry:
            raise
        log("WDA bootstrap failed, retrying session with useNewWDA=true ...")
        time.sleep(2.0)
        driver = build_driver(
            args.appium,
            args.udid,
            args.bundle_id,
            args.no_reset,
            xcode_org_id=args.xcode_org_id,
            updated_wda_bundle_id=args.updated_wda_bundle_id,
            show_xcode_log=True,
            allow_provisioning_updates=args.allow_provisioning_updates,
            allow_provisioning_device_registration=args.allow_provisioning_device_registration,
            wda_launch_timeout_ms=max(args.wda_launch_timeout_ms, 180000),
            wda_connection_timeout_ms=max(args.wda_connection_timeout_ms, 180000),
            use_new_wda=True,
            wda_startup_retries=max(3, args.wda_startup_retries),
            wda_startup_retry_interval_ms=max(20000, args.wda_startup_retry_interval_ms),
            wait_for_idle_timeout=args.wait_for_idle_timeout,
            wait_for_quiescence=args.wait_for_quiescence,
        )

    end_at = (time.time() + args.max_minutes * 60) if args.max_minutes > 0 else None
    if end_at is not None:
        log(f"Run with max-minutes={args.max_minutes}, but process exits only on confirmed win.")
    else:
        log("Run without max time limit; process exits only on confirmed win.")

    last_click_key: Optional[str] = None
    last_click_ts = 0.0
    last_swipe_ts = 0.0

    open_retry_count = 0
    no_open_rounds = 0
    collapse_reopen_rounds = 0
    unfinished_same_popup_rounds = 0

    room_enter_ts = time.time()
    last_progress_ts = time.time()
    last_countdown_task_try_ts = 0.0

    try:
        log("Started. Please manually stay in target live room.")
        while True:
            if end_at is not None and time.time() >= end_at:
                end_at = None
                log("Max-minutes reached; continue running until confirmed win.")

            # Red-packet interruption: close overlays and exit immediately.
            if contains_red_packet_exit_signal(driver, ocr_engine):
                log("Detected red-packet signal ('的红包'), dismiss overlays and exit.")
                dismiss_overlays(driver, ocr_engine, rounds=6)
                size = driver.get_window_size()
                tap(driver, int(size["width"] * 0.5), int(size["height"] * 0.38))
                time.sleep(0.2)
                return 0

            # Highest priority: joined success with lucky-bag context -> wait draw result.
            if contains_success_with_context(driver, ocr_engine):
                log("Detected success text in loop, entering draw-result wait flow.")
                draw_outcome = handle_post_join_draw_flow(
                    driver,
                    ocr_engine,
                    draw_countdown_grace=args.draw_countdown_grace,
                    draw_poll_interval=args.draw_poll_interval,
                    draw_sample_interval=args.draw_sample_interval,
                    draw_result_max_wait=args.draw_result_max_wait,
                    post_swipe_wait=args.post_swipe_wait,
                )
                if draw_outcome == "win":
                    return 0
                if draw_outcome in ("lose", "switched"):
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    room_enter_ts = time.time()
                last_progress_ts = time.time()
                continue

            if contains_blocked(driver, ocr_engine):
                if time.time() - last_swipe_ts >= args.blocked_swipe_cooldown:
                    if has_active_draw_countdown(driver, ocr_engine):
                        log("Blocked text seen but active draw countdown is running; stay in current room.")
                        last_progress_ts = time.time()
                        time.sleep(random.uniform(args.interval_min, args.interval_max))
                        continue
                    log("Detected blocked text, swiping to next live room...")
                    switched = switch_room_hard(
                        driver,
                        ocr_engine,
                        post_wait=args.post_swipe_wait,
                        precheck_reason="blocked-text",
                    )
                    if switched:
                        last_swipe_ts = time.time()
                        open_retry_count = 0
                        no_open_rounds = 0
                        collapse_reopen_rounds = 0
                        unfinished_same_popup_rounds = 0
                        room_enter_ts = time.time()
                    last_progress_ts = time.time()
                    continue

            if contains_lose_popup(driver, ocr_engine):
                log("Detected lose-result popup, close and switch to next room.")
                switched = switch_room_hard(
                    driver,
                    ocr_engine,
                    post_wait=args.post_swipe_wait,
                    precheck_reason="lose-popup",
                )
                if switched:
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    room_enter_ts = time.time()
                last_progress_ts = time.time()
                continue

            if (
                time.time() - room_enter_ts >= args.room_stall_seconds
                and time.time() - last_progress_ts >= args.room_stall_seconds
                and time.time() - last_swipe_ts >= args.blocked_swipe_cooldown
            ):
                if has_active_draw_countdown(driver, ocr_engine):
                    task_taps, last_countdown_task_try_ts = try_task_actions_during_active_countdown(
                        driver,
                        ocr_engine,
                        last_countdown_task_try_ts,
                    )
                    if task_taps > 0:
                        log(f"Countdown branch task taps: {task_taps}")
                    log("Room stall check skipped: active draw countdown detected.")
                    last_progress_ts = time.time()
                    time.sleep(random.uniform(args.interval_min, args.interval_max))
                    continue
                if try_open_popup_recheck_before_switch(driver, ocr_engine, reason="room-stall"):
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    last_progress_ts = time.time()
                    time.sleep(random.uniform(args.interval_min, args.interval_max))
                    continue
                log(f"Room stall timeout ({args.room_stall_seconds:.0f}s without progress), switch to next room.")
                switched = switch_room_hard(driver, ocr_engine, post_wait=args.post_swipe_wait)
                if switched:
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    room_enter_ts = time.time()
                last_progress_ts = time.time()
                continue

            # A) Direct join button first.
            join_hit = pick_best_hit(filter_short_hits(native_candidates(driver, JOIN_KEYWORDS), 10), JOIN_KEYWORDS)
            if join_hit is None and ocr_engine is not None:
                join_hit = pick_best_hit(filter_short_hits(ocr_candidates(driver, ocr_engine, JOIN_KEYWORDS), 10), JOIN_KEYWORDS)

            if join_hit is not None:
                click_key = join_hit.text
                if click_key == last_click_key and time.time() - last_click_ts < 5:
                    time.sleep(0.4)
                    continue

                log(f"Tap JOIN ({join_hit.source}) -> '{join_hit.text}' @ ({join_hit.x},{join_hit.y})")
                tap(driver, join_hit.x, join_hit.y)
                last_click_key = click_key
                last_click_ts = time.time()
                last_progress_ts = time.time()

                open_retry_count = 0
                no_open_rounds = 0
                collapse_reopen_rounds = 0
                unfinished_same_popup_rounds = 0

                time.sleep(0.9)
                continue

            # B) Open lucky-bag panel.
            open_hit = find_open_entry_hit(driver, ocr_engine)

            if open_hit is None:
                no_open_rounds += 1
                if no_open_rounds >= 1 and time.time() - last_swipe_ts >= args.blocked_swipe_cooldown:
                    if has_active_draw_countdown(driver, ocr_engine):
                        task_taps, last_countdown_task_try_ts = try_task_actions_during_active_countdown(
                            driver,
                            ocr_engine,
                            last_countdown_task_try_ts,
                        )
                        if task_taps > 0:
                            log(f"Countdown branch task taps: {task_taps}")
                        log("No-open switch skipped: active draw countdown detected.")
                        last_progress_ts = time.time()
                        time.sleep(random.uniform(args.interval_min, args.interval_max))
                        continue
                    if try_open_popup_recheck_before_switch(driver, ocr_engine, reason="no-open"):
                        open_retry_count = 0
                        no_open_rounds = 0
                        collapse_reopen_rounds = 0
                        unfinished_same_popup_rounds = 0
                        last_progress_ts = time.time()
                        time.sleep(random.uniform(args.interval_min, args.interval_max))
                        continue
                    log("No lucky-bag button in current room, swiping to next live room...")
                    switched = switch_room_hard(driver, ocr_engine, post_wait=args.post_swipe_wait)
                    if switched:
                        last_swipe_ts = time.time()
                        open_retry_count = 0
                        no_open_rounds = 0
                        collapse_reopen_rounds = 0
                        unfinished_same_popup_rounds = 0
                        room_enter_ts = time.time()
                    last_progress_ts = time.time()
                time.sleep(random.uniform(args.interval_min, args.interval_max))
                continue

            no_open_rounds = 0
            if not is_safe_open_entry_point(driver, open_hit.x, open_hit.y):
                log(f"Skip unsafe OPEN hit ({open_hit.source}) @ ({open_hit.x},{open_hit.y})")
                invalidate_open_entry_cache()
                open_retry_count += 1
                no_open_rounds += 1
                last_progress_ts = time.time()
                time.sleep(0.25)
                continue

            click_key = f"open:{open_hit.text}"
            if click_key == last_click_key and time.time() - last_click_ts < 3:
                time.sleep(0.2)
                continue

            log(f"Tap OPEN ({open_hit.source}) -> '{open_hit.text}' @ ({open_hit.x},{open_hit.y})")
            tap(driver, open_hit.x, open_hit.y)
            last_click_key = click_key
            last_click_ts = time.time()
            last_progress_ts = time.time()
            open_retry_count += 1
            time.sleep(0.55)

            open_entry_left = parse_open_entry_countdown_seconds(open_hit.text)
            if open_entry_left is not None:
                log(f"Open-entry countdown parsed: {open_entry_left}s")

            popup_texts = merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=True)

            if is_popularity_board_popup(popup_texts):
                log("Popularity-board popup detected after OPEN, dismiss overlays and continue scanning.")
                invalidate_open_entry_cache()
                dismiss_overlays(driver, ocr_engine, rounds=4)
                size = driver.get_window_size()
                tap(driver, int(size["width"] * 0.5), int(size["height"] * 0.38))
                time.sleep(0.20)
                last_progress_ts = time.time()
                continue

            if is_diamond_luckybag_popup(popup_texts):
                log("Diamond lucky-bag popup detected, skip and switch to next room.")
                switched = switch_room_hard(
                    driver,
                    ocr_engine,
                    post_wait=args.post_swipe_wait,
                    precheck_reason="diamond-popup",
                )
                if switched:
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    room_enter_ts = time.time()
                last_progress_ts = time.time()
                continue

            if is_non_physical_luckybag_popup(popup_texts):
                log("Non-physical lucky-bag popup detected, skip and switch to next room.")
                switched = switch_room_hard(
                    driver,
                    ocr_engine,
                    post_wait=args.post_swipe_wait,
                    precheck_reason="non-physical-popup",
                )
                if switched:
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    room_enter_ts = time.time()
                last_progress_ts = time.time()
                continue

            if is_popup_countdown_zero(popup_texts):
                log("Lucky-bag popup countdown is 0, skip and switch to next room.")
                switched = switch_room_hard(
                    driver,
                    ocr_engine,
                    post_wait=args.post_swipe_wait,
                    precheck_reason="popup-countdown-zero",
                )
                if switched:
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    room_enter_ts = time.time()
                last_progress_ts = time.time()
                continue

            popup_left = parse_popup_draw_countdown_seconds(popup_texts)
            if popup_left is None and has_popup_draw_anchor(popup_texts):
                time.sleep(0.25)
                popup_texts = merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=True)
                popup_left = parse_popup_draw_countdown_seconds(popup_texts)
            if popup_left is None and open_entry_left is not None:
                popup_left = open_entry_left
                log(f"Fallback to open-entry countdown: {popup_left}s")

            popup_countdown_unreadable = popup_left is None and has_popup_draw_anchor(popup_texts)
            if popup_countdown_unreadable:
                log("Lucky-bag popup countdown unreadable, try task actions first.")

            if popup_left is not None and popup_left > 300:
                ref_dbg = parse_reference_value_yuan(popup_texts)
                log(f"Long-countdown popup detected (countdown={popup_left}s, ref={ref_dbg}元).")

            value_filter_reason = classify_reference_value_filter(popup_texts)
            if value_filter_reason:
                ref_val = parse_reference_value_yuan(popup_texts)
                if value_filter_reason == "low-value-under-10":
                    log(f"Physical bag filtered (ref={ref_val}元 < 10), switch to next room.")
                else:
                    log(f"Physical bag filtered (countdown={popup_left}s, ref={ref_val}元; >5m && <100), switch to next room.")
                switched = switch_room_hard(
                    driver,
                    ocr_engine,
                    post_wait=args.post_swipe_wait,
                    precheck_reason=value_filter_reason,
                )
                if switched:
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    room_enter_ts = time.time()
                last_progress_ts = time.time()
                continue

            task_taps, still_unfinished, fans_confirmed = run_task_panel_actions(driver, ocr_engine, rounds=6)
            if task_taps > 0:
                log(f"Task panel taps: {task_taps}")
                last_progress_ts = time.time()

            if fans_confirmed:
                log("Fans-group confirm succeeded in popup; dismiss overlays and reopen lucky-bag panel for remaining tasks.")
                close_overlays_and_reopen_luckybag(driver, ocr_engine)
                last_click_key = None
                last_click_ts = 0.0
                open_retry_count = 0
                no_open_rounds = 0
                collapse_reopen_rounds = 0
                unfinished_same_popup_rounds = 0
                last_progress_ts = time.time()
                time.sleep(0.35)
                continue

            if still_unfinished:
                unfinished_same_popup_rounds += 1
                log(f"Task still unfinished in popup ({unfinished_same_popup_rounds}/{args.max_unfinished_rounds}), continue trying.")
                collapse_reopen_rounds = 0
                if unfinished_same_popup_rounds >= args.max_unfinished_rounds:
                    log("Unfinished task persists, switch to next room.")
                    switched = switch_room_hard(
                        driver,
                        ocr_engine,
                        post_wait=args.post_swipe_wait,
                        precheck_reason="unfinished-task-rounds",
                    )
                    if switched:
                        last_swipe_ts = time.time()
                        open_retry_count = 0
                        no_open_rounds = 0
                        collapse_reopen_rounds = 0
                        unfinished_same_popup_rounds = 0
                        room_enter_ts = time.time()
                    last_progress_ts = time.time()
                    continue
            else:
                unfinished_same_popup_rounds = 0

            post_popup_texts = merged_scene_texts(driver, ocr_engine, prefer_ocr_for_popup=True, popup_lower_half=True)
            popup_visible_after = is_luckybag_popup_visible(post_popup_texts)
            success_after = contains_success_in_texts(post_popup_texts) and has_luckybag_context(post_popup_texts)

            post_left = parse_popup_draw_countdown_seconds(post_popup_texts)
            if post_left is None:
                post_left = parse_countdown_seconds(post_popup_texts)

            if post_left and post_left > 0 and has_task_success_cta_text(post_popup_texts):
                log(f"Detected task-success CTA with countdown={post_left}s, entering draw-result wait flow.")
                draw_outcome = handle_post_join_draw_flow(
                    driver,
                    ocr_engine,
                    draw_countdown_grace=args.draw_countdown_grace,
                    draw_poll_interval=args.draw_poll_interval,
                    draw_sample_interval=args.draw_sample_interval,
                    draw_result_max_wait=args.draw_result_max_wait,
                    post_swipe_wait=args.post_swipe_wait,
                )
                if draw_outcome == "win":
                    return 0
                if draw_outcome in ("lose", "switched"):
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    room_enter_ts = time.time()
                last_progress_ts = time.time()
                continue

            if success_after:
                log("Detected success text after task actions, entering draw-result wait flow.")
                draw_outcome = handle_post_join_draw_flow(
                    driver,
                    ocr_engine,
                    draw_countdown_grace=args.draw_countdown_grace,
                    draw_poll_interval=args.draw_poll_interval,
                    draw_sample_interval=args.draw_sample_interval,
                    draw_result_max_wait=args.draw_result_max_wait,
                    post_swipe_wait=args.post_swipe_wait,
                )
                if draw_outcome == "win":
                    return 0
                if draw_outcome in ("lose", "switched"):
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    room_enter_ts = time.time()
                last_progress_ts = time.time()
                continue

            popup_countdown_unreadable = (post_left is None) and has_popup_draw_anchor(post_popup_texts)

            if task_taps > 0 and (not still_unfinished) and (not success_after) and (not popup_visible_after):
                collapse_reopen_rounds += 1
                log(
                    f"Popup collapsed after task tap but no success ({collapse_reopen_rounds}/{args.max_collapse_reopen_rounds})."
                )
                if collapse_reopen_rounds >= args.max_collapse_reopen_rounds:
                    log("No progress after repeated collapsed-task flow, switch to next room.")
                    switched = switch_room_hard(
                        driver,
                        ocr_engine,
                        post_wait=args.post_swipe_wait,
                        precheck_reason="collapsed-task-flow",
                    )
                    if switched:
                        last_swipe_ts = time.time()
                        open_retry_count = 0
                        no_open_rounds = 0
                        collapse_reopen_rounds = 0
                        unfinished_same_popup_rounds = 0
                        room_enter_ts = time.time()
                    last_progress_ts = time.time()
                continue

            if popup_visible_after or success_after:
                collapse_reopen_rounds = 0

            if popup_countdown_unreadable and (not success_after):
                log("Lucky-bag popup has '后开奖' but countdown remains unreadable after task actions, switch.")
                switched = switch_room_hard(
                    driver,
                    ocr_engine,
                    post_wait=args.post_swipe_wait,
                    precheck_reason="popup-countdown-unreadable",
                )
                if switched:
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    room_enter_ts = time.time()
                last_progress_ts = time.time()
                continue

            if open_retry_count >= args.open_retry_before_swipe and time.time() - last_swipe_ts >= args.blocked_swipe_cooldown:
                if has_active_draw_countdown(driver, ocr_engine):
                    log("OPEN-retry switch skipped: active draw countdown detected.")
                    last_progress_ts = time.time()
                    time.sleep(random.uniform(args.interval_min, args.interval_max))
                    continue
                if try_open_popup_recheck_before_switch(driver, ocr_engine, reason="open-retry"):
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    last_progress_ts = time.time()
                    time.sleep(random.uniform(args.interval_min, args.interval_max))
                    continue
                log("OPEN retries exceeded without JOIN, swiping to next live room...")
                switched = switch_room_hard(driver, ocr_engine, post_wait=args.post_swipe_wait)
                if switched:
                    last_swipe_ts = time.time()
                    open_retry_count = 0
                    no_open_rounds = 0
                    collapse_reopen_rounds = 0
                    unfinished_same_popup_rounds = 0
                    room_enter_ts = time.time()
                last_progress_ts = time.time()

            time.sleep(random.uniform(args.interval_min, args.interval_max))

    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    sys.exit(main())
