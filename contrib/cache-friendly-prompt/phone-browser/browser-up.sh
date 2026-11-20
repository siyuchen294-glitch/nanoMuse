#!/bin/sh
# browser-up.sh v3 —— 一条命令拉起 Xvfb + 非 headless Chromium + x11vnc(幂等)
# 用法: sh /root/browser-up.sh
# 之后:手机装任意 VNC 客户端连 127.0.0.1:5900(必须在手机上连;电脑走 adb forward 到不了,见下)
set -u
PORT=9333
RFB=5900
PROFILE=/root/.chrome-profile-x
CHROME=""
for c in /usr/bin/chromium-browser /usr/bin/chromium /usr/lib/chromium/chrome; do
  [ -x "$c" ] && CHROME="$c" && break
done
[ -z "$CHROME" ] && { echo "!! 没 chromium"; exit 1; }

alive() { wget -qO- -T 2 "http://127.0.0.1:$1/json/version" >/dev/null 2>&1; }

mkdir -p "$PROFILE" /tmp
export HOME=/root TMPDIR=/tmp

# 1) Xvfb
if pgrep -f "Xvfb :99" >/dev/null 2>&1; then
  echo "1) Xvfb 已在跑"
else
  echo "1) 启动 Xvfb :99"
  if command -v setsid >/dev/null 2>&1; then
    setsid Xvfb :99 -screen 0 1280x900x24 >/root/xvfb.log 2>&1 &
  else
    nohup Xvfb :99 -screen 0 1280x900x24 >/root/xvfb.log 2>&1 &
  fi
  sleep 2
  pgrep -f "Xvfb :99" >/dev/null 2>&1 && echo "   ok" || { echo "   !! 失败"; tail -5 /root/xvfb.log; }
fi

# 2) Chromium(非 headless)
if alive $PORT; then
  echo "2) Chromium 已在跑(:$PORT)"
else
  echo "2) 启动 Chromium(:$PORT, DISPLAY=:99)"
  export DISPLAY=:99
  if command -v setsid >/dev/null 2>&1; then
    setsid "$CHROME" --no-sandbox --disable-dev-shm-usage --disable-gpu \
      --remote-debugging-port=$PORT --remote-allow-origins=* \
      --user-data-dir="$PROFILE" --window-size=1280,900 \
      --no-first-run --no-default-browser-check about:blank >/root/chrome-x.log 2>&1 &
  else
    nohup "$CHROME" --no-sandbox --disable-dev-shm-usage --disable-gpu \
      --remote-debugging-port=$PORT --remote-allow-origins=* \
      --user-data-dir="$PROFILE" --window-size=1280,900 \
      --no-first-run --no-default-browser-check about:blank >/root/chrome-x.log 2>&1 &
  fi
  i=0
  while [ $i -lt 30 ]; do alive $PORT && break; i=$((i+1)); sleep 1; done
  alive $PORT && echo "   CDP ok" || { echo "   !! CDP 未就绪"; tail -12 /root/chrome-x.log; }
fi

# 3) x11vnc —— PRoot 没有 SysV 共享内存,必须 -noshm -noxdamage,否则 shmget 失败直接退出
if pgrep -f "x11vnc -display :99" >/dev/null 2>&1; then
  echo "3) x11vnc 已在跑(:$RFB)"
else
  echo "3) 启动 x11vnc(:$RFB)"
  if ! command -v x11vnc >/dev/null 2>&1; then
    apk add --no-cache x11vnc >/root/apk-vnc.log 2>&1 || { echo "   !! apk 失败"; tail -5 /root/apk-vnc.log; }
  fi
  if command -v setsid >/dev/null 2>&1; then
    setsid x11vnc -display :99 -localhost -nopw -forever -shared -noshm -noxdamage -rfbport $RFB >/root/vnc.log 2>&1 &
  else
    nohup x11vnc -display :99 -localhost -nopw -forever -shared -noshm -noxdamage -rfbport $RFB >/root/vnc.log 2>&1 &
  fi
  sleep 2
  pgrep -f "x11vnc -display :99" >/dev/null 2>&1 && echo "   ok" || { echo "   !! 失败"; tail -8 /root/vnc.log; }
fi

echo
echo "就绪:"
echo "  VNC   127.0.0.1:$RFB  (必须在手机的 VNC 客户端里连;-localhost 生效,电脑 adb forward 到不了)"
echo "  CDP   http://127.0.0.1:$PORT  (CDP HTTP 端点在本实例上不应答,属已知现象;操作用 MCP 工具)"
