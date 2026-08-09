# -*- coding: utf-8 -*-
"""stdio 协议测试：模拟 MCP 客户端与 deepseek_web_search_mcp.py 通信。
覆盖 initialize / tools/list / tools/call(web_search) / tools/call(web_search_deep)。
用法：python test_stdio.py [--deep]
"""
import json
import subprocess
import sys
import os

KEY = os.environ.get("DEEPSEEK_API_KEY", "")
SCRIPT = os.path.join(os.path.dirname(__file__), "deepseek_web_search_mcp.py")

def main():
    print("启动子进程...", flush=True)
    env = dict(os.environ)
    if KEY:
        env["DEEPSEEK_API_KEY"] = KEY
    proc = subprocess.Popen(
        [sys.executable, SCRIPT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )

    def send(obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        frame = f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8") + payload
        proc.stdin.write(frame)
        proc.stdin.flush()

    def recv(timeout=300):
        # 用线程读取 + join 超时：通知类请求（无响应）不会永久卡死
        box = {}

        def _read():
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
            raw = proc.stdout.read(length)
            box["data"] = raw

        import threading
        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if "none" in box:
            return None
        if "data" not in box:
            # 超时且线程仍在阻塞读：放弃该线程（daemon），跳过本次响应
            return None
        return json.loads(box["data"].decode("utf-8"))

    def call(msg, label, expect_response=True):
        send(msg)
        if not expect_response:
            print(f"\n=== {label}（无响应）===", flush=True)
            return None
        resp = recv()
        print(f"\n=== {label} ===", flush=True)
        if resp is None:
            print("无响应", flush=True); return None
        if "error" in resp:
            print("ERROR:", resp["error"], flush=True); return resp
        result = resp["result"]
        content = result.get("content", [])
        for c in content:
            if c.get("type") == "text":
                print(c["text"][:3000], flush=True)
        return resp

    try:
        call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, "initialize")
        call({"jsonrpc": "2.0", "id": 2, "method": "notifications/initialized", "params": {}}, "initialized 通知", expect_response=False)
        call({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}, "tools/list")
        call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "web_search", "arguments": {"query": "2026年8月9日之前的最近一周，AI 行业发生了什么重要事件？"}}},
             "tools/call web_search")
        if "--fast" in sys.argv:
            call({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "web_search_fast", "arguments": {"query": "2026年8月9日美元兑人民币汇率是多少？"}}},
                 "tools/call web_search_fast")
        if "--deep" in sys.argv:
            call({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                  "params": {"name": "web_search_deep", "arguments": {"query": "DeepSeek V4-Flash 正式版相比预览版有哪些能力变化？价格如何？"}}},
                 "tools/call web_search_deep")
    finally:
        proc.stdin.close()
        proc.wait(timeout=300)
        err = proc.stderr.read().decode("utf-8", "replace")
        if err.strip():
            print("\n=== stderr ===")
            print(err[:2000])

if __name__ == "__main__":
    main()
