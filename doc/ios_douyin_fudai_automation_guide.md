# iOS 抖音福袋自动化方案（当前实现）

## 1. 目标与当前范围

本项目用于 iOS 真机直播间中持续执行福袋流程，核心目标是：

1. 自动识别并打开福袋入口。
2. 自动执行任务动作（含 `一键发表评论` 与 `加入粉丝团` 相关按钮）。
3. 在已参与后等待开奖并判断结果。
4. 对无效房间或不符合策略的福袋自动切房。
5. 支持单设备与多设备并行运行。

当前默认策略为“持续运行直到明确中奖或触发红包退出信号”，`--max-minutes` 仅用于提醒，不会强制退出。

---

## 2. 代码与脚本结构

- 主流程脚本：`ios_douyin_fudai_bot.py`
- 多设备管理器：`ios_multi_device_manager.py`
- 单设备启动脚本：`run_ios_douyin_fudai.sh`
- 多设备启动脚本：`run_ios_multi_device_manager.sh`
- 清理脚本：`cleanup_ios_fudai_debug.sh`
- 依赖文件：`requirements_ios_fudai.txt`

---

## 3. 环境依赖

建议环境：

1. macOS + Xcode（含命令行工具）
2. Node.js + Appium + xcuitest driver
3. Python 3.10+

安装示例：

```bash
brew install node
npm i -g appium
appium driver install xcuitest
python3 -m pip install -r requirements_ios_fudai.txt
```

Python OCR 引擎：`rapidocr-onnxruntime`（可选；不可用时自动退回 native-only 检索）。

---

## 4. 主流程状态机（单设备）

主流程是确定性的状态流：

1. `scan`：扫描福袋入口（优先左上区域，带缓存和 OCR 冷却）。
2. `open`：点击入口进入福袋弹窗。
3. `task`：执行任务动作（评论、加粉丝团、去完成等）。
4. `wait draw/result`：等待倒计时与开奖结果。
5. `switch room`：不满足策略时切到下一直播间。

### 4.1 关键判定

- 成功上下文：只有在福袋语境下出现 `已参与/参与成功` 才进入开奖等待。
- 开奖结果判定：
  - 倒计时仍在运行（>1s）时，不判定输赢。
  - `恭喜抽中/恭喜中奖` 等判定为 `win`。
  - `未中奖/很遗憾/下次再来` 等判定为 `lose`（仅在倒计时结束后生效）。

### 4.2 任务动作策略

任务按钮优先级包含：

- `一键发表评论`
- `一键加入粉丝团/去加入粉丝团/加入粉丝团`
- `去完成/去参与/立即参与` 等

粉丝团相关流程支持：

1. 点击加入粉丝团 CTA。
2. 识别并点击确认按钮（如 `确认加入`、`加入并关注`）。
3. 关闭遮罩并回到福袋面板继续任务。

### 4.3 切房触发条件（当前实现）

满足以下任一情况会触发切房：

1. 明确 `lose` 结果。
2. 命中 blocked 文案，且当前没有活跃倒计时。
3. 房间长时间无进展（`room_stall_seconds`）。
4. 连续找不到入口或 OPEN 重试超限。
5. 弹窗属于非实物（钻石/抖币/红包等）。
6. 弹窗倒计时为 `00:00`（视为无效倒计时）。
7. 价格过滤命中：
   - `参考价值 < 10` 元
   - `倒计时 > 240 秒` 且 `参考价值 < 500` 元
8. 弹窗显示“后开奖”但倒计时持续不可读（任务动作后仍不可读）。

> 切房前会做一次 `pre-switch recheck`（尝试再开一次福袋+任务探测），尽量避免误切。

---

## 5. 切房手势与校验

当前切房手势逻辑：

1. 从屏幕正中开始竖直上滑（避免误触公屏区域）。
2. 单次尝试只执行 **一次** 上滑。
3. 每次尝试后等待 `max(5s, post_swipe_wait) + [0~2s随机]`。
4. 通过房间指纹（上半屏文本集合）校验是否已换房。
5. 校验失败才进入下一次尝试。

