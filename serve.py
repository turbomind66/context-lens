#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
context-lens / serve.py

本地实时服务：页面每 3 秒拉一次 /api/data，服务端只重新解析"变过"的日志文件
（按 mtime + size 判断），没变化时直接回 unchanged，几乎零开销。

    python serve.py            # http://127.0.0.1:8765
    python serve.py 9000       # 换端口

只读会话日志，不写入任何宿主目录。
"""
import http.server, socketserver, json, gzip, os, sys, threading, time, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import collect

CACHE = {}
LOCK = threading.Lock()
STATE = {"rev": None, "raw": b"", "built_at": 0.0, "builds": 0}


def refresh():
    """重新扫描（增量），返回 (rev, raw_bytes, unchanged)。"""
    with LOCK:
        sessions, hosts, rev = collect.scan(CACHE)
        if rev == STATE["rev"] and STATE["raw"]:
            return rev, STATE["raw"], True
        data = collect.build_data(sessions, hosts)
        data["rev"] = rev
        data["servedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        STATE.update(rev=rev, raw=raw, built_at=time.time())
        STATE["builds"] += 1
        return rev, raw, False


def full_item(sid, step, cat, label):
    """按会话 id + (step,cat,label) 重新解析源日志返回未截断正文（F11 按需取全文）。
    用 step/cat/label 而非序号匹配：序号会随会话增长/降采样窗口漂移，而 step 稳定。
    临时放大 MAX_BROWSER 让 re-parse 不截断 browser，保证命中。"""
    src = host = None
    with LOCK:
        for ent in CACHE.values():
            for s in ent["s"]:
                if s.get("id") == sid:
                    src, host = s.get("src"), s.get("host")
                    break
            if src:
                break
    if not src or not host:
        return None
    h = next((x for x in collect.HOSTS if x["id"] == host), None)
    if not h:
        return None
    old_full, old_mb = collect.FULL, collect.MAX_BROWSER
    collect.FULL, collect.MAX_BROWSER = True, 10_000_000
    try:
        got = h["parse"](src)
        if isinstance(got, dict):
            got = [got]
        for s in (got or []):
            for b in (s.get("browser") or []):
                if b.get("step") == step and b.get("cat") == cat and b.get("label") == label:
                    return {"cat": b["cat"], "label": b["label"], "step": b["step"],
                            "tok": b["tok"], "text": b["preview"]}
    except Exception:
        return None
    finally:
        collect.FULL, collect.MAX_BROWSER = old_full, old_mb
    return None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静音轮询日志

    def _send(self, code, body, ctype="application/json; charset=utf-8", gz=False):
        if gz and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 5)
            enc = "gzip"
        else:
            enc = None
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if enc:
            self.send_header("Content-Encoding", enc)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/ping":
            return self._send(200, b'{"ok":true}')

        if path == "/api/data":
            try:
                rev, raw, unchanged = refresh()
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}).encode())
            if qs.get("rev", [""])[0] == rev:
                return self._send(200, json.dumps({"rev": rev, "unchanged": True}).encode(), gz=True)
            return self._send(200, raw, gz=True)

        if path == "/api/item":
            sid = qs.get("sid", [""])[0]
            cat = qs.get("cat", [""])[0]
            label = qs.get("label", [""])[0]
            try:
                step = int(qs.get("step", ["-1"])[0])
            except Exception:
                step = -1
            r = full_item(sid, step, cat, label)
            if r is None:
                return self._send(404, json.dumps({"error": "not found"}).encode())
            return self._send(200, json.dumps(r, ensure_ascii=False).encode(), gz=True)

        # 静态文件
        rel = path.lstrip("/") or "index.html"
        if rel == "data.js":
            # 没起服务时也能用：返回一次静态快照
            try:
                rev, raw, _ = refresh()
            except Exception:
                raw = b"{}"
            body = b"window.LENS_DATA = " + raw + b";\n"
            return self._send(200, body, "application/javascript; charset=utf-8", gz=True)

        fp = os.path.normpath(os.path.join(HERE, rel))
        if not fp.startswith(HERE) or not os.path.isfile(fp):
            return self._send(404, b"not found", "text/plain")
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml"}.get(os.path.splitext(fp)[1], "application/octet-stream")
        with open(fp, "rb") as f:
            return self._send(200, f.read(), ctype, gz=True)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    t0 = time.time()
    sessions, hosts, _ = collect.scan(CACHE, verbose=True)   # 预热缓存，首个请求就不必再解析
    n_host = sum(1 for h in hosts if h["sessions"])
    print(f"\n首次扫描 {len(sessions)} 个会话 / {n_host} 个宿主，耗时 {time.time()-t0:.2f}s")
    srv = Server(("127.0.0.1", port), Handler)
    print(f"实时服务已启动 → http://127.0.0.1:{port}    (Ctrl+C 退出)\n")
    try:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
