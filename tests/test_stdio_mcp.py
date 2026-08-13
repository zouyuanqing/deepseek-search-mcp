# -*- coding: utf-8 -*-
"""stdio 端到端测试：模拟 MCP 客户端与服务器通信。

无 API Key 时：协议冒烟（initialize / ping / tools/list / 参数校验错误）。
配置了 DEEPSEEK_API_KEY 或 MIMO_API_KEY 时：追加真实搜索调用（耗时较长）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "deepseek_web_search_mcp.py")


class StdioClient:
    def __init__(self, env_extra=None):
        if env_extra is not None:
            env = dict(env_extra)      # 显式环境为准（可剔除 key）
        else:
            env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )

    def send(self, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        frame = b"Content-Length: %d\r\n\r\n%s" % (len(payload), payload)
        self.proc.stdin.write(frame)
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

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        for f in (self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except Exception:
                pass


class TestStdioProtocol(unittest.TestCase):
    """无 key 冒烟：协议层必须完整可用，工具调用返回友好错误。"""

    @classmethod
    def setUpClass(cls):
        env = dict(os.environ)
        for k in ("DEEPSEEK_API_KEY", "MIMO_API_KEY"):
            env.pop(k, None)
        cls.client = StdioClient(env)
        cls._seq = 0

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def _call(self, method, params=None, rid=None):
        self._seq += 1
        self.client.send({"jsonrpc": "2.0", "id": rid or self._seq,
                          "method": method, "params": params or {}})
        return self.client.recv(60)

    def test_initialize(self):
        resp = self._call("initialize", {"protocolVersion": "2025-06-18"})
        self.assertIn("result", resp)
        self.assertEqual(resp["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "deepseek-search-mcp")

    def test_ping(self):
        resp = self._call("ping")
        self.assertEqual(resp["result"], {})

    def test_tools_list_contains_all(self):
        resp = self._call("tools/list")
        names = [t["name"] for t in resp["result"]["tools"]]
        for expect in ("web_search", "web_search_fast", "web_search_deep",
                       "mimo_search", "fetch_page", "health"):
            self.assertIn(expect, names)

    def test_tools_list_schema(self):
        resp = self._call("tools/list")
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        ws = tools["web_search"]["inputSchema"]
        self.assertIn("location", ws["properties"])
        self.assertIn("query", ws["required"])
        self.assertIn("enrich_mimo", tools["web_search_deep"]["inputSchema"]["properties"])
        self.assertIn("max_keyword", tools["mimo_search"]["inputSchema"]["properties"])
        self.assertIn("url", tools["fetch_page"]["inputSchema"]["required"])

    def test_missing_param_error(self):
        resp = self._call("tools/call", {"name": "web_search", "arguments": {}})
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32602)

    def test_unknown_tool(self):
        resp = self._call("tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_method(self):
        resp = self._call("bogus/method", rid=999)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_no_key_friendly_error(self):
        resp = self._call("tools/call", {"name": "web_search",
                                         "arguments": {"query": "test"}})
        text = resp["result"]["content"][0]["text"]
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("DEEPSEEK_API_KEY", text)

    def test_mimo_search_no_key_error(self):
        resp = self._call("tools/call", {"name": "mimo_search",
                                         "arguments": {"query": "test"}})
        text = resp["result"]["content"][0]["text"]
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("MIMO_API_KEY", text)

    def test_fetch_page_bad_url(self):
        resp = self._call("tools/call", {"name": "fetch_page",
                                         "arguments": {"url": "ftp://x.com"}})
        text = resp["result"]["content"][0]["text"]
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("仅支持", text)


@unittest.skipUnless(os.environ.get("MIMO_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"),
                     "需要真实 API Key 才运行搜索链路")
class TestStdioLiveSearch(unittest.TestCase):
    """有 key 的真实搜索链路（耗时较长）。"""

    @classmethod
    def setUpClass(cls):
        env = dict(os.environ)
        env.setdefault("BACKEND", "auto")
        cls.client = StdioClient(env)
        cls._seq = 0

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def _call(self, method, params=None):
        self._seq += 1
        self.client.send({"jsonrpc": "2.0", "id": self._seq,
                          "method": method, "params": params or {}})
        return self.client.recv(600)

    def test_mimo_search_live(self):
        resp = self._call("tools/call", {"name": "mimo_search",
                                         "arguments": {"query": "2026年8月12日A股市场表现如何？",
                                                       "location": "中国",
                                                       "max_keyword": 2, "limit": 3}})
        result = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(result["backend"], "mimo")
        self.assertTrue(result["answer"])
        self.assertGreaterEqual(len(result["sources"]), 1)
        self.assertEqual(result["citations"][0]["url"].startswith("http"), True)

    def test_health_live(self):
        resp = self._call("tools/call", {"name": "health", "arguments": {}})
        result = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