注意：近期已修复“同次尝试双滑导致连续切两间”的问题。

---

## 6. 多设备管理器（推荐）

`ios_multi_device_manager.py` 提供：

- `discover`：发现可用设备 UDID
- `start`：按设备启动 Appium+Bot（自动分配端口）
- `status`：查看运行状态
- `logs`：查看设备日志
- `stop`：停止一个或全部设备

### 6.1 设备自动过滤（已实现）

默认只保留“Appium-ready 的有线 iOS 真机”（通过 `xcrun devicectl` 过滤）：

- iOS（iPhone/iPad）
- physical
- paired
- transport 为 wired（默认）

可选参数：

- `--allow-network-devices`：允许包含非有线设备。

当显式传入 `--devices` 时，不可用 UDID 会自动 `[skip]`，不会强拉起失败设备。

### 6.2 多设备隔离资源

每台设备独立：

1. Appium 端口（`appium-base-port` 起）
2. WDA 本地端口（`wda-base-port` 起）
3. MJPEG 端口（`mjpeg-base-port` 起）
4. `derivedDataPath`
5. 独立日志文件

运行状态目录（默认）：

- `.runtime/multi-device-manager/state.json`
- `.runtime/multi-device-manager/logs/<udid>.bot.log`
- `.runtime/multi-device-manager/logs/<udid>.appium.log`
- `.runtime/multi-device-manager/derived_data/<udid>/`

`stop` 时会额外清理该 UDID 的残留 `xcodebuild` 进程。

---

## 7. 启动方式

### 7.1 多设备一键启动（推荐）

```bash
./run_ios_multi_device_manager.sh
```

常用环境变量：

- `DEVICES="udid1,udid2"`
- `OPEN_LOG_WINDOWS=0|1`
- `LOG_KIND=bot|appium|both`
- `ALLOW_NETWORK_DEVICES=0|1`
- `SHOW_XCODE_LOG=0|1`

示例：

```bash
DEVICES="00008030-00166DC93E50802E" OPEN_LOG_WINDOWS=0 ./run_ios_multi_device_manager.sh
```

### 7.2 单设备启动

```bash
./run_ios_douyin_fudai.sh
```

或直接运行 Python（用于调试特定参数）：

```bash
python3 ios_douyin_fudai_bot.py --udid auto --appium http://127.0.0.1:4723
```

---

## 8. 日志与观察

管理器查看状态：

```bash
python3 ios_multi_device_manager.py status
```

查看单设备日志：

```bash
python3 ios_multi_device_manager.py logs --device <UDID> --kind both --lines 120
```

实时 tail：

```bash
tail -F .runtime/multi-device-manager/logs/<UDID>.bot.log
tail -F .runtime/multi-device-manager/logs/<UDID>.appium.log
```

---

## 9. 故障排查（当前常见）

1. `Unknown device or simulator UDID`
   - 设备不可用于 Appium 会被新过滤逻辑自动跳过。
   - 显式传入 `--devices` 时仍会打印 skip 原因。

2. `Connection was refused to port 8100`
   - 常见于 WDA 启动阶段，需观察后续是否完成握手。
   - 必要时加 `SHOW_XCODE_LOG=1` 查看构建细节。

3. 设备残留进程导致异常
   - 可执行：

```bash
python3 ios_multi_device_manager.py stop
./cleanup_ios_fudai_debug.sh
```

---

## 10. 当前默认关键参数（简表）

- 切房冷却：`blocked-swipe-cooldown=3.0`
- OPEN重试阈值：`open-retry-before-swipe=4`
- 切房后等待：`post-swipe-wait=5.0`
- 开奖轮询：`draw-poll-interval=1.0`
- 开奖最大等待：`draw-result-max-wait=240`
- 房间停滞阈值：`room-stall-seconds=45`
- 价格过滤：`<10` 或 `>240秒 且 <500`

如需调参，优先在 `run_ios_multi_device_manager.sh` 的环境变量里改，再观察日志迭代。
