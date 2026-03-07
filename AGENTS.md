# AGENTS.md — Douyin 福袋 iOS Automation Bot

This file gives Codex full context on the project so it can assist effectively without re-reading the entire codebase from scratch each session.

---

## Project Overview

An iOS automation bot that monitors Douyin (抖音) live-stream rooms, detects 福袋 (lucky bag) giveaway events, participates automatically, waits for draw results, and **sends iMessage notifications** for every round result. It targets **physical-prize bags only** and skips non-physical rewards (红包, 抖币, 金币), expired bags, and low-value bags.

**Stack:** Python 3 · Appium (XCUITest) · RapidOCR · PIL · NumPy · AppleScript (iMessage)
**Platform:** macOS host → physical iPhone via USB (LiveContainer / WDA)

---

## File Map

```
ios_douyin_fudai_bot.py          # Main bot — single file, ~1626 lines
ios_multi_device_manager.py      # Multi-device orchestrator (runs N bots in parallel)
run_ios_douyin_fudai.sh          # Shell launcher with env-var overrides
run_ios_multi_device_manager.sh  # Shell launcher for multi-device mode
cleanup_ios_fudai_debug.sh       # Kills stale WDA / Appium processes
requirements_ios_fudai.txt       # pip deps
doc/ios_douyin_fudai_automation_guide.md  # High-level human guide
AGENTS.md                        # This file
```

---

## Architecture — State Machine

```
SCAN → OPEN → INSPECT → TASK → WAIT_DRAW → RESULT → (SWITCH or loop back)
```

| Phase | What happens |
|---|---|
| **SCAN** | Look for 福袋 entry icon. Miss ≥2 rounds → SWITCH. Stale 00:00 popup → dismiss first. |
| **OPEN** | Tap icon. Wait for half-screen popup. Retry up to `open_retry_before_swipe` times. |
| **INSPECT** | Read popup. Classify as FUDAI / NONPHYSICAL / EXPIRED / LOW_VALUE. Save `ref_value` to `state.current_bag_ref`. |
| **TASK** | Execute tasks in order: comment → fans-group join → generic buttons. |
| **WAIT_DRAW** | Poll for win/lose. Detect frozen 00:00 popup and close it. Heartbeat every 20s. |
| **RESULT** | Log result, send iMessage. WIN → exit. LOSE/timeout → SWITCH. 00:00 expired → re-scan same room. |
| **SWITCH** | Swipe up to next live room. |

State lives in `BotState` dataclass — no module-level mutable globals.

---

## Screen Layout Constants

Calibrated across two observed live rooms on different devices.

```python
# Entry icon / label region
#   Room A (414pt, PROYA):      icon  at x=55pt  (13% W), y=235pt (26% H)
#   Room B (390pt, FlowerWest): label at x=138pt (35% W), y=147pt (17% H)
# DO NOT tighten X_MAX below 0.45 — position varies by room layout.
ENTRY_X_MIN = 0.03   # exclude far-left bezel
ENTRY_X_MAX = 0.45   # right edge of left overlay cluster
ENTRY_Y_MIN = 0.10   # below status bar
ENTRY_Y_MAX = 0.42   # above comment/chat area

POPUP_Y_MIN = 0.47   # half-screen popup panel top edge

ENTRY_MAX_W_RATIO = 0.22
ENTRY_MAX_H_RATIO = 0.12
ENTRY_MAX_ASPECT  = 4.0    # text labels can be wider than tall
ENTRY_MIN_SIDE_PX = 10
```

> **Lesson (2026-03-06):** Tightening `ENTRY_X_MAX` to `0.22` broke detection in rooms where the
> 福袋 label sits at 35% W. Always keep the box wide-but-bounded.

Popup visual reference (logical pts, 414×896):

| UI element | Approx y-range |
|---|---|
| 超级福袋 title | 430–460 |
| `MM:SS 后开奖` countdown | 470–510 |
| Prize image + title card | 510–650 |
| 参与任务 + 已达成 rows | 660–770 |
| 参与成功 等待开奖 CTA | 790–850 |

