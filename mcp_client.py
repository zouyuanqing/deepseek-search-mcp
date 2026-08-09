# -*- coding: utf-8 -*-
"""通用 MCP stdio 客户端：连接任意 stdio MCP 服务器，执行 initialize + tools/call。
用法：
  python mcp_client.py --cmd "npx" --args "-y tavily-mcp" --tool tavily-search --params '{"query":"...","max_results":5}'
  python mcp_client.py --cmd "python" --args "deepseek_web_search_mcp.py" --tool web_search --params '{"query":"..."}'
"""
import argparse
import json
import subprocess
import sys
import threading
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd", required=True)
    ap.add_argument("--args", default="")
    ap.add_argument("--tool", default="")
    ap.add_argument("--params", default="")
    ap.add_argument("--params-file", default="", help="从 JSON 文件读工具参数")
    ap.add_argument("--list", action="store_true", help="只列工具清单")
    ap.add_argument("--jsonl", action="store_true", help="使用 NDJSON 行协议（新版 MCP SDK）")
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE 附加环境变量")
    args = ap.parse_args()

    env = dict(os.environ)
    for kv in args.env:
        k, _, v = kv.partition("=")
        env[k] = v

    proc = subprocess.Popen(
        [args.cmd] + args.args.split(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )

    def send(obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if args.jsonl:
            proc.stdin.write(payload + b"\n")
        else:
            frame = f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8") + payload
            proc.stdin.write(frame)
        proc.stdin.flush()

    def recv(timeout=300):
        box = {}
        def _read():
            if args.jsonl:
                line = proc.stdout.readline()
                box["data"] = line if line else None
                return
            line = proc.stdout.readline()
            if not line:
                box["none"] = True
                return
            length = 0
            if line.strip().lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip() or 0)
            while True:
                h = proc.stdout.readline()
                if h in (b"\r\n", b"\n"):
                    break
                k, v = h.split(b":", 1)
                if k.strip().lower() == b"content-length":
                    length = int(v.strip())
            box["data"] = proc.stdout.read(length)
        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if args.jsonl:
            if "data" not in box:
                return {"timeout": True}
            raw = box["data"]
            if not raw:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return None
        if "none" in box:
            return None
        if "data" not in box:
            return {"timeout": True}
        return json.loads(box["data"].decode("utf-8"))

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "mcp-client-test", "version": "0.1.0"}}})
        init = recv(30)
        print("INIT:", json.dumps(init, ensure_ascii=False)[:300] if init else None, flush=True)
        send({"jsonrpc": "2.0", "id": 2, "method": "notifications/initialized", "params": {}})

        if args.list:
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
            tl = recv(30)
            if tl and "result" in tl:
                for t in tl["result"].get("tools", []):
                    print("TOOL:", t["name"], "|", t.get("description", "")[:120], flush=True)
            return

        if args.params_file:
            with open(args.params_file, "r", encoding="utf-8") as f:
                params = json.load(f)
        else:
            params = json.loads(args.params or "{}")
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": args.tool, "arguments": params}})
        resp = recv()
        if resp is None:
            print("无响应", flush=True)
        elif "timeout" in resp:
            print("超时", flush=True)
        elif "error" in resp:
            print("ERROR:", json.dumps(resp["error"], ensure_ascii=False), flush=True)
        else:
            print("RESULT:", flush=True)
            for c in resp["result"].get("content", []):
                if c.get("type") == "text":
                    print(c["text"], flush=True)
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace")
        if err.strip():
            print("\n[stderr]", err[:1500], flush=True)


if __name__ == "__main__":
    main()
