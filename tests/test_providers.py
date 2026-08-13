# -*- coding: utf-8 -*-
"""providers.py 单元测试：请求构造 / 响应解析 / 错误分支 / fallback 链。
所有 HTTP 均被 monkeypatch 模拟，不发真实请求。
"""
from __future__ import annotations

import io
import os
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, ".")

from mcp_search.models import Location
from mcp_search.providers import (DeepSeekClient, FallbackChain, MimoClient,
                                  ProviderError, _http_post, _post_with_retry,
                                  build_backends)


class TestPostWithRetry(unittest.TestCase):
    @mock.patch("mcp_search.providers._http_post")
    def test_success_no_retry(self, mock_post):
        mock_post.return_value = (200, {"ok": 1})
        self.assertEqual(_post_with_retry("u", {}, {}, 10), (200, {"ok": 1}))
        self.assertEqual(mock_post.call_count, 1)

    @mock.patch("mcp_search.providers._http_post")
    def test_network_error_retry_then_success(self, mock_post):
        mock_post.side_effect = [(0, {"error": {"message": "网络错误"}}), (200, {"ok": 1})]
        self.assertEqual(_post_with_retry("u", {}, {}, 10, backoff_s=0), (200, {"ok": 1}))
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("mcp_search.providers._http_post")
    def test_5xx_retry(self, mock_post):
        mock_post.side_effect = [(502, {"error": {"message": "bad gateway"}}), (200, {"ok": 1})]
        self.assertEqual(_post_with_retry("u", {}, {}, 10, backoff_s=0), (200, {"ok": 1}))

    @mock.patch("mcp_search.providers._http_post")
    def test_429_retry(self, mock_post):
        mock_post.side_effect = [(429, {"error": {"message": "rate limited"}}), (200, {"ok": 1})]
        self.assertEqual(_post_with_retry("u", {}, {}, 10, backoff_s=0), (200, {"ok": 1}))

    @mock.patch("mcp_search.providers._http_post")
    def test_4xx_no_retry(self, mock_post):
        mock_post.return_value = (401, {"error": {"message": "invalid key"}})
        self.assertEqual(_post_with_retry("u", {}, {}, 10, backoff_s=0)[0], 401)
        self.assertEqual(mock_post.call_count, 1)

    @mock.patch("mcp_search.providers._http_post")
    def test_all_fail_returns_last(self, mock_post):
        mock_post.side_effect = [(0, {"error": {"message": "e1"}}), (500, {"error": {"message": "e2"}})]
        status, resp = _post_with_retry("u", {}, {}, 10, backoff_s=0)
        self.assertEqual(status, 500)
        self.assertEqual(resp["error"]["message"], "e2")