---

## Key Classes & Functions

### Detection
- `find_entry_icon(driver, ocr, cache)` — 4-tier: cache → XML → native predicate → OCR fallback
- `EntryCache` — TTL 18s, OCR rate-limit 2.5s cooldown
- `scrape_elements(driver, ...)` — constrained XML page-source parser

### Popup Analysis
- `analyze_popup(texts) → PopupInfo` — classifies kind, parses countdown + ref value, checks task/success state
- `PopupKind` enum: `NONE / FUDAI / NONPHYSICAL / EXPIRED / LOW_VALUE`
- `_parse_countdown(texts)` — handles `MM:SS 后开奖`, `X分Y秒`, `NNN秒`, tokenised digit sequences
- `_parse_ref_value(texts)` — primary: `参考价值: ¥52`; secondary: `¥N` skipping `¥0`
- `_is_prize_nonphysical(texts)` — skips fans-group CTA lines to avoid diamond-cost false positives

### Task Execution
- `pick_hits(driver, ocr, keywords, y_min_r)` — XML + OCR element finder in popup region
- Task order: comment (`一键发表评论`) → fans-group (`加入粉丝团`) → confirm → generic (`去完成`)
- `KW_TASK_DONE = ["已达成", "已完成"]` — all done → skip task phase

### Draw Result & Notifications
- `wait_for_result(driver, ocr, ...)` — polls win/lose; detects frozen 00:00, returns `"expired_no_result"`
- `notify_imessage(phone, msg)` — iMessage via `osascript`; no-op if `phone=None`
- `BotState.current_bag_ref` — ¥ ref value saved at INSPECT, used in notification
- `BotState.current_bag_round` — monotonic counter incremented on every result

### Text & Screenshot Helpers
- `merged_texts(driver, ocr, lower_half)` — XML + OCR texts, deduplicated
- `ocr_texts(...)` — crops to `POPUP_Y_MIN`; casts OCR score to `float` (RapidOCR returns str)
- `screenshot_np(driver, retries=2)` — retries on WDA socket hang-up

### Overlay Dismissal
- `dismiss_overlays(driver, ocr, rounds)` — 3-stage: (1) × button top-right, (2) named close buttons, (3) tap above popup or swipe-down

---

## iMessage Notifications

Every draw round sends an iMessage to `--notify-phone`:

```
🎉 福袋开奖结果   ← WIN
😔 福袋开奖结果   ← LOSE / unknown
⏱ 福袋开奖结果   ← 00:00 expired

第 N 轮
结果：中奖！🏆 / 未中奖 / 未检测到 (00:00 超时)
奖品参考价：¥5999
时间：HH:MM:SS
```

Implemented in `notify_imessage()` via `osascript` → Messages app.
Fires in all 3 result branches. Verified working: `+8613422623453`.

---

## 00:00 Stale Popup Handling

After a draw completes the popup freezes at `00:00 后开奖`. Three paths close it:

| Scenario | Detection | Action |
|---|---|---|
| Bot starts with 00:00 popup | SCAN: `countdown=None` + `"00"` + `"超级福袋"` in text | `dismiss_overlays` → SCAN |
| `wait_for_result` parses `left=0` | `left <= 1` probe for 8s | No result → `"expired_no_result"` |
| Countdown unparseable, popup frozen | `else` branch: `frozen_zero` + `zero_since` timer | After `grace+6s` → `"expired_no_result"` |

`"expired_no_result"` → `dismiss_overlays(rounds=4)` → `state.phase = Phase.SCAN`.

---

## Known Bugs Fixed

### 1. RapidOCR score is a string, not float
RapidOCR 1.2.3 returns score as `'0.825...'`. Use `float(score or 0)` everywhere.
Affected: `ocr_texts()`, `find_entry_icon()` OCR block, `pick_hits()` OCR block.

