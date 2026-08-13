# -*- coding: utf-8 -*-
"""搜索提供者：DeepSeekClient（全自动）/ MimoClient（raw 检索）+ FallbackChain（降级链）。
协议差异（Responses API vs Chat Completions API）全部封装在本文件内。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .models import Citation, Location, SearchResult

DEFAULT_UA = "mcp-search/0.3 (+deepseek-search-mcp)"


class ProviderError(Exception):
    """用户可见的提供者错误（网络/HTTP/解析）。"""


# ----------------------------- 通用 HTTP -----------------------------

def _http_post(url: str, body: dict, headers: Dict[str, str], timeout: int) -> "tuple[int, dict]":
    """POST JSON，返回 (status, json)。HTTP 错误 / 网络错误归一化为 dict。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": DEFAULT_UA, **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, {"error": {"message": raw[:500]}}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": {"message": raw[:500]}}
        msg = payload.get("error", {}).get("message", str(e))
        return e.code, {"error": {"message": msg}}
    except urllib.error.URLError as e:
        return 0, {"error": {"message": "网络错误: %s" % (e.reason,)}}
    except TimeoutError:
        return 0, {"error": {"message": "网络错误: 请求超时"}}
    except OSError as e:
        # ConnectionResetError / socket 类错误（Windows 长连接被重置等）
        return 0, {"error": {"message": "网络错误: %s" % (e,)}}
    except Exception as e:
        return 0, {"error": {"message": "请求异常: %s" % (e,)}}


def _parse_error(resp: dict, default: str) -> str:
    err = resp.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if msg:
            return str(msg)
    return default


def _post_with_retry(url: str, body: dict, headers: Dict[str, str], timeout: int,
                     retries: int = 1, backoff_s: float = 2.0) -> "tuple[int, dict]":
    """POST + 瞬态错误重试：网络错误（status 0）、429、5xx 各重试一次，指数退避。
    4xx（鉴权/参数错误）不重试。"""
    last = (0, {"error": {"message": "未知错误"}})
    for attempt in range(retries + 1):
        status, resp = _http_post(url, body, headers, timeout)
        if status != 0 and status != 429 and status < 500:
            return status, resp
        last = (status, resp)
        if attempt < retries:
            time.sleep(backoff_s * (attempt + 1))
    return last


# ----------------------------- DeepSeek（Responses API，全自动搜索） -----------------------------

SYS_SEARCH = (
    "你是高准确度联网搜索助手。使用 web_search 工具回答用户问题，必须："
    "1) 搜索后用真实来源核实关键事实，不凭记忆作答；"
    "2) 优先采用权威来源（官方文档、新闻社、学术来源），多来源交叉验证；"
    "3) 所有具体数字、榜单、排名、价格必须与来源一一对应，严禁把不同对象的数据张冠李戴；"
    "4) 回答末尾列出引用的来源链接；"
    "5) 信息不足或来源冲突时明确说明不确定性，不要编造。"
)

SYS_SEARCH_FAST = (
    "你是快速联网搜索助手。用户对响应速度有要求，请："
    "1) 尽量用最少的搜索轮次（通常 1-2 次）获取足够信息后直接回答；"
    "2) 答案简洁扼要，聚焦核心信息，不做冗余展开；"
    "3) 所有具体数字、榜单、排名、价格必须来自搜索结果，引用数据时注明出处，"
    "严禁把不同对象的数值张冠李戴；"
    "4) 无法从搜索结果确认的数值不要给出具体数字，明确说明不确定；"
    "5) 回答末尾附上引用的来源链接。"
)


