#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
context-lens / collect.py  (multi-host)

扫描本机所有已知 Agent 的会话日志，归一化成同一套"上下文账本"，
输出 data.js 给 index.html 消费。只读日志，不修改任何会话数据。

支持的宿主（自动探测，没有就跳过）：
  · WorkBuddy AI   ~/.workbuddy-ai/projects/<proj>/<session>.jsonl
  · Qwen Code      ~/.qwen/projects/<proj>/chats/<session>.jsonl
  · Claude Code    ~/.claude/projects/<proj>/<session>.jsonl
  · Codex CLI      ~/.codex/sessions/**/rollout-*.jsonl
"""
import json, os, glob, ast, collections, datetime, sqlite3, re, struct

# ── 可调参数 ────────────────────────────────────────────────
CONTEXT_WINDOW = 1_000_000          # 上下文窗口大小（按你的模型改）
PRICE = {"cache": 0.02, "input": 0.50, "output": 4.00}   # ¥/百万 token，估算用
MAX_REQUESTS = 600
MAX_BROWSER = 70
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_JS = os.path.join(HERE, "data.js")
HOME = os.path.expanduser("~")

CATS = ["host", "user", "assistant", "think", "targs", "tres"]


# ── 通用工具 ────────────────────────────────────────────────
def est(text):
    """粗略 token 估算：中日韩字符 ~1 token/字，其它 ~3.5 字符/token。"""
    if not text:
        return 0
    s = str(text)
    cjk = 0
    for ch in s:
        if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff":
            cjk += 1
    return int(cjk + (len(s) - cjk) / 3.5) + 1


def blocks(v):
    if isinstance(v, str):
        try:
            v = ast.literal_eval(v)
        except Exception:
            return [{"type": "text", "text": v}]
    return v if isinstance(v, list) else []


def text_of(v):
    out = []
    for b in blocks(v):
        if isinstance(b, dict):
            t = b.get("text")
            if isinstance(t, str) and t:
                out.append(t)
    return "\n".join(out)


def ts_of(d):
    try:
        return int(d.get("timestamp") or 0)
    except Exception:
        return 0


def iso_ms(s):
    if not s:
        return 0
    try:
        return int(datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def norm_comp(cum, actual):
    """把累计估算值补差/缩放到供应商实报的 input_tokens。"""
    total = sum(cum.values())
    comp = {k: float(v) for k, v in cum.items()}
    for c in CATS:
        comp.setdefault(c, 0.0)
    if actual <= 0:
        return comp
    if total <= 0:
        comp["host"] = float(actual)
        return comp
    if total <= actual:
        comp["host"] += actual - total
        return comp
    k = actual / total
    return {c: comp[c] * k for c in CATS}


def cost_of(cached, inp, out):
    uncached = max(0, inp - cached)
    return (cached * PRICE["cache"] + uncached * PRICE["input"] + out * PRICE["output"]) / 1e6


# ── 归一化构建器 ────────────────────────────────────────────
class Builder:
    """各宿主适配器只管把事件喂进来，账本在这里统一算。"""

    def __init__(self):
        self.cum = collections.Counter()
        self.requests = collections.deque(maxlen=MAX_REQUESTS)
        self.browser = collections.deque(maxlen=MAX_BROWSER * 3)
        self.calls = {}
        self.tools = collections.Counter()
        self.files = {"read": 0, "write": 0, "search": 0}
        self.commands = collections.deque(maxlen=40)
        self.t_model = self.t_tool = 0.0
        self.anchor = None          # 上一个"产出型"事件的 ts（算模型耗时用）
        self.step = 0
        self.turns = 0
        self.tin = self.tout = self.tcached = 0
        self.step_tool = collections.Counter()
        self.first_ts = self.last_ts = None
        self._cid = 0
        self.est = False            # 该会话是否有 token 是估算出来的
        self.est_mode = False       # 适配器显式开启：本宿主不上报 usage

    def _touch(self, ts):
        if not self.first_ts:
            self.first_ts = ts
        self.last_ts = ts

    def user(self, ts, text):
        self._touch(ts)
        self.turns += 1
        n = est(text)
        self.cum["user"] += n
        self.browser.append({"cat": "user", "label": "用户消息", "tok": n,
                             "step": self.step, "preview": (text or "")[:170]})
        self.anchor = ts

    def assistant(self, ts, text):
        self._touch(ts)
        n = est(text)
        self.cum["assistant"] += n
        if n:
            self.browser.append({"cat": "assistant", "label": "助手消息", "tok": n,
                                 "step": self.step, "preview": (text or "")[:170]})
        self.anchor = ts

    def think(self, ts, text):
        self._touch(ts)
        n = est(text)
        self.cum["think"] += n
        if n:
            self.browser.append({"cat": "think", "label": "模型思考", "tok": n,
                                 "step": self.step, "preview": (text or "")[:170]})
        # 思考不移动 anchor：模型耗时算到"上一次产出"为止

    def call(self, ts, name, args_text, usage, call_id=None):
        self._touch(ts)
        self.step += 1
        name = name or "?"
        self.tools[name] += 1
        a_txt = args_text if isinstance(args_text, str) else json.dumps(args_text, ensure_ascii=False)
        na = est(a_txt)
        self.cum["targs"] += na
        self.browser.append({"cat": "targs", "label": f"工具参数 · {name}", "tok": na,
                             "step": self.step, "preview": a_txt[:170]})

        if call_id is None:
            self._cid += 1
            call_id = f"_auto{self._cid}"
        self.calls[call_id] = (ts, name, self.step)

        model_ms = max(0, ts - self.anchor) if self.anchor else 0
        self.t_model += model_ms / 1000.0

        u = usage or {}
        i_tok = u.get("in")
        o_tok = u.get("out")
        c_tok = u.get("cached")
        if i_tok is None and self.est_mode:
            # 宿主不上报 usage：按字符密度估算（会话级标记 est）
            comp = norm_comp(self.cum, 0)
            i_tok = int(round(sum(comp.values())))
            self.est = True
        else:
            comp = norm_comp(self.cum, i_tok or 0)
        i_tok = int(i_tok or 0)
        o_tok = int(o_tok or 0)
        c_tok = int(c_tok or 0)
        self.tin += i_tok
        self.tout += o_tok
        self.tcached += c_tok
        self.requests.append({
            "i": self.step, "ts": ts, "in": i_tok, "out": o_tok, "cached": c_tok,
            "ms": model_ms, "toolMs": 0,
            "comp": {k: round(v, 1) for k, v in comp.items()},
        })
        self.anchor = ts

    def result(self, ts, name, out_text, call_id=None):
        self._touch(ts)
        got = self.calls.pop(call_id, None) if call_id is not None else None
        if got:
            dur = max(0.0, (ts - got[0]) / 1000.0)
            self.t_tool += dur
            self.step_tool[got[2]] += dur
        name = name or "?"
        nr = est(out_text)
        self.cum["tres"] += nr
        self.browser.append({"cat": "tres", "label": f"工具结果 · {name}", "tok": nr,
                             "step": self.step, "preview": (out_text or "")[:170]})
        low = name.lower()
        if low in ("read", "read_file", "readfile", "view"):
            self.files["read"] += 1
        elif low in ("write", "edit", "notebookedit", "write_file", "replace"):
            self.files["write"] += 1
        elif low in ("grep", "glob", "search_data", "websearch", "search"):
            self.files["search"] += 1
        self.anchor = ts

    def command(self, ts, cmd):
        self.commands.append({"cmd": str(cmd)[:160], "ts": ts})

    def extra_call(self, ts, name, args_text):
        """同一轮里的附加工具调用：只记工具/参数，不再产生一次"请求"。"""
        self._touch(ts)
        name = name or "?"
        self.tools[name] += 1
        a_txt = args_text if isinstance(args_text, str) else json.dumps(args_text, ensure_ascii=False)
        na = est(a_txt)
        self.cum["targs"] += na
        self.browser.append({"cat": "targs", "label": f"工具参数 · {name}", "tok": na,
                             "step": self.step, "preview": a_txt[:170]})

    def finish(self, meta):
        if not self.requests:
            return None
        reqs = list(self.requests)
        last = reqs[-1]
        wall = max(0.0, ((self.last_ts or 0) - (self.first_ts or 0)) / 1000.0)
        for r in reqs:
            r["toolMs"] = round(self.step_tool.get(r["i"], 0) * 1000)
        idle = max(0.0, wall - self.t_model - self.t_tool)
        title = meta.get("title")
        if not title:
            for b in self.browser:
                if b["cat"] == "user" and b["preview"].strip():
                    title = b["preview"].replace("\n", " ")[:44]
                    break
        return {
            "id": meta["id"],
            "host": meta["host"],
            "est": self.est,
            "title": title or meta["id"][:8],
            "cwd": meta.get("cwd") or "",
            "project": meta.get("project") or "",
            "startTs": self.first_ts or 0,
            "endTs": self.last_ts or 0,
            "wallSec": round(wall, 1),
            "turns": self.turns,
            "steps": self.step,
            "requests": reqs,
            "tools": dict(self.tools.most_common(12)),
            "toolCount": sum(self.tools.values()),
            "files": self.files,
            "commands": list(self.commands),
            "browser": list(self.browser)[-MAX_BROWSER:],
            "totals": {
                "in": self.tin, "out": self.tout, "cached": self.tcached,
                "ctx": last["in"], "window": CONTEXT_WINDOW,
                "cost": round(cost_of(self.tcached, self.tin, self.tout), 4),
            },
            "timing": {
                "model": round(self.t_model, 1),
                "tool": round(self.t_tool, 1),
                "idle": round(idle, 1),
            },
        }


# ── 适配器：WorkBuddy AI ────────────────────────────────────
def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def iter_jsonl(path):
    """流式读取，避免 60MB+ 的会话日志整份进内存。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def parse_workbuddy(path, host="workbuddy"):
    b = Builder()
    title = cwd = None
    any_ev = False
    for d in iter_jsonl(path):
        any_ev = True
        typ = d.get("type")
        ts = ts_of(d)
        if d.get("cwd") and not cwd:
            cwd = d["cwd"]
        if typ == "ai-title" and d.get("aiTitle"):
            title = d["aiTitle"]
        elif typ == "message":
            txt = text_of(d.get("content"))
            (b.user if d.get("role") == "user" else b.assistant)(ts, txt)
        elif typ == "reasoning":
            b.think(ts, text_of(d.get("rawContent") or d.get("content")))
        elif typ == "function_call":
            name = d.get("name") or "?"
            a = d.get("arguments")
            a_txt = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
            u = (d.get("message") or {}).get("usage") or {}
            b.call(ts, name, a_txt, {
                "in": u.get("input_tokens"), "out": u.get("output_tokens"),
                "cached": u.get("cache_read_input_tokens"),
            }, d.get("callId"))
            if name in ("Bash", "PowerShell"):
                try:
                    j = json.loads(a_txt)
                    b.command(ts, j.get("command") or j.get("description") or a_txt)
                except Exception:
                    b.command(ts, a_txt)
        elif typ == "function_call_result":
            out = d.get("output")
            o_txt = text_of(out) if isinstance(out, (list, str)) else text_of([out])
            b.result(ts, d.get("name"), o_txt, d.get("callId"))
    if not any_ev:
        return None
    return b.finish({"id": os.path.basename(path).rsplit(".jsonl", 1)[0], "host": host,
                     "title": title, "cwd": cwd, "project": os.path.basename(os.path.dirname(path))})


