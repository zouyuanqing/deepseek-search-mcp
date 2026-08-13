# -*- coding: utf-8 -*-
"""统一数据模型：Location / Citation / SearchResult / HealthReport。
工具层与适配器层只通过这里的模型交互，后端协议差异被隔离在 providers.py。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ----------------------------- Location -----------------------------

_CN_ADMIN_SUFFIXES = (
    "特别行政区",
    "自治区",
    "省",
    "市",
    "地区",
    "自治州",
    "盟",
    "自治县",
    "县",
    "区",
)

_CN_PREFIXES = ("中华人民共和国", "中国")


@dataclass
class Location:
    """结构化地理位置。country / region / city 均可选。"""

    country: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None

    @classmethod
    def parse(cls, raw: Any) -> "Optional[Location]":
        """双格式解析：结构化 dict 或自由文本字符串（启发式拆分为行政层级）。

        - {"country": "中国", "region": "湖北", "city": "武汉"} -> 直接映射
        - "湖北省武汉市" / "中国湖北省武汉市" / "新疆维吾尔自治区乌鲁木齐市"
        - "北京市"（直辖市单级）-> region=北京市
        - "Wuhan, Hubei, China"（英文逗号分隔，识别中国/常见国家）
        - 无法解析 -> None（表示未提供位置）
        """
        if raw is None:
            return None
        if isinstance(raw, dict):
            return cls._from_dict(raw)
        if not isinstance(raw, str):
            return None
        return cls._from_text(raw.strip())

    @staticmethod
    def _from_dict(raw: Dict[str, Any]) -> "Optional[Location]":
        def _v(key: str) -> Optional[str]:
            v = raw.get(key)
            if v is None:
                return None
            s = str(v).strip()
            return s or None

        loc = Location(country=_v("country"), region=_v("region"), city=_v("city"))
        return loc if (loc.country or loc.region or loc.city) else None

    @classmethod
    def _from_text(cls, text: str) -> "Optional[Location]":
        if not text:
            return None
        loc = Location()

        # 1. 国家前缀
        for prefix in _CN_PREFIXES:
            if text.startswith(prefix):
                loc.country = "中国"
                text = text[len(prefix):].strip("，,、 ")
                break
        if loc.country is None:
            m = re.match(r"^(cn|china)\b", text, re.I)
            if m:
                loc.country = "中国"
                text = text[m.end():].strip("，,、 ")

        # 2. 英文（无中文）: 逗号分隔，识别已知国家，其余按地址习惯分配
        if not re.search(r"[\u4e00-\u9fff]", text):
            parts = [p.strip() for p in re.split(r"[,，、/|;；]", text) if p.strip()]
            if parts:
                if loc.country is None:
                    known = {"china": "中国", "usa": "美国", "us": "美国", "uk": "英国",
                             "japan": "日本", "korea": "韩国", "south korea": "韩国",
                             "germany": "德国", "france": "法国", "russia": "俄罗斯",
                             "india": "印度", "canada": "加拿大", "australia": "澳大利亚",
                             "brazil": "巴西"}
                    ci = next((i for i, p in enumerate(parts) if p.lower() in known), None)
                    if ci is not None:
                        loc.country = known[parts[ci].lower()]
                        rest = parts[:ci] + parts[ci + 1:]
                        if len(rest) >= 2:
                            if ci == len(parts) - 1:          # 国家在末尾: [city, region]
                                loc.city, loc.region = rest[0], rest[1]
                            else:                              # 国家在开头: [region, city]
                                loc.region, loc.city = rest[0], rest[1]
                        elif rest:
                            loc.region = rest[0]
                    else:
                        if len(parts) >= 2:
                            loc.region, loc.city = parts[0], parts[1]
                        else:
                            loc.region = parts[0]
                else:  # 前缀已剥离国家，剩余从大到小: [region, city]
                    if len(parts) >= 2:
                        loc.region, loc.city = parts[0], parts[1]
                    elif parts:
                        loc.region = parts[0]
            return loc if (loc.country or loc.region or loc.city) else None

        # 3. 中文行政层级切分
        def cut(text: str, suffixes) -> "tuple[Optional[str], str]":
            for suf in suffixes:
                idx = text.find(suf)
                if idx >= 0:
                    return text[: idx + len(suf)], text[idx + len(suf):].strip("，,、 ")
            return None, text

        region, rest = cut(text, ("特别行政区", "自治区", "省"))
        if region is None:
            region, rest = cut(text, ("市", "地区", "自治州", "盟"))
        if region:
            loc.region = region

        city, rest2 = cut(rest, ("市", "地区", "自治州", "盟", "自治县", "县", "区"))
        if city:
            loc.city = city
            if rest2:  # "武汉市洪山区" -> city=武汉市, rest=洪山区, 追加提升精度
                loc.city = loc.city + rest2
        elif rest:
            loc.city = rest

        return loc if (loc.country or loc.region or loc.city) else None

    def to_mimo(self) -> Optional[dict]:
        """映射为 MiMo user_location 对象（只含非空字段）。"""
        m = {"type": "approximate"}
        if self.country:
            m["country"] = self.country
        if self.region:
            m["region"] = self.region
        if self.city:
            m["city"] = self.city
        return m if len(m) > 1 else None

    def to_prompt(self) -> str:
        """拼成文本，供 DeepSeek 侧注入查询上下文。"""
        parts = [p for p in (self.country, self.region, self.city) if p]
        return "/".join(parts)

    def to_dict(self) -> dict:
        return {"country": self.country, "region": self.region, "city": self.city}


# ----------------------------- 搜索结果 -----------------------------

@dataclass
class Citation:
    """单条引用来源。"""

    title: str = ""
    url: str = ""
    site_name: str = ""
    publish_time: str = ""
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "site_name": self.site_name,
            "publish_time": self.publish_time,
            "summary": self.summary,
        }


@dataclass
class SearchResult:
    """统一搜索结果：答案正文 + 来源。backend 标明实际执行的后端。"""

    answer: str
    sources: List[str] = field(default_factory=list)
    citations: List[Citation] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    backend: str = ""
    finish_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "sources": self.sources,
            "citations": [c.to_dict() for c in self.citations],
            "usage": self.usage,
            "backend": self.backend,
            "finish_reason": self.finish_reason,
        }


@dataclass
class HealthReport:
    """健康检查结果。"""

    status: str
    checks: Dict[str, Any] = field(default_factory=dict)
    detail: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "checks": self.checks, "detail": self.detail}


# ----------------------------- 工具函数 -----------------------------

def extract_urls(text: str) -> List[str]:
    """从 markdown 正文提取来源 URL（[text](url) 或裸 http(s) 链接），去重保序。"""
    urls: List[str] = []
    for m in re.finditer(r"\[[^\]]*\]\(((?:https?://)[^)\s]+)\)", text or ""):
        urls.append(m.group(1))
    for m in re.finditer(r"(?<![\w])(https?://[^\s)\]]+)", text or ""):
        u = m.group(1).rstrip(".,;:!?。，、；：！？")
        if u not in urls:
            urls.append(u)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