### 2. ¥0 false-positive in ref value parsing
Popup shows `¥0 参考价值: ¥52`. Secondary regex must use `[1-9]` start to skip `¥0`.

### 3. Popup Y boundary too low
`POPUP_Y_MIN = 0.52` missed title and countdown. Fixed to `0.47`.

### 4. Countdown `== 0` vs `is None`
Undetected countdown is `None` not `0`. Always guard `is not None` before comparing.

### 5. Entry region too tight for multi-room
`ENTRY_X_MAX = 0.22` missed FlowerWest room (icon at 35% W). Fixed to `0.45`.

### 6. WDA socket hang-up crashes bot
`screenshot_np()` retries twice. OCR call sites catch exceptions, fall back to XML-only.

### 7. `dismiss_overlays` variable name collision
`h_` used for both height and Hit object → NameError. Fixed: inner hit = `hh`, screen size = `sw, sh`.

---

## Skip Conditions (SWITCH triggers)

- Popup kind is `NONPHYSICAL`, `EXPIRED`, or `LOW_VALUE`
- Entry icon not found ≥2 consecutive scan rounds
- `open_retry_before_swipe` (4) attempts all failed
- Tasks unfinished for `max_unfinished_rounds` (3) rounds
- Room stalled `room_stall_seconds` (45s) with no state change
- Draw result is LOSE or `draw_result_max_wait` (240s) exceeded

---

## Running the Bot

### Prerequisites
```bash
# 1. Appium server (v3+, XCUITest driver)
npm i -g appium
appium driver install xcuitest

# 2. Python deps
pip install -r requirements_ios_fudai.txt --break-system-packages

# 3. Device: iPhone connected via USB, Douyin open, inside a live room
```

### Quick start
```bash
python3 ios_douyin_fudai_bot.py \
  --xcode-org-id 997XR67PRS \
  --updated-wda-bundle-id com.see2see.livecontainer \
  --allow-provisioning-updates \
  --use-new-wda \
  --notify-phone +8613422623453

# Via shell script (env-var overrides)
./run_ios_douyin_fudai.sh
```

### Key CLI flags

| Flag | Default | Purpose |
|---|---|---|
| `--udid` | auto | Device UDID |
| `--bundle-id` | `com.ss.iphone.ugc.Aweme` | Douyin bundle ID |
| `--notify-phone` | — | iMessage results to this number |
| `--max-minutes` | 0 (∞) | Auto-stop after N minutes |
| `--open-retry-before-swipe` | 4 | Taps before giving up on a room |
| `--post-swipe-wait` | 5.0s | Settle time after room swipe |
| `--draw-countdown-grace` | 2.0s | Extra wait after countdown hits 0 |
| `--draw-result-max-wait` | 240s | Max time to wait for draw result |
| `--room-stall-seconds` | 45.0s | Switch if no state change |
| `--max-unfinished-rounds` | 3 | Give up on tasks after N rounds |
| `--xcode-org-id` | — | Team ID for WDA provisioning |
| `--updated-wda-bundle-id` | — | e.g. `com.see2see.livecontainer` |
| `--use-new-wda` | false | Force fresh WDA install |

---

## Device Discovery Policy

- Every run dynamically discovers currently connected iOS devices.
- No fixed UDID should be hardcoded in code or docs.
- Device model exclusion: `iPhone 13 Pro Max` (also matches product type `iPhone14,3`).

Appium: `http://127.0.0.1:4723`
WDA xcodeOrgId: `997XR67PRS`
WDA bundle: `com.see2see.livecontainer`
Notify phone: `+8613422623453`

---

## Keyword Reference