def parse_codebuddy(path):
    """CodeBuddy CLI 与 WorkBuddy 同源，日志格式一致。"""
    return parse_workbuddy(path, host="codebuddy")


# ── 适配器：Qwen Code ───────────────────────────────────────
def parse_qwen(path):
    evs = read_jsonl(path)
    if not evs:
        return None
    evs.sort(key=lambda d: iso_ms(d.get("timestamp")))
    b = Builder()
    cwd, model, title = None, None, None
    for d in evs:
        typ = d.get("type")
        ts = iso_ms(d.get("timestamp"))
        cwd = cwd or d.get("cwd")
        model = model or d.get("model")
        msg = d.get("message")
        if isinstance(msg, str):
            try:
                msg = ast.literal_eval(msg)
            except Exception:
                msg = {"parts": [{"text": msg}]}
        parts = (msg or {}).get("parts") if isinstance(msg, dict) else None
        parts = parts or []

        if typ == "user":
            b.user(ts, text_of(parts) or text_of(msg))
        elif typ == "assistant":
            u = d.get("usageMetadata") or {}
            calls = [p["functionCall"] for p in parts
                     if isinstance(p, dict) and p.get("functionCall")]
            name0 = (calls[0].get("name") if calls else "model") or "model"
            args0 = json.dumps(calls[0].get("args") or {}, ensure_ascii=False) if calls else text_of(parts)
            b.call(ts, name0, args0, {
                "in": u.get("promptTokenCount"), "out": u.get("candidatesTokenCount"),
                "cached": u.get("cachedContentTokenCount"),
            })
            for fc in calls[1:]:
                b.extra_call(ts, fc.get("name") or "?",
                             json.dumps(fc.get("args") or {}, ensure_ascii=False))
            if text_of(parts):
                b.assistant(ts, text_of(parts))
            th = u.get("thoughtsTokenCount") or 0
            if th:
                b.think(ts, "x" * int(th * 3.5))
        elif typ == "tool_result":
            r = d.get("toolCallResult") or {}
            b.result(ts, r.get("name") or "tool",
                     json.dumps(r.get("result") or r, ensure_ascii=False)[:4000], r.get("callId"))
    s = b.finish({"id": os.path.basename(path).rsplit(".jsonl", 1)[0], "host": "qwen",
                  "title": title, "cwd": cwd,
                  "project": os.path.basename(os.path.dirname(os.path.dirname(path)))})
    if s:
        s["model"] = model
    return s


