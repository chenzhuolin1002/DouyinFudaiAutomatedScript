#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/zhuolinchen/Desktop/WeChatAIDrivenRE"
PY_SCRIPT="$ROOT/scripts/ios_douyin_fudai_bot.py"

# Defaults (can be overridden via environment variables)
UDID="${UDID:-00008030-00166DC93E50802E}"
APPIUM_URL="${APPIUM_URL:-http://127.0.0.1:4723}"
MAX_MINUTES="${MAX_MINUTES:-5}"
XCODE_ORG_ID="${XCODE_ORG_ID:-997XR67PRS}"
UPDATED_WDA_BUNDLE_ID="${UPDATED_WDA_BUNDLE_ID:-com.see2see.livecontainer}"
BLOCKED_SWIPE_COOLDOWN="${BLOCKED_SWIPE_COOLDOWN:-3.0}"
OPEN_RETRY_BEFORE_SWIPE="${OPEN_RETRY_BEFORE_SWIPE:-4}"

if ! command -v appium >/dev/null 2>&1; then
  echo "appium not found. Install first: npm i -g appium"
  exit 1
fi

if ! lsof -iTCP:4723 -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "[run] starting appium on :4723"
  nohup appium -p 4723 > /tmp/appium_run.log 2>&1 &
  sleep 2
fi

echo "[run] start bot"
python3 "$PY_SCRIPT" \
  --udid "$UDID" \
  --appium "$APPIUM_URL" \
  --max-minutes "$MAX_MINUTES" \
  --xcode-org-id "$XCODE_ORG_ID" \
  --updated-wda-bundle-id "$UPDATED_WDA_BUNDLE_ID" \
  --allow-provisioning-updates \
  --allow-provisioning-device-registration \
  --blocked-swipe-cooldown "$BLOCKED_SWIPE_COOLDOWN" \
  --open-retry-before-swipe "$OPEN_RETRY_BEFORE_SWIPE"