class DeepSeekClient:
    """DeepSeek Responses API 原生 web_search：模型自动多轮搜索 + 核实 + 带引用合成。"""

    name = "deepseek"

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-v4-flash", timeout_s: int = 180,
                 max_output: int = 8192, max_output_fast: int = 2048,
                 debug: bool = False):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.max_output = max_output
        self.max_output_fast = max_output_fast
        self.debug = debug

    # -- 内部 -- #
    def _post(self, path: str, body: dict) -> "tuple[int, dict]":
        return _post_with_retry(self.base_url + path, body,
                                {"Authorization": "Bearer " + self.api_key}, self.timeout_s)

    @staticmethod
    def _extract_messages(output: Any) -> List[str]:
        parts = []
        for item in output or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text" and part.get("text"):
                    parts.append(part["text"])
        return parts

    @staticmethod
    def _build_query(query: str, location: Optional[Location]) -> str:
        if not location:
            return query
        return f"{query}\n\n（请优先检索并核实以下地区的本地相关信息：{location.to_prompt()}）"

    # -- 对外 -- #
    def search(self, query: str, fast: bool = False, location: Optional[Location] = None,
               **kw: Any) -> SearchResult:
        body = {
            "model": self.model,
            "instructions": SYS_SEARCH_FAST if fast else SYS_SEARCH,
            "input": self._build_query(query, location),
            "tools": [{"type": "web_search"}],
            "tool_choice": "auto",
            "stream": False,
            "max_output_tokens": self.max_output_fast if fast else self.max_output,
        }
        if fast:
            body["reasoning"] = {"effort": "low"}
        for attempt in range(2):  # 空结果重试一次（实测偶发）
            status, resp = self._post("/v1/responses", body)
            if status != 200:
                raise ProviderError(_parse_error(resp, f"DeepSeek HTTP {status}"))
            texts = self._extract_messages(resp.get("output"))
            answer = "\n".join(texts).strip() if texts else str(resp.get("output_text") or "").strip()
            if answer:
                from .models import extract_urls
                return SearchResult(answer=answer, sources=extract_urls(answer),
                                    usage=resp.get("usage") or {}, backend=self.name)
            if attempt == 0:
                time.sleep(2)
        raise ProviderError("DeepSeek 未返回搜索结果，请稍后重试或换个问法")

    def chat(self, instructions: str, user_msg: str, max_tokens: int = 2048) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": max_tokens,
            "stream": False,
        }
        status, resp = self._post("/v1/chat/completions", body)
        if status != 200:
            raise ProviderError(_parse_error(resp, f"DeepSeek HTTP {status}"))
        return resp["choices"][0]["message"]["content"]

    def health(self) -> dict:
        try:
            r = self.search("当前时间的最新全球头条新闻是什么？简要回答。")
            return {"ok": True, "model": self.model, "sample": r.answer[:200],
                    "sources": len(r.sources)}
        except ProviderError as e:
            return {"ok": False, "model": self.model, "error": str(e)}


# ----------------------------- MiMo（Chat Completions API，raw 检索） -----------------------------

SYS_MIMO_RAW = (
    "你是联网检索助手。用户的问题可能不需要长篇回答："
    "1) 优先基于 web_search 工具返回的实时检索结果回答；"
    "2) 直接、简洁地概括与问题直接相关的关键信息；"
    "3) 回答中可以列出关键事实与数字；"
    "4) 检索不到相关信息时如实说明，不要编造。"
)