# ── 适配器：Claude Code ─────────────────────────────────────
def parse_claude(path):
    evs = read_jsonl(path)
    if not evs:
        return None
    evs.sort(key=lambda d: iso_ms(d.get("timestamp")))
    b = Builder()
    cwd, title = None, None
    for d in evs:
        typ = d.get("type")
        ts = iso_ms(d.get("timestamp"))
        cwd = cwd or d.get("cwd")
        if typ == "summary" and d.get("summary"):
            title = d["summary"]
        m = d.get("message") or {}
        content = m.get("content")
        if typ == "user":
            if isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content):
                for x in content:
                    if isinstance(x, dict) and x.get("type") == "tool_result":
                        c = x.get("content")
                        b.result(ts, "tool", c if isinstance(c, str) else text_of(c), x.get("tool_use_id"))
            else:
                b.user(ts, content if isinstance(content, str) else text_of(content))
        elif typ == "assistant":
            u = m.get("usage") or {}
            texts, thinks, calls = [], [], []
            for x in (content if isinstance(content, list) else []):
                if not isinstance(x, dict):
                    continue
                if x.get("type") == "text":
                    texts.append(x.get("text") or "")
                elif x.get("type") == "thinking":
                    thinks.append(x.get("thinking") or "")
                elif x.get("type") == "tool_use":
                    calls.append(x)
            usage = {"in": u.get("input_tokens"), "out": u.get("output_tokens"),
                     "cached": u.get("cache_read_input_tokens")}
            if calls:
                b.call(ts, calls[0].get("name") or "?",
                       json.dumps(calls[0].get("input") or {}, ensure_ascii=False),
                       usage, calls[0].get("id"))
                for c in calls[1:]:
                    b.extra_call(ts, c.get("name") or "?",
                                 json.dumps(c.get("input") or {}, ensure_ascii=False))
            else:
                b.call(ts, "model", "\n".join(texts), usage)
            if thinks:
                b.think(ts, "\n".join(thinks))
            if texts:
                b.assistant(ts, "\n".join(texts))
    return b.finish({"id": os.path.basename(path).rsplit(".jsonl", 1)[0], "host": "claude",
                     "title": title, "cwd": cwd, "project": os.path.basename(os.path.dirname(path))})


