#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PY_SCRIPT="$ROOT/ios_douyin_fudai_bot.py"

# Defaults (can be overridden via environment variables)
UDID="${UDID:-00008030-00166DC93E50802E}"
APPIUM_URL="${APPIUM_URL:-http://127.0.0.1:4723}"
MAX_MINUTES="${MAX_MINUTES:-0}"
XCODE_ORG_ID="${XCODE_ORG_ID:-997XR67PRS}"
UPDATED_WDA_BUNDLE_ID="${UPDATED_WDA_BUNDLE_ID:-com.see2see.livecontainer}"
BLOCKED_SWIPE_COOLDOWN="${BLOCKED_SWIPE_COOLDOWN:-3.0}"
OPEN_RETRY_BEFORE_SWIPE="${OPEN_RETRY_BEFORE_SWIPE:-4}"
POST_SWIPE_WAIT="${POST_SWIPE_WAIT:-3.0}"
DRAW_COUNTDOWN_GRACE="${DRAW_COUNTDOWN_GRACE:-2.0}"
DRAW_POLL_INTERVAL="${DRAW_POLL_INTERVAL:-1.0}"
DRAW_RESULT_MAX_WAIT="${DRAW_RESULT_MAX_WAIT:-240}"
WDA_LAUNCH_TIMEOUT_MS="${WDA_LAUNCH_TIMEOUT_MS:-120000}"
WDA_CONNECTION_TIMEOUT_MS="${WDA_CONNECTION_TIMEOUT_MS:-120000}"

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
  --open-retry-before-swipe "$OPEN_RETRY_BEFORE_SWIPE" \
  --post-swipe-wait "$POST_SWIPE_WAIT" \
  --draw-countdown-grace "$DRAW_COUNTDOWN_GRACE" \
  --draw-poll-interval "$DRAW_POLL_INTERVAL" \
  --draw-result-max-wait "$DRAW_RESULT_MAX_WAIT" \
  --wda-launch-timeout-ms "$WDA_LAUNCH_TIMEOUT_MS" \
  --wda-connection-timeout-ms "$WDA_CONNECTION_TIMEOUT_MS"
