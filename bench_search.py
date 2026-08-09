# -*- coding: utf-8 -*-
"""DeepSeek-search-mcp vs Tavily-mcp 同题对比。
两个 MCP 服务器各自用其协议调用，输出结构化结果对比。
用法: python bench_search.py "<query>"
"""
import json
import os
import subprocess
import sys
import threading
import time

DS_SCRIPT = os.path.join(os.path.dirname(__file__), "deepseek_web_search_mcp.py")
TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")


def spawn(cmd, args_list, env_extra):
    return subprocess.Popen(
        [cmd] + args_list,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(os.environ, **env_extra),
    )


class Client:
    """Content-Length 帧协议客户端（deepseek-search-mcp）。"""

    def __init__(self, proc):
        self.proc = proc

    def send(self, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.proc.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8") + payload)
        self.proc.stdin.flush()

    def recv(self, timeout=300):
        box = {}
        def _read():
            line = self.proc.stdout.readline()
            if not line:
                box["none"] = True
                return
            length = 0
            if line.strip().lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip() or 0)
            while True:
                h = self.proc.stdout.readline()
                if h in (b"\r\n", b"\n"):
                    break
                k, v = h.split(b":", 1)
                if k.strip().lower() == b"content-length":
                    length = int(v.strip())
            box["data"] = self.proc.stdout.read(length)
        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if "none" in box:
            return None
        if "data" not in box:
            return {"timeout": True}
        return json.loads(box["data"].decode("utf-8"))

    def call(self, name, args_, timeout=300):
        self.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                   "params": {"name": name, "arguments": args_}})
        return self.recv(timeout)


class JsonlClient(Client):
    """NDJSON 行协议客户端（tavily-mcp，新版 SDK）。"""

    def send(self, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.proc.stdin.write(payload + b"\n")
        self.proc.stdin.flush()

    def recv(self, timeout=300):
        box = {}
        def _read():
            box["data"] = self.proc.stdout.readline()
        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if "data" not in box:
            return {"timeout": True}
        if not box["data"]:
            return None
        try:
            return json.loads(box["data"].decode("utf-8"))
        except json.JSONDecodeError:
            return None


def init_mcp(client):
    client.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "bench", "version": "0.1"}}})
    client.recv(30)
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})


def extract_text(resp):
    if resp is None:
        return "无响应"
    if "timeout" in resp:
        return "超时"
    if "error" in resp:
        return "ERROR: " + json.dumps(resp["error"], ensure_ascii=False)
    parts = []
    for c in resp["result"].get("content", []):
        if c.get("type") == "text":
            parts.append(c["text"])
    return "\n".join(parts)


def bench_tavily(query):
    proc = spawn("npx.cmd", ["-y", "tavily-mcp"], {"TAVILY_API_KEY": TAVILY_KEY})
    c = JsonlClient(proc)
    try:
        init_mcp(c)
        t0 = time.time()
        resp = c.call("tavily_search", {"query": query, "search_depth": "advanced", "max_results": 5}, timeout=120)
        dt = time.time() - t0
        return dt, extract_text(resp)
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def bench_deepseek(query, deep=False):
    proc = spawn(sys.executable, [DS_SCRIPT], {"DEEPSEEK_API_KEY": DEEPSEEK_KEY})
    c = Client(proc)
    try:
        init_mcp(c)
        tool = "web_search_deep" if deep else "web_search"
        t0 = time.time()
        resp = c.call(tool, {"query": query}, timeout=300)
        dt = time.time() - t0
        return dt, extract_text(resp)
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main():
    # 支持 --deep 标记 + query；--deep 可放任意位置
    argv = sys.argv[1:]
    deep = "--deep" in argv
    query = next((a for a in argv if not a.startswith("--")), "DeepSeek V4-Flash API pricing per million tokens")
    print(f"# 对比查询: {query}\n", flush=True)

    print("=== [Tavily MCP] tavily_search (advanced) ===", flush=True)
    t0, r0 = bench_tavily(query)
    print(f"耗时 {t0:.1f}s:", flush=True)
    print(r0[:3500], flush=True)

    print("\n=== [DeepSeek MCP] web_search" + ("_deep" if deep else "") + " ===", flush=True)
    t1, r1 = bench_deepseek(query, deep)
    print(f"耗时 {t1:.1f}s:", flush=True)
    print(r1[:3500], flush=True)


if __name__ == "__main__":
    main()
