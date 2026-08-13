# -*- coding: utf-8 -*-
"""models.py 单元测试：Location 双格式解析 / extract_urls。"""
from __future__ import annotations

import sys
import unittest

sys.path.insert(0, ".")  # 允许直接运行

from mcp_search.models import Location, extract_urls


class TestLocationParse(unittest.TestCase):
    def test_dict_direct(self):
        loc = Location.parse({"country": "中国", "region": "湖北", "city": "武汉"})
        self.assertEqual(loc.country, "中国")
        self.assertEqual(loc.region, "湖北")
        self.assertEqual(loc.city, "武汉")

    def test_dict_partial(self):
        loc = Location.parse({"region": "北京"})
        self.assertIsNone(loc.country)
        self.assertEqual(loc.region, "北京")
        self.assertIsNone(loc.city)

    def test_dict_empty(self):
        self.assertIsNone(Location.parse({}))
        self.assertIsNone(Location.parse({"country": "", "region": None}))

    def test_cn_two_level(self):
        loc = Location.parse("湖北省武汉市")
        self.assertIsNone(loc.country)
        self.assertEqual(loc.region, "湖北省")
        self.assertEqual(loc.city, "武汉市")

    def test_cn_with_country(self):
        loc = Location.parse("中国湖北省武汉市")
        self.assertEqual(loc.country, "中国")
        self.assertEqual(loc.region, "湖北省")
        self.assertEqual(loc.city, "武汉市")

    def test_cn_municipality(self):
        loc = Location.parse("北京市")
        self.assertEqual(loc.region, "北京市")
        self.assertIsNone(loc.city)

    def test_cn_single_city(self):
        loc = Location.parse("武汉市")
        self.assertEqual(loc.region, "武汉市")
        self.assertIsNone(loc.city)

    def test_cn_autonomous_region(self):
        loc = Location.parse("新疆维吾尔自治区乌鲁木齐市")
        self.assertEqual(loc.region, "新疆维吾尔自治区")
        self.assertEqual(loc.city, "乌鲁木齐市")

    def test_cn_hk(self):
        loc = Location.parse("香港特别行政区")
        self.assertEqual(loc.region, "香港特别行政区")

    def test_cn_district_appended(self):
        loc = Location.parse("中国湖北省武汉市洪山区")
        self.assertEqual(loc.country, "中国")
        self.assertEqual(loc.region, "湖北省")
        self.assertEqual(loc.city, "武汉市洪山区")

    def test_en_country_last(self):
        loc = Location.parse("Wuhan, Hubei, China")
        self.assertEqual(loc.country, "中国")
        self.assertEqual(loc.region, "Hubei")
        self.assertEqual(loc.city, "Wuhan")

    def test_en_country_first(self):
        loc = Location.parse("China, Hubei, Wuhan")
        self.assertEqual(loc.country, "中国")
        self.assertEqual(loc.region, "Hubei")
        self.assertEqual(loc.city, "Wuhan")

    def test_en_no_country(self):
        loc = Location.parse("Hubei, Wuhan")
        self.assertIsNone(loc.country)
        self.assertEqual(loc.region, "Hubei")
        self.assertEqual(loc.city, "Wuhan")

    def test_plain_english(self):
        loc = Location.parse("Silicon Valley")
        self.assertEqual(loc.region, "Silicon Valley")

    def test_null_and_types(self):
        self.assertIsNone(Location.parse(None))
        self.assertIsNone(Location.parse(""))
        self.assertIsNone(Location.parse("   "))
        self.assertIsNone(Location.parse(42))
        self.assertIsNone(Location.parse(3.14))

    def test_to_mimo(self):
        loc = Location.parse("中国湖北省武汉市")
        self.assertEqual(loc.to_mimo(), {"type": "approximate", "country": "中国",
                                         "region": "湖北省", "city": "武汉市"})
        self.assertIsNone(Location().to_mimo())
        self.assertEqual(Location(country="中国").to_mimo(),
                         {"type": "approximate", "country": "中国"})

    def test_to_prompt(self):
        loc = Location.parse("中国湖北省武汉市")
        self.assertEqual(loc.to_prompt(), "中国/湖北省/武汉市")


class TestExtractUrls(unittest.TestCase):
    def test_markdown_links(self):
        text = "参考 [官方文档](https://example.com/doc) 与 [新闻](https://news.example.com/a)。"
        self.assertEqual(extract_urls(text), ["https://example.com/doc", "https://news.example.com/a"])

    def test_bare_urls(self):
        text = "来源 https://a.com/x 和 https://b.com/y?q=1。"
        self.assertEqual(extract_urls(text), ["https://a.com/x", "https://b.com/y?q=1"])

    def test_dedupe_preserve_order(self):
        text = "https://a.com/x 再次 https://a.com/x 然后 https://b.com/y"
        self.assertEqual(extract_urls(text), ["https://a.com/x", "https://b.com/y"])

    def test_empty(self):
        self.assertEqual(extract_urls("没有链接"), [])
        self.assertEqual(extract_urls(""), [])
        self.assertEqual(extract_urls(None), [])


if __name__ == "__main__":
    unittest.main()
