#!/usr/bin/env python3
"""nmgate —— 客户端无关的缓存友好网关(OpenAI 兼容,纯标准库)

手机端的对话脑在 APK 原生层,PRoot 里的 python3 摸不到它,装不了包内补丁;
app 只暴露 contextLimitTokens 一个开关。那就把手术做在**请求线上**:

  1. 把第一条 system 里的易变内容(时间、本轮召回、目标、日历、设备状态)
     挪到最后一条 user 消息里 → 前缀从第一个字节起就稳定,DeepSeek 缓存能命中。
  2. 重复灌入的同步片段与重复消息就地折叠,历史只追加不重排。
  3. 记账:从上游 usage 里取 prompt_cache_hit_tokens / prompt_cache_miss_tokens。

用法:
    python3 nmgate.py --serve      # 默认 127.0.0.1:8790,上游 https://api.deepseek.com
    python3 nmgate.py --stats
    python3 nmgate.py --selftest

客户端把 base_url 指向 http://<本机>:8790/v1,key 原样透传。
手机走 USB 时: adb reverse tcp:8790 tcp:8790,base_url 填 http://127.0.0.1:8790/v1
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.1.0"
CRLF = bytes([13, 10])
DEFAULTS = {
    "listen_host": "127.0.0.1",
    "listen_port": 8790,
    "upstream": "https://api.deepseek.com/v1",
    "time_in_tail": 1,
    "collapse_duplicates": 1,
    "dedupe_sync_blocks": 1,
    "move_volatile_sections": 1,
}
LOCK = threading.Lock()
SYNC_MARK = "[Meanwhile, in this same conversation"
VOLATILE_WORDS = ("remember", "memory", "goal", "calendar", "device", "phone", "screen", "记忆", "设备")


def home() -> Path:
    d = Path(os.environ.get("NMGATE_DIR") or (Path.home() / ".nanomuse" / "nmgate"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def config() -> dict:
    cfg = dict(DEFAULTS)
    p = home() / "config.json"
    if p.exists():
        try:
            raw = json.loads(p.read_text("utf-8"))
            cfg.update({k: v for k, v in raw.items() if k in cfg})
        except Exception:
            pass
    if os.environ.get("NMGATE_PORT"):
        try:
            cfg["listen_port"] = int(os.environ["NMGATE_PORT"])
        except ValueError:
            pass
    if os.environ.get("NMGATE_UPSTREAM"):
        cfg["upstream"] = os.environ["NMGATE_UPSTREAM"]
    return cfg


def stats_add(fields: dict) -> None:
    path = home() / "stats.json"
    with LOCK:
        try:
            data = json.loads(path.read_text("utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        agg = data.setdefault("totals", {})
        for k, v in fields.items():
            if isinstance(v, int):
                agg[k] = agg.get(k, 0) + v
            else:
                agg[k] = v
        agg["requests"] = agg.get("requests", 0) + 1
        agg["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        data.setdefault("recent", []).insert(0, dict(fields, at=agg["last_at"]))
        data["recent"] = data["recent"][:50]
        try:
            path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        except Exception:
            pass


# --------------------------------------------------------------- 手术
def _is_time_line(line: str) -> bool:
    low = line.strip().lower().lstrip("-* ")
    return low.startswith("current date") or low.startswith("current time") or low.startswith("today")


def split_system(text: str, move_sections: bool = True) -> tuple[str, str]:
    """稳定头 / 易变尾。抽走时间行;顶层 '## ' 小节的标题含易变词就整节后移。"""
    if not text:
        return "", ""
    head_lines: list[str] = []
    tail_bits: list[str] = []
    block: list[str] = []
    block_title = ""

    def flush():
        if not block:
            return
        body = chr(10).join(block).strip()
        if not body:
            return
        if move_sections and block_title and any(w in block_title for w in VOLATILE_WORDS):
            tail_bits.append(body)
        else:
            head_lines.append(body)

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            block = [line]
            block_title = line[3:].strip().lower()
            continue
        if _is_time_line(line):
            tail_bits.append(line.strip())
            continue
        block.append(line)
    flush()
    return chr(10).join(head_lines).strip(), chr(10).join(tail_bits).strip()


def surgery(messages: list, cfg: dict) -> tuple[list, dict]:
    out: list = []
    stats = {"moved_tail": 0, "dropped_dupes": 0, "dropped_sync": 0}
    seen: set = set()
    tail = ""

    for idx, msg in enumerate(messages):
        role = str(msg.get("role", ""))
        content = msg.get("content")
        if role == "system" and idx == 0 and isinstance(content, str):
            head, tail_part = split_system(content, bool(cfg.get("move_volatile_sections")))
            tail = tail_part if cfg.get("time_in_tail") else ""
            stats["moved_tail"] = 1 if tail else 0
            out.append(dict(msg, content=head))
            continue

        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        key = (role, text.strip()[:4000])
        if cfg.get("collapse_duplicates") and text.strip() and key in seen:
            stats["dropped_dupes"] += 1
            continue
        if cfg.get("dedupe_sync_blocks") and SYNC_MARK in text and text.strip() in seen:
            stats["dropped_sync"] += 1
            continue
        seen.add(key)
        out.append(msg)

    if tail:
        for i in range(len(out) - 1, -1, -1):
            if str(out[i].get("role")) == "user" and isinstance(out[i].get("content"), str):
                out[i] = dict(out[i], content="{{context}}" + chr(10) + tail + chr(10) + "{{/context}}" + chr(10) + chr(10) + out[i]["content"])
                break
        else:
            out.append({"role": "user", "content": "{{context}}" + chr(10) + tail + chr(10) + "{{/context}}"})
    return out, stats


# --------------------------------------------------------------- 转发
def _upstream_request(payload: bytes, headers: dict, upstream: str):
    req = urllib.request.Request(upstream.rstrip("/") + "/chat/completions", data=payload, method="POST")
    for k, v in headers.items():
        if k.lower() in ("host", "content-length", "accept-encoding", "connection"):
            continue
        req.add_header(k, v)
    return req


def forward(payload: bytes, headers: dict, upstream: str):
    try:
        with urllib.request.urlopen(_upstream_request(payload, headers, upstream), timeout=300) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except Exception as exc:
        return 502, {"content-type": "application/json"}, json.dumps({"error": {"message": "nmgate: " + str(exc)}}).encode()


def usage_of(body: bytes) -> dict:
    out = {}
    try:
        usage = (json.loads(body.decode("utf-8", "ignore")).get("usage") or {})
        for src, dst in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"),
                         ("prompt_cache_hit_tokens", "cache_hit"), ("prompt_cache_miss_tokens", "cache_miss")):
            if isinstance(usage.get(src), int):
                out[dst] = usage[src]
    except Exception:
        pass
    return out


def usage_from_sse(text: str) -> dict:
    idx = text.rfind('"usage"')
    if idx < 0:
        return {}
    brace = text.find("{", idx)
    if brace < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[brace:])
    except Exception:
        return {}
    out = {}
    for src, dst in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"),
                     ("prompt_cache_hit_tokens", "cache_hit"), ("prompt_cache_miss_tokens", "cache_miss")):
        if isinstance(obj.get(src), int):
            out[dst] = obj[src]
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cfg = DEFAULTS

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            if k.lower() in ("content-length", "transfer-encoding", "connection", "content-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        if self.path.startswith("/stats"):
            p = home() / "stats.json"
            return self._send(200, p.read_bytes() if p.exists() else b"{}",
                              {"content-type": "application/json; charset=utf-8"})
        if self.path.startswith("/health"):
            return self._send(200, json.dumps({"ok": True, "version": VERSION, "cfg": self.cfg}).encode(),
                              {"content-type": "application/json"})
        if self.path.startswith("/v1/"):
            try:
                req = urllib.request.Request(self.cfg["upstream"].rstrip("/") + self.path[3:],
                                             headers={k: v for k, v in self.headers.items()
                                                      if k.lower() == "authorization"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return self._send(resp.status, resp.read(), dict(resp.headers))
            except Exception as exc:
                return self._send(502, json.dumps({"error": {"message": str(exc)}}).encode(),
                                  {"content-type": "application/json"})
        return self._send(404, b'{"error":"not found"}', {"content-type": "application/json"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not self.path.rstrip("/").endswith("chat/completions"):
            return self.do_GET()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._send(400, b'{"error":"bad json"}', {"content-type": "application/json"})

        rewritten, surg = surgery(body.get("messages") or [], self.cfg)
        body["messages"] = rewritten
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {k: v for k, v in self.headers.items()}
        model = str(body.get("model", ""))

        if body.get("stream"):
            return self._stream(payload, headers, model, surg)

        code, up_headers, resp = forward(payload, headers, self.cfg["upstream"])
        stats_add(dict(usage_of(resp), model=model, **surg))
        return self._send(code, resp, up_headers)

    def _stream(self, payload: bytes, headers: dict, model: str, surg: dict):
        try:
            resp = urllib.request.urlopen(_upstream_request(payload, headers, self.cfg["upstream"]), timeout=300)
        except urllib.error.HTTPError as exc:
            return self._send(exc.code, exc.read(), dict(exc.headers or {}))
        except Exception as exc:
            return self._send(502, json.dumps({"error": {"message": str(exc)}}).encode(),
                              {"content-type": "application/json"})

        self.send_response(getattr(resp, "status", 200))
        self.send_header("content-type", resp.headers.get("content-type") or "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        tail = ""
        while True:
            try:
                chunk = resp.read(1024)
            except Exception:
                break
            if not chunk:
                break
            tail = (tail + chunk.decode("utf-8", "ignore"))[-16384:]
            try:
                self.wfile.write(("%x" % len(chunk)).encode() + CRLF + chunk + CRLF)
                self.wfile.flush()
            except Exception:
                break
        try:
            self.wfile.write(b"0" + CRLF + CRLF)
            self.wfile.flush()
        except Exception:
            pass
        stats_add(dict(usage_from_sse(tail), model=model, **surg))


def serve() -> int:
    cfg = config()
    Handler.cfg = cfg
    srv = ThreadingHTTPServer((cfg["listen_host"], cfg["listen_port"]), Handler)
    print("nmgate " + VERSION + "  http://" + cfg["listen_host"] + ":" + str(cfg["listen_port"]) + "/v1  ->  " + cfg["upstream"])
    print("账本: " + str(home() / "stats.json"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def show_stats() -> int:
    p = home() / "stats.json"
    if not p.exists():
        print("还没有请求经过网关。")
        return 0
    t = json.loads(p.read_text("utf-8")).get("totals", {})
    hit, miss = t.get("cache_hit", 0), t.get("cache_miss", 0)
    print("== nmgate 账本 ==")
    print("请求数        : %s" % t.get("requests", 0))
    print("输入 tokens   : %s" % t.get("prompt_tokens", 0))
    print("输出 tokens   : %s" % t.get("completion_tokens", 0))
    if hit or miss:
        print("命中 / 未命中 : %s / %s   命中率 %.1f%%" % (hit, miss, 100.0 * hit / max(1, hit + miss)))
        eq = miss + hit * 0.1
        print("等效输入量    : %.0f(相对全未命中省 %.0f%%)" % (eq, (1 - eq / max(1, hit + miss)) * 100))
    print("折叠: 重复消息 %s 条 / 同步块 %s 次 / 尾部迁移 %s 次"
          % (t.get("dropped_dupes", 0), t.get("dropped_sync", 0), t.get("moved_tail", 0)))
    print("最近: " + str(t.get("last_at", "-")))
    return 0


def selftest() -> int:
    seen = {}

    class Up(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            seen["body"] = json.loads(self.rfile.read(n).decode())
            body = json.dumps({"id": "x", "object": "chat.completion",
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 100, "completion_tokens": 5,
                                         "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class UpSSE(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            self.rfile.read(n)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            pieces = ['data: {"choices":[{"delta":{"content":"he"}}]}' + chr(10) + chr(10),
                      'data: {"choices":[{"delta":{"content":"llo"}}]}' + chr(10) + chr(10),
                      'data: {"usage":{"prompt_tokens":50,"prompt_cache_hit_tokens":40,"prompt_cache_miss_tokens":10}}' + chr(10) + chr(10),
                      "data: [DONE]" + chr(10) + chr(10)]
            for text in pieces:
                blob = text.encode()
                self.wfile.write(("%x" % len(blob)).encode() + CRLF + blob + CRLF)
                self.wfile.flush()
            self.wfile.write(b"0" + CRLF + CRLF)
            self.wfile.flush()

    config_home = home()
    honest = config_home / "stats.json"
    before = honest.read_text("utf-8") if honest.exists() else ""

    up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    Handler.cfg = dict(DEFAULTS, upstream="http://127.0.0.1:%d" % up.server_address[1])
    gw = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/v1/chat/completions" % gw.server_address[1]

    system = ("You are Muse." + chr(10) + "## How you work" + chr(10) + "- act" + chr(10) +
              "## Context" + chr(10) + "- Current date/time: 2026-11-15 23:00 (+0800)" + chr(10) +
              "- Workspace: /root" + chr(10) + "## What you remember about the user" + chr(10) + "- likes tea" + chr(10))
    base = [{"role": "system", "content": system}, {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "答一"}]
    heads = []
    for i in range(3):
        msgs = base + ([{"role": "user", "content": "第二轮"}, {"role": "assistant", "content": "答二"}] if i else [])
        payload = json.dumps({"model": "deepseek-flash", "messages": msgs}).encode()
        req = urllib.request.Request(url, data=payload, headers={"content-type": "application/json",
                                                                "authorization": "Bearer test"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200
        got = seen["body"]["messages"]
        assert "Current date/time" not in got[0]["content"], got[0]["content"][:150]
        assert "likes tea" not in got[0]["content"]
        last_user = [m for m in got if m["role"] == "user"][-1]
        assert "Current date/time" in last_user["content"] and "{{context}}" in last_user["content"]
        heads.append(got[0]["content"])
    assert len(set(heads)) == 1, "稳定头在三轮里不一致"

    # 重复消息折叠
    dup = [{"role": "system", "content": system}, {"role": "user", "content": "hi"},
           {"role": "user", "content": "hi"}, {"role": "user", "content": "hi"}]
    payload = json.dumps({"model": "deepseek-flash", "messages": dup}).encode()
    req = urllib.request.Request(url, data=payload, headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()
    assert len([m for m in seen["body"]["messages"] if m["role"] == "user"]) == 1

    # 流式
    up2 = ThreadingHTTPServer(("127.0.0.1", 0), UpSSE)
    threading.Thread(target=up2.serve_forever, daemon=True).start()
    Handler.cfg = dict(DEFAULTS, upstream="http://127.0.0.1:%d" % up2.server_address[1])
    gw2 = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=gw2.serve_forever, daemon=True).start()
    payload = json.dumps({"model": "deepseek-flash", "stream": True,
                          "messages": [{"role": "system", "content": system}, {"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % gw2.server_address[1],
                                 data=payload, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode()
    assert "he" in raw and "llo" in raw and raw.count("data:") >= 3, raw[:300]

    if honest.exists():
        honest.write_text(before or "{}", "utf-8")
    print("nmgate 自测通过:稳定头三轮一致 / 易变尾迁移 / 重复折叠 / 流式逐块透传 / usage 记账")
    return 0


def main(argv) -> int:
    if "--serve" in argv:
        return serve()
    if "--stats" in argv:
        return show_stats()
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