# ── 适配器：Codex CLI ───────────────────────────────────────
def parse_codex(path):
    evs = read_jsonl(path)
    if not evs:
        return None
    b = Builder()
    cwd = None
    prev = {"in": 0, "out": 0, "cached": 0}
    for d in evs:
        typ = d.get("type")
        ts = iso_ms(d.get("timestamp"))
        payload = d.get("payload") or {}
        if typ == "session_meta":
            cwd = cwd or payload.get("cwd")
        elif typ == "event_msg" and payload.get("type") == "token_count":
            info = (payload.get("info") or {}).get("total_token_usage") or {}
            cur = {"in": info.get("input_tokens", 0), "out": info.get("output_tokens", 0),
                   "cached": info.get("cached_input_tokens", 0)}
            delta = {k: max(0, cur[k] - prev[k]) for k in cur}
            prev = cur
            if any(delta.values()):
                b.call(ts, "model", "", delta)
        elif typ == "response_item":
            pt = payload.get("type")
            if pt == "message":
                txt = text_of(payload.get("content"))
                (b.user if payload.get("role") == "user" else b.assistant)(ts, txt)
            elif pt == "function_call":
                b.extra_call(ts, payload.get("name") or "?", payload.get("arguments") or "")
            elif pt == "function_call_output":
                b.result(ts, "tool", payload.get("output") or "", payload.get("call_id"))
    return b.finish({"id": os.path.basename(path).rsplit(".jsonl", 1)[0], "host": "codex",
                     "title": None, "cwd": cwd, "project": os.path.basename(os.path.dirname(path))})


# ── 适配器：千问办公 QwenWork（SQLite） ─────────────────────
QWENWORK_DB = os.path.join(HOME, "AppData", "Roaming", "QwenWorkCN", "data", "agents.db")


def parse_qwenwork(path):
    """一个 sqlite 库里有多条 chat，返回会话列表。该宿主不上报 token，全部为估算。"""
    import sqlite3
    if not os.path.exists(path):
        return []
    out = []
    try:
        con = _connect_ro(path)
        con.row_factory = sqlite3.Row
        chats = con.execute("select * from chats where deleted_at is null").fetchall()
        for ch in chats:
            rows = con.execute(
                "select * from messages where chat_id=? order by sequence asc", (ch["id"],)
            ).fetchall()
            if not rows:
                continue
            b = Builder()
            b.est_mode = True          # 千问办公不上报 token
            cwd = ch["worktree_path"] or ""
            model = None
            for r in rows:
                ts = int(float(r["created_at"] or 0) * 1000)
                role = r["role"]
                try:
                    parts = json.loads(r["parts"] or "[]")
                except Exception:
                    parts = []
                try:
                    meta = json.loads(r["metadata"] or "{}")
                except Exception:
                    meta = {}
                model = model or meta.get("modelId")
                texts, thinks, tools = [], [], []
                for p in parts:
                    if not isinstance(p, dict):
                        continue
                    pt = p.get("type") or ""
                    if pt == "text":
                        texts.append(p.get("text") or "")
                    elif pt in ("tool-Thinking", "thinking", "reasoning"):
                        thinks.append(p.get("text") or p.get("input", {}).get("text", "")
                                      if isinstance(p.get("input"), dict) else p.get("text") or "")
                    elif pt.startswith("tool-"):
                        tools.append(p)
                if role == "user":
                    b.user(ts, "\n".join(texts))
                else:
                    tname = (tools[0].get("toolName") if tools else None) or "model"
                    targs = tools[0].get("input") if tools else None
                    b.call(ts, tname,
                           json.dumps(targs or {"text": "\n".join(texts)}, ensure_ascii=False),
                           {"in": None, "out": est("\n".join(texts)), "cached": 0})
                    for t in tools[1:]:
                        b.extra_call(ts, t.get("toolName") or "?",
                                     json.dumps(t.get("input") or {}, ensure_ascii=False))
                    for t in tools:
                        o = t.get("output") or t.get("result")
                        if o is not None:
                            b.result(ts, t.get("toolName") or "tool",
                                     o if isinstance(o, str) else json.dumps(o, ensure_ascii=False),
                                     t.get("toolCallId"))
                    if thinks:
                        b.think(ts, "\n".join(x for x in thinks if x))
                    if texts:
                        b.assistant(ts, "\n".join(texts))
            s = b.finish({"id": "qw-" + str(ch["id"]), "host": "qwenwork",
                          "title": ch["name"], "cwd": cwd,
                          "project": os.path.basename(cwd.rstrip("\\/")) or "QwenWork"})
            if s:
                s["model"] = model
                out.append(s)
        con.close()
    except Exception as e:
        print("  ! qwenwork:", e)
    return out


# ── 桌面端索引：~/.workbuddy-ai/workbuddy.db ───────────────
WORKBUDDY_DB = os.path.join(HOME, ".workbuddy-ai", "workbuddy.db")


_DB_COPY = {}   # (path, mtime, size) -> 临时副本路径


