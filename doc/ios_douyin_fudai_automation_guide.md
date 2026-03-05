# iOS 抖音福袋自动化方案（重构版）

## 1. 目标与范围

持续在 iOS 真机直播间中执行福袋流程，核心目标：

1. 识别并点击福袋入口图标（左上区域小图标）。
2. 读取福袋半屏弹窗，判断是否值得参与。
3. 自动完成参与任务（评论、加粉丝团、去完成等）。
4. 等待开奖并判断结果（中奖退出，未中奖/超时切房）。
5. 支持单设备与多设备并行（通过 `ios_multi_device_manager.py`）。

---

## 2. 架构：显式状态机

主循环是 **6 个明确阶段**，无嵌套 if-else 意大利面：

```
SCAN → OPEN → INSPECT → TASK → WAIT_DRAW → RESULT
                 ↑                              |
                 └──────── SWITCH ←─────────────┘
```

| 阶段 | 说明 |
|------|------|
| `SCAN` | 检查成功/失败信号；找 JOIN 直接按钮；找福袋入口图标 |
| `OPEN` | 点击入口图标打开半屏弹窗 |
| `INSPECT` | 解析弹窗类型（有效/非实物/低价/已过期）；决定下一步 |
| `TASK` | 执行参与任务列表（评论→加粉丝团→通用按钮） |
| `WAIT_DRAW` | 等待倒计时归零，监听开奖结果 |
| `SWITCH` | 切换到下一直播间 |

---

## 3. 重要设计变更（对比旧版）

### 3.1 状态无全局变量
所有运行状态存在 `BotState` dataclass 中，包括入口缓存 `EntryCache`。
适配多设备时各实例完全隔离。

### 3.2 入口图标检测锚定到已知区域
基于截图分析，福袋入口图标固定在左上角 **"带货总榜" 行下方**第二个图标处：
- 屏幕区域：x ∈ [5%, 45%]，y ∈ [12%, 42%]
- 三层检测管线：XML页面源 → 原生 Predicate → OCR（冷却限速）
- 不再需要复杂的多候选排序和形状过滤

### 3.3 弹窗分析函数化
`analyze_popup(texts) -> PopupInfo` 返回结构化信息：
- `kind`: FUDAI / NONPHYSICAL / EXPIRED / LOW_VALUE
- `countdown_sec`, `ref_value`
- `has_unfinished_tasks`, `has_success`

### 3.4 钻石福袋判断修复
**旧版 bug**：弹窗底部的 `"加入粉丝团 (1钻石)"` 是参与成本按钮，不是奖品类型，
旧版错误地将其识别为"钻石福袋"并直接切房。

**新版**：`_is_prize_nonphysical()` 跳过含 `粉丝团` 的文本行，只看奖品描述区域。

### 3.5 任务执行顺序明确
```
评论任务 → 加入粉丝团 (含二次确认) → 通用任务按钮
```
每轮结束后重新读取弹窗状态，循环最多 6 轮。

### 3.6 开奖等待简化
- `_parse_countdown()` 只在看到 `后开奖` 锚点时才信任 `mm:ss` 格式，避免误读界面时钟。
- 倒计时归零时立即进入探测循环（最多 grace+5 秒）。
- 弹窗消失时自动尝试重新点击入口（`reopen_interval` 秒一次）。

---

## 4. 跳过/切房触发条件

| 条件 | 动作 |
|------|------|
| 福袋类型 = NONPHYSICAL (红包/抖币) | 切房 |
| 福袋类型 = EXPIRED (倒计时=0) | 切房 |
| 福袋类型 = LOW_VALUE (参考价值<10¥, 或>4分钟且<500¥) | 切房 |
| 未完成任务轮次达到 `max_unfinished_rounds` | 切房 |
| 开奖结果 = lose 或 unknown_after_countdown | 切房 |
| 入口图标找不到 ≥2 轮 | 切房 |
| OPEN 重试次数达到 `open_retry_before_swipe` | 切房 |
| 房间停滞超过 `room_stall_seconds` | 切房 |
| 发现封禁/结束等文案 | 切房 |

---

## 5. 切房手势

与旧版相同：
- 屏幕正中竖直上滑，最多 4 次尝试
- 每次等待 `max(5s, post_swipe_wait) + 随机 0~2s`
- 通过房间指纹（上半屏文本集合）校验是否换房

---

## 6. 多设备管理器（不变）

`ios_multi_device_manager.py` 提供 `discover / start / status / logs / stop` 命令，
每台设备独立 Appium 端口、WDA 端口、MJPEG 端口、日志文件。

启动：
```bash
./run_ios_multi_device_manager.sh
# 或指定设备：
DEVICES="00008030-00166DC93E50802E" ./run_ios_multi_device_manager.sh
```

---

## 7. 关键参数（默认值）

| 参数 | 默认 | 说明 |
|------|------|------|
| `open-retry-before-swipe` | 4 | OPEN 失败多少次后切房 |
| `post-swipe-wait` | 5.0s | 切房后等待时间 |
| `draw-countdown-grace` | 2.0s | 倒计时结束后额外探测时间 |
| `draw-poll-interval` | 1.5s | 开奖轮询间隔 |
| `draw-result-max-wait` | 240s | 开奖最大等待时间 |
| `room-stall-seconds` | 45s | 房间停滞阈值 |
| `max-unfinished-rounds` | 3 | 未完成任务最大重试轮次 |

---

## 8. 故障排查

1. **UDID 不识别**：运行 `python3 ios_multi_device_manager.py discover` 确认设备可见。
2. **WDA 启动失败**：加 `SHOW_XCODE_LOG=1` 查看 xcode 构建日志。
3. **残留进程**：
   ```bash
   python3 ios_multi_device_manager.py stop
   ```
4. **找不到福袋图标**：先截图确认直播间确实有福袋活动，入口图标需在屏幕左上 x∈[5%,45%] y∈[12%,42%] 区域内。