class MimoClient:
    """小米 MiMo 联网搜索（raw 模式）：一次检索返回结构化来源 + 简短摘要，
    不做多轮编排，定位类似 Tavily 的\"检索不思考\"。"""

    name = "mimo"

    def __init__(self, api_key: str, base_url: str = "https://api.xiaomimimo.com",
                 model: str = "mimo-v2.5-pro", timeout_s: int = 180,
                 force_search: bool = True, max_keyword: int = 5, limit: int = 5,
                 max_output: int = 4096, max_output_fast: int = 2048,
                 debug: bool = False):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.force_search = force_search
        self.max_keyword = max(1, max_keyword)
        self.limit = max(1, limit)
        self.max_output = max_output
        self.max_output_fast = max_output_fast
        self.debug = debug

    # -- 内部 -- #
    def _post(self, path: str, body: dict) -> "tuple[int, dict]":
        return _post_with_retry(self.base_url + path, body, {"api-key": self.api_key}, self.timeout_s)

    @staticmethod
    def _parse_citations(annotations: Any) -> List[Citation]:
        out = []
        for a in annotations or []:
            if not isinstance(a, dict):
                continue
            url = str(a.get("url") or "").strip()
            if not url:
                continue
            out.append(Citation(
                title=str(a.get("title") or ""),
                url=url,
                site_name=str(a.get("site_name") or ""),
                publish_time=str(a.get("publish_time") or ""),
                summary=str(a.get("summary") or ""),
            ))
        return out

    def _search_body(self, query: str, fast: bool, location: Optional[Location],
                     max_keyword: Optional[int], limit: Optional[int]) -> dict:
        tool: Dict[str, Any] = {"type": "web_search", "force_search": self.force_search}
        tool["max_keyword"] = max(1, max_keyword if max_keyword else
                                  (self.max_keyword // 2 if fast else self.max_keyword))
        tool["limit"] = max(1, limit if limit else (self.limit // 2 if fast else self.limit))
        loc = location.to_mimo() if location else None
        if loc:
            tool["user_location"] = loc
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYS_MIMO_RAW},
                {"role": "user", "content": query},
            ],
            "tools": [tool],
            "tool_choice": "auto",
            "max_completion_tokens": self.max_output_fast if fast else self.max_output,
            "stream": False,
            "thinking": {"type": "disabled"},  # 避免思考模式导致 content 为空/不稳定
        }

    # -- 对外 -- #
    def search(self, query: str, fast: bool = False, location: Optional[Location] = None,
               **kw: Any) -> SearchResult:
        max_keyword = kw.get("max_keyword")
        limit = kw.get("limit")
        body = self._search_body(query, fast, location, max_keyword, limit)
        for attempt in range(2):  # 空结果重试一次（实测偶发）
            status, resp = self._post("/v1/chat/completions", body)
            if status != 200:
                raise ProviderError(_parse_error(resp, f"MiMo HTTP {status}"))
            choices = resp.get("choices") or []
            if not choices:
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise ProviderError("MiMo 未返回结果")
            msg = choices[0].get("message") or {}
            answer = str(msg.get("content") or "").strip()
            if not answer:
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise ProviderError("MiMo 未返回搜索内容，请稍后重试")
            citations = self._parse_citations(msg.get("annotations"))
            sources = []
            for c in citations:
                if c.url not in sources:
                    sources.append(c.url)
            from .models import extract_urls
            for u in extract_urls(answer):
                if u not in sources:
                    sources.append(u)
            usage = resp.get("usage") or {}
            return SearchResult(answer=answer, sources=sources, citations=citations,
                                usage=usage, backend=self.name,
                                finish_reason=choices[0].get("finish_reason"))
        raise ProviderError("MiMo 未返回搜索内容，请稍后重试")

    def chat(self, instructions: str, user_msg: str, max_tokens: int = 2048) -> str:
        """普通 Chat Completion（不启用搜索），用于深搜规划/综合的降级路径。"""
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": user_msg},
            ],
            "max_completion_tokens": max_tokens,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        status, resp = self._post("/v1/chat/completions", body)
        if status != 200:
            raise ProviderError(_parse_error(resp, f"MiMo HTTP {status}"))
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        content = str(msg.get("content") or "").strip()
        if not content:
            reasoning = str(msg.get("reasoning_content") or "").strip()
            hint = "（仅返回推理内容，疑似 thinking 模式或输出被截断）" if reasoning else ""
            raise ProviderError("MiMo 未返回内容%s" % hint)
        return content

    def health(self) -> dict:
        try:
            r = self.search("当前时间的最新全球头条新闻是什么？简要回答。")
            return {"ok": True, "model": self.model, "sample": r.answer[:200],
                    "sources": len(r.sources), "web_search_usage": r.usage.get("web_search_usage")}
        except ProviderError as e:
            return {"ok": False, "model": self.model, "error": str(e)}


# ----------------------------- Fallback 链 -----------------------------

class FallbackChain:
    """有序后端降级链：逐个尝试，返回首个成功；全部失败聚合错误。

    - BACKEND=deepseek -> [deepseek]        仅 DeepSeek
    - BACKEND=mimo     -> [mimo]            仅 MiMo（独立基础服务）
    - BACKEND=auto     -> [deepseek, mimo]  DeepSeek 优先，失败自动降级 MiMo
    """

    def __init__(self, backends: List[Any]):
        if not backends:
            raise ProviderError("没有任何可用的搜索后端（请检查 API Key 配置）")
        self.backends = backends

    @property
    def names(self) -> List[str]:
        return [b.name for b in self.backends]

    def search(self, query: str, fast: bool = False, location: Optional[Location] = None,
               **kw: Any) -> SearchResult:
        errors = []
        for b in self.backends:
            try:
                return b.search(query, fast=fast, location=location, **kw)
            except ProviderError as e:
                errors.append("%s: %s" % (b.name, e))
        raise ProviderError("所有后端均失败: " + " | ".join(errors))

    def chat(self, instructions: str, user_msg: str, max_tokens: int = 2048) -> str:
        errors = []
        for b in self.backends:
            try:
                return b.chat(instructions, user_msg, max_tokens)
            except ProviderError as e:
                errors.append("%s: %s" % (b.name, e))
        raise ProviderError("所有后端 chat 调用均失败: " + " | ".join(errors))

    def health(self) -> dict:
        checks = {}
        for b in self.backends:
            checks[b.name] = b.health()
        ok = all(c.get("ok") for c in checks.values())
        return {"ok": ok, "backends": checks, "chain": self.names}