def _connect_ro(db_path):
    """只读打开。WAL 模式下若宿主进程占着 -shm，只读连接会报
    'unable to open database file'，此时复制一份到临时目录再读。"""
    import sqlite3, shutil, tempfile
    uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=3)
        con.execute("select count(*) from sqlite_master").fetchone()
        return con
    except Exception:
        pass
    st = os.stat(db_path)
    key = (db_path, st.st_mtime_ns, st.st_size)
    tmp = _DB_COPY.get(key)
    if not tmp or not os.path.exists(tmp):
        tmp = os.path.join(tempfile.gettempdir(), "lens_" + os.path.basename(db_path))
        for ext in ("", "-wal", "-shm"):
            src = db_path + ext
            if os.path.exists(src):
                try:
                    shutil.copy2(src, tmp + ext)
                except Exception:
                    pass
        _DB_COPY.clear()
        _DB_COPY[key] = tmp
    return sqlite3.connect(tmp)


def read_workbuddy_index(db_path=None):
    """桌面端会话索引：真实 context_window、当前占用 used、额度消耗 credits。
    这些是宿主的官方 token-meter 投影，比按 1M 估算准得多。"""
    db_path = db_path or WORKBUDDY_DB
    out = {}
    if not os.path.exists(db_path):
        return out
    try:
        con = _connect_ro(db_path)
        con.row_factory = sqlite3.Row
        for r in con.execute("select id,cwd,title,custom_title,model,context_window,"
                             "created_at,last_activity_at,deleted_at from sessions"):
            if r["deleted_at"]:
                continue
            out[r["id"]] = {
                "title": (r["custom_title"] or r["title"] or "").strip() or None,
                "cwd": r["cwd"], "model": r["model"],
                "window": r["context_window"],
                "createdAt": int(float(r["created_at"] or 0)),
                "lastAt": int(float(r["last_activity_at"] or 0)),
            }
        for r in con.execute("select session_id,used,size,credit_json from session_usage"):
            d = out.setdefault(r["session_id"], {})
            if r["used"]:
                d["used"] = int(float(r["used"]))
            if r["size"] and not d.get("window"):
                d["window"] = int(float(r["size"]))
            try:
                cr = json.loads(r["credit_json"] or "{}")
                d["credits"] = round(sum(float(v) for v in cr.values()
                                         if isinstance(v, (int, float))), 3)
            except Exception:
                pass
        con.close()
    except Exception as e:
        print(f"  ! workbuddy.db({db_path}): {e}")
    return out


def apply_index(s, m):
    """把官方投影合并进日志解析结果。"""
    t = s["totals"]
    if m.get("window"):
        t["window"] = int(m["window"])
    if m.get("used"):
        t["ctx"] = int(m["used"])
    if m.get("credits"):
        s["credits"] = m["credits"]
    if m.get("model"):
        s["model"] = m["model"]
    if m.get("title"):
        s["title"] = m["title"]
    s["indexed"] = True


def make_index_session(sid, m, host="workbuddyai"):
    """只在索引里、没有日志明细的会话。"""
    return {
        "id": sid, "host": host, "est": False, "indexOnly": True,
        "title": m.get("title") or sid[:8], "cwd": m.get("cwd") or "",
        "project": os.path.basename((m.get("cwd") or "").rstrip("\\/")) or "workbuddy",
        "startTs": m.get("createdAt") or 0, "endTs": m.get("lastAt") or 0,
        "wallSec": 0, "turns": 0, "steps": 0,
        "requests": [], "tools": {}, "toolCount": 0,
        "files": {"read": 0, "write": 0, "search": 0}, "commands": [], "browser": [],
        "totals": {"in": 0, "out": 0, "cached": 0,
                   "ctx": int(m.get("used") or 0),
                   "window": int(m.get("window") or CONTEXT_WINDOW), "cost": 0.0},
        "timing": {"model": 0, "tool": 0, "idle": 0},
        "credits": m.get("credits", 0),
        "model": m.get("model"),
    }


# ── 适配器：豆包工作 DoubaoWork ─────────────────────────────
DOUBAOWORK_DIR = os.path.join(HOME, "AppData", "Local", "DoubaoWork", "User Data", "Default",
                              ".doubaowork", "agent_mode", "workspace", ".sessions")
DOUBAOWORK_IDB = os.path.join(HOME, "AppData", "Local", "DoubaoWork", "User Data", "Default",
                              "IndexedDB", "chrome_doubaowork-chat_0.indexeddb.leveldb")

_RE_CTX = re.compile(rb'"system_prompt":(\d+),"messages":(\d+),"skills":(\d+),"tools":(\d+)')
_RE_WIN = re.compile(rb'size":(\d+)')
_RE_CONV = re.compile(rb'local_conversation_id_.{0,48}?(\d{10,})"', re.S)
_DB_CTX_CACHE = {"sig": None, "val": {}}
_DOUBAO_CTX_FILE = os.path.join(HERE, "doubao_ctx_cache.json")


