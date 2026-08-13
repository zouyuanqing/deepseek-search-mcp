# -*- coding: utf-8 -*-
"""fetch.py 测试：本地 http.server 起真实 HTTP 服务验证抓取/提取/错误分支。"""
from __future__ import annotations

import http.server
import json
import sys
import threading
import unittest

sys.path.insert(0, ".")

from mcp_search.fetch import _is_private_host, _is_private_ip, fetch_page

HTML_PAGE = """<!DOCTYPE html><html><head><title>测试标题</title>
<script>var x = 1;</script><style>.a{color:red}</style></head>
<body><nav>导航噪声</nav><article><h1>一级标题</h1>
<p>第一段内容<span>行内文字</span></p><p>第二段内容。</p></article>
<footer>页脚噪声</footer></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/json":
            body = json.dumps({"title": "报告", "items": [1, 2, 3]}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/text":
            body = "纯文本内容 line1\nline2".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/bin":
            body = b"\x89PNG\r\n\x1a\nfake"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/slow":
            import time
            time.sleep(3)
            self.send_response(200)
            self.end_headers()
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class FetchTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.t = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)


class TestFetchPage(FetchTestBase):
    def _fetch(self, path, **kw):
        kw.setdefault("allow_private", True)  # 本地测试服务器
        return fetch_page(self.url(path), **kw)

    def test_html_extraction(self):
        r = self._fetch("/ok")
        self.assertEqual(r["title"], "测试标题")
        self.assertIn("一级标题", r["text"])
        self.assertIn("第一段内容 行内文字", r["text"].replace("\n", " "))
        self.assertIn("第二段内容", r["text"])
        self.assertNotIn("导航噪声", r["text"])
        self.assertNotIn("页脚噪声", r["text"])
        self.assertFalse(r["truncated"])

    def test_json_content(self):
        r = self._fetch("/json")
        data = json.loads(r["text"])
        self.assertEqual(data["title"], "报告")

    def test_plain_text(self):
        r = self._fetch("/text")
        self.assertIn("纯文本内容", r["text"])

    def test_binary_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self._fetch("/bin")
        self.assertIn("不支持的内容类型", str(cm.exception))

    def test_404(self):
        with self.assertRaises(ValueError) as cm:
            self._fetch("/nope")
        self.assertIn("404", str(cm.exception))

    def test_redirect_followed(self):
        r = self._fetch("/redirect")
        self.assertIn("一级标题", r["text"])
        self.assertEqual(r["final_url"], self.url("/ok"))

    def test_timeout(self):
        with self.assertRaises(ValueError) as cm:
            self._fetch("/slow", timeout=1)
        self.assertIn("网络错误", str(cm.exception))


class TestFetchValidation(unittest.TestCase):
    def test_bad_scheme(self):
        with self.assertRaises(ValueError):
            fetch_page("ftp://example.com/x")

    def test_empty_url(self):
        with self.assertRaises(ValueError):
            fetch_page("")
        with self.assertRaises(ValueError):
            fetch_page("  ")

    def test_ssrf_private_ip(self):
        with self.assertRaises(ValueError) as cm:
            fetch_page("http://127.0.0.1:8080/x")
        self.assertIn("SSRF", str(cm.exception))
        with self.assertRaises(ValueError):
            fetch_page("http://10.0.0.1/x")
        with self.assertRaises(ValueError):
            fetch_page("http://192.168.1.1/x")
        with self.assertRaises(ValueError):
            fetch_page("http://172.16.5.5/x")
        with self.assertRaises(ValueError):
            fetch_page("http://localhost:80/x")

    def test_private_ip_helpers(self):
        self.assertTrue(_is_private_ip("127.0.0.1"))
        self.assertTrue(_is_private_ip("10.1.2.3"))
        self.assertTrue(_is_private_ip("192.168.0.1"))
        self.assertTrue(_is_private_ip("172.16.0.1"))
        self.assertTrue(_is_private_ip("169.254.1.1"))
        self.assertTrue(_is_private_ip("100.64.0.1"))
        self.assertTrue(_is_private_ip("224.0.0.1"))
        self.assertFalse(_is_private_ip("8.8.8.8"))
        self.assertFalse(_is_private_ip("1.1.1.1"))

    def test_private_host_helpers(self):
        self.assertTrue(_is_private_host("localhost"))
        self.assertTrue(_is_private_host("127.0.0.1"))
        self.assertFalse(_is_private_host("8.8.8.8"))


if __name__ == "__main__":
    unittest.main()