class TestEmptyResultRetry(unittest.TestCase):
    def _ds_resp(self, texts):
        return (200, {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": t} for t in texts]}]})

    @mock.patch("mcp_search.providers.DeepSeekClient._post")
    def test_deepseek_empty_then_success(self, mock_post):
        c = DeepSeekClient("sk-ds")
        mock_post.side_effect = [self._ds_resp([]), self._ds_resp(["重试后成功"])]
        r = c.search("q")
        self.assertIn("重试后成功", r.answer)
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("mcp_search.providers.DeepSeekClient._post")
    def test_deepseek_empty_twice_raises(self, mock_post):
        c = DeepSeekClient("sk-ds")
        mock_post.side_effect = [self._ds_resp([]), self._ds_resp([])]
        with self.assertRaises(ProviderError):
            c.search("q")
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("mcp_search.providers.MimoClient._post")
    def test_mimo_empty_then_success(self, mock_post):
        c = MimoClient("sk-mm")
        mock_post.side_effect = [
            (200, {"choices": [{"message": {"content": None, "annotations": []}}]}),
            (200, {"choices": [{"message": {"content": "内容", "annotations": []}}]}),
        ]
        r = c.search("q")
        self.assertEqual(r.answer, "内容")
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("mcp_search.providers.MimoClient._post")
    def test_mimo_empty_choices_retry(self, mock_post):
        c = MimoClient("sk-mm")
        mock_post.side_effect = [
            (200, {"choices": []}),
            (200, {"choices": [{"message": {"content": "ok", "annotations": []}}]}),
        ]
        r = c.search("q")
        self.assertEqual(r.answer, "ok")

    @mock.patch("mcp_search.providers.MimoClient._post")
    def test_mimo_empty_twice_raises(self, mock_post):
        c = MimoClient("sk-mm")
        mock_post.side_effect = [(200, {"choices": []}), (200, {"choices": []})]
        with self.assertRaises(ProviderError):
            c.search("q")


class TestHttpPost(unittest.TestCase):
    @mock.patch("mcp_search.providers.urllib.request.urlopen")
    def test_ok(self, mock_open):
        ctx = mock.MagicMock()
        ctx.status = 200
        ctx.read.return_value = b'{"ok": true}'
        ctx.__enter__.return_value = ctx
        mock_open.return_value = ctx
        status, resp = _http_post("https://x.com/v1/r", {"a": 1}, {}, 10)
        self.assertEqual((status, resp), (200, {"ok": True}))
        req = mock_open.call_args[0][0]
        self.assertEqual(req.full_url, "https://x.com/v1/r")
        self.assertEqual(req.get_header("Content-type"), "application/json")

    @mock.patch("mcp_search.providers.urllib.request.urlopen")
    def test_http_error_json(self, mock_open):
        mock_open.side_effect = HTTPError("https://x.com", 401, "Unauthorized", {},
                                          io.BytesIO(b'{"error": {"message": "Invalid API key"}}'))
        status, resp = _http_post("https://x.com", {}, {}, 10)
        self.assertEqual(status, 401)
        self.assertEqual(resp["error"]["message"], "Invalid API key")

    @mock.patch("mcp_search.providers.urllib.request.urlopen")
    def test_network_error(self, mock_open):
        mock_open.side_effect = URLError("boom")
        status, resp = _http_post("https://x.com", {}, {}, 10)
        self.assertEqual(status, 0)
        self.assertIn("网络错误", resp["error"]["message"])


DS_RESP = {
    "id": "x", "object": "response",
    "output": [
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "答案正文 [来源](https://a.com/1)"}]}
    ],
    "output_text": "",
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}

MIMO_RESP = {
    "id": "y", "object": "chat.completion", "model": "mimo-v2.5-pro",
    "choices": [{
        "index": 0, "finish_reason": "stop",
        "message": {
            "role": "assistant",
            "content": "MiMo 摘要",
            "tool_calls": None,
            "annotations": [
                {"type": "url_citation", "title": "标题A", "url": "https://a.com/1",
                 "site_name": "站A", "publish_time": "2026-08-12T00:00:00", "summary": "摘要A"},
                {"type": "url_citation", "title": "标题B", "url": "https://b.com/2",
                 "site_name": "站B", "publish_time": "", "summary": ""},
            ],
        },
    }],
    "usage": {"prompt_tokens": 100, "completion_tokens": 20,
              "web_search_usage": {"tool_usage": 2, "page_usage": 3}},
}


class TestDeepSeekClient(unittest.TestCase):
    def setUp(self):
        self.c = DeepSeekClient("sk-ds", timeout_s=30, max_output=4096, max_output_fast=512)

    @mock.patch("mcp_search.providers._http_post", return_value=(200, DS_RESP))
    def test_search_request_and_parse(self, mock_post):
        r = self.c.search("测试问题")
        self.assertEqual(r.backend, "deepseek")
        self.assertIn("答案正文", r.answer)
        self.assertEqual(r.sources, ["https://a.com/1"])
        # 请求构造断言
        url, body, headers, timeout = mock_post.call_args[0]
        self.assertTrue(url.endswith("/v1/responses"))
        self.assertEqual(body["model"], "deepseek-v4-flash")
        self.assertEqual(body["tools"], [{"type": "web_search"}])
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["max_output_tokens"], 4096)
        self.assertIn("Bearer sk-ds", headers["Authorization"])

    @mock.patch("mcp_search.providers._http_post", return_value=(200, DS_RESP))
    def test_search_fast(self, mock_post):
        self.c.search("q", fast=True)
        body = mock_post.call_args[0][1]
        self.assertEqual(body["max_output_tokens"], 512)
        self.assertEqual(body["reasoning"], {"effort": "low"})

    @mock.patch("mcp_search.providers._http_post", return_value=(200, DS_RESP))
    def test_search_with_location_injects_prompt(self, mock_post):
        self.c.search("武汉天气", location=Location.parse("湖北省武汉市"))
        body = mock_post.call_args[0][1]
        self.assertIn("湖北省/武汉市", body["input"])

    @mock.patch("mcp_search.providers._http_post", return_value=(500, {"error": {"message": "server down"}}))
    def test_http_error(self, mock_post):
        with self.assertRaises(ProviderError) as cm:
            self.c.search("q")
        self.assertIn("server down", str(cm.exception))

    @mock.patch("mcp_search.providers._http_post",
                return_value=(200, {"output": [], "output_text": ""}))
    def test_empty_answer_raises(self, mock_post):
        with self.assertRaises(ProviderError):
            self.c.search("q")

    @mock.patch("mcp_search.providers._http_post", return_value=(200, {
        "choices": [{"message": {"content": "综合答案"}}]}))
    def test_chat(self, mock_post):
        out = self.c.chat("sys", "user", 100)
        self.assertEqual(out, "综合答案")
        body = mock_post.call_args[0][1]
        self.assertTrue(body["messages"][0]["content"] == "sys")
        self.assertEqual(body["max_tokens"], 100)


