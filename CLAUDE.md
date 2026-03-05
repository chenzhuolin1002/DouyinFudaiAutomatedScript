# CLAUDE.md — Douyin 福袋 iOS Automation Bot

This file gives Claude Code full context on the project so it can assist effectively without re-reading the entire codebase from scratch each session.

---

## Project Overview

An iOS automation bot that monitors Douyin (抖音) live-stream rooms, detects 福袋 (lucky bag) giveaway events, participates automatically, and waits for draw results. It targets **physical-prize bags only** and skips non-physical rewards (红包, 抖币, 金币), expired bags, and low-value bags.

**Stack:** Python 3 · Appium (XCUITest) · RapidOCR · PIL · NumPy  
**Platform:** macOS host → physical iPhone via USB (LiveContainer / WDA)

---

## File Map

```
ios_douyin_fudai_bot.py          # Main bot — single file, ~1400 lines
ios_multi_device_manager.py      # Multi-device orchestrator (runs N bots in parallel)
run_ios_douyin_fudai.sh          # Shell launcher with env-var overrides
run_ios_multi_device_manager.sh  # Shell launcher for multi-device mode
cleanup_ios_fudai_debug.sh       # Kills stale WDA / Appium processes
requirements_ios_fudai.txt       # pip deps
doc/ios_douyin_fudai_automation_guide.md  # High-level human guide
CLAUDE.md                        # This file
```

---

## Architecture — State Machine

The bot runs a strict phase loop with no implicit fallthrough:

```
SCAN → OPEN → INSPECT → TASK → WAIT_DRAW → RESULT → (SWITCH or loop back)
```

| Phase | What happens |
|---|---|
| **SCAN** | Look for 福袋 entry icon in top-left region. Miss ≥2 rounds → SWITCH. |
| **OPEN** | Tap icon. Wait for half-screen popup. Retry up to `open_retry_before_swipe` times. |
| **INSPECT** | Read popup. Classify as FUDAI / NONPHYSICAL / EXPIRED / LOW_VALUE. Skip if not FUDAI. |
| **TASK** | Execute participation tasks in order: comment → fans-group join → generic buttons. |
| **WAIT_DRAW** | Poll for win/lose signals. Retry unfinished tasks during wait. Timeout → SWITCH. |
| **RESULT** | Log win/lose. WIN → exit. LOSE/timeout → SWITCH. |
| **SWITCH** | Swipe up to next live room. |

State lives in `BotState` dataclass — no module-level mutable globals.

---

## Screen Layout Constants

Derived from visual analysis of a real device screenshot (414×896pt logical, iPhone XR).  
**Always keep these in sync if the layout changes.**

```python
# Entry icon (pink countdown thumbnail, top-left column)
ENTRY_X_MIN = 0.04   # icon left edge ~x=30pt
ENTRY_X_MAX = 0.22   # icon right edge ~x=80pt
ENTRY_Y_MIN = 0.22   # below 带货总榜 row (~y=195pt)
ENTRY_Y_MAX = 0.32   # above product-bullet list

# Half-screen popup dark panel
POPUP_Y_MIN = 0.47   # panel top edge ~y=430pt (48% of 896)

# Entry icon shape limits
ENTRY_MAX_W_RATIO = 0.15   # ~48pt wide on 414pt screen
ENTRY_MAX_H_RATIO = 0.09
ENTRY_MAX_ASPECT  = 1.6    # nearly square
ENTRY_MIN_SIDE_PX = 12
```

Visual reference elements on the popup (logical pts, 414×896):

| UI element | Approx y-range |
|---|---|
| 超级福袋 title | 430–460 |
| `MM:SS 后开奖` countdown | 470–510 |
| Prize image + title card | 510–650 |
| 参与任务 + 已达成 rows | 660–770 |
| 参与成功 等待开奖 CTA button | 790–850 |

---

## Key Classes & Functions

### Detection
- `find_entry_icon(driver, ocr, cache)` — 4-tier lookup: cache → XML scrape → native predicate → OCR fallback
- `EntryCache` — TTL-based cache (18s) with OCR rate-limiting (2.5s cooldown)
- `scrape_elements(driver, ...)` — constrained XML page-source parser

### Popup Analysis
- `analyze_popup(texts) → PopupInfo` — classifies kind, parses countdown + ref value, checks task/success state
- `PopupKind` enum: `NONE / FUDAI / NONPHYSICAL / EXPIRED / LOW_VALUE`
- `_parse_countdown(texts)` — handles `MM:SS 后开奖`, `X分Y秒`, `NNN秒`, and OCR-tokenised digit sequences
- `_parse_ref_value(texts)` — primary: `参考价值: ¥52`; secondary: any `¥N` (skips `¥0` participation price)
- `_is_prize_nonphysical(texts)` — skips fans-group CTA lines to avoid diamond-cost false positives

