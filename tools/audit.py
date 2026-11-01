#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
context-lens / tools/audit.py —— 自检工具箱（全程只读，不写任何宿主目录）

子命令：
  tokens              独立复核各宿主 token/会话计数，对照 collect.py 的解析结果
  daily               校验 data.js 的每日时间桶覆盖率（应为 100%）与编队数据
  probes              深挖「只清点」应用与候选宿主的本地数据目录结构
  ldb <dir> [关键词]  用 leveldb.py 试读一个 IndexedDB / Local Storage 目录

典型用法：
  python tools/audit.py tokens -n 3       # 每宿主抽查最近 3 个会话
  python tools/audit.py daily             # 改动聚合逻辑后验证覆盖率
  python tools/audit.py probes --depth 4  # 接新宿主前摸清本地存储结构
  python tools/audit.py ldb "...indexeddb.leveldb" conversation

只打印文件清单、条数与 usage 数值等量级信息（ldb 试读的值截断到
100 字符，仅作本地调试）；不导出、不落盘任何会话正文。
"""
import argparse
import collections
import datetime
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根
sys.path.insert(0, HERE)
import collect        # noqa: E402  只借用解析器与常量，全程只读
import leveldb        # noqa: E402

HOME = os.path.expanduser("~")
SKIP_DIRS = {"Cache", "Code Cache", "GPUCache", "ShaderCache", "DawnCache", "DawnGraphiteCache",
             "DawnWebGPUCache", "blob_storage", "Crashpad", "logs", "Log", "Logs", "temp", "Temp",
             "node_modules", ".git", "GrShaderCache", "Crash Reports", "Service Worker",
             "VideoDecodeStats", "imainfo"}
INTERESTING = (".sqlite", ".db", ".ldb", ".sst", "leveldb", "storage", "session",
               "chat", "conversation", "message", "history")


# ── tokens：独立复核 ────────────────────────────────────────
def _jsonl(p):
    out = []
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def _iso_ms(s):
    if not s:
        return 0
    try:
        return int(datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _recent(pattern, n, recursive=False):
    return sorted(glob.glob(pattern, recursive=recursive), key=os.path.getmtime, reverse=True)[:n]


def _wb_check(host_dir, label, n):
    files = _recent(os.path.join(HOME, host_dir, "projects", "*", "*.jsonl"), n)
    print(f"\n=== {label}（最近 {len(files)} 个会话独立复核）===")
    for p in files:
        fcs = [d for d in _jsonl(p) if d.get("type") == "function_call"]
        seq = []
        for d in fcs:
            u = (d.get("message") or {}).get("usage") or {}
            seq.append((u.get("input_tokens"), u.get("output_tokens"), u.get("cache_read_input_tokens")))
        tin = sum(x[0] or 0 for x in seq)
        tout = sum(x[1] or 0 for x in seq)
        tc = sum(x[2] or 0 for x in seq)
        dup = sum(1 for a, b in zip(seq, seq[1:]) if a == b and a[0])   # 双计哨兵
        s = collect.parse_workbuddy(p)
        t = (s or {}).get("totals") or {}
        got = (t.get("in") or 0, t.get("out") or 0, t.get("cached") or 0)
        ok = "✓" if got == (tin, tout, tc) else "✗"
        print(f"  {ok} {os.path.basename(p)[:24]:<26} 事件={len(seq):<5}"
              f" in={tin:>10,} out={tout:>9,} cached={tc:>10,} 相邻同usage={dup}")


def _qwen_check(n):
    files = _recent(os.path.join(HOME, ".qwen", "projects", "*", "chats", "*.jsonl"), n)
    print(f"\n=== Qwen Code（最近 {len(files)} 个会话独立复核）===")
    for p in files:
        tin = tout = tc = 0
        for d in _jsonl(p):
            if d.get("type") == "assistant":
                u = d.get("usageMetadata") or {}
                tin += u.get("promptTokenCount") or 0
                tout += u.get("candidatesTokenCount") or 0
                tc += u.get("cachedContentTokenCount") or 0
        s = collect.parse_qwen(p)
        t = (s or {}).get("totals") or {}
        got = (t.get("in") or 0, t.get("out") or 0, t.get("cached") or 0)
        ok = "✓" if got == (tin, tout, tc) else "✗"
        print(f"  {ok} {os.path.basename(p)[:24]:<26} in={tin:>10,} out={tout:>9,} cached={tc:>10,}")


def _codex_check(n):
    files = _recent(os.path.join(HOME, ".codex", "sessions", "**", "rollout-*.jsonl"), n, recursive=True)
    print(f"\n=== Codex CLI（最近 {len(files)} 个会话独立复核）===")
    for p in files:
        prev = {"in": 0, "out": 0, "cached": 0}
        din = dout = dc = 0
        for d in _jsonl(p):
            if d.get("type") == "event_msg" and (d.get("payload") or {}).get("type") == "token_count":
                info = ((d.get("payload") or {}).get("info") or {}).get("total_token_usage") or {}
                cur = {"in": info.get("input_tokens", 0), "out": info.get("output_tokens", 0),
                       "cached": info.get("cached_input_tokens", 0)}
                din += max(0, cur["in"] - prev["in"])
                dout += max(0, cur["out"] - prev["out"])
                dc += max(0, cur["cached"] - prev["cached"])
                prev = cur
        s = collect.parse_codex(p)
        t = (s or {}).get("totals") or {}
        got = (t.get("in") or 0, t.get("out") or 0)
        ok = "✓" if got == (din, dout) else "✗"
        print(f"  {ok} {os.path.basename(p)[:24]:<26} Δin={din:>10,} Δout={dout:>9,} Δcached={dc:>10,}")


def _qwenwork_check():
    db = os.path.join(HOME, "AppData", "Roaming", "QwenWorkCN", "data", "agents.db")
    print("\n=== 千问办公（SQLite 独立计数）===")
    if not os.path.exists(db):
        print(f"  库不存在: {db}")
        return
    tmp = os.path.join(tempfile.gettempdir(), "audit_agents.db")
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(db + ext):
            try:
                shutil.copy2(db + ext, tmp + ext)
            except Exception:
                pass
    con = sqlite3.connect(tmp)
    try:
        n_chat = con.execute("select count(*) from chats where deleted_at is null").fetchone()[0]
        n_msg = con.execute("select count(*) from messages").fetchone()[0]
    finally:
        con.close()
    got = collect.parse_qwenwork(db)
    ok = "✓" if len(got) == n_chat else "✗"
    print(f"  {ok} chats(未删)={n_chat} messages={n_msg} -> collect {len(got)} 个会话")


def _doubaowork_check():
    root = collect.DOUBAOWORK_DIR
    print("\n=== 豆包工作（目录独立计数）===")
    if not os.path.isdir(root):
        print(f"  目录不存在: {root}")
        return
    dirs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    with_traj = sum(1 for d in dirs
                    if glob.glob(os.path.join(root, d, "agents", "*", "system", "trajectory.jsonl")))
    no_reply = 0
    for d in dirs:
        if not collect.parse_doubaowork(os.path.join(root, d)):
            no_reply += 1
    try:
        with open(collect._DOUBAO_CTX_FILE, encoding="utf-8") as f:
            n_ctx = len(json.load(f))
    except Exception:
        n_ctx = 0
    print(f"  .sessions 子目录={len(dirs)} 含trajectory={with_traj}"
          f" 未产出会话(仅用户输入无回复)={no_reply} 官方ctx缓存={n_ctx}")


def cmd_tokens(args):
    _wb_check(".workbuddy-ai", "WorkBuddy AI", args.n)
    _wb_check(".workbuddy", "WorkBuddy 5.x", args.n)
    _wb_check(".codebuddy", "CodeBuddy CLI", args.n)
    _qwen_check(args.n)
    _codex_check(args.n)
    _qwenwork_check()
    _doubaowork_check()


# ── daily：data.js 覆盖率与编队数据 ─────────────────────────
def cmd_daily(_args):
    fp = os.path.join(HERE, "data.js")
    with open(fp, encoding="utf-8") as f:
        D = json.loads(f.read().split("=", 1)[1].rstrip().rstrip(";"))
    sess = D["sessions"]
    tot = sum(s["totals"]["in"] for s in sess)
    bkt = sum(v["i"] for s in sess for v in (s.get("daily") or {}).values())
    print(f"会话={len(sess)}  hosts={[(h['name'], h['sessions']) for h in D['hosts'] if h['sessions']]}")
    print(f"每日时间桶覆盖率: {bkt / tot * 100:.1f}%  ({bkt:,}/{tot:,})"
          f"  {'✓' if bkt == tot else '✗ 应为 100%'}")
    n_agents = sum(len(s.get("agents") or []) for s in sess)
    n_tasks = sum(len(s.get("tasks") or []) for s in sess)
    hit = sum(1 for a in (a for s in sess for a in s.get("agents") or []) if a.get("reportTok"))
    no_timing = sum(1 for s in sess if s.get("timingKnown") is False)
    print(f"编队: 子代理记录={n_agents}(拿到报告{hit}) 任务={n_tasks}  耗时占位会话={no_timing}")
    print(f"probes: " + "; ".join(f"{p['name']}={p['state']}" for p in D.get("probes", []) if p["exists"]))


# ── probes：本地数据目录结构 ────────────────────────────────
def _walk(root, depth, max_depth, out, budget):
    if depth >= max_depth or budget[0] <= 0 or not os.path.isdir(root):
        return
    try:
        entries = sorted(os.listdir(root))
    except Exception:
        return
    for e in entries:
        fp = os.path.join(root, e)
        if os.path.isdir(fp):
            if e in SKIP_DIRS:
                continue
            _walk(fp, depth + 1, max_depth, out, budget)
        else:
            try:
                sz = os.path.getsize(fp)
            except Exception:
                sz = 0
            low = e.lower()
            dir_low = root.lower()
            if sz >= 4096 or any(k in low for k in INTERESTING) or \
               any(k in dir_low for k in ("indexeddb", "local storage", "leveldb")):
                out.append((sz, os.path.relpath(fp, root)))
                budget[0] -= 1
                if budget[0] <= 0:
                    return


def cmd_probes(args):
    targets = []
    for p in collect.PROBES:
        targets.append((p["name"], p["paths"]))
    if args.all:
        targets += [("Claude Code", [os.path.join(HOME, ".claude")]),
                    ("Codex CLI", [os.path.join(HOME, ".codex")]),
                    ("千问办公", [os.path.dirname(collect.QWENWORK_DB)]),
                    ("豆包工作", [collect.DOUBAOWORK_DIR])]
    for name, paths in targets:
        for root in paths:
            print(f"\n### {name}: {root.replace(HOME, '~')}")
            if not os.path.exists(root):
                print("    （不存在）")
                continue
            found = []
            _walk(root, 0, args.depth, found, [args.max])
            if not found:
                print("    （无符合条件的数据文件）")
            for sz, rel in sorted(found, reverse=True)[:args.max]:
                print(f"     {sz:>12,}  {rel}")


def cmd_ldb(args):
    recs = leveldb.read_db(args.dir, want=args.keyword.encode() if args.keyword else None)
    print(f"records={len(recs)}")
    for k, v in list(recs.items())[:args.show]:
        print(f"  KEY {k[-48:]!r}")
        print(f"  VAL[{len(v)}B] {v[:100]!r}")


def main():
    ap = argparse.ArgumentParser(description="context-lens 自检工具箱（只读）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("tokens", help="独立复核各宿主 token/会话计数")
    p1.add_argument("-n", type=int, default=3, help="每宿主抽查最近 N 个会话（默认 3）")
    p1.set_defaults(fn=cmd_tokens)

    p2 = sub.add_parser("daily", help="校验 data.js 每日桶覆盖率与编队数据")
    p2.set_defaults(fn=cmd_daily)

    p3 = sub.add_parser("probes", help="深挖「只清点」应用的本地数据目录")
    p3.add_argument("--depth", type=int, default=3, help="递归深度（默认 3）")
    p3.add_argument("--max", type=int, default=120, help="每目录最多列多少个文件（默认 120）")
    p3.add_argument("--all", action="store_true", help="连同可解析宿主的数据目录一起列")
    p3.set_defaults(fn=cmd_probes)

    p4 = sub.add_parser("ldb", help="用 leveldb.py 试读一个 IndexedDB/Local Storage 目录")
    p4.add_argument("dir", help="*.indexeddb.leveldb 或 Local Storage/leveldb 目录")
    p4.add_argument("keyword", nargs="?", default=None, help="只保留 value 里含该关键词的记录")
    p4.add_argument("--show", type=int, default=5, help="打印前 N 条样本（默认 5，值截断 100 字符）")
    p4.set_defaults(fn=cmd_ldb)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
