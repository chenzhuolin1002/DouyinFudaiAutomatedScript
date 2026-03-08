#!/usr/bin/env python3
"""
iOS Douyin 福袋 (Lucky Bag) Automation Bot  —  Refactored
=========================================================
Architecture: clean state-machine with explicit phases.

Phase flow:
  SCAN → OPEN → INSPECT → TASK → WAIT_DRAW → RESULT → SWITCH

Key improvements over previous version:
- No module-level mutable globals (state lives in BotState dataclass)
- Entry icon detection anchored to known screen region from visual analysis
- Diamond-bag vs fans-group-cost correctly distinguished
- Main loop is a clean dispatch table, not a 300-line if-chain
- Task panel reads the actual half-screen layout (title / countdown / tasks / CTA)
- Room fingerprint & switch logic unchanged but isolated in RoomNavigator
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import numpy as np
from appium import webdriver
from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy
from PIL import Image

try:
    from rapidocr_onnxruntime import RapidOCR
except Exception:
    RapidOCR = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def notify_imessage(phone: Optional[str], msg: str) -> None:
    """Send an iMessage via AppleScript. Silently skips if phone is None."""
    if not phone:
        return
    safe_msg = msg.replace('"', '\\"').replace("'", "\\'")
    safe_phone = phone.strip()
    script = (
        f'tell application "Messages"\n'
        f'  set targetService to 1st service whose service type = iMessage\n'
        f'  set targetBuddy to buddy "{safe_phone}" of targetService\n'
        f'  send "{safe_msg}" to targetBuddy\n'
        f'end tell'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=10,
                       capture_output=True)
        log(f"📱 iMessage sent to {safe_phone}")
    except Exception as e:
        log(f"[WARN] iMessage send failed: {e}")


# ---------------------------------------------------------------------------
# Screen layout constants (derived from screenshot analysis)
#
# The 福袋 entry icon lives in the TOP-LEFT cluster below the "带货总榜" row.
# On a 390-pt-wide screen it sits at roughly (x=120, y=253).
# We define a generous safe box:  x ∈ [5%, 45%],  y ∈ [12%, 42%]
# The half-screen popup occupies y > 55% of screen height.
# ---------------------------------------------------------------------------

# Entry icon region (ratio of screen W/H)
# Calibrated across two observed rooms:
#   Room A (414pt, PROYA):     福袋 icon at x=55pt  (13% W), y=235pt (26% H)
#   Room B (390pt, FlowerWest): 福袋 label at x=138pt (35% W), y=147pt (17% H)
# The icon can appear anywhere in the top-left quadrant depending on overlays.
# We use a wide-but-bounded box that excludes the right-side product panel
# and the very top nav bar (status bar + streamer info row at y<10%).
ENTRY_X_MIN = 0.03   # exclude far-left bezel
ENTRY_X_MAX = 0.45   # right edge of left overlay cluster
ENTRY_Y_MIN = 0.10   # below status bar / streamer name row
ENTRY_Y_MAX = 0.42   # above mid-screen comment/chat area

# Popup region (dark half-screen panel starts here)
# Screenshot shows panel top edge at ~y=430pt = 47.9% of 896.
# OLD value 0.52 (466pt) missed the 超级福袋 title and countdown row.
POPUP_Y_MIN = 0.47   # was 0.52

# Max element size for the entry icon / label
# Room A: square thumbnail ~48x48pt (11% W, 5% H)
# Room B: text label ~60x30pt (15% W, 3.5% H)
# Keep limits loose enough to catch both; shape filter rejects large product cards.
ENTRY_MAX_W_RATIO = 0.22   # reject anything wider than 22% screen width
ENTRY_MAX_H_RATIO = 0.12   # reject anything taller than 12% screen height
ENTRY_MAX_ASPECT   = 4.0   # text labels can be wider than tall
ENTRY_MIN_SIDE_PX  = 10    # must be at least 10pt on shorter side

# ---------------------------------------------------------------------------
# Keyword lists  (concise, no duplicates)
# ---------------------------------------------------------------------------

KW_OPEN_ENTRY     = ["福袋", "超级福袋", "幸运福袋", "福袋抽奖"]
KW_JOIN           = ["去参与", "立即参与", "参与抽奖", "马上参与"]

# Tasks inside the popup
KW_FANS_JOIN      = [
    "加入粉丝团",
    "去加入粉丝团",
    "立即加入粉丝团",
    "加入购物粉丝团",
    "去加入购物粉丝团",
    "立即加入购物粉丝团",
    "加入粉丝",
]
KW_FANS_CONFIRM   = [
    "确认加入",
    "确认加入粉丝团",
    "确认",
    "加入并关注",
    "立即加入",
    "同意并加入",
    "支付并加入",
    "开通粉丝团",
]
KW_COMMENT_TASK   = ["一键发表评论", "一键评论", "发表评论", "去评论"]
KW_TASK_GENERIC   = ["去完成", "去参与", "立即参与", "参与抽奖", "一键参与", "观看直播"]

KW_TASK_UNFINISHED = ["未达成", "未完成", "未满足"]
KW_TASK_DONE       = ["已达成", "已完成"]   # right-side status on each completed task row
KW_SUCCESS         = ["已参与", "参与成功", "等待开奖", "参与成功 等待开奖"]
KW_FANS_DONE       = ["已加入粉丝团", "本场已加入", "已加入", "已达成", "已完成", "已点亮"]

KW_WIN  = ["恭喜抽中", "恭喜你抽中", "恭喜你中奖了", "抽中福袋"]
KW_LOSE = ["未中奖", "未中签", "没有抽中福袋", "很遗憾", "下次再来", "擦肩而过"]
# Prize-claim overlay (often appears above the half-screen popup area).
# Example:
#   恭喜抽中福袋 / 立即领取奖品 / 已阅读并同意 / 用户协议 / 隐私政策
KW_WIN_CLAIM_CORE = ["恭喜抽中福袋", "立即领取奖品"]
KW_WIN_CLAIM_AUX  = ["已阅读并同意", "用户协议", "隐私政策"]

# Bags we skip
KW_NONPHYSICAL    = ["红包", "金币", "福气值", "现金红包", "抖币", "音浪"]
KW_PHYSICAL_HINT  = ["实物", "商品", "礼品", "包邮"]

# Popup identification anchors
KW_POPUP_ANCHOR   = ["后开奖", "后开", "超级福袋", "参与任务", "参考价值"]
KW_POPULARITY     = ["人气榜", "带货榜", "销量榜", "热销榜", "小时榜", "贡献榜", "粉丝榜"]

# Blocked / stale room signals
KW_BLOCKED        = ["暂未开始", "活动已结束", "已结束", "不可参与", "无法参与",
                     "已抢完", "人数已满"]
KW_LOSE_POPUP     = ["没有抽中福袋", "未中签", "查看中奖观众", "开奖结果", "已开奖"]
KW_RED_PACKET     = ["的红包"]

# Safe close overlays
KW_CLOSE          = ["关闭", "取消", "我知道了", "稍后再说"]
KW_RISKY_CLOSE    = ["返回推荐", "返回直播", "退出直播", "离开直播", "关闭直播", "结束观看"]

# Text we must NOT mistake for the entry icon label
KW_OPEN_BLOCKLIST = ["没有抽中", "未抽中", "抽中福袋", "已开奖", "开奖结果"]


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

@dataclass
class Hit:
    text: str
    x:    int
    y:    int
    w:    int = 0
    h:    int = 0
    src:  str = "?"


def _to_int(v: object, default: int = 0) -> int:
    try:
        return int(float(str(v)))
    except Exception:
        return default


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(k in text for k in keywords)


def _joined(texts: list[str]) -> str:
    return " | ".join(texts)


# ---------------------------------------------------------------------------
# Appium driver
# ---------------------------------------------------------------------------

EXCLUDED_DEVICE_MODEL_NAMES = {"iphone 13 pro max"}
EXCLUDED_DEVICE_PRODUCT_TYPES = {"iphone14,3"}


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
        with open(tmp_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    result = data.get("result", {}) if isinstance(data, dict) else {}
    devices = result.get("devices", []) if isinstance(result, dict) else []
    wired: list[str] = []
    non_wired: list[str] = []
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
        if _is_excluded_device_model(device_name, marketing_name, product_type=product_type):
            continue

        transport_type = str(conn.get("transportType") or "").strip().lower()
        if only_wired and transport_type and transport_type != "wired":
            continue
        if transport_type == "wired":
            wired.append(udid)
        else:
            non_wired.append(udid)

    ordered = wired if only_wired else (wired + non_wired)
    return list(dict.fromkeys(ordered))


def auto_detect_udid() -> Optional[str]:
    for only_wired in (True, False):
        udids = _discover_connected_udids_from_devicectl(only_wired=only_wired)
        if udids:
            return udids[0]
    try:
        out = subprocess.check_output(
            ["xcrun", "xctrace", "list", "devices"],
            stderr=subprocess.STDOUT, text=True,
        )
    except Exception:
        return None
    p = re.compile(r"\(([0-9A-Fa-f-]{20,})\)\s*$")
    for line in out.splitlines():
        s = line.strip()
        if not s or any(x in s for x in ("Simulator", "Mac", "Watch")):
            continue
        if _is_excluded_device_model(s):
            continue
        m = p.search(s)
        if m:
            return m.group(1)
    return None


def build_driver(appium_url: str, udid: str, bundle_id: str, **caps) -> webdriver.Remote:
    opts = XCUITestOptions()
    opts.platform_name = "iOS"
    opts.automation_name = "XCUITest"
    opts.udid = udid
    opts.bundle_id = bundle_id
    opts.set_capability("noReset", True)
    for k, v in caps.items():
        opts.set_capability(k, v)
    return webdriver.Remote(appium_url, options=opts)


# ---------------------------------------------------------------------------
# Low-level UI primitives
# ---------------------------------------------------------------------------

def tap(driver: webdriver.Remote, x: int, y: int) -> None:
    driver.execute_script("mobile: tap", {"x": int(x), "y": int(y)})


def screen_size(driver: webdriver.Remote, default: tuple[int,int] = (414, 896)) -> tuple[int, int]:
    try:
        s = driver.get_window_size()
        return int(s["width"]), int(s["height"])
    except Exception:
        return default


def screenshot_np(driver: webdriver.Remote, retries: int = 2) -> np.ndarray:
    """Take screenshot with retry on WDA socket hang-up."""
    from io import BytesIO
    last_exc: Exception = RuntimeError("no attempts")
    for attempt in range(1 + retries):
        try:
            png = driver.get_screenshot_as_png()
            return np.array(Image.open(BytesIO(png)).convert("RGB"))
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(0.8)
    raise last_exc


# ---------------------------------------------------------------------------
# Page-source element scraper  (replaces the huge source_hits() function)
# ---------------------------------------------------------------------------

_SCRAPE_TYPES = {
    "XCUIElementTypeButton",
    "XCUIElementTypeStaticText",
    "XCUIElementTypeOther",
}

def scrape_elements(
    driver: webdriver.Remote,
    types:    Optional[set[str]] = None,
    keywords: Optional[list[str]] = None,
    x_min_r: float = 0.0,
    x_max_r: float = 1.0,
    y_min_r: float = 0.0,
    y_max_r: float = 1.0,
) -> list[Hit]:
    """Parse XML page source and return matching hits within the given region."""
    try:
        root = ET.fromstring(driver.page_source)
    except Exception:
        return []

    # Infer screen size from first sizeable element
    sw = sh = 0
    for el in root.iter():
        w = _to_int(el.attrib.get("width"))
        h = _to_int(el.attrib.get("height"))
        if w > 100 and h > 100:
            sw, sh = w, h
            break
    if sw <= 0: sw = 390
    if sh <= 0: sh = 844

    x0 = int(sw * x_min_r); x1 = int(sw * x_max_r)
    y0 = int(sh * y_min_r); y1 = int(sh * y_max_r)
    type_set = types or _SCRAPE_TYPES

    out: list[Hit] = []
    for el in root.iter():
        t = el.attrib.get("type", "")
        if t not in type_set:
            continue
        vis = str(el.attrib.get("visible", "1")).lower()
        if vis not in ("1", "true", "yes"):
            continue
        x = _to_int(el.attrib.get("x"))
        y = _to_int(el.attrib.get("y"))
        w = _to_int(el.attrib.get("width"))
        h = _to_int(el.attrib.get("height"))
        if w < 4 or h < 4:
            continue
        cx = x + w // 2
        cy = y + h // 2
        if not (x0 <= cx <= x1 and y0 <= cy <= y1):
            continue
        text = (
            el.attrib.get("name") or
            el.attrib.get("label") or
            el.attrib.get("value") or ""
        ).strip()
        if not text:
            continue
        if keywords and not _contains_any(text, keywords):
            continue
        out.append(Hit(text=text, x=cx, y=cy, w=w, h=h, src="xml"))
    return out


def visible_texts(
    driver: webdriver.Remote,
    lower_half: bool = False,
) -> list[str]:
    """Return deduplicated visible text strings from page source."""
    y_min = POPUP_Y_MIN if lower_half else 0.0
    hits = scrape_elements(driver, y_min_r=y_min)
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        t = h.text.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def ocr_texts(
    driver: webdriver.Remote,
    ocr: object,
    lower_half: bool = False,
    min_score: float = 0.45,
) -> list[str]:
    if ocr is None:
        return []
    try:
        img = screenshot_np(driver)
    except Exception as e:
        log(f"[WARN] screenshot failed in ocr_texts ({e.__class__.__name__}), skipping OCR")
        return []
    if lower_half:
        cut = int(img.shape[0] * POPUP_Y_MIN)
        img = img[cut:]
    result, _ = ocr(img)  # type: ignore[operator]
    if not result:
        return []
    return [
        str(text).strip()
        for _, text, score in result
        if text and float(score or 0) >= min_score
    ]


def merged_texts(
    driver: webdriver.Remote,
    ocr: object,
    lower_half: bool = False,
) -> list[str]:
    """Native XML texts + OCR texts, deduplicated."""
    native = visible_texts(driver, lower_half=lower_half)
    extra  = ocr_texts(driver, ocr, lower_half=lower_half)
    seen = set(native)
    result = list(native)
    for t in extra:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


# ---------------------------------------------------------------------------
# Entry icon detection
# (Anchored to the known top-left cluster from the screenshot)
# ---------------------------------------------------------------------------

def find_entry_icon(
    driver: webdriver.Remote,
    ocr: object,
    cache: "EntryCache",
) -> Optional[Hit]:
    """
    Locate the 福袋 entry icon in the top-left region.

    Strategy (fast-path first):
      1. Use cached hit if fresh and still in safe region.
      2. XML scrape of the constrained top-left box.
      3. Native Appium predicate query.
      4. OCR fallback (rate-limited).
    """
    w, h = screen_size(driver)

    def _in_region(x: int, y: int) -> bool:
        return (
            int(w * ENTRY_X_MIN) <= x <= int(w * ENTRY_X_MAX) and
            int(h * ENTRY_Y_MIN) <= y <= int(h * ENTRY_Y_MAX)
        )

    def _shape_ok(hit: Hit) -> bool:
        if hit.w <= 0 or hit.h <= 0:
            return True
        if hit.w < ENTRY_MIN_SIDE_PX or hit.h < ENTRY_MIN_SIDE_PX:
            return False
        if hit.w > w * ENTRY_MAX_W_RATIO or hit.h > h * ENTRY_MAX_H_RATIO:
            return False
        aspect = max(hit.w, hit.h) / max(1, min(hit.w, hit.h))
        return aspect <= ENTRY_MAX_ASPECT

    def _text_ok(text: str) -> bool:
        t = text.strip()
        return bool(t) and not _contains_any(t, KW_OPEN_BLOCKLIST)

    # 1. Cache
    cached = cache.get()
    if cached and _in_region(cached.x, cached.y):
        return cached

    # 2. XML scrape (fastest, most reliable)
    xml_hits = [
        h for h in scrape_elements(
            driver,
            types={"XCUIElementTypeButton", "XCUIElementTypeStaticText"},
            keywords=KW_OPEN_ENTRY,
            x_min_r=ENTRY_X_MIN, x_max_r=ENTRY_X_MAX,
            y_min_r=ENTRY_Y_MIN, y_max_r=ENTRY_Y_MAX,
        )
        if _text_ok(h.text) and _shape_ok(h)
    ]
    if xml_hits:
        best = min(xml_hits, key=lambda h: (abs(h.w - h.h), h.y, h.x))
        cache.set(best)
        return best

    # 3. Native predicate query
    try:
        kw_expr = " OR ".join(
            f"name CONTAINS '{k}' OR label CONTAINS '{k}'" for k in KW_OPEN_ENTRY
        )
        elements = driver.find_elements(
            AppiumBy.IOS_PREDICATE,
            f"(type == 'XCUIElementTypeButton' OR type == 'XCUIElementTypeStaticText') AND ({kw_expr})",
        )
        native_hits: list[Hit] = []
        for el in elements:
            try:
                r = el.rect
                if r["width"] < 8 or r["height"] < 8:
                    continue
                cx = int(r["x"] + r["width"] / 2)
                cy = int(r["y"] + r["height"] / 2)
                if not _in_region(cx, cy):
                    continue
                text = (el.get_attribute("name") or el.get_attribute("label") or "").strip()
                if not _text_ok(text):
                    continue
                hit = Hit(text=text, x=cx, y=cy, w=int(r["width"]), h=int(r["height"]), src="native")
                if _shape_ok(hit):
                    native_hits.append(hit)
            except Exception:
                continue
        if native_hits:
            best = min(native_hits, key=lambda h: (abs(h.w - h.h), h.y, h.x))
            cache.set(best)
            return best
    except Exception:
        pass

    # 4. OCR fallback (rate-limited)
    if not cache.ocr_ready():
        return None
    cache.mark_ocr_used()
    if ocr is None:
        return None
    try:
        img = screenshot_np(driver)
    except Exception as e:
        log(f"[WARN] screenshot failed in find_entry_icon OCR ({e.__class__.__name__}), skipping")
        return None
    result, _ = ocr(img)  # type: ignore[operator]
    if not result:
        return None
    ocr_hits: list[Hit] = []
    for box, text, score in result:
        t = str(text or "").strip()
        if not t or float(score or 0) < 0.45:
            continue
        if not _contains_any(t, KW_OPEN_ENTRY) or _contains_any(t, KW_OPEN_BLOCKLIST):
            continue
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
        cx = sum(xs) // len(xs)
        cy = sum(ys) // len(ys)
        if not _in_region(cx, cy):
            continue
        hit = Hit(text=t, x=cx, y=cy,
                  w=max(xs) - min(xs), h=max(ys) - min(ys), src="ocr")
        if _shape_ok(hit):
            ocr_hits.append(hit)
    if ocr_hits:
        best = min(ocr_hits, key=lambda h: (abs(h.w - h.h), h.y, h.x))
        cache.set(best)
        return best
    return None


# ---------------------------------------------------------------------------
# Entry icon cache
# ---------------------------------------------------------------------------

class EntryCache:
    def __init__(self, ttl: float = 18.0, ocr_cooldown: float = 2.5) -> None:
        self._hit:          Optional[Hit] = None
        self._ts:           float = 0.0
        self._ttl:          float = ttl
        self._ocr_cooldown: float = ocr_cooldown
        self._next_ocr:     float = 0.0

    def get(self) -> Optional[Hit]:
        if self._hit and time.time() - self._ts < self._ttl:
            return Hit(**vars(self._hit))
        return None

    def set(self, hit: Hit) -> None:
        self._hit = hit
        self._ts  = time.time()

    def invalidate(self) -> None:
        self._hit = None
        self._ts  = 0.0

    def ocr_ready(self) -> bool:
        return time.time() >= self._next_ocr

    def mark_ocr_used(self) -> None:
        self._next_ocr = time.time() + self._ocr_cooldown


# ---------------------------------------------------------------------------
# Popup analysis  (the half-screen 福袋 window)
# ---------------------------------------------------------------------------

class PopupKind(Enum):
    NONE        = auto()  # not visible
    FUDAI       = auto()  # legit physical lucky bag  ✅ proceed
    NONPHYSICAL = auto()  # cash / coins / 抖币 — skip
    EXPIRED     = auto()  # countdown == 0
    LOW_VALUE   = auto()  # ref price < threshold

    # Diamond bags:
    # NOTE: "加入粉丝团 (1钻石)" is a PARTICIPATION COST, not a prize type.
    # We only skip if the PRIZE itself is described as 钻石/抖币.
    # Detection: "钻石" appears in prize title/description, NOT in CTA button text.


@dataclass
class PopupInfo:
    kind:          PopupKind = PopupKind.NONE
    countdown_sec: Optional[int] = None   # seconds until draw
    ref_value:     Optional[float] = None  # prize reference value in ¥
    has_unfinished_tasks: bool = False
    has_success:   bool = False            # 已参与 / 参与成功


def _parse_countdown(texts: list[str]) -> Optional[int]:
    """
    Parse 'XX:YY 后开奖' or standalone 'XX分YY秒' patterns.
    Only trust mm:ss when '后开奖' anchor is present, to avoid
    misreading the live-stream clock.
    """
    joined = " ".join(texts)

    # Most reliable: "MM:SS 后开奖"
    m = re.search(r"(\d{1,2})\s*[:：]\s*([0-5]\d)\s*(?:后开奖|后开)", joined)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    # "X分Y秒 后开奖"
    m = re.search(r"(\d{1,2})\s*分(?:钟)?\s*(\d{1,2})\s*秒\s*(?:后开奖|后开)", joined)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    # "NNN秒 后开奖"
    m = re.search(r"(\d{1,3})\s*秒\s*(?:后开奖|后开)", joined)
    if m:
        return int(m.group(1))

    # Tokenised "00 : 33 后开奖" (OCR splits digits and colon)
    for i, tok in enumerate(texts):
        if "后开奖" not in tok and "后开" not in tok:
            continue
        window = texts[max(0, i - 5): i + 1]
        nums = [int(t) for t in window if re.fullmatch(r"\d{1,2}", t.strip())]
        if len(nums) >= 2:
            mm, ss = nums[-2], nums[-1]
            if 0 <= mm <= 59 and 0 <= ss <= 59:
                return mm * 60 + ss
    return None


def _parse_ref_value(texts: list[str]) -> Optional[float]:
    # Primary: '参考价值: ¥52' pattern seen in screenshot
    for t in texts:
        m = re.search(
            r"(?:参考)?(?:价值|价)[^0-9¥￥]{0,10}[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
            t,
        )
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except Exception:
                continue
    # Secondary: standalone ¥ amount — skip ¥0 (participation cost shown as free)
    for t in texts:
        m = re.search(r"[¥￥]\s*([1-9][0-9,]*(?:\.[0-9]+)?)", t)
        if m:
            try:
                v = float(m.group(1).replace(",", ""))
                if v >= 1.0:   # skip ¥0 participation price
                    return v
            except Exception:
                continue
    return None


def _is_prize_nonphysical(texts: list[str]) -> bool:
    """
    Detect non-physical prize bags (cash, coins, 抖币…).
    IMPORTANT: "加入粉丝团 (1钻石)" is a CTA button cost, not the prize type.
    We check prize description text (upper part of popup), not the CTA button.
    """
    joined = _joined(texts)

    # If there are physical hints, it's not non-physical
    if _contains_any(joined, KW_PHYSICAL_HINT):
        return False

    # Look for non-physical keywords OUTSIDE of fans-group CTA context
    # The fans-group CTA lines contain "粉丝团" near "钻石"
    for t in texts:
        if "粉丝团" in t:
            continue  # skip fans-group lines
        if _contains_any(t, KW_NONPHYSICAL):
            # Confirm it's in the prize area (not a cost line)
            if any(k in t for k in ["红包", "金币", "福气值", "现金红包", "抖币", "音浪"]):
                return True
    return False


def analyze_popup(texts: list[str]) -> PopupInfo:
    joined = _joined(texts)
    info = PopupInfo()

    # Is this even a 福袋 popup?
    if not _contains_any(joined, KW_POPUP_ANCHOR):
        # Secondary check: has unfinished task markers
        if not _contains_any(joined, KW_TASK_UNFINISHED):
            return info  # PopupKind.NONE

    # Popularity board (not a lucky bag)
    if _contains_any(joined, KW_POPULARITY) and not _contains_any(joined, ["后开奖", "参与任务", "参考价值"]):
        return info

    info.kind = PopupKind.FUDAI

    info.countdown_sec = _parse_countdown(texts)
    info.ref_value     = _parse_ref_value(texts)

    info.has_unfinished_tasks = _contains_any(joined, KW_TASK_UNFINISHED)

    # All tasks show '已达成' on the right and no '未达成' → treat as success
    all_tasks_confirmed = (
        _contains_any(joined, KW_TASK_DONE) and
        not _contains_any(joined, KW_TASK_UNFINISHED)
    )
    info.has_success = all_tasks_confirmed or any(
        k in t and "人已参与" not in t
        for k in KW_SUCCESS
        for t in texts
    )

    # Countdown == 0 or ≤2s → expired
    # Guard: only mark EXPIRED when we actually parsed a value (not None)
    if info.countdown_sec is not None and info.countdown_sec <= 2:
        info.kind = PopupKind.EXPIRED
        return info

    # Non-physical prize?
    if _is_prize_nonphysical(texts):
        info.kind = PopupKind.NONPHYSICAL
        return info

    # Value filter
    if info.ref_value is not None:
        if info.ref_value < 10.0:
            info.kind = PopupKind.LOW_VALUE
            return info
        if (info.countdown_sec is not None and
                info.countdown_sec > 240 and
                info.ref_value < 500.0):
            info.kind = PopupKind.LOW_VALUE
            return info

    return info  # PopupKind.FUDAI  ✅


# ---------------------------------------------------------------------------
# Task executor  (handles the 参与任务 section of the half-screen popup)
# ---------------------------------------------------------------------------

class TaskResult(Enum):
    SUCCESS     = auto()  # 已参与 confirmed
    TASKS_DONE  = auto()  # all tasks tapped, no unfinished markers left
    STILL_OPEN  = auto()  # tasks remain, keep retrying
    EXPIRED     = auto()  # countdown hit zero mid-task
    POPUP_LOST  = auto()  # task popup disappeared, reopen in same room


def pick_hits(
    driver: webdriver.Remote,
    ocr: object,
    keywords: list[str],
    y_min_r: float = 0.45,
    max_text_len: int = 20,
) -> list[Hit]:
    """Find tappable elements matching keywords in the popup (lower screen)."""
    _, h = screen_size(driver)
    hits = scrape_elements(
        driver,
        types={"XCUIElementTypeButton", "XCUIElementTypeStaticText"},
        keywords=keywords,
        y_min_r=y_min_r,
    )
    # Filter: correct text, not blocklisted
    filtered = [
        h_ for h_ in hits
        if (
            len(h_.text) <= max_text_len and
            _contains_any(h_.text, keywords) and
            not _contains_any(h_.text, ["参与条件", "任务说明", "参与任务",
                                         "人已参与", "已参与人数"])
        )
    ]
    # Deduplicate by proximity
    out: list[Hit] = []
    for h_ in filtered:
        if not any(abs(h_.x - e.x) < 15 and abs(h_.y - e.y) < 15 for e in out):
            out.append(h_)

    # OCR fallback
    if not out and ocr is not None:
        try:
            img = screenshot_np(driver)
        except Exception as e:
            log(f"[WARN] screenshot failed in pick_hits ({e.__class__.__name__}), skipping OCR")
            return out
        cut = int(img.shape[0] * y_min_r)
        crop = img[cut:]
        result, _ = ocr(crop)  # type: ignore[operator]
        if result:
            for box, text, score in result:
                t = str(text or "").strip()
                if not t or float(score or 0) < 0.45:
                    continue
                if not _contains_any(t, keywords):
                    continue
                ys_box = [int(p[1]) for p in box]
                xs_box = [int(p[0]) for p in box]
                cy = sum(ys_box) // len(ys_box) + cut
                cx = sum(xs_box) // len(xs_box)
                hit = Hit(text=t, x=cx, y=cy, src="ocr")
                if not any(abs(hit.x - e.x) < 15 and abs(hit.y - e.y) < 15 for e in out):
                    out.append(hit)
    return out


def execute_tasks(
    driver: webdriver.Remote,
    ocr: object,
    max_rounds: int = 5,
) -> TaskResult:
    """
    Work through the task list in the popup.

    Order:
      1. Comment task ("一键发表评论")
      2. Fans-group join + confirm
      3. Generic task buttons
    After each tap-round, re-read the popup to check for success or countdown=0.
    """
    for round_idx in range(max_rounds):
        texts = merged_texts(driver, ocr, lower_half=True)
        popup = analyze_popup(texts)

        if popup.kind == PopupKind.NONE:
            # Handle transient animation/frame switches: recheck once before declaring popup lost.
            time.sleep(0.35)
            retry_texts = merged_texts(driver, ocr, lower_half=True)
            retry_popup = analyze_popup(retry_texts)
            if retry_popup.kind == PopupKind.NONE:
                log("Task: popup missing during task phase.")
                return TaskResult.POPUP_LOST
            texts = retry_texts
            popup = retry_popup

        if popup.has_success:
            log("Task: 已参与 confirmed.")
            return TaskResult.SUCCESS

        if popup.kind == PopupKind.EXPIRED:
            log("Task: countdown hit 0 during tasks.")
            return TaskResult.EXPIRED

        if popup.kind == PopupKind.FUDAI and (not popup.has_unfinished_tasks) and round_idx > 0:
            log("Task: no more unfinished tasks.")
            return TaskResult.TASKS_DONE

        tapped_this_round: list[tuple[int, int]] = []

        sw, sh = screen_size(driver)

        def _tap_hit(h: Hit, label: str, allow_duplicate: bool = False) -> None:
            if (not allow_duplicate) and any(abs(h.x - px) < 12 and abs(h.y - py) < 12 for px, py in tapped_this_round):
                return
            log(f"  Tap {label} → '{h.text}' @ ({h.x},{h.y})")
            tap(driver, h.x, h.y)
            tapped_this_round.append((h.x, h.y))
            time.sleep(0.35)

        def _tap_task_target(
            h: Hit,
            label: str,
            allow_duplicate: bool = False,
        ) -> None:
            # Click only the matched text/button target to avoid right-side product-entry mis-taps.
            _tap_hit(h, label, allow_duplicate=allow_duplicate)

        def _tap_overlay_dismiss_point(label: str, rounds: int = 1) -> None:
            sw_, sh_ = screen_size(driver)
            dismiss_x = int(sw_ * 0.5)
            # Fans-step2 panel has no explicit close button; use a higher point above panel top.
            dismiss_y = max(1, min(sh_ - 1, int(sh_ * POPUP_Y_MIN) - 16))
            for _ in range(max(1, rounds)):
                log(f"  Tap {label} point @ ({dismiss_x},{dismiss_y})")
                tap(driver, dismiss_x, dismiss_y)
                time.sleep(0.35)

        def _close_fans_overlay_and_reopen_entry() -> bool:
            log("  Fans flow done — closing fans overlay and returning via 福袋入口.")
            # Fans panel doesn't always contain 福袋 anchors, so force the same
            # overlay-dismiss tap point before generic cleanup.
            _tap_overlay_dismiss_point("fans-overlay-dismiss", rounds=2)
            try:
                after_force = _joined(merged_texts(driver, ocr, lower_half=True))
            except Exception:
                after_force = ""
            if _contains_any(after_force, ["我的等级特权", "升级任务", "查看全部等级特权", "亲密度"]):
                _tap_overlay_dismiss_point("fans-overlay-dismiss-retry", rounds=1)
            dismiss_overlays(driver, ocr, rounds=3)
            time.sleep(0.45)
            local_cache = EntryCache(ttl=2.0, ocr_cooldown=0.8)
            deadline = time.time() + 8.0
            while time.time() < deadline:
                entry = find_entry_icon(driver, ocr, local_cache)
                if entry is None:
                    local_cache.invalidate()
                    time.sleep(0.35)
                    continue
                log(f"  Re-open 福袋 ({entry.src}) → '{entry.text}' @ ({entry.x},{entry.y})")
                tap(driver, entry.x, entry.y)
                time.sleep(0.75)
                post_texts = merged_texts(driver, ocr, lower_half=True)
                post_popup = analyze_popup(post_texts)
                if post_popup.kind != PopupKind.NONE:
                    return True
                local_cache.invalidate()
                time.sleep(0.35)
            log("  [WARN] Failed to restore 福袋 popup after fans flow.")
            return False

        def _fans_task_marked_done() -> bool:
            # Best-effort textual check: fans task row should flip to done wording.
            try:
                now_texts = merged_texts(driver, ocr, lower_half=True)
            except Exception:
                return False
            normalized = re.sub(r"[\s|]+", "", _joined(now_texts))
            if any(k in normalized for k in KW_FANS_DONE):
                return True
            if re.search(r"(加入购物粉丝团|加入粉丝团|加入粉丝).{0,8}(已达成|已完成|已加入)", normalized):
                return True
            if re.search(r"(已达成|已完成|已加入).{0,8}(加入购物粉丝团|加入粉丝团|加入粉丝)", normalized):
                return True
            return False

        def _wait_fans_done(timeout_s: float = 2.8) -> bool:
            deadline = time.time() + max(0.2, timeout_s)
            while time.time() < deadline:
                if _fans_task_marked_done():
                    return True
                time.sleep(0.35)
            return False

        def _filter_step2_hits(raw_hits: list[Hit], step1_hit: Hit) -> list[Hit]:
            hits = [
                x for x in raw_hits
                if not _contains_any(x.text, ["粉丝团规则", "粉丝团权益", "购物粉丝团权益", "待解锁", "酷炫勋章"])
            ]
            # NOTE:
            # On some rooms/devices, step2 button keeps nearly the same text/style/position
            # as step1. Do not filter by proximity to step1 coordinates.
            preferred_step2 = [x for x in hits if "购物粉丝团" not in x.text]
            if preferred_step2:
                hits = preferred_step2
            preferred_cta = [x for x in hits if x.x >= int(sw * 0.45)]
            if preferred_cta:
                hits = preferred_cta
            return sorted(hits, key=lambda z: (z.y, z.x), reverse=True)

        def _wait_for_stable_step2_hits(step1_hit: Hit, timeout_s: float = 6.5) -> list[Hit]:
            deadline = time.time() + max(0.4, timeout_s)
            stable_rounds = 0
            last_sig = ""
            while time.time() < deadline:
                raw_hits = pick_hits(driver, ocr, KW_FANS_JOIN, y_min_r=0.35, max_text_len=24)
                hits = _filter_step2_hits(raw_hits, step1_hit)
                if hits:
                    sig = "|".join(f"{x.text}@{x.x},{x.y}" for x in hits[:3])
                    if sig == last_sig:
                        stable_rounds += 1
                    else:
                        last_sig = sig
                        stable_rounds = 1
                    if stable_rounds >= 2:
                        time.sleep(0.25)
                        return hits
                else:
                    stable_rounds = 0
                    last_sig = ""
                time.sleep(0.3)
            return []

        def _filter_confirm_hits(raw_hits: list[Hit]) -> list[Hit]:
            hits = [
                x for x in raw_hits
                if not _contains_any(x.text, ["粉丝团规则", "粉丝团权益", "购物粉丝团权益", "待解锁", "酷炫勋章", "规则", "权益"])
            ]
            return sorted(hits, key=lambda z: (z.y, z.x), reverse=True)

        def _wait_for_stable_confirm_hits(timeout_s: float = 4.8) -> list[Hit]:
            deadline = time.time() + max(0.4, timeout_s)
            stable_rounds = 0
            last_sig = ""
            while time.time() < deadline:
                raw_hits = pick_hits(driver, ocr, KW_FANS_CONFIRM, y_min_r=0.20, max_text_len=40)
                hits = _filter_confirm_hits(raw_hits)
                if hits:
                    sig = "|".join(f"{x.text}@{x.x},{x.y}" for x in hits[:3])
                    if sig == last_sig:
                        stable_rounds += 1
                    else:
                        last_sig = sig
                        stable_rounds = 1
                    if stable_rounds >= 2:
                        time.sleep(0.2)
                        return hits
                else:
                    stable_rounds = 0
                    last_sig = ""
                time.sleep(0.3)
            return []

        # 1. Comment task — search wider y range and log if missing
        comment_hits = pick_hits(driver, ocr, KW_COMMENT_TASK, y_min_r=0.35, max_text_len=12)
        if comment_hits:
            for h in comment_hits:
                _tap_hit(h, "comment-task-label")
            time.sleep(0.6)  # wait for comment to register
        else:
            log("  [WARN] Comment button not found — dumping popup texts for debug")
            for t in texts:
                log(f"    popup text: {t!r}")

        # 2. Fans-group join (explicit 3-step flow)
        # Step 1: click fans button on 福袋 popup task row
        # Step 2: wait for secondary fans panel and click its fans button again
        # Step 3: optional confirm popup -> click confirm
        fans_hits = pick_hits(driver, ocr, KW_FANS_JOIN, y_min_r=0.55, max_text_len=24)
        fans_hits = [
            x for x in fans_hits
            if not _contains_any(x.text, ["粉丝团规则", "粉丝团权益", "购物粉丝团权益", "待解锁", "酷炫勋章"])
        ]
        if fans_hits:
            # Execute one deterministic fans flow per round to avoid duplicate step loops.
            # Prefer lower/right candidate to hit the actual CTA instead of the left label.
            h = sorted(fans_hits, key=lambda z: (z.y, z.x), reverse=True)[0]
            # Step 1
            _tap_task_target(h, "fans-step1")
            time.sleep(0.6)  # allow secondary panel animation to settle
            fans_done = _wait_fans_done(1.4)

            # Step 2
            step2_done = False
            if not fans_done:
                step2_hits = _wait_for_stable_step2_hits(h, timeout_s=6.5)
                if step2_hits:
                    preview = ", ".join(f"{x.text}@({x.x},{x.y})/{x.src}" for x in step2_hits[:3])
                    log(f"  fans-step2 candidates: {preview}")
                    c = step2_hits[0]
                    # Step2: tap the matched CTA first; only add right-side tap when needed.
                    _tap_hit(c, "fans-step2", allow_duplicate=True)
                    step2_done = True
                    time.sleep(0.5)  # wait for optional confirm popup
                    fans_done = _wait_fans_done(1.2)
                else:
                    # Fallback for rooms where step2 looks almost identical to step1.
                    log("  [WARN] fans-step2 not detected; retrying same-spot tap.")
                    _tap_hit(h, "fans-step2-same-spot", allow_duplicate=True)
                    step2_done = True
                    time.sleep(0.5)
                    fans_done = _wait_fans_done(1.2)

            # Step 3 (optional)
            confirm_done = False
            if not fans_done:
                confirm_hits = _wait_for_stable_confirm_hits(timeout_s=4.8)
                if confirm_hits:
                    c = confirm_hits[0]
                    _tap_task_target(
                        c,
                        "fans-step3-confirm",
                        allow_duplicate=True,
                    )
                    confirm_done = True
                    time.sleep(0.5)
                    fans_done = _wait_fans_done(2.2)
                else:
                    log("  fans-step3-confirm not shown (optional), continue.")

            # Verify fans task completion before exiting this branch.
            if not fans_done:
                fallback_hits = pick_hits(driver, ocr, KW_FANS_CONFIRM + KW_FANS_JOIN, y_min_r=0.30, max_text_len=40)
                fallback_hits = [
                    x for x in fallback_hits
                    if not _contains_any(x.text, ["粉丝团规则", "粉丝团权益", "购物粉丝团权益", "待解锁", "酷炫勋章", "规则", "权益"])
                ]
                if fallback_hits:
                    for c in sorted(fallback_hits, key=lambda z: (z.y, z.x), reverse=True)[:2]:
                        _tap_task_target(
                            c,
                            "fans-fallback",
                            allow_duplicate=True,
                        )
                        if _wait_fans_done(1.6):
                            fans_done = True
                            break
            if fans_done:
                log("  Fans task marked done.")
            else:
                log("  [WARN] Fans task still not done after step2/confirm attempts.")

            # Give the post-step2/confirm panel a short settle window before close taps.
            settle_s = 1.0 if (step2_done or confirm_done) else 0.5
            log(f"  Wait {settle_s:.1f}s before closing fans overlay.")
            time.sleep(settle_s)

            _close_fans_overlay_and_reopen_entry()

        # 3. Generic task buttons
        for h in pick_hits(driver, ocr, KW_TASK_GENERIC):
            _tap_hit(h, "generic-task")

        if not tapped_this_round:
            log(f"Task: no tappable buttons found (round {round_idx+1}/{max_rounds}).")
            break

        time.sleep(0.5)

    # Final check
    texts = merged_texts(driver, ocr, lower_half=True)
    popup = analyze_popup(texts)
    if popup.has_success:
        return TaskResult.SUCCESS
    if popup.kind == PopupKind.NONE:
        return TaskResult.POPUP_LOST
    if popup.kind == PopupKind.FUDAI and not popup.has_unfinished_tasks:
        return TaskResult.TASKS_DONE
    return TaskResult.STILL_OPEN


# ---------------------------------------------------------------------------
# Draw result detection
# ---------------------------------------------------------------------------

RE_WIN  = [re.compile(r"恭喜.*抽中"), re.compile(r"恭喜.*中奖"), re.compile(r"抽中.*福袋")]
RE_LOSE = [re.compile(r"未中奖"), re.compile(r"未中签"), re.compile(r"很遗憾"),
           re.compile(r"下次再来"), re.compile(r"擦肩而过")]


def detect_result(texts: list[str]) -> Optional[str]:
    """Return 'win', 'lose', or None.  Does not settle during active countdown."""
    joined = _joined(texts)
    countdown = _parse_countdown(texts)
    if countdown is not None and countdown > 1:
        return None   # still running, do not settle

    if _contains_any(joined, KW_WIN) or any(p.search(joined) for p in RE_WIN):
        return "win"
    if _contains_any(joined, KW_LOSE) or any(p.search(joined) for p in RE_LOSE):
        return "lose"
    return None


def detect_win_claim_popup(driver: webdriver.Remote) -> bool:
    """
    Detect the dedicated prize-claim overlay.
    This overlay can appear in the upper/middle screen and may be missed by
    lower-half-only text scans.
    """
    try:
        texts = visible_texts(driver, lower_half=False)
    except Exception:
        return False
    joined = _joined(texts)
    if all(k in joined for k in KW_WIN_CLAIM_CORE):
        return True
    return ("恭喜抽中福袋" in joined) and _contains_any(joined, KW_WIN_CLAIM_AUX)


def wait_for_result(
    driver: webdriver.Remote,
    ocr: object,
    max_wait: int = 240,
    poll: float = 1.5,
    grace: float = 2.0,
    reopen_interval: float = 8.0,
    entry_cache: Optional["EntryCache"] = None,
) -> str:
    """
    Sit in the draw-wait phase until we get win/lose or timeout.
    Periodically retaps the entry icon if the popup collapses.
    Also retries any remaining tasks (e.g. comment auto-send).
    """
    log(f"Waiting for draw result (max {max_wait}s)…")
    deadline      = time.time() + max_wait
    dyn_deadline  = deadline
    last_reopen   = 0.0
    last_task_try = 0.0
    last_left: Optional[int] = None
    zero_since:   Optional[float] = None   # when we first saw left==0

    while time.time() < dyn_deadline:
        try:
            texts = merged_texts(driver, ocr, lower_half=True)
        except Exception as e:
            log(f"  [WARN] WAIT_DRAW read error: {e} — retrying in 2s")
            time.sleep(2.0)
            continue

        # Prize-claim overlay can be outside lower-half region; check full screen.
        if detect_win_claim_popup(driver):
            log("Result: WIN (claim popup detected) ✓")
            return "win"

        result = detect_result(texts)
        if result == "win":
            # Double-confirm with native texts
            if detect_result(visible_texts(driver, lower_half=True)) == "win":
                log("Result: WIN ✓")
                return "win"
            log("Win signal not confirmed by native texts, keep waiting.")
        elif result == "lose":
            log("Result: LOSE")
            return "lose"

        left = _parse_countdown(texts)
        if left is not None:
            # Extend dynamic deadline to cover the countdown + grace
            target = time.time() + left + grace
            if target > dyn_deadline:
                dyn_deadline = target
            if last_left is None or abs(left - last_left) >= 3:
                log(f"  Countdown: {left}s")
                last_left = left

            # Try any remaining tasks periodically
            if time.time() - last_task_try > 10.0:
                joined = _joined(texts)
                if _contains_any(joined, KW_TASK_UNFINISHED) or \
                   _contains_any(joined, ["一键发表评论", "加入粉丝团"]):
                    log("  Re-trying unfinished tasks during draw wait…")
                    execute_tasks(driver, ocr, max_rounds=2)
                last_task_try = time.time()

            # Immediately probe when countdown reaches 0
            if left <= 1:
                if zero_since is None:
                    zero_since = time.time()
                # Probe for win/lose for up to grace+6s
                probe_end = zero_since + max(8.0, grace + 6.0)
                while time.time() < probe_end:
                    try:
                        probe_texts = visible_texts(driver, lower_half=True)
                        r = detect_result(probe_texts)
                        if r:
                            log(f"Result after countdown: {r}")
                            return r
                    except Exception:
                        pass
                    time.sleep(poll)
                # Timed out waiting for result — popup is stale, close and re-scan
                log("  No result detected at 00:00 — dismissing popup, re-scanning room.")
                return "expired_no_result"
            else:
                zero_since = None  # reset if countdown is running again
        else:
            # No countdown visible at all — popup may have collapsed or 00:00 is
            # a stale frozen display that _parse_countdown can't match (no 后开奖 anchor).
            now = time.time()

            # Direct text scan for frozen 00:00 popup
            raw_texts = visible_texts(driver, lower_half=True)
            raw_joined = " ".join(raw_texts)
            popup_visible = "超级福袋" in raw_joined or "后开奖" in raw_joined or "等待开奖" in raw_joined
            frozen_zero   = "00" in raw_joined and ("后开奖" in raw_joined or "超级福袋" in raw_joined)

            if popup_visible and frozen_zero:
                if zero_since is None:
                    zero_since = now
                    log("  Detected 00:00 frozen popup — waiting briefly for result…")
                elif now - zero_since > grace + 6.0:
                    log("  Popup frozen at 00:00 with no result — closing and re-scanning.")
                    return "expired_no_result"
            else:
                zero_since = None

            # Heartbeat every 20s
            if not hasattr(wait_for_result, '_hb') or now - getattr(wait_for_result, '_hb', 0) >= 20:
                remaining = max(0, int(dyn_deadline - now))
                log(f"  Polling for result… ({remaining}s left)")
                wait_for_result._hb = now  # type: ignore[attr-defined]

            # Popup may have collapsed — try to reopen
            if now - last_reopen > reopen_interval and entry_cache:
                open_hit = entry_cache.get()
                if open_hit and not popup_visible:
                    log(f"  Reopen popup → ({open_hit.x},{open_hit.y})")
                    tap(driver, open_hit.x, open_hit.y)
                    last_reopen = now
                    time.sleep(0.7)

        time.sleep(poll)

    # Deadline hit
    if detect_win_claim_popup(driver):
        log("Result at deadline: win (claim popup)")
        return "win"
    texts = visible_texts(driver, lower_half=True)
    r = detect_result(texts)
    if r:
        log(f"Result at deadline: {r}")
        return r
    log("Draw result unknown at deadline.")
    return "timeout"


# ---------------------------------------------------------------------------
# Room navigation
# ---------------------------------------------------------------------------

_SWIPE_PROFILES = (
    # Short, decisive flicks to reduce long-press misclassification.
    (0.22, 0.58, 0.78),
    (0.28, 0.62, 0.84),
    (0.34, 0.66, 0.90),
    (0.40, 0.66, 0.90),
)
_IGNORE_IN_FP = ("福袋", "关闭", "关注", "分享", "更多", "评论", "说点什么",
                  "人气榜", "带货榜", "榜单", "直播中", "小时榜")

# Home-screen tab bar tokens — if we see these we're NOT in a live room
_HOME_TAB_TOKENS = ("综合", "用户", "视频", "朋友", "消息", "我")

def is_in_live_room(driver: webdriver.Remote) -> bool:
    """Return False if the bot has drifted to the Douyin home/search screen."""
    try:
        hits = scrape_elements(
            driver,
            types={"XCUIElementTypeButton", "XCUIElementTypeStaticText"},
            x_min_r=ENTRY_X_MIN, x_max_r=ENTRY_X_MAX,
            y_min_r=0.08, y_max_r=0.20,
        )
        texts = " ".join(h.text for h in hits)
        if _contains_any(texts, list(_HOME_TAB_TOKENS)):
            return False
    except Exception:
        pass
    return True

def relaunch_into_live(driver: webdriver.Remote) -> None:
    """If not in a live room, press Home and reopen Douyin to resume a live feed."""
    log("[WARN] Not in a live room — relaunching Douyin.")
    try:
        driver.execute_script("mobile: pressButton", {"name": "home"})
        time.sleep(1.5)
        driver.execute_script("mobile: launchApp", {"bundleId": "com.ss.iphone.ugc.Aweme"})
        time.sleep(4.0)
        log("  Douyin relaunched. Please navigate into a live room.")
    except Exception as e:
        log(f"  Relaunch failed: {e}")


def room_fingerprint(driver: webdriver.Remote) -> frozenset[str]:
    _, h = screen_size(driver)
    hits = scrape_elements(
        driver,
        types={"XCUIElementTypeStaticText", "XCUIElementTypeButton"},
        y_min_r=0.04, y_max_r=0.44,
    )
    tokens: set[str] = set()
    for hit in hits:
        t = re.sub(r"\s+", "", hit.text.strip())
        if not t or len(t) < 2 or len(t) > 28:
            continue
        if _contains_any(t, list(_IGNORE_IN_FP)):
            continue
        if re.fullmatch(r"[0-9:：]+", t):
            continue
        tokens.add(t)
        if len(tokens) >= 12:
            break
    return frozenset(tokens)


def room_changed(before: frozenset[str], after: frozenset[str]) -> bool:
    if not before or not after:
        return False
    common = len(before & after)
    baseline = max(1, min(len(before), len(after)))
    return common / baseline < 0.45


def dismiss_overlays(driver: webdriver.Remote, ocr: object, rounds: int = 3) -> int:
    closed = 0
    sw, sh = screen_size(driver)
    dismiss_x = int(sw * 0.5)
    # Use popup top edge (POPUP_Y_MIN) and move 5pt upward to avoid tapping popup content.
    dismiss_y = max(1, min(sh - 1, int(sh * POPUP_Y_MIN) - 5))
    for _ in range(rounds):
        # Check if the 福袋 popup is open; if so use fixed safe tap point above popup.
        popup_texts = merged_texts(driver, ocr, lower_half=True)
        if _contains_any(_joined(popup_texts), KW_POPUP_ANCHOR + KW_POPULARITY):
            log(f"  Tap overlay-dismiss point @ ({dismiss_x},{dismiss_y})")
            tap(driver, dismiss_x, dismiss_y)
            closed += 1
            time.sleep(0.35)
            # Verify popup gone
            post = visible_texts(driver, lower_half=True)
            if not _contains_any(_joined(post), KW_POPUP_ANCHOR):
                break  # dismissed successfully
            # Still open — try a swipe-down gesture on the popup
            log("  Popup still open, trying swipe-down to close.")
            driver.execute_script("mobile: dragFromToWithVelocity", {
                "fromX": sw * 0.5, "fromY": sh * 0.65,
                "toX":   sw * 0.5, "toY":   sh * 0.90,
                "velocity": 800,
            })
            time.sleep(0.4)
            continue
        break
    return closed


def switch_room(
    driver: webdriver.Remote,
    ocr: object,
    entry_cache: EntryCache,
    post_wait: float = 5.0,
) -> bool:
    """Swipe up to go to the next live room. Returns True if verified."""
    before = room_fingerprint(driver)
    entry_cache.invalidate()
    dismiss_overlays(driver, ocr)

    w, h = screen_size(driver)
    # Tap neutral area first to release any overlay focus
    tap(driver, int(w * 0.5), int(h * 0.38))
    time.sleep(0.15)

    for idx, (duration, start_r, dist_r) in enumerate(_SWIPE_PROFILES, 1):
        x     = int(w * 0.50)
        start = int(h * start_r)
        dist  = int(h * dist_r)
        end   = max(int(h * 0.01), start - dist)
        min_travel = int(h * 0.24)
        if start - end < min_travel:
            end = max(int(h * 0.01), start - min_travel)

        driver.execute_script(
            "mobile: dragFromToForDuration",
            {"duration": duration, "fromX": x, "fromY": start, "toX": x, "toY": end},
        )
        log(f"  Swipe attempt {idx}: ({x},{start})→({x},{end}), {duration:.2f}s")
        wait = max(5.0, post_wait) + random.uniform(0.0, 2.0)
        time.sleep(wait)

        after = room_fingerprint(driver)
        if room_changed(before, after):
            log(f"  Room switched (attempt {idx}). ✓")
            return True
        log(f"  Room switch not verified (attempt {idx}), retrying…")
        dismiss_overlays(driver, ocr, rounds=2)
        tap(driver, int(w * 0.5), int(h * 0.38))
        time.sleep(0.12)

    log("  Room switch failed after all attempts.")
    return False


# ---------------------------------------------------------------------------
# Bot state
# ---------------------------------------------------------------------------

class Phase(Enum):
    SCAN        = auto()  # look for entry icon
    OPEN        = auto()  # tap entry icon to open popup
    INSPECT     = auto()  # read popup, decide what to do
    TASK        = auto()  # execute task list
    WAIT_DRAW   = auto()  # wait for draw result
    SWITCH      = auto()  # move to next room


@dataclass
class BotState:
    phase:               Phase = Phase.SCAN
    entry_cache:         EntryCache = field(default_factory=EntryCache)

    # current bag being tracked
    current_bag_ref:     Optional[float] = None   # ¥ ref value of current bag
    current_bag_round:   int = 0                  # round counter across all bags

    # counters
    open_retries:        int = 0
    no_open_rounds:      int = 0
    unfinished_rounds:   int = 0
    collapse_rounds:     int = 0

    # timestamps
    room_enter_ts:       float = field(default_factory=time.time)
    last_swipe_ts:       float = 0.0
    last_progress_ts:    float = field(default_factory=time.time)

    # last open tap dedup
    last_tap_key:        Optional[str] = None
    last_tap_ts:         float = 0.0

    def reset_for_new_room(self) -> None:
        self.entry_cache.invalidate()
        self.open_retries      = 0
        self.no_open_rounds    = 0
        self.unfinished_rounds = 0
        self.collapse_rounds   = 0
        self.room_enter_ts     = time.time()
        self.last_progress_ts  = time.time()
        self.last_tap_key      = None
        self.last_tap_ts       = 0.0

    def mark_progress(self) -> None:
        self.last_progress_ts = time.time()

    def stalled(self, threshold: float) -> bool:
        now = time.time()
        return (
            now - self.room_enter_ts    >= threshold and
            now - self.last_progress_ts >= threshold and
            now - self.last_swipe_ts    >= 4.0
        )


# ---------------------------------------------------------------------------
# Main bot loop
# ---------------------------------------------------------------------------

def run_bot(driver: webdriver.Remote, ocr: object, args: argparse.Namespace) -> int:
    """Main event loop. Returns 0 on win, 1 otherwise."""
    state = BotState()
    log("Bot started. Stay on a live room page.")

    while True:

        # ── Global interrupt: red-packet signal ──────────────────────────
        rp_texts = merged_texts(driver, ocr, lower_half=False)
        if _contains_any(_joined(rp_texts), KW_RED_PACKET):
            log("Red-packet signal detected — exiting.")
            dismiss_overlays(driver, ocr, rounds=6)
            return 0

        # ── PHASE: SWITCH ────────────────────────────────────────────────
        if state.phase == Phase.SWITCH:
            switched = switch_room(driver, ocr, state.entry_cache, post_wait=args.post_swipe_wait)
            if switched:
                state.last_swipe_ts = time.time()
                state.reset_for_new_room()
            state.phase = Phase.SCAN
            continue

        # ── PHASE: SCAN ──────────────────────────────────────────────────
        if state.phase == Phase.SCAN:
            # Prize-claim overlay can appear outside lower-half popup region.
            if detect_win_claim_popup(driver):
                state.current_bag_round += 1
                prize_str = f"¥{state.current_bag_ref:.0f}" if state.current_bag_ref else "未知奖品"
                msg = (f"🎉 福袋开奖结果\n"
                       f"第 {state.current_bag_round} 轮\n"
                       f"结果：中奖！ 🏆\n"
                       f"奖品参考价：{prize_str}\n"
                       f"时间：{time.strftime('%H:%M:%S')}")
                log("🎉 WIN claim popup detected in SCAN! Stopping bot.")
                notify_imessage(args.notify_phone, msg)
                return 0

            # Check for immediate success text (e.g. from previous join)
            texts = merged_texts(driver, ocr, lower_half=True)
            popup = analyze_popup(texts)
            if popup.has_success and popup.kind == PopupKind.FUDAI:
                # Guard: don't enter WAIT_DRAW if countdown is already at 0 or
                # undetectable (frozen 00:00 display) — draw is over, close popup.
                joined_lower = _joined(texts)
                frozen_zero = (
                    (popup.countdown_sec is not None and popup.countdown_sec <= 2) or
                    (popup.countdown_sec is None and
                     ("超级福袋" in joined_lower or "等待开奖" in joined_lower) and
                     "00" in joined_lower)
                )
                if frozen_zero:
                    log("Success popup at 00:00 — closing stale popup and re-scanning.")
                    dismiss_overlays(driver, ocr, rounds=4)
                    state.reset_for_new_room()
                    state.phase = Phase.SCAN
                    continue
                log("Success text detected in SCAN — going straight to WAIT_DRAW.")
                state.phase = Phase.WAIT_DRAW
                continue

            # Check for lose popup
            if _contains_any(_joined(texts), KW_LOSE_POPUP):
                log("Lose popup detected — switching room.")
                state.phase = Phase.SWITCH
                continue

            # Check for blocked text
            if _contains_any(_joined(merged_texts(driver, ocr)), KW_BLOCKED):
                log("Blocked text detected — switching room.")
                state.phase = Phase.SWITCH
                continue

            # If popup is open (no icon found but popup visible), dismiss first
            popup_peek = analyze_popup(texts)
            if popup_peek.kind != PopupKind.NONE:
                log("Popup still open during SCAN — dismissing before looking for icon.")
                dismiss_overlays(driver, ocr, rounds=3)
                time.sleep(0.4)
                state.entry_cache.invalidate()
                state.mark_progress()
                continue

            # Look for direct JOIN button
            join_hits = scrape_elements(driver, keywords=KW_JOIN, y_min_r=0.4)
            if join_hits:
                best = min(join_hits, key=lambda h: (len(h.text), h.y))
                log(f"Tap JOIN → '{best.text}' @ ({best.x},{best.y})")
                tap(driver, best.x, best.y)
                state.mark_progress()
                time.sleep(0.9)
                state.phase = Phase.SCAN  # re-scan for success
                continue

            # Guard: if we've drifted to the Douyin home screen, relaunch
            if not is_in_live_room(driver):
                relaunch_into_live(driver)
                state.reset_for_new_room()
                time.sleep(5.0)
                continue

            # Look for entry icon
            entry = find_entry_icon(driver, ocr, state.entry_cache)
            if entry is None:
                state.no_open_rounds += 1
                # Debug: dump all elements visible in the entry region
                region_els = scrape_elements(
                    driver,
                    types={"XCUIElementTypeButton", "XCUIElementTypeStaticText", "XCUIElementTypeImage"},
                    x_min_r=ENTRY_X_MIN, x_max_r=ENTRY_X_MAX,
                    y_min_r=ENTRY_Y_MIN, y_max_r=ENTRY_Y_MAX,
                )
                log(f"No 福袋 entry icon found (#{state.no_open_rounds}). Region elements: {[(e.text, e.x, e.y) for e in region_els]}")
                if state.stalled(args.room_stall_seconds) and state.no_open_rounds >= 2:
                    log(
                        f"Room stalled for {args.room_stall_seconds}s with no entry icon "
                        f"for {state.no_open_rounds} scans — switching room."
                    )
                    state.phase = Phase.SWITCH
                elif state.no_open_rounds >= 4:
                    log("Entry icon absent — switching room.")
                    state.phase = Phase.SWITCH
                else:
                    time.sleep(random.uniform(args.interval_min, args.interval_max))
                continue

            state.no_open_rounds = 0
            state.mark_progress()
            state.phase = Phase.OPEN
            continue

        # ── PHASE: OPEN ──────────────────────────────────────────────────
        if state.phase == Phase.OPEN:
            entry = find_entry_icon(driver, ocr, state.entry_cache)
            if entry is None:
                log("Entry icon lost before tap — back to SCAN.")
                state.phase = Phase.SCAN
                continue

            tap_key = f"{entry.x}:{entry.y}"
            if tap_key == state.last_tap_key and time.time() - state.last_tap_ts < 3:
                time.sleep(0.3)
                continue

            log(f"Tap OPEN ({entry.src}) → '{entry.text}' @ ({entry.x},{entry.y})")
            tap(driver, entry.x, entry.y)
            state.last_tap_key  = tap_key
            state.last_tap_ts   = time.time()
            state.open_retries += 1
            state.mark_progress()
            time.sleep(0.6)

            state.phase = Phase.INSPECT
            continue

        # ── PHASE: INSPECT ───────────────────────────────────────────────
        if state.phase == Phase.INSPECT:
            texts  = merged_texts(driver, ocr, lower_half=True)
            popup  = analyze_popup(texts)

            log(f"Popup: kind={popup.kind.name}, countdown={popup.countdown_sec}s, ref={popup.ref_value}¥")

            if popup.kind == PopupKind.NONE:
                # Popup didn't open — may have opened popularity board
                if _contains_any(_joined(texts), KW_POPULARITY):
                    log("  Popularity board appeared — dismissing.")
                    dismiss_overlays(driver, ocr)
                    state.entry_cache.invalidate()
                    state.phase = Phase.SCAN
                    continue
                # Retry open
                if state.open_retries >= args.open_retry_before_swipe:
                    log("  Open retries exhausted — switching room.")
                    state.phase = Phase.SWITCH
                else:
                    state.phase = Phase.OPEN
                continue

            if popup.kind in (PopupKind.NONPHYSICAL, PopupKind.EXPIRED, PopupKind.LOW_VALUE):
                log(f"  Skip popup ({popup.kind.name}) — switching room.")
                state.phase = Phase.SWITCH
                continue

            # PopupKind.FUDAI ✅
            state.open_retries = 0  # valid popup resets retry counter
            # Save bag info for result notification
            state.current_bag_ref = popup.ref_value

            if popup.has_success:
                log("  Already joined — going to WAIT_DRAW.")
                state.phase = Phase.WAIT_DRAW
                continue

            state.phase = Phase.TASK
            continue

        # ── PHASE: TASK ──────────────────────────────────────────────────
        if state.phase == Phase.TASK:
            log("Executing tasks…")
            result = execute_tasks(driver, ocr, max_rounds=6)
            log(f"Task result: {result.name}")
            state.mark_progress()

            if result == TaskResult.SUCCESS:
                state.phase = Phase.WAIT_DRAW
                continue

            if result == TaskResult.EXPIRED:
                log("Popup expired during tasks — switching room.")
                state.phase = Phase.SWITCH
                continue

            if result == TaskResult.TASKS_DONE:
                # Tasks done but no explicit success text yet — inspect again
                state.phase = Phase.INSPECT
                continue

            if result == TaskResult.POPUP_LOST:
                log("Task popup lost — reopening 福袋 in current room.")
                state.unfinished_rounds = 0
                dismiss_overlays(driver, ocr, rounds=2)
                state.entry_cache.invalidate()
                reopen = find_entry_icon(driver, ocr, state.entry_cache)
                if reopen:
                    log(f"  Re-open after popup lost ({reopen.src}) → '{reopen.text}' @ ({reopen.x},{reopen.y})")
                    tap(driver, reopen.x, reopen.y)
                    time.sleep(0.55)
                    state.phase = Phase.INSPECT
                else:
                    log("  Entry icon not found after popup lost — back to SCAN.")
                    state.phase = Phase.SCAN
                continue

            # STILL_OPEN — tasks remain unfinished
            state.unfinished_rounds += 1
            limit = args.max_unfinished_rounds
            log(f"Tasks still unfinished ({state.unfinished_rounds}/{limit}).")

            if state.unfinished_rounds >= limit:
                log("Unfinished task limit reached — switching room.")
                state.phase = Phase.SWITCH
            else:
                # Try re-inspecting / re-opening popup before retrying tasks
                dismiss_overlays(driver, ocr, rounds=2)
                reopen = find_entry_icon(driver, ocr, state.entry_cache)
                if reopen:
                    tap(driver, reopen.x, reopen.y)
                    time.sleep(0.55)
                state.phase = Phase.TASK
            continue

        # ── PHASE: WAIT_DRAW ─────────────────────────────────────────────
        if state.phase == Phase.WAIT_DRAW:
            draw_result = wait_for_result(
                driver, ocr,
                max_wait=args.draw_result_max_wait,
                poll=args.draw_poll_interval,
                grace=args.draw_countdown_grace,
                entry_cache=state.entry_cache,
            )
            if draw_result == "win":
                state.current_bag_round += 1
                prize_str = f"¥{state.current_bag_ref:.0f}" if state.current_bag_ref else "未知奖品"
                msg = (f"🎉 福袋开奖结果\n"
                       f"第 {state.current_bag_round} 轮\n"
                       f"结果：中奖！ 🏆\n"
                       f"奖品参考价：{prize_str}\n"
                       f"时间：{time.strftime('%H:%M:%S')}")
                log("🎉 WIN! Stopping bot.")
                notify_imessage(args.notify_phone, msg)
                return 0
            elif draw_result in ("lose", "unknown_after_countdown"):
                state.current_bag_round += 1
                prize_str = f"¥{state.current_bag_ref:.0f}" if state.current_bag_ref else "未知奖品"
                msg = (f"😔 福袋开奖结果\n"
                       f"第 {state.current_bag_round} 轮\n"
                       f"结果：未中奖\n"
                       f"奖品参考价：{prize_str}\n"
                       f"时间：{time.strftime('%H:%M:%S')}")
                notify_imessage(args.notify_phone, msg)
                log(f"Draw outcome: {draw_result} — switching room.")
                state.phase = Phase.SWITCH
            elif draw_result == "expired_no_result":
                state.current_bag_round += 1
                prize_str = f"¥{state.current_bag_ref:.0f}" if state.current_bag_ref else "未知奖品"
                msg = (f"⏱ 福袋开奖结果\n"
                       f"第 {state.current_bag_round} 轮\n"
                       f"结果：未检测到 (00:00 超时)\n"
                       f"奖品参考价：{prize_str}\n"
                       f"时间：{time.strftime('%H:%M:%S')}")
                notify_imessage(args.notify_phone, msg)
                log("Draw outcome: 00:00 reached, no result — closing popup, re-scanning room.")
                dismiss_overlays(driver, ocr, rounds=4)
                state.reset_for_new_room()
                state.phase = Phase.SCAN
            else:
                log(f"Draw outcome: {draw_result} — staying in room, re-scanning.")
                state.reset_for_new_room()
                state.phase = Phase.SCAN
            state.last_swipe_ts = time.time()
            continue

        # Fallback — should never reach here
        time.sleep(random.uniform(args.interval_min, args.interval_max))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Douyin 福袋 bot (refactored)")

    # Connection
    parser.add_argument("--appium",   default="http://127.0.0.1:4723")
    parser.add_argument("--udid",     default="auto")
    parser.add_argument("--bundle-id", default="com.ss.iphone.ugc.Aweme")
    parser.add_argument("--no-reset", action="store_true", default=True)

    # WDA / provisioning
    parser.add_argument("--xcode-org-id",                         default=None)
    parser.add_argument("--updated-wda-bundle-id",                default=None)
    parser.add_argument("--show-xcode-log",                       action="store_true")
    parser.add_argument("--allow-provisioning-updates",           action="store_true")
    parser.add_argument("--allow-provisioning-device-registration", action="store_true")
    parser.add_argument("--wda-launch-timeout-ms",                type=int,   default=120000)
    parser.add_argument("--wda-connection-timeout-ms",            type=int,   default=120000)
    parser.add_argument("--use-new-wda",                          action="store_true")
    parser.add_argument("--wda-startup-retries",                  type=int,   default=2)
    parser.add_argument("--wda-startup-retry-interval-ms",        type=int,   default=15000)
    parser.add_argument("--wait-for-idle-timeout",                type=float, default=0.0)
    parser.add_argument("--wait-for-quiescence",                  action="store_true")
    parser.add_argument("--wda-local-port",                       type=int,   default=None)
    parser.add_argument("--mjpeg-server-port",                    type=int,   default=None)
    parser.add_argument("--derived-data-path",                    default=None)

    # Bot tuning
    parser.add_argument("--max-minutes",              type=int,   default=0)
    parser.add_argument("--interval-min",             type=float, default=0.7)
    parser.add_argument("--interval-max",             type=float, default=1.2)
    parser.add_argument("--blocked-swipe-cooldown",   type=float, default=4.0)
    parser.add_argument("--open-retry-before-swipe",  type=int,   default=4)
    parser.add_argument("--post-swipe-wait",          type=float, default=5.0)
    parser.add_argument("--draw-countdown-grace",     type=float, default=2.0)
    parser.add_argument("--draw-poll-interval",       type=float, default=1.5)
    parser.add_argument("--draw-result-max-wait",     type=int,   default=240)
    parser.add_argument("--room-stall-seconds",       type=float, default=45.0)
    parser.add_argument("--max-unfinished-rounds",    type=int,   default=3)
    parser.add_argument("--notify-phone",             type=str,   default=None,
                        help="Phone number to iMessage draw results to (e.g. +8613812345678)")

    args = parser.parse_args()

    if args.udid == "auto":
        detected = auto_detect_udid()
        if not detected:
            raise RuntimeError(
                "Cannot auto-detect eligible UDID (iPhone 13 Pro Max is excluded). "
                "Connect another supported device or pass --udid explicitly."
            )
        args.udid = detected
        log(f"Auto-detected UDID: {args.udid}")

    ocr = RapidOCR() if RapidOCR is not None else None
    if ocr is None:
        log("OCR engine unavailable — running native XML mode only.")

    log("Connecting to Appium…")
    wda_caps: dict = {
        "wdaLaunchTimeout":      args.wda_launch_timeout_ms,
        "wdaConnectionTimeout":  args.wda_connection_timeout_ms,
        "useNewWDA":             args.use_new_wda,
        "wdaStartupRetries":     args.wda_startup_retries,
        "wdaStartupRetryInterval": args.wda_startup_retry_interval_ms,
        "waitForIdleTimeout":    args.wait_for_idle_timeout,
        "waitForQuiescence":     args.wait_for_quiescence,
    }
    if args.xcode_org_id:
        wda_caps["xcodeOrgId"]      = args.xcode_org_id
        wda_caps["xcodeSigningId"]  = "Apple Development"
    if args.updated_wda_bundle_id:
        wda_caps["updatedWDABundleId"] = args.updated_wda_bundle_id
    if args.show_xcode_log:
        wda_caps["showXcodeLog"] = True
    if args.allow_provisioning_updates:
        wda_caps["allowProvisioningUpdates"] = True
    if args.allow_provisioning_device_registration:
        wda_caps["allowProvisioningDeviceRegistration"] = True
    if args.wda_local_port:
        wda_caps["wdaLocalPort"] = args.wda_local_port
    if args.mjpeg_server_port:
        wda_caps["mjpegServerPort"] = args.mjpeg_server_port
    if args.derived_data_path:
        wda_caps["derivedDataPath"] = args.derived_data_path

    def _is_wda_bootstrap_error(err: Exception) -> bool:
        s = str(err or "")
        keys = [
            "WebDriverAgent",
            "xcodebuild failed",
            "Could not proxy command",
            "socket hang up",
            "Bad Gateway",
            "Connection refused",
            "Max retries exceeded",
            "Connection was refused to port",
        ]
        return any(k in s for k in keys)

    driver: Optional[webdriver.Remote] = None
    bootstrap_attempts = 4
    last_bootstrap_error: Optional[Exception] = None
    for attempt in range(1, bootstrap_attempts + 1):
        try:
            driver = build_driver(args.appium, args.udid, args.bundle_id, **wda_caps)
            break
        except Exception as e:
            last_bootstrap_error = e
            if (not _is_wda_bootstrap_error(e)) or attempt >= bootstrap_attempts:
                raise
            wait_s = min(12.0, 2.0 * attempt)
            log(f"WDA bootstrap failed ({e.__class__.__name__}) — retry {attempt}/{bootstrap_attempts} in {wait_s:.1f}s…")
            wda_caps["useNewWDA"] = True
            wda_caps["wdaLaunchTimeout"] = max(int(wda_caps["wdaLaunchTimeout"]), 240000)
            wda_caps["wdaConnectionTimeout"] = max(int(wda_caps["wdaConnectionTimeout"]), 240000)
            wda_caps["wdaStartupRetries"] = max(6, int(wda_caps["wdaStartupRetries"]))
            wda_caps["wdaStartupRetryInterval"] = max(35000, int(wda_caps["wdaStartupRetryInterval"]))
            time.sleep(wait_s)

    if driver is None and last_bootstrap_error is not None:
        raise last_bootstrap_error

    try:
        return run_bot(driver, ocr, args)
    except Exception as e:
        log(f"Fatal error: {e}")
        # Attempt to reconnect once and resume
        log("Attempting to reconnect to WDA...")
        try:
            driver.quit()
        except Exception:
            pass
        time.sleep(3)
        try:
            driver = build_driver(args.appium, args.udid, args.bundle_id, **wda_caps)
            log("Reconnected. Resuming bot.")
            return run_bot(driver, ocr, args)
        except Exception as e2:
            log(f"Reconnect failed: {e2}")
            return 1
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
