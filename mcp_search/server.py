#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 服务器入口：stdio + JSON-RPC 2.0（Content-Length 帧，兼容 NDJSON 客户端）。

工具：
  web_search       DeepSeek 全自动联网搜索（支持 location）
  web_search_fast  快速模式（低推理强度 + 精简指令）
  web_search_deep  深搜：拆解子查询 -> 多路检索 -> 交叉核验综合（支持 enrich_mimo 注入 MiMo 来源）
  mimo_search      MiMo raw 检索（类似 Tavily，返回结构化来源 + 摘要）
  fetch_page       抓取 URL 转纯文本，供 agent 验证来源
  health           诊断各后端与 fetch 链路

配置环境变量：
  BACKEND          deepseek | mimo | auto（默认 deepseek；auto 时 DeepSeek 失败自动降级 MiMo）
  DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL / DEEPSEEK_TIMEOUT_S
  DEEPSEEK_MAX_OUTPUT / DEEPSEEK_MAX_OUTPUT_FAST / DEEPSEEK_DEBUG
  MIMO_API_KEY / MIMO_BASE_URL / MIMO_MODEL / MIMO_TIMEOUT_S
  MIMO_FORCE_SEARCH / MIMO_MAX_KEYWORD / MIMO_LIMIT / MIMO_MAX_OUTPUT / MIMO_MAX_OUTPUT_FAST / MIMO_DEBUG
  MCP_FETCH_TIMEOUT_S / MCP_FETCH_MAX_CHARS（fetch_page 默认值）
  DEBUG              设 1 输出调试日志到 stderr
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .fetch import fetch_page
from .models import Location, extract_urls
from .providers import (FallbackChain, ProviderError, build_backends,
                        build_mimo_client)


# ----------------------------- 配置 -----------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


DEBUG = _env("DEBUG", "0") == "1"
ALLOW_PRIVATE = _env("MCP_FETCH_ALLOW_PRIVATE", "0") == "1"

MAX_OUTPUT = _env_int("DEEPSEEK_MAX_OUTPUT", 8192)
FETCH_TIMEOUT = _env_int("MCP_FETCH_TIMEOUT_S", 20)
FETCH_MAX_CHARS = _env_int("MCP_FETCH_MAX_CHARS", 20000)


def log(*args):
    if DEBUG:
        print("[mcp-search]", *args, file=sys.stderr, flush=True)


# ----------------------------- 运行时（后端链 / 独立 MiMo） -----------------------------

CHAIN: Optional[FallbackChain] = None
CHAIN_ERROR: Optional[str] = None
MIMO: Any = None  # 独立 MiMo 客户端（mimo_search 工具用）


def init_runtime():
    global CHAIN, CHAIN_ERROR, MIMO
    try:
        CHAIN = build_backends()
    except ProviderError as e:
        CHAIN = None
        CHAIN_ERROR = str(e)
    MIMO = build_mimo_client()
    log("backend chain:", CHAIN.names if CHAIN else "N/A", "mimo_independent:", bool(MIMO))


def _require_chain() -> FallbackChain:
    if CHAIN is None:
        raise ProviderError(CHAIN_ERROR or "没有可用的搜索后端")
    return CHAIN


# ----------------------------- 深搜编排 -----------------------------

SYS_DEEP_PLAN = (
    "你是搜索规划器。用户会给出一个需要深度调研的问题。"
    "请拆解为若干独立、具体、互补的子查询，覆盖问题的不同角度与关键事实。"
    "只输出 JSON 数组，形如 [\"子查询1\", \"子查询2\", ...]，不要输出其他内容。"
)

SYS_DEEP_SYNTH = (
    "你是高准确度调研分析师。以下是针对同一问题的多路搜索结果（各自独立搜索、来源独立）。"
    "请交叉核验：1) 提取各来源一致确认的事实；2) 标注来源冲突或存疑之处；"
    "3) 综合成结构化的最终答案，按要点组织；4) 每个要点后附来源链接。"
    "信息不足时明确说明，不要编造。"
)


