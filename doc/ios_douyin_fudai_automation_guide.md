# iOS 抖音福袋自动参与方案（Appium + OCR）

## 1. 目标与范围

本方案用于 iOS 真机上自动执行以下流程：

1. 当前直播间若无 `福袋/红包/抽奖` 入口，立即上滑切到下一个直播间。
2. 发现入口后点击，进入半屏任务窗。
3. 在任务窗内优先点击红底白字按钮（如 `一键发表评论`、`去完成`、`立即参与`）。
4. 检测到 `已参与/参与成功` 后退出。

主脚本：

- `/Users/zhuolinchen/Desktop/WeChatAIDrivenRE/scripts/ios_douyin_fudai_bot.py`

---

## 2. 环境部署

### 2.1 基础依赖

1. 安装 Xcode（含命令行工具）
2. 安装 Node.js（建议 LTS）
3. 安装 Appium 3
4. 安装 Python 3.10+

建议命令：

```bash
brew install node
npm i -g appium
appium driver install xcuitest
python3 -m pip install -r /Users/zhuolinchen/Desktop/WeChatAIDrivenRE/scripts/requirements_ios_fudai.txt
```

### 2.2 iOS 真机前置

1. iPhone 开启开发者模式，并信任当前 Mac。
2. Xcode 中使用你的 Apple 开发者账号登录。
3. 确认可用 Team ID（本次使用示例：`997XR67PRS`）。
4. 准备可签名的 WDA Bundle 前缀（本次使用示例：`com.see2see.livecontainer`）。

---

## 3. 快速运行

推荐使用封装脚本：

```bash
bash /Users/zhuolinchen/Desktop/WeChatAIDrivenRE/scripts/run_ios_douyin_fudai.sh
```

默认参数已包含：

- `UDID=00008030-00166DC93E50802E`
- `XCODE_ORG_ID=997XR67PRS`
- `UPDATED_WDA_BUNDLE_ID=com.see2see.livecontainer`
- `MAX_MINUTES=5`

你也可以直接运行 Python：

```bash
python3 /Users/zhuolinchen/Desktop/WeChatAIDrivenRE/scripts/ios_douyin_fudai_bot.py \
  --udid 00008030-00166DC93E50802E \
  --appium http://127.0.0.1:4723 \
  --max-minutes 5 \
  --xcode-org-id 997XR67PRS \
  --updated-wda-bundle-id com.see2see.livecontainer \
  --allow-provisioning-updates \
  --allow-provisioning-device-registration \
  --blocked-swipe-cooldown 3.0 \
  --open-retry-before-swipe 4
```

---

## 4. 关键策略（当前实现）

1. **无入口即切房**  
   单轮检测不到 `福袋/红包/抽奖`，立即上滑换房。

2. **任务窗红按钮优先**  
   点开福袋后优先识别并点击红色 CTA 形状按钮（红底白字常见样式）。

3. **文本任务次优先**  
   若红按钮识别不到，再点文本任务按钮（如 `一键发表评论`、`去完成`）。

4. **误点过滤**  
   屏蔽 `xx人已参与`、`共x份`、`剩余`、`开奖` 等状态文案。

5. **成功判定**  
   仅将明确 `已参与/参与成功` 且符合长度规则的文本作为成功信号。

---

## 5. 常见问题与排障

### 5.1 `xcodebuild failed with code 65`

高频原因：WDA 签名配置不完整，缺少 `*.xctrunner` profile。  
典型报错：

- `No profiles for 'com.see2see.livecontainer.xctrunner' were found`

处理方式：

1. 传入：
   - `--xcode-org-id`
   - `--updated-wda-bundle-id`
   - `--allow-provisioning-updates`
   - `--allow-provisioning-device-registration`
2. 确认 Apple 账号在 Xcode 可正常签名。

### 5.2 Appium 4723 端口未启动

```bash
appium -p 4723
```

### 5.3 只点到入口，不出现成功

可能是活动规则要求更多任务步骤或评论发送失败。可调：

1. 延长 `--max-minutes`
2. 调整 `--open-retry-before-swipe`
3. 在脚本中加入评论输入与发送动作（后续可扩展）

---

## 6. 调试结束与缓存清理

清理脚本：

```bash
bash /Users/zhuolinchen/Desktop/WeChatAIDrivenRE/scripts/cleanup_ios_fudai_debug.sh
```

此脚本会：

1. 停止 Appium 与脚本进程
2. 清空 `/tmp/appium.log` 与 `/tmp/appium_run.log`
3. 执行 WDA 的 `xcodebuild clean`

---

## 7. 相关文件

1. 主脚本：`/Users/zhuolinchen/Desktop/WeChatAIDrivenRE/scripts/ios_douyin_fudai_bot.py`
2. 依赖：`/Users/zhuolinchen/Desktop/WeChatAIDrivenRE/scripts/requirements_ios_fudai.txt`
3. 运行脚本：`/Users/zhuolinchen/Desktop/WeChatAIDrivenRE/scripts/run_ios_douyin_fudai.sh`
4. 清理脚本：`/Users/zhuolinchen/Desktop/WeChatAIDrivenRE/scripts/cleanup_ios_fudai_debug.sh`