```python
KW_OPEN_ENTRY      = ["福袋"]
KW_OPEN_BLOCKLIST  = ["没有抽中", "未抽中", "抽中福袋", "已开奖", "开奖结果"]

KW_JOIN            = ["去参与", "立即参与", "参与抽奖", "马上参与"]
KW_FANS_JOIN       = ["加入粉丝团", "去加入粉丝团", "立即加入粉丝团", "加入粉丝"]
KW_FANS_CONFIRM    = ["确认加入", "确认", "加入并关注", "立即加入"]
KW_COMMENT_TASK    = ["一键发表评论", "一键评论", "发表评论", "去评论"]

KW_TASK_UNFINISHED = ["未达成", "未完成", "未满足"]
KW_TASK_DONE       = ["已达成", "已完成"]
KW_SUCCESS         = ["已参与", "参与成功", "等待开奖", "参与成功 等待开奖"]

KW_WIN             = ["恭喜抽中", "恭喜你抽中", "恭喜你中奖了", "抽中福袋"]
KW_LOSE            = ["未中奖", "未中签", "没有抽中福袋", "很遗憾", "下次再来", "擦肩而过"]

KW_NONPHYSICAL     = ["红包", "金币", "福气值", "现金红包", "抖币", "音浪"]
KW_BLOCKED         = ["暂未开始", "活动已结束", "已结束", "不可参与", "已抢完", "人数已满"]
KW_POPUP_ANCHOR    = ["后开奖", "后开", "超级福袋", "参与任务", "参考价值"]
KW_CLOSE           = ["关闭", "取消", "我知道了", "稍后再说"]
```

---

## Common Failure Modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `TypeError: '>=' not supported … str and float` | OCR score not cast | `float(score or 0)` everywhere |
| All bags marked EXPIRED | `countdown is None` not guarded | Check `is not None` first |
| ¥52 bag skipped as LOW_VALUE | `¥0` matched as ref | Use `[1-9]` start in secondary regex |
| Icon never found | Region too tight | Check `ENTRY_X/Y_MIN/MAX`; screenshot to verify |
| Popup text not read | `POPUP_Y_MIN` too high | Lower toward `0.47`; screenshot to verify |
| Bot stuck at 00:00 | Stale popup not dismissed | See 00:00 Stale Popup Handling section |
| WDA crash / socket hang-up | Screenshot timeout | `screenshot_np()` retries; OCR falls back to XML |
| iMessage not sent | AppleScript permissions | Check System Prefs → Privacy → Automation → Messages |

---

## Debugging Tips

```bash
# Live screenshot from device
python3 - <<'EOF'
from appium import webdriver
from appium.options.ios import XCUITestOptions
import re
import subprocess

udid = None
out = subprocess.check_output(["xcrun", "xctrace", "list", "devices"], text=True, stderr=subprocess.STDOUT)
for line in out.splitlines():
    s = line.strip()
    if not s or any(x in s for x in ("Simulator", "Mac", "Watch")):
        continue
    m = re.search(r"\(([0-9A-Fa-f-]{20,})\)\s*$", s)
    if m:
        udid = m.group(1)
        break
if not udid:
    raise RuntimeError("No connected iOS device found")

opts = XCUITestOptions()
opts.udid = udid
opts.bundle_id = "com.ss.iphone.ugc.Aweme"
opts.set_capability("noReset", True)
driver = webdriver.Remote("http://127.0.0.1:4723", options=opts)
driver.get_screenshot_as_file("/Users/zhuolinchen/Desktop/snap.png")
driver.quit()
EOF

# Watch live log
tail -f /tmp/fudai_notify.log

# Kill stale processes
./cleanup_ios_fudai_debug.sh
pkill -f ios_douyin_fudai_bot
```

---

## What NOT to Change Without Testing

- `POPUP_Y_MIN` — too low → live-stream clock parsed as countdown
- `_parse_countdown()` — `后开奖` anchor guard prevents live clock misread
- `_is_prize_nonphysical()` — `粉丝团` line skip is intentional; removing it breaks diamond detection
- `KW_OPEN_BLOCKLIST` — prevents result overlays being mistaken for new 福袋 entry icon
- `notify_imessage()` recipient phone — hardcoded as `--notify-phone` arg, not in source