def _parse_queries(raw: str, limit: int = 6) -> List[str]:
    """解析深搜子查询。优先 JSON 数组，回退到按行/编号提取。"""
    if not raw or not raw.strip():
        raise ProviderError("子查询输出为空")
    text = raw.strip()
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            queries = json.loads(m.group(0))
            queries = [str(q).strip() for q in queries if str(q).strip()]
            if queries:
                return queries[:limit]
        except json.JSONDecodeError:
            pass
    lines = [ln for ln in re.split(r"\n+", text) if ln.strip()]
    queries = []
    for ln in lines:
        s = re.sub(r"^\s*(?:[0-9]+[.)、]\s*|[-*•]\s*|\"|')", "", ln).strip()
        s = re.sub(r"[\"']$", "", s).strip()
        if s and len(s) >= 3:
            queries.append(s)
    # 单行且无编号/符号前缀的纯文本不是查询列表
    if len(lines) == 1 and not re.match(r"^\s*(?:[0-9]+[.)、]|[-*•])", lines[0]):
        queries = []
    if not queries:
        raise ProviderError("无法解析子查询输出")
    return queries[:limit]


def _fallback_queries(query: str) -> List[str]:
    """规划失败/解析失败时的默认子查询（保证深搜始终可推进）。"""
    return [
        "%s（最新进展与官方信息）" % query,
        "%s（关键数据与事实）" % query,
        "%s（争议、不同观点与风险）" % query,
    ]


def _plan_queries(chain: FallbackChain, query: str) -> List[str]:
    """生成深搜子查询：chat 规划 + 1 次重试；仍失败则回退默认拆解。"""
    last_err = None
    for attempt in range(2):
        try:
            plan_raw = chain.chat(SYS_DEEP_PLAN, query, max_tokens=1024)
            sub_queries = _parse_queries(plan_raw)
            if sub_queries:
                return sub_queries
        except Exception as e:  # 规划阶段任何异常（含空输出/解析失败/网络）均可回退
            last_err = e
            if attempt == 0:
                log("plan 失败，重试一次:", e)
                time.sleep(1)
                continue
            break
    log("plan 失败，使用默认子查询拆解:", last_err)
    return _fallback_queries(query)


def _join_segments_fallback(query: str, segments: List[dict]) -> str:
    """综合调用失败时的降级答案：直接拼接各子查询检索结果。"""
    parts = ["（综合调用失败，以下为各子查询检索结果的直接汇总）",
             "研究问题：%s" % query, ""]
    ok = 0
    for i, seg in enumerate(segments, 1):
        if seg.get("answer"):
            ok += 1
            parts.append("--- 子问题%d: %s ---\n%s" % (i, seg["sub_query"], seg["answer"]))
        else:
            parts.append("--- 子问题%d: %s ---\n(检索失败: %s)" % (i, seg["sub_query"], seg.get("error")))
    if ok == 0:
        parts.append("所有子查询检索均失败，请稍后重试。")
    return "\n\n".join(parts)


# ----------------------------- 工具实现 -----------------------------

