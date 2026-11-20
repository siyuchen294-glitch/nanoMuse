#!/bin/sh
# browser_setup3.sh —— 修正版:PRoot 里能真的跑起来
# 已知三个坑(实测):
#   1) TMPDIR 默认指向 /data/user/0/... (PRoot 里不存在) -> ProcessSingleton 失败,必须 TMPDIR=/tmp
#   2) 没有 /dev/shm -> 必须 --disable-dev-shm-usage
#   3) 写 MCP 配置用 node,不需要 python3
# 幂等:已装的包会秒过。用法: nohup sh /root/browser_setup3.sh > /root/browser_setup.log 2>&1 &
set -u
ts() { date +%H:%M:%S; }

echo "[$(ts)] == 0/7 准备目录"
mkdir -p /root/.chrome-profile /tmp /var/minis/shared
chmod 1777 /tmp 2>/dev/null || true

echo "[$(ts)] == 1/7 镜像(清华优先,失败不动现有的)"
if [ -f /etc/apk/repositories ] && ! grep -q mirrors.tuna /etc/apk/repositories 2>/dev/null; then
  cp /etc/apk/repositories /root/apk.repositories.bak 2>/dev/null
  sed -i 's#https\{0,1\}://dl-cdn.alpinelinux.org#https://mirrors.tuna.tsinghua.edu.cn#g' /etc/apk/repositories
  sed -i 's#http://mirrors.tuna.tsinghua.edu.cn#https://mirrors.tuna.tsinghua.edu.cn#g' /etc/apk/repositories
fi
cat /etc/apk/repositories 2>/dev/null | head -3

echo "[$(ts)] == 2/7 apk update"
apk update 2>&1 | tail -2

echo "[$(ts)] == 3/7 安装 chromium nodejs npm(大件)"
apk add --no-cache chromium nodejs npm 2>&1 | tail -4
CHROME=""
for c in /usr/bin/chromium-browser /usr/bin/chromium /usr/lib/chromium/chrome; do
  [ -x "$c" ] && CHROME="$c" && break
done
[ -z "$CHROME" ] && { echo "[$(ts)] !! 没找到 chromium"; exit 1; }
echo "[$(ts)]    $("$CHROME" --version 2>&1 | head -1)"

echo "[$(ts)] == 4/7 写启动脚本 /root/browser-start.sh(带 TMPDIR / HOME / --disable-dev-shm-usage)"
cat > /root/browser-start.sh <<EOF
#!/bin/sh
# 关键三行: HOME / TMPDIR / --disable-dev-shm-usage,缺一个就 Failed to create ProcessSingleton
export HOME=/root
export TMPDIR=/tmp
mkdir -p /tmp /root/.chrome-profile 2>/dev/null
pkill -f "remote-debugging-port=9222" 2>/dev/null
sleep 1
exec "$CHROME" --headless=new --no-sandbox --disable-dev-shm-usage --disable-gpu \\
  --disable-software-rasterizer --remote-debugging-port=9222 --remote-allow-origins=* \\
  --user-data-dir=/root/.chrome-profile --window-size=1280,900 about:blank
EOF
chmod +x /root/browser-start.sh

echo "[$(ts)] == 5/7 起 Chromium 并等 CDP"
nohup /root/browser-start.sh >/root/chrome.log 2>&1 &
i=0
while [ $i -lt 30 ]; do
  wget -qO- http://127.0.0.1:9222/json/version >/root/cdp.json 2>/dev/null && break
  i=$((i+1)); sleep 1
done
if [ -s /root/cdp.json ]; then echo "[$(ts)]    CDP OK"; head -c 160 /root/cdp.json; echo
else echo "[$(ts)] !! CDP 未就绪"; tail -15 /root/chrome.log; exit 1; fi

echo "[$(ts)] == 6/7 chrome-devtools-mcp"
npm config set registry https://registry.npmmirror.com >/dev/null 2>&1
command -v chrome-devtools-mcp >/dev/null || npm i -g chrome-devtools-mcp 2>&1 | tail -3
command -v chrome-devtools-mcp >/dev/null && echo "[$(ts)]    ok" || echo "[$(ts)] !! 失败(不影响前面已装好的)"

echo "[$(ts)] == 7/7 MCP 配置(node 写,不依赖 python3)"
node -e '
const fs=require("fs"),dir="/var/minis/mcp-servers",p=dir+"/servers.json";
try{fs.mkdirSync(dir,{recursive:true})}catch(e){}
let d={};try{d=JSON.parse(fs.readFileSync(p,"utf8"))}catch(e){d={}}
d.mcpServers=d.mcpServers||{};
d.mcpServers.browser={command:"chrome-devtools-mcp",args:["--browserUrl","http://127.0.0.1:9222"],
  note:"手机本地 Chromium(持久 profile):开页、点击、填表、截图、读文本、跑 JS",enabled:true,createdAt:Date.now()};
fs.writeFileSync(p,JSON.stringify(d,null,2));
console.log("   browser:",JSON.stringify(d.mcpServers.browser));
'

echo "[$(ts)] == DONE"
echo "    启动: sh /root/browser-start.sh &"
echo "    自检: wget -qO- http://127.0.0.1:9222/json/version"
