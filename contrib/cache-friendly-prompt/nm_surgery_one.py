
#!/usr/bin/env python3
"""nanoMuse 记忆与上下文外科手术 —— 单文件版 (v1.0.1)

不改 nanoMuse 一行源码:
  稳定头(人设/规则/工具/画像/常驻记忆/历史摘要) + 只追加的正文 + 易变尾(时钟/召回)塞进最后一条 user。

安装(在 nanoMuse 的 Linux 沙箱里):
    python3 nm_surgery_one.py --install
    python3 nm_surgery_one.py --check
    python3 nm_surgery_one.py --stats
    python3 nm_surgery_one.py --uninstall     # 完全回滚
    python3 nm_surgery_one.py --selftest      # 离线自测

只写 site-packages/nm_surgery_one.py 和 nm_surgery_one.pth。
记忆库 / 会话 / config.toml 一个字节都不碰。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sys
import sysconfig
import time
from pathlib import Path

VERSION = "1.0.1"
PTH_NAME = "nm_surgery_one.pth"
MODULE_NAME = "nm_surgery_one"

DEFAULTS = {
    "context_tokens": 96000,
    "keep_messages": 24,
    "compact_chunk": 12,
    "core_memories": 8,
    "recall_limit": 8,
    "recall_tokens": 1200,
    "time_in_tail": 1,
    "digest_items": 60,
    "digest_item_chars": 240,
}

TAIL_SECTIONS = {
    "What you remember about the user",
    "Active goals",
    "Calendar",
    "This phone",
    "The phone",
    "This computer's screen",
    "The user's other devices",
}

_TIME_LINE = re.compile(r"^\s*-\s*Current date/time:.*$", re.MULTILINE)
_MEMORY_BLOCK = re.compile(r"\n?## What you remember about the user.*?(?=\n## |\Z)", re.DOTALL)
_CJK = re.compile(r"[\u3400-\u9fff]")
_WORD = re.compile(r"[^\W_\u3400-\u9fff]+")

# ----------------------------------------------------------------- 配置 / 状态
def data_dir() -> Path:
    for env in ("NANOMUSE_DATA_DIR", "NM_DATA_DIR"):
        if os.environ.get(env):
            return Path(os.environ[env]).expanduser()
    home = Path.home()
    cands = [home / ".nanomuse", home / ".nanomuse" / "desktop", Path("/root/.nanomuse")]
    for c in cands:
        if (c / "config.toml").exists() or (c / "memory.db").exists():
            return c
    for c in cands:
        if c.exists():
            return c
    return home / ".nanomuse"


def settings() -> dict:
    cfg = dict(DEFAULTS)
    try:
        p = data_dir() / "nm_surgery.json"
        if p.exists():
            raw = json.loads(p.read_text("utf-8"))
            for k, v in raw.items():
                if k in cfg:
                    cfg[k] = v
    except Exception:
        pass
    for env, key in (("NM_CONTEXT_TOKENS", "context_tokens"), ("NM_KEEP_MESSAGES", "keep_messages"),
                     ("NM_COMPACT_CHUNK", "compact_chunk"), ("NM_CORE_MEMORIES", "core_memories"),
                     ("NM_RECALL_LIMIT", "recall_limit"), ("NM_RECALL_TOKENS", "recall_tokens")):
        if os.environ.get(env):
            try:
                cfg[key] = int(os.environ[env])
            except ValueError:
                pass
    if os.environ.get("NM_TIME_IN_TAIL") in ("0", "false", "False"):
        cfg["time_in_tail"] = 0
    return cfg


def state_file() -> Path:
    d = data_dir() / "nm_surgery"
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


_STATE: dict | None = None


def state() -> dict:
    global _STATE
    if _STATE is None:
        try:
            _STATE = json.loads(state_file().read_text("utf-8"))
        except Exception:
            _STATE = {}
        _STATE.setdefault("conversations", {})
        _STATE.setdefault("stats", {})
    return _STATE


def save_state() -> None:
    try:
        tmp = state_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(state(), ensure_ascii=False), "utf-8")
        os.replace(tmp, state_file())
    except Exception:
        pass


def conv_state(cid: str) -> dict:
    return state()["conversations"].setdefault(
        cid, {"anchor": 0, "digest": "", "core_fp": "", "core_text": "", "requests": 0}
    )


def bump(key: str, value: int = 1) -> None:
    st = state()["stats"]
    st[key] = st.get(key, 0) + value


# ----------------------------------------------------------------- 提示词切分
def split_system_prompt(text: str, time_in_tail: bool = True) -> tuple[str, str]:
    head_parts: list[str] = []
    tail_parts: list[str] = []
    time_line = ""
    if time_in_tail:
        m = _TIME_LINE.search(text or "")
        if m:
            time_line = m.group(0).strip()
            text = _TIME_LINE.sub("", text, count=1)
    blocks: list[tuple[str, list[str]]] = [("", [])]
    for line in (text or "").splitlines():
        if line.startswith("## "):
            blocks.append((line[3:].strip(), [line]))
        else:
            blocks[-1][1].append(line)
    for title, body in [(t, "\n".join(b)) for t, b in blocks]:
        if title and title in TAIL_SECTIONS:
            tail_parts.append(body.strip("\n"))
        else:
            head_parts.append(body.strip("\n"))
    if time_line:
        tail_parts.insert(0, time_line)
    head = "\n\n".join(p for p in head_parts if p.strip()).strip()
    tail = "\n\n".join(p for p in tail_parts if p.strip()).strip()
    return head, tail


def render_core_memory(items, limit: int) -> str:
    rows = []
    for item in list(items)[:limit]:
        body = " ".join(str(getattr(item, "content", item) or "").split())
        if body:
            rows.append("- " + body)
    return ("## Standing memory\n" + "\n".join(rows)) if rows else ""


def memory_fingerprint(items) -> str:
    h = hashlib.sha256()
    for item in items:
        h.update(str(getattr(item, "id", "")).encode("utf-8", "ignore"))
        h.update(b"\x1f")
        h.update(str(getattr(item, "content", item)).encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


# ----------------------------------------------------------------- 压缩摘要
DIGEST_HEADER = "## Earlier in this conversation (compact digest)"


def _role(m) -> str:
    r = getattr(m, "role", "")
    return str(getattr(r, "value", r) or "").lower()


def _text(m) -> str:
    c = getattr(m, "content", "")
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) if isinstance(p, dict) else str(getattr(p, "text", p)) for p in c)
    return " ".join(str(c or "").split())


def _tools(m) -> str:
    names = []
    for call in getattr(m, "tool_calls", None) or []:
        fn = getattr(call, "function", None)
        name = getattr(fn, "name", None) or getattr(call, "name", "")
        if name:
            names.append(str(name))
    return ",".join(names[:6])


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def summarize_block(messages, item_chars: int = 240) -> list[str]:
    rows = []
    for m in messages:
        role = _role(m)
        if role == "user":
            body = _clip(_text(m), item_chars)
            if body:
                rows.append("- 用户: " + body)
        elif role == "assistant":
            body = _clip(_text(m), item_chars)
            tools = _tools(m)
            if body and tools:
                rows.append("- 我: %s (调用 %s)" % (body, tools))
            elif body:
                rows.append("- 我: " + body)
            elif tools:
                rows.append("- 调用工具: " + tools)
    return rows


def merge_digest(old: str, new_rows, max_items: int = 60) -> str:
    old_rows, elided = [], 0
    for line in (old or "").splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("(更早"):
            elided = int("".join(ch for ch in line if ch.isdigit()) or 0)
            continue
        old_rows.append(line)
    rows = old_rows + list(new_rows)
    if len(rows) > max_items:
        elided += len(rows) - max_items
        rows = rows[len(rows) - max_items:]
    out = [DIGEST_HEADER]
    if elided:
        out.append("(更早的 %d 条对话已折叠,不再逐条保留)" % elided)
    out.extend(rows)
    return "\n".join(out)


# ----------------------------------------------------------------- 召回重排
def _tokens(text: str) -> set:
    toks = {w.casefold() for w in _WORD.findall(text or "") if len(w) > 1}
    cjk = "".join(_CJK.findall(text or ""))
    toks.update(cjk[i:i + 2] for i in range(len(cjk) - 1))
    if len(cjk) == 1:
        toks.add(cjk)
    return toks


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _age_days(stamp: str) -> float:
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 86400.0)
    except Exception:
        return 365.0


def _content(item) -> str:
    return " ".join(str(getattr(item, "content", item) or "").split())


def rank(candidates, query: str, limit: int):
    scored = []
    for item in candidates:
        body = _content(item)
        if not body:
            continue
        rel = _overlap(body, query)
        if rel <= 0:
            continue
        age = _age_days(getattr(item, "updated_at", "") or getattr(item, "created_at", ""))
        rec = 1.0 / (1.0 + math.log1p(age))
        cat = str(getattr(item, "category", "") or "").lower()
        cat_w = 1.0 if cat in ("profile", "identity", "person", "preference", "relationship") else 0.5
        scored.append((0.62 * rel + 0.23 * rec + 0.15 * cat_w, item))
    scored.sort(key=lambda p: -p[0])
    picked = []
    for _, item in scored:
        if any(_overlap(_content(item), _content(k)) >= 0.85 for k in picked):
            continue
        picked.append(item)
        if len(picked) >= limit:
            break
    return picked


def pick_core(memory, limit: int):
    if memory is None or limit <= 0:
        return []
    try:
        items = list(memory.all(limit=200) or [])
    except Exception:
        return []
    def key(item):
        cat = str(getattr(item, "category", "") or "").lower()
        return (0 if cat in ("profile", "identity", "person", "preference", "relationship") else 1,
                _age_days(getattr(item, "updated_at", "") or getattr(item, "created_at", "")))
    items.sort(key=key)
    return items[:limit]


def recall(memory, query: str, limit: int, budget_tokens: int):
    if memory is None or limit <= 0 or not query:
        return []
    cands = []
    try:
        cands.extend(memory.relevant(query, limit=max(limit * 4, 12)) or [])
    except Exception:
        pass
    if len(cands) < limit:
        try:
            cands.extend(memory.all(limit=120) or [])
        except Exception:
            pass
    out, used = [], 0
    for item in rank(cands, query, limit):
        cost = len(_content(item)) // 2 + 8
        if out and used + cost > budget_tokens:
            break
        out.append(item)
        used += cost
    return out


# ----------------------------------------------------------------- 请求组装
class _LiteMsg:
    def __init__(self, role, content=""):
        self.role, self.content = role, content
        self.tool_calls, self.images, self.meta = [], [], []

    def model_copy(self, update=None):
        clone = copy.copy(self)
        for k, v in (update or {}).items():
            setattr(clone, k, v)
        return clone


def _copy_with(m, **changes):
    if hasattr(m, "model_copy"):
        try:
            return m.model_copy(update=changes)
        except Exception:
            pass
    clone = copy.copy(m)
    for k, v in changes.items():
        try:
            setattr(clone, k, v)
        except Exception:
            return m
    return clone


def _system(text: str, template=None):
    try:
        from nanomuse.schema import Message
        return Message.system(text)
    except Exception:
        if template is not None:
            return _copy_with(template, role="system", content=text)
        return _LiteMsg("system", text)


def _estimate(messages, tools=None) -> int:
    try:
        from nanomuse.llm.budget import estimate_tokens
        return int(estimate_tokens(messages, tools))
    except Exception:
        pass
    total = 0
    for m in messages:
        body = _text(m)
        cjk = sum(1 for ch in body if "\u3400" <= ch <= "\u9fff")
        total += int(cjk / 1.6 + (len(body) - cjk) / 4) + 8
    return total


CTX_OPEN, CTX_CLOSE = "{{context}}", "{{/context}}"


def build_request(agent, system_prompt: str, tools=None):
    cfg = settings()
    cid = str(getattr(agent, "conversation_id", "") or "default")
    st = conv_state(cid)
    messages = list(getattr(agent, "messages", []) or [])
    template = messages[0] if messages and _role(messages[0]) == "system" else None

    head, tail = split_system_prompt(system_prompt, bool(cfg["time_in_tail"]))
    if not head:
        head = system_prompt

    memory = getattr(agent, "memory", None)
    core = pick_core(memory, int(cfg["core_memories"]))
    fp = memory_fingerprint(core)
    if st.get("core_fp") != fp:
        st["core_fp"] = fp
        st["core_text"] = render_core_memory(core, int(cfg["core_memories"]))
        save_state()

    anchor = int(st.get("anchor") or 0)
    if anchor > len(messages):
        anchor, st["digest"] = 0, ""
    window = messages[anchor:]
    digest = st.get("digest") or ""
    budget = int(cfg["context_tokens"])
    if budget and _estimate(window, tools) > budget:
        target = max(anchor, len(messages) - max(int(cfg["keep_messages"]), 8))
        while target < len(messages) and _role(messages[target]) != "user":
            target += 1
        if target - anchor >= int(cfg["compact_chunk"]) and target < len(messages):
            digest = merge_digest(digest, summarize_block(messages[anchor:target], int(cfg["digest_item_chars"])),
                                  int(cfg["digest_items"]))
            st["anchor"], st["digest"] = target, digest
            save_state()
            window = messages[target:]

    latest_user = ""
    for m in reversed(messages):
        if _role(m) == "user":
            latest_user = _text(m)
            break
    tail = _MEMORY_BLOCK.sub("", tail).strip()
    recalled = recall(memory, latest_user, int(cfg["recall_limit"]), int(cfg["recall_tokens"]))
    if recalled:
        rows = "\n".join("- " + _content(i) for i in recalled)
        tail = (tail + "\n\n" if tail else "") + "## What you remember about the user\n" + rows

    head_full = head
    if st.get("core_text"):
        head_full += "\n\n" + st["core_text"]
    if digest:
        head_full += "\n\n" + digest
    out = [_system(head_full, template)]

    limit_images = 4
    try:
        limit_images = int(getattr(agent.settings.agent, "max_context_images", 4) or 0)
    except Exception:
        pass
    seen, copies = 0, []
    for m in reversed(window):
        if getattr(m, "images", None):
            seen += 1
            if limit_images and seen > limit_images:
                copies.append(_copy_with(m, images=[]))
                continue
        copies.append(m)
    copies.reverse()
    out.extend(copies)

    if tail:
        for i in range(len(out) - 1, 0, -1):
            if _role(out[i]) == "user":
                out[i] = _copy_with(out[i], content="%s\n%s\n%s\n\n%s" % (CTX_OPEN, tail, CTX_CLOSE, _text(out[i])))
                break
        else:
            out.insert(1, _system(tail))

    st["requests"] = int(st.get("requests") or 0) + 1
    bump("requests")
    save_state()
    return out


# ----------------------------------------------------------------- 钩子
def _wrap_llm(agent) -> None:
    llm = getattr(agent, "llm", None)
    if llm is None or getattr(llm, "_nm_wrapped", False):
        return
    ask = getattr(llm, "ask", None)
    if ask is None:
        return

    async def wrapped(*args, **kwargs):
        response = await ask(*args, **kwargs)
        try:
            usage = getattr(response, "usage", None) or {}
            hit = int(usage.get("prompt_cache_hit_tokens") or 0)
            miss = int(usage.get("prompt_cache_miss_tokens") or 0)
            prompt = int(usage.get("prompt_tokens") or 0)
            if prompt or hit or miss:
                bump("provider_prompt_tokens", prompt)
                bump("provider_cache_hit", hit)
                bump("provider_cache_miss", miss)
                save_state()
        except Exception:
            pass
        return response

    try:
        llm.ask = wrapped
        llm._nm_wrapped = True
    except Exception:
        pass


def _patch_core(module) -> None:
    Agent = getattr(module, "Agent", None)
    if Agent is None or getattr(Agent, "_nm_patched", False):
        return

    def _request_context(self, system_prompt, tools=None):
        return build_request(self, system_prompt, tools)

    Agent._request_context = _request_context
    init = Agent.__init__

    def __init__(self, *args, **kwargs):
        init(self, *args, **kwargs)
        try:
            _wrap_llm(self)
        except Exception:
            pass

    Agent.__init__ = __init__
    Agent._nm_patched = True


class _Finder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "nanomuse.agent.core":
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec = find(fullname, path, target)
            if spec is None or spec.loader is None:
                continue
            original = spec.loader

            class _Loader:
                def create_module(self, spec_):
                    return original.create_module(spec_)

                def exec_module(self, module):
                    original.exec_module(module)
                    try:
                        _patch_core(module)
                    except Exception as exc:
                        sys.stderr.write("[nm_surgery] patch failed: %s\n" % exc)

                def __getattr__(self, name):
                    return getattr(original, name)

            spec.loader = _Loader()
            return spec
        return None


def install_hooks() -> None:
    if any(isinstance(f, _Finder) for f in sys.meta_path):
        return
    sys.meta_path.insert(0, _Finder())
    if "nanomuse.agent.core" in sys.modules:
        try:
            _patch_core(sys.modules["nanomuse.agent.core"])
        except Exception:
            pass


# ----------------------------------------------------------------- CLI
def site_packages() -> Path:
    override = os.environ.get("NM_SITE")
    if override:
        return Path(override)
    paths = sysconfig.get_paths()
    for key in ("purelib", "platlib"):
        if paths.get(key):
            return Path(paths[key])
    raise SystemExit("找不到 site-packages")


def cmd_install() -> int:
    site = site_packages()
    print("python       : %s (%s)" % (sys.executable, sys.version.split()[0]))
    print("site-packages: %s" % site)
    try:
        import nanomuse
        print("nanomuse     : %s @ %s" % (nanomuse.__version__, Path(nanomuse.__file__).parent))
    except Exception as exc:
        print("nanomuse     : 未安装 (%s)" % exc)
    target = site / (MODULE_NAME + ".py")
    src = Path(__file__).resolve()
    if target.exists() and target.resolve() != src:
        target.unlink()
    if target.resolve() != src:
        target.write_bytes(src.read_bytes())
    (site / PTH_NAME).write_text("import %s\n" % MODULE_NAME, encoding="utf-8")
    print("已安装       : %s" % target)
    print("已启用       : %s" % (site / PTH_NAME))
    print("下一步: 强停 nanoMuse 再打开,然后 python3 %s --check" % src.name)
    return 0


def cmd_uninstall() -> int:
    site = site_packages()
    for p in (site / (MODULE_NAME + ".py"), site / PTH_NAME):
        if p.exists():
            p.unlink()
            print("已删除 %s" % p)
    print("记忆库 / 会话 / config.toml 未被改动。")
    return 0


def cmd_check() -> int:
    site = site_packages()
    print("python       : %s" % sys.executable)
    print("site-packages: %s" % site)
    ok = (site / (MODULE_NAME + ".py")).exists() and (site / PTH_NAME).exists()
    print("补丁文件齐全 : %s" % ok)
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); import %s, nanomuse.agent.core as c;"
            "print('Agent mounted:', getattr(c.Agent, '_nm_patched', False));"
            "print('version:', %s.VERSION)" % (str(site), MODULE_NAME, MODULE_NAME))
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()[-1200:]
    if out:
        print(out)
    return 0 if (ok and proc.returncode == 0) else 1


def cmd_stats() -> int:
    cfg = settings()
    st = state()["stats"]
    hit, miss = st.get("provider_cache_hit", 0), st.get("provider_cache_miss", 0)
    print("== nm_surgery 统计 ==")
    print("数据目录      : %s" % (data_dir() / "nm_surgery"))
    print("请求数        : %s" % st.get("requests", 0))
    if hit or miss:
        print("缓存命中 tokens: %s   未命中: %s" % (hit, miss))
        print("缓存命中率    : %.1f%%" % (100.0 * hit / max(1, hit + miss)))
        eq = miss + hit * 0.1
        print("等效输入量    : %.0f tokens(按命中价≈1/10 折算,相对全未命中省 %.0f%%)"
              % (eq, (1 - eq / max(1, hit + miss)) * 100))
    else:
        print("还没有用量记录:确认已重启运行时,并聊过至少一轮。")
    for cid, row in list(state()["conversations"].items())[:10]:
        print("  会话 %-22s 锚点 %-4s 摘要 %-5s 字 请求 %s"
              % (cid[:22], row.get("anchor", 0), len(row.get("digest") or ""), row.get("requests", 0)))
    print("参数: %s" % json.dumps(cfg, ensure_ascii=False))
    return 0


def cmd_selftest() -> int:
    class M:
        def __init__(self, role, content=""):
            self.role, self.content, self.tool_calls, self.images = role, content, [], []

        def model_copy(self, update=None):
            c = copy.copy(self)
            for k, v in (update or {}).items():
                setattr(c, k, v)
            return c

    class A:
        def __init__(self):
            self.messages, self.memory, self.conversation_id = [], None, "selftest"
            self.settings = type("S", (), {"agent": type("X", (), {"max_context_images": 2})()})()

    a = A()
    for i in range(30):
        a.messages.append(M("user", "第%d轮用户请求" % i * 3))
        a.messages.append(M("assistant", "第%d轮做完" % i * 3))
    sysp = ("You are Muse. ## How you work\n- x\n## Context\n- Current date/time: 2026-11-15 22:00\n"
            "## Active goals\n- g1")
    out1 = build_request(a, sysp, None)
    out2 = build_request(a, sysp.replace("22:00", "22:01"), None)
    assert out1[0].content == out2[0].content, "稳定头不该随时钟变化"
    last_user = [m for m in out1 if m.role == "user"][-1]
    assert CTX_OPEN in last_user.content and "Current date/time" in last_user.content
    print("自测通过:头部稳定 %d 字,尾部注入正常,消息 %d 条" % (len(out1[0].content), len(out1)))
    return 0


def main(argv) -> int:
    if "--install" in argv:
        return cmd_install()
    if "--uninstall" in argv:
        return cmd_uninstall()
    if "--check" in argv:
        return cmd_check()
    if "--stats" in argv:
        return cmd_stats()
    if "--selftest" in argv:
        return cmd_selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
else:
    install_hooks()