class TestMimoClient(unittest.TestCase):
    def setUp(self):
        self.c = MimoClient("sk-mimo", timeout_s=30, max_keyword=5, limit=5,
                            max_output=4096, max_output_fast=2048)

    @mock.patch("mcp_search.providers._http_post", return_value=(200, MIMO_RESP))
    def test_search_request_and_parse(self, mock_post):
        r = self.c.search("北京天气")
        self.assertEqual(r.backend, "mimo")
        self.assertEqual(r.answer, "MiMo 摘要")
        self.assertEqual(r.finish_reason, "stop")
        self.assertEqual(len(r.citations), 2)
        self.assertEqual(r.citations[0].title, "标题A")
        self.assertEqual(r.citations[0].site_name, "站A")
        self.assertEqual(r.citations[0].publish_time, "2026-08-12T00:00:00")
        self.assertEqual(r.sources, ["https://a.com/1", "https://b.com/2"])
        self.assertEqual(r.usage["web_search_usage"], {"tool_usage": 2, "page_usage": 3})
        # 请求构造断言
        url, body, headers, timeout = mock_post.call_args[0]
        self.assertTrue(url.endswith("/v1/chat/completions"))
        self.assertEqual(headers["api-key"], "sk-mimo")
        self.assertEqual(body["model"], "mimo-v2.5-pro")
        self.assertEqual(body["stream"], False)
        tool = body["tools"][0]
        self.assertEqual(tool["type"], "web_search")
        self.assertTrue(tool["force_search"])
        self.assertEqual(tool["max_keyword"], 5)
        self.assertEqual(tool["limit"], 5)
        self.assertNotIn("user_location", tool)
        self.assertEqual(body["max_completion_tokens"], 4096)

    @mock.patch("mcp_search.providers._http_post", return_value=(200, MIMO_RESP))
    def test_search_fast_halves_budget(self, mock_post):
        self.c.search("q", fast=True)
        tool = mock_post.call_args[0][1]["tools"][0]
        self.assertEqual(tool["max_keyword"], 2)
        self.assertEqual(tool["limit"], 2)

    @mock.patch("mcp_search.providers._http_post", return_value=(200, MIMO_RESP))
    def test_search_overrides(self, mock_post):
        self.c.search("q", max_keyword=8, limit=1)
        tool = mock_post.call_args[0][1]["tools"][0]
        self.assertEqual(tool["max_keyword"], 8)
        self.assertEqual(tool["limit"], 1)

    @mock.patch("mcp_search.providers._http_post", return_value=(200, MIMO_RESP))
    def test_search_with_location_maps_user_location(self, mock_post):
        self.c.search("北京天气", location=Location.parse("北京市"))
        tool = mock_post.call_args[0][1]["tools"][0]
        self.assertEqual(tool["user_location"],
                         {"type": "approximate", "region": "北京市"})

    @mock.patch("mcp_search.providers._http_post",
                return_value=(200, {"choices": [{"message": {"content": None}}]}))
    def test_empty_content_raises(self, mock_post):
        with self.assertRaises(ProviderError):
            self.c.search("q")

    @mock.patch("mcp_search.providers._http_post",
                return_value=(200, {"choices": []}))
    def test_no_choices_raises(self, mock_post):
        with self.assertRaises(ProviderError):
            self.c.search("q")

    @mock.patch("mcp_search.providers._http_post",
                return_value=(429, {"error": {"message": "rate limited"}}))
    def test_http_error(self, mock_post):
        with self.assertRaises(ProviderError) as cm:
            self.c.search("q")
        self.assertIn("rate limited", str(cm.exception))

    @mock.patch("mcp_search.providers._http_post", return_value=(200, {
        "choices": [{"message": {"content": "规划输出"}}]}))
    def test_chat(self, mock_post):
        out = self.c.chat("sys", "user", 100)
        self.assertEqual(out, "规划输出")
        body = mock_post.call_args[0][1]
        self.assertEqual(body["max_completion_tokens"], 100)
        self.assertNotIn("tools", body)

    def test_annotations_skip_invalid(self):
        ann = [{"url": ""}, {"url": "https://ok.com/x"}, "garbage", None,
               {"url": "https://ok2.com/y", "type": "doc_citation"}]
        out = MimoClient._parse_citations(ann)
        self.assertEqual([c.url for c in out], ["https://ok.com/x", "https://ok2.com/y"])