# ----------------------------- 配置解析 -----------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name)
    if not v:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def build_mimo_client() -> Optional[MimoClient]:
    """独立构造 MiMo 客户端（mimo_search 工具使用，不依赖 BACKEND 链）。无 key 返回 None。"""
    mm_key = _env("MIMO_API_KEY")
    if not mm_key:
        return None
    return MimoClient(
        mm_key, _env("MIMO_BASE_URL", "https://api.xiaomimimo.com"),
        _env("MIMO_MODEL", "mimo-v2.5-pro"),
        _env_int("MIMO_TIMEOUT_S", 180),
        _env_bool("MIMO_FORCE_SEARCH", True),
        _env_int("MIMO_MAX_KEYWORD", 5),
        _env_int("MIMO_LIMIT", 5),
        _env_int("MIMO_MAX_OUTPUT", 4096),
        _env_int("MIMO_MAX_OUTPUT_FAST", 2048),
        _env_bool("MIMO_DEBUG", False),
    )


def build_backends(backend_mode: Optional[str] = None,
                   deepseek_key: Optional[str] = None,
                   mimo_key: Optional[str] = None) -> FallbackChain:
    """按 BACKEND 模式构建降级链。缺失 Key 的后端自动跳过（auto 模式）。"""
    mode = (backend_mode or _env("BACKEND", "deepseek")).lower()
    if mode not in ("deepseek", "mimo", "auto"):
        raise ProviderError("BACKEND 取值无效（应为 deepseek / mimo / auto）")

    ds_key = deepseek_key if deepseek_key is not None else _env("DEEPSEEK_API_KEY")
    mm_key = mimo_key if mimo_key is not None else _env("MIMO_API_KEY")

    def _ds():
        return DeepSeekClient(
            ds_key, _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            _env("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            _env_int("DEEPSEEK_TIMEOUT_S", 180),
            _env_int("DEEPSEEK_MAX_OUTPUT", 8192),
            _env_int("DEEPSEEK_MAX_OUTPUT_FAST", 2048),
            _env_bool("DEEPSEEK_DEBUG", False),
        )

    def _mm():
        return MimoClient(
            mm_key, _env("MIMO_BASE_URL", "https://api.xiaomimimo.com"),
            _env("MIMO_MODEL", "mimo-v2.5-pro"),
            _env_int("MIMO_TIMEOUT_S", 180),
            _env_bool("MIMO_FORCE_SEARCH", True),
            _env_int("MIMO_MAX_KEYWORD", 5),
            _env_int("MIMO_LIMIT", 5),
            _env_int("MIMO_MAX_OUTPUT", 4096),
            _env_int("MIMO_MAX_OUTPUT_FAST", 2048),
            _env_bool("MIMO_DEBUG", False),
        )

    backends = []
    if mode in ("deepseek", "auto"):
        if ds_key:
            backends.append(_ds())
        elif mode == "deepseek":
            raise ProviderError("未设置环境变量 DEEPSEEK_API_KEY（BACKEND=deepseek 必须配置）")
    if mode in ("mimo", "auto"):
        if mm_key:
            backends.append(_mm())
        elif mode == "mimo":
            raise ProviderError("未设置环境变量 MIMO_API_KEY（BACKEND=mimo 必须配置）")
    if not backends:
        raise ProviderError("未配置任何 API Key：需要 DEEPSEEK_API_KEY 或 MIMO_API_KEY（BACKEND=%s）" % mode)
    return FallbackChain(backends)