### Task Execution
- `pick_hits(driver, ocr, keywords, y_min_r)` — finds tappable elements in popup region via XML + OCR
- Task order: **comment** (`一键发表评论`) → **fans-group join** (`加入粉丝团`) → fans confirm → generic (`去完成`)
- `KW_TASK_DONE = ["已达成", "已完成"]` — detected on right side of each task row; all done → skip task phase

### Text Helpers
- `merged_texts(driver, ocr, lower_half)` — XML visible texts + OCR, deduplicated
- `ocr_texts(...)` — crops image to `POPUP_Y_MIN` when `lower_half=True`; casts `score` to `float` (RapidOCR returns score as string)

---

## Known Bugs Fixed (important for future edits)

### 1. RapidOCR score is a string, not float
RapidOCR 1.2.3 returns `score` as `'0.825...'` (str), not float.  
**All comparisons must use `float(score or 0)`**, never bare `score >= threshold`.

```python
# CORRECT
if text and float(score or 0) >= min_score:
# WRONG — raises TypeError
if text and (score or 0) >= min_score:
```

Affected locations: `ocr_texts()`, `find_entry_icon()` OCR block, `pick_hits()` OCR block.

### 2. ¥0 false-positive in ref value parsing
Popup shows `¥0 参考价值: ¥52` (free entry, prize is ¥52). The secondary `¥` regex must skip `¥0`:
```python
# CORRECT — skips ¥0
re.search(r"[¥￥]\s*([1-9][0-9,]*(?:\.[0-9]+)?)", t)
# WRONG — matches ¥0 → returns 0.0 → LOW_VALUE false skip
re.search(r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]+)?)", t)
```

### 3. Popup Y boundary too low
`POPUP_Y_MIN = 0.52` missed the title and countdown rows. Corrected to `0.47`.

### 4. Countdown `== 0` vs `is None`
Undetected countdown returns `None`, not `0`. The expired check must guard:
```python
# CORRECT
if info.countdown_sec is not None and info.countdown_sec <= 2:
# WRONG — treats None as 0 → marks every undetected bag as EXPIRED
if info.countdown_sec == 0:
```

---

## Skip Conditions (SWITCH triggers)

The bot switches to the next live room when any of these are true:

- Popup kind is `NONPHYSICAL`, `EXPIRED`, or `LOW_VALUE`
- Entry icon not found for ≥2 consecutive scan rounds
- `open_retry_before_swipe` (default 4) open attempts all failed
- All tasks remain unfinished for `max_unfinished_rounds` (default 3) rounds
- Room stalled with no state change for `room_stall_seconds` (default 45s)
- Draw result is LOSE or `draw_result_max_wait` (default 240s) exceeded

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
# Auto-detect device
python3 ios_douyin_fudai_bot.py

# Explicit UDID (preferred)
python3 ios_douyin_fudai_bot.py --udid 00008030-00166DC93E50802E

# Via shell script (supports env-var overrides)
./run_ios_douyin_fudai.sh
```

### Common env-var overrides for the shell script
```bash
UDID=00008030-00166DC93E50802E \
MAX_MINUTES=60 \
DRAW_RESULT_MAX_WAIT=180 \
./run_ios_douyin_fudai.sh
```

### Key CLI flags
| Flag | Default | Purpose |
|---|---|---|
| `--udid` | auto | Device UDID |
| `--bundle-id` | `com.ss.iphone.ugc.Aweme` | Douyin bundle ID |
| `--max-minutes` | 0 (unlimited) | Auto-stop after N minutes |
| `--open-retry-before-swipe` | 4 | Taps before giving up on a room |
| `--post-swipe-wait` | 5.0s | Settle time after room swipe |
| `--draw-countdown-grace` | 2.0s | Extra wait after countdown hits 0 |
| `--draw-result-max-wait` | 240s | Max time to wait for draw result |
| `--room-stall-seconds` | 45.0s | Switch if room produces no new state |
| `--max-unfinished-rounds` | 3 | Give up on tasks after N rounds |
| `--xcode-org-id` | — | Team ID for WDA provisioning |
| `--updated-wda-bundle-id` | — | e.g. `com.see2see.livecontainer` |

---

## Device Info (current test devices)

| Name | iOS | UDID | Status |
|---|---|---|---|
| zhuolin的iPhone | 26.3 | `00008030-00166DC93E50802E` | ✅ Primary bot device |
| zhuolin的 iPhone | 26.3.1 | `00008110-001A3D0C2252801E` | Secondary / spare |

Appium server: `http://127.0.0.1:4723`  
WDA xcodeOrgId: `997XR67PRS`  
WDA bundle: `com.see2see.livecontainer`