def _load_doubao_ctx():
    try:
        with open(_DOUBAO_CTX_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_doubao_ctx(d):
    try:
        with open(_DOUBAO_CTX_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def read_doubao_context():
    """从豆包工作的 IndexedDB（LevelDB）里取官方 context_window_usage。
    返回 {conversation_id: {system,messages,skills,tools,total,window}}。

    ⚠️ 这个字段只在请求进行中出现，宿主会不断重写 IndexedDB，
    压实后旧版本就没了。所以这里做「边跑边抓 + 落盘累积」：
    每次扫描把新抓到的合并进 doubao_ctx_cache.json，抓不到就用旧值。
    """
    sig = []
    for p in glob.glob(os.path.join(DOUBAOWORK_IDB, "*.ldb")) + glob.glob(os.path.join(DOUBAOWORK_IDB, "*.log")):
        try:
            st = os.stat(p)
            sig.append((os.path.basename(p), st.st_mtime_ns, st.st_size))
        except Exception:
            pass
    sig = tuple(sorted(sig))
    if sig and _DB_CTX_CACHE["sig"] == sig:
        return _DB_CTX_CACHE["val"]

    acc = _load_doubao_ctx()          # 之前累积的
    fresh = 0
    if os.path.isdir(DOUBAOWORK_IDB):
        try:
            import leveldb as ldb
        except Exception as e:
            print("  ! leveldb module:", e)
            ldb = None
        entries = []
        if ldb:
            for p in glob.glob(os.path.join(DOUBAOWORK_IDB, "*.ldb")):
                try:
                    for ikey, val in ldb.read_sstable(p):
                        if len(ikey) >= 8 and b"context_window_usage" in val:
                            entries.append((int.from_bytes(ikey[-8:], "little") >> 8, val))
                except Exception:
                    continue
            for p in glob.glob(os.path.join(DOUBAOWORK_IDB, "*.log")):
                try:
                    for seq, _key, val in ldb.read_wal(p):
                        if b"context_window_usage" in val:
                            entries.append((seq, val))
                except Exception:
                    continue
        entries.sort(key=lambda x: -x[0])
        for seq, val in entries:
            m = _RE_CTX.search(val)
            c = _RE_CONV.search(val)
            if not m or not c:
                continue
            cid = c.group(1).decode()
            if cid in acc:
                continue
            w = _RE_WIN.search(val)
            d = {"system": int(m.group(1)), "messages": int(m.group(2)),
                 "skills": int(m.group(3)), "tools": int(m.group(4)),
                 "window": int(w.group(1)) if w else 0}
            d["total"] = d["system"] + d["messages"] + d["skills"] + d["tools"]
            acc[cid] = d
            fresh += 1
    if fresh:
        _save_doubao_ctx(acc)
    _DB_CTX_CACHE["sig"] = sig
    _DB_CTX_CACHE["val"] = acc
    return acc


def doubaowork_sig(session_dir):
    """会话目录的签名 = 里面所有 trajectory 的 (mtime,size) 汇总。"""
    trajs = glob.glob(os.path.join(session_dir, "agents", "*", "system", "trajectory.jsonl"))
    if not trajs:
        return (0, 0)
    sig = []
    for t in trajs:
        try:
            st = os.stat(t)
            sig.append((st.st_mtime_ns, st.st_size))
        except Exception:
            pass
    return (max(s[0] for s in sig) if sig else 0, sum(s[1] for s in sig))


def parse_doubaowork(session_dir):
    """豆包工作把每个 agent 的对话写在 agents/<id>/system/trajectory.jsonl，
    是 OpenAI 风格的 messages，但**不带 usage**，所以 token 一律估算。"""
    trajs = sorted(glob.glob(os.path.join(session_dir, "agents", "*", "system", "trajectory.jsonl")))
    if not trajs:
        return None
    b = Builder()
    b.est_mode = True
    first_user = None
    for t in trajs:
        try:
            mtime = int(os.path.getmtime(t) * 1000)
        except Exception:
            mtime = 0
        for d in iter_jsonl(t):
            role = d.get("role")
            content = d.get("content")
            txt = content if isinstance(content, str) else text_of(content)
            if role == "user":
                if txt.strip():
                    if not first_user:
                        first_user = txt.strip().replace("\n", " ")
                    b.user(mtime, txt)
            elif role == "assistant":
                calls = d.get("tool_calls") or []
                if calls:
                    fn = calls[0].get("function") or {}
                    args = fn.get("arguments")
                    b.call(mtime, fn.get("name") or "tool",
                           args if isinstance(args, str) else json.dumps(args, ensure_ascii=False),
                           {"in": None, "out": est(txt), "cached": 0})
                    for c in calls[1:]:
                        f2 = c.get("function") or {}
                        a2 = f2.get("arguments")
                        b.extra_call(mtime, f2.get("name") or "tool",
                                     a2 if isinstance(a2, str) else json.dumps(a2, ensure_ascii=False))
                else:
                    b.call(mtime, "model", "", {"in": None, "out": est(txt), "cached": 0})
                if txt.strip():
                    b.assistant(mtime, txt)
            elif role == "tool":
                b.result(mtime, "tool", txt)
    sid = os.path.basename(session_dir.rstrip("\\/"))
    # 标题取第一条用户消息（board.md 是模板，标题没信息量）
    return b.finish({"id": "dbw-" + sid, "host": "doubaowork",
                     "title": (first_user or "")[:44] or None,
                     "cwd": "", "project": "DoubaoWork"})


# ── 宿主注册表 ──────────────────────────────────────────────
# 注意：WorkBuddy（5.x，~/.workbuddy）与 WorkBuddy AI（~/.workbuddy-ai）是两个不同产品，
# 数据目录、索引库都是各自独立的一套。
HOSTS = [
    {"id": "workbuddy", "name": "WorkBuddy", "color": "#0EA5E9", "tokens": True,
     "glob": os.path.join(HOME, ".workbuddy", "projects", "*", "*.jsonl"),
     "db": os.path.join(HOME, ".workbuddy", "workbuddy.db"),
     "parse": parse_workbuddy, "home": "~/.workbuddy/projects"},
    {"id": "workbuddyai", "name": "WorkBuddy AI", "color": "#6366F1", "tokens": True,
     "glob": os.path.join(HOME, ".workbuddy-ai", "projects", "*", "*.jsonl"),
     "db": os.path.join(HOME, ".workbuddy-ai", "workbuddy.db"),
     "parse": parse_workbuddy, "home": "~/.workbuddy-ai/projects"},
    {"id": "codebuddy", "name": "CodeBuddy CLI", "color": "#8B5CF6", "tokens": True,
     "glob": os.path.join(HOME, ".codebuddy", "projects", "*", "*.jsonl"),
     "parse": parse_codebuddy, "home": "~/.codebuddy/projects"},
    {"id": "qwen", "name": "Qwen Code", "color": "#F59E0B", "tokens": True,
     "glob": os.path.join(HOME, ".qwen", "projects", "*", "chats", "*.jsonl"),
     "parse": parse_qwen, "home": "~/.qwen/projects"},
    {"id": "qwenwork", "name": "千问办公 QwenWork", "color": "#D97706", "tokens": False,
     "glob": QWENWORK_DB, "parse": parse_qwenwork, "home": "~/AppData/Roaming/QwenWorkCN/data"},
    {"id": "doubaowork", "name": "豆包工作 DoubaoWork", "color": "#2563EB", "tokens": False,
     "glob": os.path.join(DOUBAOWORK_DIR, "*"), "parse": parse_doubaowork,
     "sig": doubaowork_sig, "home": "~/.doubaowork/.../.sessions"},
    {"id": "claude", "name": "Claude Code", "color": "#EC4899", "tokens": True,
     "glob": os.path.join(HOME, ".claude", "projects", "*", "*.jsonl"),
     "parse": parse_claude, "home": "~/.claude/projects"},
    {"id": "codex", "name": "Codex CLI", "color": "#10B981", "tokens": True,
     "glob": os.path.join(HOME, ".codex", "sessions", "**", "rollout-*.jsonl"),
     "parse": parse_codex, "home": "~/.codex/sessions", "recursive": True},
]

# 已发现但拿不到 token 的 Agent（只做清点，不进统计）
PROBES = [
    {"name": "豆包 Doubao", "path": os.path.join(HOME, "AppData", "Roaming", "Doubao"),
     "count": "files", "note": "只有 public_config.json，对话全在云端，本地无记录"},
    {"name": "百度搭子 DuMate", "path": os.path.join(HOME, "AppData", "Local", "Programs", "DuMate"),
     "count": "files", "note": "已安装，未发现本地会话库"},
    {"name": "Qoder", "path": os.path.join(HOME, ".qoder-cn"),
     "count": "files", "note": "仅入口脚本"},
    {"name": "Cherry Studio", "path": os.path.join(HOME, ".cherrystudio"),
     "count": "files", "note": "仅配置，会话库未找到"},
    {"name": "ima copilot", "path": os.path.join(HOME, "AppData", "Local", "ima.copilot"),
     "count": "files", "note": "IM 消息库，非 agent 会话"},
    {"name": "Claude Desktop", "path": os.path.join(HOME, "AppData", "Local", "Claude"),
     "count": "files", "note": "桌面端，会话不在本地明文"},
]


def probe_all():
    out = []
    for p in PROBES:
        path = p["path"]
        exists = os.path.exists(path)
        n = 0
        if exists:
            try:
                if p["count"] == "dirs":
                    n = sum(1 for x in os.scandir(path) if x.is_dir())
                else:
                    n = sum(1 for _ in os.scandir(path))
            except Exception:
                n = 0
        out.append({"name": p["name"], "path": path.replace(HOME, "~").replace("\\", "/"),
                    "exists": exists, "count": n, "note": p["note"]})
    return out



def day_of(ts):
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")


def scan(cache=None, verbose=False):
    """扫一遍所有宿主。cache: {path: {'sig': (mtime_ns,size), 's': [sessions]}}
    只重新解析签名变化的文件，供实时服务复用。"""
    if cache is None:
        cache = {}
    sessions, host_info, rev_parts = [], [], []
    for h in HOSTS:
        files = glob.glob(h["glob"], recursive=h.get("recursive", False))
        found, ok, mine = len(files), 0, []
        for f in files:
            try:
                st = os.stat(f)
                sig = h["sig"](f) if h.get("sig") else (st.st_mtime_ns, st.st_size)
                rev_parts.append(f"{f}:{sig[0]}:{sig[1]}")
                hit = cache.get(f)
                if hit and hit["sig"] == sig:
                    got = hit["s"]
                else:
                    got = h["parse"](f)
                    if isinstance(got, dict):
                        got = [got]
                    got = got or []
                    cache[f] = {"sig": sig, "s": got}
            except Exception as e:
                if verbose:
                    print(f"  ! {h['id']} {os.path.basename(f)}: {e}")
                got = []
            for s in got:
                s["host"] = h["id"]
                s["est"] = bool(s.get("est"))
                mine.append(s)
                ok += 1
        sessions.extend(mine)

        # 豆包工作：从 IndexedDB 取官方上下文构成（实报）
        if h["id"] == "doubaowork":
            ctx = read_doubao_context()
            hit_n = 0
            for s in mine:
                sid = s["id"][4:] if s["id"].startswith("dbw-") else s["id"]
                c = ctx.get(sid)
                if not c:
                    continue
                s["realCtx"] = c
                if c["window"]:
                    s["totals"]["window"] = c["window"]
                if c["total"]:
                    s["totals"]["ctx"] = c["total"]
                hit_n += 1
            if verbose and hit_n:
                print(f"{'  官方上下文':<18} {hit_n}/{len(mine)} 个会话拿到 context_window_usage")

        # 该宿主的桌面端索引（真实窗口 / 占用 / 额度）
        db = h.get("db")
        if db and os.path.exists(db):
            try:
                st = os.stat(db)
                rev_parts.append(f"db:{db}:{st.st_mtime_ns}:{st.st_size}")
            except Exception:
                pass
            idx = read_workbuddy_index(db)
            have = set()
            for s in mine:
                have.add(s["id"])
                m = idx.get(s["id"])
                if m:
                    apply_index(s, m)
            extra = 0
            for sid, m in idx.items():
                if sid in have or not m.get("used"):
                    continue
                sessions.append(make_index_session(sid, m, h["id"]))
                extra += 1
            if verbose and extra:
                print(f"{'  索引补充':<18} +{extra} 个只有索引、无日志明细的会话")

        host_info.append({"id": h["id"], "name": h["name"], "color": h["color"],
                          "tokens": h.get("tokens", True),
                          "home": h["home"], "files": found, "sessions": ok,
                          "status": "ok" if ok else ("empty" if found else "absent")})
        if verbose:
            print(f"{h['name']:<18} files={found:<3} sessions={ok}")

    rev = str(hash(tuple(sorted(rev_parts))))
    return sessions, host_info, rev


def trim_for_output(sessions, full_n=40, keep_points=28):
    """输出瘦身：只给最近 full_n 个会话保留完整明细，其余降采样，
    否则几百个会话会把 data.js 撑到十几 MB。返回新列表，不改缓存对象。"""
    order = sorted(range(len(sessions)), key=lambda i: sessions[i]["endTs"], reverse=True)
    keep_full = set(order[:full_n])
    out = []
    for i, s in enumerate(sessions):
        if i in keep_full:
            out.append(s)
            continue
        rq = s["requests"]
        if len(rq) > keep_points:
            step = (len(rq) - 1) / (keep_points - 1)
            rq = [rq[int(round(k * step))] for k in range(keep_points)]
        v = dict(s)
        v["requests"] = rq
        v["browser"] = []
        v["commands"] = []
        v["downsampled"] = True
        out.append(v)
    return out


def build_data(sessions, host_info):
    sessions = sorted(sessions, key=lambda s: s["endTs"], reverse=True)
    sessions = trim_for_output(sessions)
    daily = {}

    def slot(day):
        return daily.setdefault(day, {"in": 0, "out": 0, "cached": 0, "cost": 0.0,
                                      "ids": set(), "hosts": {}})

    for s in sessions:
        for r in s["requests"]:
            d0 = day_of(r["ts"])
            if not d0:
                continue
            d = slot(d0)
            d["in"] += r["in"]
            d["out"] += r["out"]
            d["cached"] += r["cached"]
            d["cost"] += cost_of(r["cached"], r["in"], r["out"])
            d["hosts"][s["host"]] = d["hosts"].get(s["host"], 0) + r["in"]
        d0 = day_of(s["endTs"])
        if d0:
            slot(d0)["ids"].add(s["id"])

    workspaces = {}
    for s in sessions:
        key = s["cwd"] or s["project"] or "未知"
        w = workspaces.setdefault(key, {"path": key,
                                        "name": os.path.basename(key.rstrip("\\/")) or key,
                                        "sessions": 0, "in": 0, "hosts": set()})
        w["sessions"] += 1
        w["in"] += s["totals"]["in"]
        w["hosts"].add(s["host"])
    for w in workspaces.values():
        w["hosts"] = sorted(w["hosts"])

    return {
        "generatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "contextWindow": CONTEXT_WINDOW,
        "price": PRICE,
        "hosts": host_info,
        "probes": probe_all(),
        "active": sessions[0]["id"] if sessions else None,
        "daily": {k: {"in": v["in"], "out": v["out"], "cached": v["cached"],
                      "cost": round(v["cost"], 4), "sessions": len(v["ids"]),
                      "hosts": v["hosts"]} for k, v in sorted(daily.items())},
        "workspaces": sorted(workspaces.values(), key=lambda x: -x["sessions"]),
        "sessions": sessions,
    }


def main():
    sessions, host_info, rev = scan(verbose=True)
    data = build_data(sessions, host_info)
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("window.LENS_DATA = ")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    print(f"\n总计 {len(sessions)} 个会话 / {sum(1 for h in host_info if h['sessions'])} 个有数据的宿主"
          f" -> {OUT_JS} ({os.path.getsize(OUT_JS)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