def tool_web_search(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ProviderError("参数 query 不能为空")
    loc = Location.parse(args.get("location"))
    t0 = time.time()
    chain = _require_chain()
    r = chain.search(query, location=loc)
    return {
        "query": query,
        "location": loc.to_dict() if loc else None,
        "answer": r.answer,
        "sources": r.sources,
        "citations": [c.to_dict() for c in r.citations],
        "backend": r.backend,
        "usage": r.usage,
        "elapsed_s": round(time.time() - t0, 1),
    }


def tool_web_search_fast(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ProviderError("参数 query 不能为空")
    loc = Location.parse(args.get("location"))
    t0 = time.time()
    chain = _require_chain()
    r = chain.search(query, fast=True, location=loc)
    return {
        "query": query,
        "mode": "fast",
        "location": loc.to_dict() if loc else None,
        "answer": r.answer,
        "sources": r.sources,
        "citations": [c.to_dict() for c in r.citations],
        "backend": r.backend,
        "usage": r.usage,
        "elapsed_s": round(time.time() - t0, 1),
    }


def tool_web_search_deep(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ProviderError("参数 query 不能为空")
    loc = Location.parse(args.get("location"))
    enrich = bool(args.get("enrich_mimo", False))
    t0 = time.time()
    chain = _require_chain()

    sub_queries = _plan_queries(chain, query)

    segments = []
    for i, sq in enumerate(sub_queries, 1):
        seg: Dict[str, Any] = {"sub_query": sq, "answer": "", "sources": [],
                               "backend": "", "error": None}
        try:
            r = chain.search(f"子问题{i}: {sq}", location=loc)
            seg.update(answer=r.answer, sources=r.sources, backend=r.backend)
        except ProviderError as e:
            seg["error"] = str(e)
        # 可选：注入 MiMo raw 检索作为补充来源（子查询已由 MiMo 完成时跳过，避免重复检索）
        if enrich and not seg["error"] and MIMO is not None and seg["backend"] != "mimo":
            try:
                mr = MIMO.search(f"子问题{i}: {sq}", location=loc)
                if mr.citations:
                    seg["mimo_extra"] = {
                        "answer": mr.answer,
                        "sources": mr.sources,
                    }
            except ProviderError as e:
                log("enrich_mimo failed:", e)
        segments.append(seg)

    parts = []
    for i, seg in enumerate(segments, 1):
        p = "--- 子问题%d: %s ---\n%s" % (i, seg["sub_query"], seg["answer"] or "(检索失败)")
        if seg.get("mimo_extra"):
            p += "\n[MiMo 补充检索]\n" + seg["mimo_extra"]["answer"]
        parts.append(p)
    try:
        synth = chain.chat(SYS_DEEP_SYNTH,
                           "研究问题：%s\n\n多路搜索结果：\n%s" % (query, "\n".join(parts)),
                           max_tokens=MAX_OUTPUT)
    except ProviderError as e:
        log("综合调用失败，降级为子查询结果直接汇总:", e)
        synth = _join_segments_fallback(query, segments)

    all_urls: List[str] = []
    for seg in segments:
        for u in seg["sources"]:
            if u not in all_urls:
                all_urls.append(u)
        for u in seg.get("mimo_extra", {}).get("sources", []):
            if u not in all_urls:
                all_urls.append(u)
    for u in extract_urls(synth):
        if u not in all_urls:
            all_urls.append(u)

    return {
        "query": query,
        "location": loc.to_dict() if loc else None,
        "enrich_mimo": enrich,
        "answer": synth,
        "sub_queries": sub_queries,
        "segments": segments,
        "sources": all_urls,
        "backend_chain": chain.names,
        "elapsed_s": round(time.time() - t0, 1),
    }


def tool_mimo_search(args: dict) -> dict:
    """MiMo raw 检索（独立于 BACKEND 链）：返回结构化来源 + 摘要，不做编排。"""
    if MIMO is None:
        raise ProviderError("未配置 MIMO_API_KEY，无法使用 mimo_search")
    query = str(args.get("query") or "").strip()
    if not query:
        raise ProviderError("参数 query 不能为空")
    loc = Location.parse(args.get("location"))
    fast = bool(args.get("fast", False))
    max_keyword = args.get("max_keyword")
    limit = args.get("limit")
    for name, v in (("max_keyword", max_keyword), ("limit", limit)):
        if v is not None:
            try:
                v = int(v)
                if not (1 <= v <= 20):
                    raise ValueError
                if name == "max_keyword":
                    max_keyword = v
                else:
                    limit = v
            except (TypeError, ValueError):
                raise ProviderError("参数 %s 必须是 1-20 的整数" % name)
    t0 = time.time()
    r = MIMO.search(query, fast=fast, location=loc, max_keyword=max_keyword, limit=limit)
    return {
        "query": query,
        "mode": "fast" if fast else "standard",
        "location": loc.to_dict() if loc else None,
        "answer": r.answer,
        "sources": r.sources,
        "citations": [c.to_dict() for c in r.citations],
        "backend": r.backend,
        "usage": r.usage,
        "elapsed_s": round(time.time() - t0, 1),
    }


def tool_fetch_page(args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    if not url:
        raise ProviderError("参数 url 不能为空")
    max_chars = args.get("max_chars")
    try:
        max_chars = int(max_chars) if max_chars else FETCH_MAX_CHARS
        max_chars = min(max_chars, 100000)
    except (TypeError, ValueError):
        max_chars = FETCH_MAX_CHARS
    t0 = time.time()
    result = fetch_page(url, timeout=FETCH_TIMEOUT, max_chars=max_chars,
                        allow_private=ALLOW_PRIVATE)
    result["elapsed_s"] = round(time.time() - t0, 1)
    return result


def tool_health(args=None) -> dict:
    checks: Dict[str, Any] = {}
    if CHAIN is not None:
        checks["chain"] = CHAIN.names
        for name, rep in CHAIN.health()["backends"].items():
            checks[name] = rep
    elif CHAIN_ERROR:
        checks["chain_error"] = CHAIN_ERROR
    if MIMO is not None:
        checks["mimo"] = MIMO.health()
    checks["fetch"] = {"ok": True, "timeout_s": FETCH_TIMEOUT, "max_chars": FETCH_MAX_CHARS}
    status = "ok" if (checks.get("chain") or checks.get("mimo", {}).get("ok")) else "error"
    return {"status": status, **checks}


# ----------------------------- MCP 协议（stdio, JSON-RPC 2.0） -----------------------------

class McpParamError(Exception):
    """参数校验错误 -> JSON-RPC -32602。"""


HANDLERS = {
    "web_search": tool_web_search,
    "web_search_fast": tool_web_search_fast,
    "web_search_deep": tool_web_search_deep,
    "mimo_search": tool_mimo_search,
    "fetch_page": tool_fetch_page,
    "health": tool_health,
}

_LOCATION_SCHEMA = {
    "type": ["object", "string", "null"],
    "description": (
        "可选，搜索位置。支持两种格式：1) 结构化对象 {\"country\":\"中国\",\"region\":\"湖北\",\"city\":\"武汉\"}；"
        "2) 文本（自动拆分行政层级，如 \"湖北省武汉市\" 或 \"武汉\"）。用于本地化检索，"
        "MiMo 映射 user_location，DeepSeek 注入查询上下文。"
    ),
}

TOOLS = [
    {
        "name": "web_search",
        "description": (
            "联网搜索（标准模式）。DeepSeek V4-Flash 原生联网搜索：自动规划多次搜索、"
            "阅读并核实来源、返回带引用的结构化答案。BACKEND=auto 时 DeepSeek 失败自动降级 MiMo。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "要搜索的问题或主题，建议写成完整问题以提高相关性（例：\"2026年8月特斯拉股价走势如何？\"）"},
                "location": _LOCATION_SCHEMA,
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_search_fast",
        "description": (
            "联网搜索（快速模式）。低推理强度 + 精简指令优先响应速度，"
            "通常 1-2 轮搜索后直接给出简洁答案。适合对时效敏感、无需深度核验的查询。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "要搜索的问题或主题（例：\"2026年8月9日美元兑人民币汇率\"）"},
                "location": _LOCATION_SCHEMA,
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_search_deep",
        "description": (
            "深度联网搜索（高准确性模式）。先把问题拆解为多个子查询分别检索，"
            "再交叉核验、综合成带引用的最终答案。适合事实核查、研究报告、技术调研。耗时更长。"
            "enrich_mimo=true 时，每路子查询额外用 MiMo 原始检索补充来源。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "要深度调研的问题，需具体、可拆解"},
                "location": _LOCATION_SCHEMA,
                "enrich_mimo": {"type": "boolean", "default": False,
                                "description": "是否用 MiMo 原始检索补充每路子查询的来源（需配置 MIMO_API_KEY）"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "mimo_search",
        "description": (
            "小米 MiMo 联网检索（raw 模式，类似 Tavily：检索不思考）。"
            "直接调用 MiMo API 返回结构化来源列表（标题/链接/站点/发布时间）+ 简短摘要，不做多轮编排。"
            "适合作为 DeepSeek 全自动搜索的补充来源，或供 agent 自行核实。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的关键词或问题"},
                "location": _LOCATION_SCHEMA,
                "fast": {"type": "boolean", "default": False,
                         "description": "true 时减少关键词与结果数，更快返回"},
                "max_keyword": {"type": "integer", "minimum": 1, "maximum": 20,
                                "description": "可选，本轮最大搜索关键词数（默认 5）"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                          "description": "可选，返回的搜索结果条数（默认 5）"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_page",
        "description": (
            "抓取指定 URL 并提取为纯文本（HTML 去噪 / JSON 原样），供验证搜索来源的真实内容。"
            "内置 SSRF 防护，拒绝内网/保留地址。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http(s) URL"},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 100000,
                              "description": "可选，提取文本上限（默认 20000）"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "health",
        "description": "健康检查：验证 DeepSeek / MiMo / fetch 链路状态。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text_result(rid, obj, is_error=False):
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2)
    return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}}


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


_USE_NDJSON = False


def write_frame(stream, obj):
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if _USE_NDJSON:
        stream.write(payload + b"\n")
    else:
        stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8"))
        stream.write(payload)
    stream.flush()


def read_frame(stream):
    first = stream.readline()
    if not first:
        return None
    line = first.strip()
    if line.startswith(b"{"):
        global _USE_NDJSON
        try:
            msg = json.loads(line.decode("utf-8"))
            _USE_NDJSON = True
            return msg
        except json.JSONDecodeError:
            pass
    content_length = 0
    if first.strip().lower().startswith(b"content-length:"):
        content_length = int(first.split(b":", 1)[1].strip() or 0)
    else:
        if b":" in first:
            k, v = first.split(b":", 1)
            if k.strip().lower() == b"content-length":
                content_length = int(v.strip() or 0)
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        if b":" in line:
            k, v = line.split(b":", 1)
            if k.strip().lower() == b"content-length":
                content_length = int(v.strip() or 0)
    if content_length <= 0:
        return None
    payload = stream.read(content_length)
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def handle_message(msg):
    if not isinstance(msg, dict):
        return None
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        req_pv = (msg.get("params") or {}).get("protocolVersion", "2025-06-18")
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": req_pv,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "deepseek-search-mcp", "version": __version__},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            return _error(rid, -32601, "未知工具: %s" % name)
        required = tool["inputSchema"].get("required", [])
        missing = [r for r in required if r not in args or args[r] in (None, "")]
        if missing:
            return _error(rid, -32602, "缺少必需参数: %s" % ", ".join(missing))
        handler = HANDLERS.get(name)
        if handler is None:
            return _error(rid, -32601, "工具未实现: %s" % name)
        try:
            result = handler(args)
            return _text_result(rid, result)
        except McpParamError as e:
            return _error(rid, -32602, "Invalid params: %s" % e)
        except ProviderError as e:
            return _text_result(rid, "错误: %s" % e, is_error=True)
        except ValueError as e:
            return _text_result(rid, "错误: %s" % e, is_error=True)
        except Exception as e:
            log("tool crash:", name, repr(e))
            return _text_result(rid, "内部错误: %s" % e, is_error=True)
    if rid is not None:
        return _error(rid, -32601, "未知方法: %s" % method)
    return None


def main():
    if "--health" in sys.argv:
        init_runtime()
        print(json.dumps(tool_health(), ensure_ascii=False, indent=2))
        return
    init_runtime()
    if CHAIN is None and MIMO is None:
        print("警告: 未配置任何 API Key（DEEPSEEK_API_KEY / MIMO_API_KEY），工具调用将失败",
              file=sys.stderr, flush=True)
    while True:
        msg = read_frame(sys.stdin.buffer)
        if msg is None:
            break
        resp = handle_message(msg)
        if resp is not None:
            write_frame(sys.stdout.buffer, resp)


if __name__ == "__main__":
    main()