---

## Keyword Reference

```python
# Entry icon
KW_OPEN_ENTRY     = ["福袋"]
KW_OPEN_BLOCKLIST = ["没有抽中", "未抽中", "抽中福袋", "已开奖", "开奖结果"]

# Participation
KW_JOIN           = ["去参与", "立即参与", "参与抽奖", "马上参与"]
KW_FANS_JOIN      = ["加入粉丝团", "去加入粉丝团", "立即加入粉丝团", "加入粉丝"]
KW_FANS_CONFIRM   = ["确认加入", "确认", "加入并关注", "立即加入"]
KW_COMMENT_TASK   = ["一键发表评论", "一键评论", "发表评论", "去评论"]

# Task status
KW_TASK_UNFINISHED = ["未达成", "未完成", "未满足"]
KW_TASK_DONE       = ["已达成", "已完成"]      # right-side per-row completion
KW_SUCCESS         = ["已参与", "参与成功", "等待开奖", "参与成功 等待开奖"]

# Draw results
KW_WIN  = ["恭喜抽中", "恭喜你抽中", "恭喜你中奖了", "抽中福袋"]
KW_LOSE = ["未中奖", "未中签", "没有抽中福袋", "很遗憾", "下次再来", "擦肩而过"]

# Skip signals
KW_NONPHYSICAL = ["红包", "金币", "福气值", "现金红包", "抖币", "音浪"]
KW_BLOCKED     = ["暂未开始", "活动已结束", "已结束", "不可参与", "已抢完", "人数已满"]
KW_POPUP_ANCHOR = ["后开奖", "后开", "超级福袋", "参与任务", "参考价值"]
```

---

## Common Failure Modes & Fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| `TypeError: '>=' not supported … str and float` | OCR score not cast to float | Wrap all score comparisons in `float(score or 0)` |
| Bot marks all bags EXPIRED | `countdown_sec is None` not guarded | Check `is not None` before comparing |
| Bot skips ¥52 bag as LOW_VALUE | `¥0` matched as ref value | Use `[1-9]` start in secondary `¥` regex |
| Entry icon never found | Region too tight for different device | Check `ENTRY_X/Y_MIN/MAX`; widen slightly then screenshot to verify |
| Popup text not read | `POPUP_Y_MIN` too high | Decrease toward `0.47`; take screenshot to verify panel top edge |
| WDA bootstrap fails | Provisioning / signing issue | Pass `--xcode-org-id 997XR67PRS --updated-wda-bundle-id com.see2see.livecontainer` |
| Bot stuck in WAIT_DRAW | 已达成 not detected as success | Confirm `KW_TASK_DONE` list and `all_tasks_confirmed` logic in `analyze_popup` |

---

## Debugging Tips

```bash
# Take a live screenshot from the device
python3 - <<'EOF'
from appium import webdriver
from appium.options.ios import XCUITestOptions
opts = XCUITestOptions()
opts.udid = "00008030-00166DC93E50802E"
opts.bundle_id = "com.ss.iphone.ugc.Aweme"
opts.set_capability("noReset", True)
driver = webdriver.Remote("http://127.0.0.1:4723", options=opts)
driver.get_screenshot_as_file("/tmp/snap.png")
driver.quit()
print("saved /tmp/snap.png")
EOF

# Dump page source XML to inspect element tree
python3 - <<'EOF'
# ... same driver setup ...
with open("/tmp/page.xml", "w") as f:
    f.write(driver.page_source)
EOF

# Watch live bot log
tail -f /tmp/fudai_bot.log

# Kill stale WDA / Appium
./cleanup_ios_fudai_debug.sh
```

---

## What NOT to Change Without Testing

- `POPUP_Y_MIN` — lowering too much causes the live-stream clock to be parsed as countdown
- `_parse_countdown()` — the `后开奖` anchor guard prevents the live clock (top-left, always visible) from being misread as the draw timer
- `_is_prize_nonphysical()` — the `粉丝团` line skip is intentional; removing it breaks diamond-cost detection
- `KW_OPEN_BLOCKLIST` — prevents win/lose result overlays from being mistaken for a new 福袋 entry icon