class TestFallbackChain(unittest.TestCase):
    def test_first_success_wins(self):
        ds = mock.Mock(name="deepseek")
        ds.name = "deepseek"
        ds.search.side_effect = ProviderError("ds down")
        mm = mock.Mock(name="mimo")
        mm.name = "mimo"
        result = mock.Mock(backend="mimo", answer="ok")
        mm.search.return_value = result
        chain = FallbackChain([ds, mm])
        out = chain.search("q")
        self.assertIs(out, result)
        self.assertEqual(out.backend, "mimo")

    def test_all_fail_aggregates(self):
        ds = mock.Mock(name="deepseek")
        ds.name = "deepseek"
        ds.search.side_effect = ProviderError("ds err")
        mm = mock.Mock(name="mimo")
        mm.name = "mimo"
        mm.search.side_effect = ProviderError("mm err")
        chain = FallbackChain([ds, mm])
        with self.assertRaises(ProviderError) as cm:
            chain.search("q")
        self.assertIn("deepseek", str(cm.exception))
        self.assertIn("mimo", str(cm.exception))

    def test_chat_fallback(self):
        ds = mock.Mock(name="deepseek")
        ds.name = "deepseek"
        ds.chat.side_effect = ProviderError("down")
        mm = mock.Mock(name="mimo")
        mm.name = "mimo"
        mm.chat.return_value = "ok"
        chain = FallbackChain([ds, mm])
        self.assertEqual(chain.chat("s", "u"), "ok")

    def test_empty_chain(self):
        with self.assertRaises(ProviderError):
            FallbackChain([])

    def test_names(self):
        ds, mm = mock.Mock(), mock.Mock()
        ds.name, mm.name = "deepseek", "mimo"
        self.assertEqual(FallbackChain([ds, mm]).names, ["deepseek", "mimo"])


class TestBuildBackends(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.clear()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_deepseek_only(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-ds"
        chain = build_backends("deepseek")
        self.assertEqual(chain.names, ["deepseek"])

    def test_deepseek_missing_key(self):
        with self.assertRaises(ProviderError):
            build_backends("deepseek")

    def test_mimo_only(self):
        os.environ["MIMO_API_KEY"] = "sk-mm"
        chain = build_backends("mimo")
        self.assertEqual(chain.names, ["mimo"])

    def test_auto_both(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-ds"
        os.environ["MIMO_API_KEY"] = "sk-mm"
        chain = build_backends("auto")
        self.assertEqual(chain.names, ["deepseek", "mimo"])

    def test_auto_skips_missing(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-ds"
        chain = build_backends("auto")
        self.assertEqual(chain.names, ["deepseek"])

    def test_auto_none(self):
        with self.assertRaises(ProviderError):
            build_backends("auto")

    def test_invalid_mode(self):
        with self.assertRaises(ProviderError):
            build_backends("bogus")


if __name__ == "__main__":
    unittest.main()
