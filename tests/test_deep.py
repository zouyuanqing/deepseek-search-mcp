# -*- coding: utf-8 -*-
"""深搜编排健壮性测试：plan 重试与 fallback、综合失败降级。"""
from __future__ import annotations

import sys
import unittest
from unittest import mock

sys.path.insert(0, ".")

from mcp_search.providers import ProviderError
from mcp_search.server import (_fallback_queries, _join_segments_fallback,
                               _parse_queries, _plan_queries)


class TestParseQueries(unittest.TestCase):
    def test_json_array(self):
        q = _parse_queries('["a问题", "b问题", "c问题"]')
        self.assertEqual(q, ["a问题", "b问题", "c问题"])

    def test_json_multiline(self):
        q = _parse_queries('[\n  "子查询一",\n  "子查询二"\n]')
        self.assertEqual(len(q), 2)

    def test_numbered_lines(self):
        q = _parse_queries("1. 查询甲\n2. 查询乙\n3. 查询丙")
        self.assertEqual(q, ["查询甲", "查询乙", "查询丙"])

    def test_empty_raises(self):
        with self.assertRaises(ProviderError):
            _parse_queries("")
        with self.assertRaises(ProviderError):
            _parse_queries("没有可用内容")

    def test_limit(self):
        q = _parse_queries('["1", "2", "3", "4", "5", "6", "7", "8"]', limit=4)
        self.assertEqual(len(q), 4)


class TestFallbackQueries(unittest.TestCase):
    def test_generates_three(self):
        q = _fallback_queries("特斯拉股价走势")
        self.assertEqual(len(q), 3)
        self.assertTrue(all("特斯拉股价走势" in s for s in q))


class TestPlanQueries(unittest.TestCase):
    def _chain(self, chat_side_effect):
        chain = mock.Mock()
        chain.chat.side_effect = chat_side_effect
        return chain

    def test_first_try_success(self):
        chain = self._chain(['["子一", "子二"]'])
        q = _plan_queries(chain, "研究问题")
        self.assertEqual(q, ["子一", "子二"])
        self.assertEqual(chain.chat.call_count, 1)

    def test_retry_after_failure(self):
        chain = self._chain([ProviderError("chat down"), '["子三", "子四"]'])
        q = _plan_queries(chain, "研究问题")
        self.assertEqual(q, ["子三", "子四"])
        self.assertEqual(chain.chat.call_count, 2)

    def test_retry_after_unparseable(self):
        chain = self._chain(["不是JSON也不是列表", '["子五"]'])
        q = _plan_queries(chain, "研究问题")
        self.assertEqual(q, ["子五"])
        self.assertEqual(chain.chat.call_count, 2)

    def test_all_fail_uses_fallback(self):
        chain = self._chain([ProviderError("e1"), ProviderError("e2")])
        q = _plan_queries(chain, "研究问题")
        self.assertEqual(len(q), 3)
        self.assertTrue(all("研究问题" in s for s in q))

    def test_empty_output_uses_fallback(self):
        chain = self._chain(["   "])
        q = _plan_queries(chain, "研究问题")
        self.assertEqual(len(q), 3)


class TestJoinSegmentsFallback(unittest.TestCase):
    def test_mixed_segments(self):
        segs = [
            {"sub_query": "A", "answer": "答案A", "error": None},
            {"sub_query": "B", "answer": "", "error": "boom"},
        ]
        out = _join_segments_fallback("研究问题X", segs)
        self.assertIn("答案A", out)
        self.assertIn("boom", out)
        self.assertIn("研究问题X", out)

    def test_all_failed(self):
        segs = [{"sub_query": "A", "answer": "", "error": "x"}]
        out = _join_segments_fallback("Q", segs)
        self.assertIn("均失败", out)


if __name__ == "__main__":
    unittest.main()
