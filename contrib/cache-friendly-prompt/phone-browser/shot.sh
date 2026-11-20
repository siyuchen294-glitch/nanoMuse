#!/bin/sh
# shot.sh (v2) —— 按实测通过的调用方式重写
#   1) new_page 开页 -> 2) 取 pageId -> 3) take_screenshot 内联 -> 4) base64 解码落盘
# 用法: sh /root/shot.sh <url> <输出路径>
# 注意: 沙箱里没有 curl,一律用 wget;evaluate_script 的参数名是 function 不是 expression
set -u
URL="${1:-about:blank}"
OUT="${2:-/var/minis/shared/shot-$(date +%H%M%S).png}"
mkdir -p /tmp /var/minis/shared

echo "1) new_page $URL"
minis-mcp-cli call browser new_page --input "{\"url\":\"$URL\"}" >/tmp/np.json 2>&1
head -c 160 /tmp/np.json; echo

PAGEID=$(node -e 'const fs=require("fs");let raw="";try{raw=fs.readFileSync("/tmp/np.json","utf8")}catch(e){}const m=raw.match(/"pageId"\s*:\s*"?([A-Za-z0-9_-]+)/);process.stdout.write(m?m[1]:"")')
echo "   pageId=${PAGEID:-<none>}"

echo "2) take_screenshot(内联)"
if [ -n "$PAGEID" ]; then
  minis-mcp-cli call browser take_screenshot --input "{\"pageId\":\"$PAGEID\",\"format\":\"png\"}" >/tmp/shot.json 2>&1
else
  minis-mcp-cli call browser take_screenshot --input '{"format":"png"}' >/tmp/shot.json 2>&1
fi
grep -q '"data"' /tmp/shot.json 2>/dev/null || { echo "!! 没拿到图片,响应:"; head -c 300 /tmp/shot.json; exit 1; }

echo "3) 解码写盘"
node -e '
const fs=require("fs");
const raw=fs.readFileSync("/tmp/shot.json","utf8");
const m=raw.match(/"data"\s*:\s*"([A-Za-z0-9+/=]+)"/);
if(!m){console.error("无 base64");process.exit(1)}
fs.writeFileSync(process.argv[1],Buffer.from(m[1],"base64"));
console.log("   saved",process.argv[1],(Buffer.from(m[1],"base64").length/1024).toFixed(0)+"KB");
' "$OUT"
