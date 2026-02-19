#!/usr/bin/env bash
set -euo pipefail

echo "[cleanup] stop bot/appium processes"
pkill -f ios_douyin_fudai_bot.py || true
pkill -f "appium -p 4723" || true
pkill -f appium || true

echo "[cleanup] truncate appium temp logs"
: > /tmp/appium.log 2>/dev/null || true
: > /tmp/appium_run.log 2>/dev/null || true

WDA_PROJECT="/Users/zhuolinchen/.appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj"
if [ -d "$WDA_PROJECT" ]; then
  echo "[cleanup] xcodebuild clean webdriveragent"
  xcodebuild -project "$WDA_PROJECT" -scheme WebDriverAgentRunner clean -quiet || true
fi

echo "[cleanup] done"
