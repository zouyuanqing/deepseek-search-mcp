#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek Web Search MCP - 基于 DeepSeek V4-Flash 的联网搜索 MCP Server
让 MCP 客户端（Claude Desktop / Codex / Hermes / Cherry Studio 等）获得高准确度联网搜索能力。

架构（参考 GitHub 主流 web search MCP 项目：mcp-brave-search / kindly-web-search-mcp-server）：
  纯文本 LLM（决策与提问）
      │  MCP 协议（stdio, JSON-RPC 2.0）
      ▼
  deepseek_web_search_mcp.py（单文件，零第三方依赖）
      │  DeepSeek Responses API（原生 web_search 工具）
      ▼
  DeepSeek V4-Flash（284B MoE，搜索 + 阅读 + 多轮核实 + 带来源引用合成）

两个工具：
  - web_search          : 标准搜索。DeepSeek V4-Flash 自动规划多次搜索、核实来源、返回带引用的答案。
  - web_search_deep     : 深度搜索（高准确性）。先生成 3-5 个子查询并行检索，再由 V4-Flash 交叉核验综合，
                          适用于事实核查、研究报告、技术调研等对准确性要求高的场景。

依赖：Python 3.9+ 标准库。零第三方运行时依赖（urllib + json）。
环境变量：
  DEEPSEEK_API_KEY      必填。DeepSeek API Key
  DEEPSEEK_BASE_URL     默认 https://api.deepseek.com
  DEEPSEEK_MODEL        默认 deepseek-v4-flash（Responses API 仅支持该模型）
  DEEPSEEK_TIMEOUT_S    默认 180（深搜模式多轮搜索耗时较长）
  DEEPSEEK_MAX_OUTPUT   默认 8192（单轮搜索合成答案的最大输出 token）
  DEEPSEEK_DEEP_QUERIES 默认 4（深搜子查询数量，范围 1-6）
  DEEPSEEK_DEBUG        设 1 输出日志到 stderr
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ----------------------------- 配置 -----------------------------

def _env(name, default=""):
    return os.environ.get(name, default).strip()

def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

API_KEY = _env("DEEPSEEK_API_KEY")
BASE_URL = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = _env("DEEPSEEK_MODEL", "deepseek-v4-flash")
TIMEOUT_S = _env_int("DEEPSEEK_TIMEOUT_S", 180)
MAX_OUTPUT = _env_int("DEEPSEEK_MAX_OUTPUT", 8192)
DEEP_QUERIES = max(1, min(6, _env_int("DEEPSEEK_DEEP_QUERIES", 4)))
DEBUG = _env("DEEPSEEK_DEBUG", "0") == "1"

SYS_SEARCH = (
    "你是高准确度联网搜索助手。使用 web_search 工具回答用户问题，必须："
    "1) 搜索后用真实来源核实关键事实，不凭记忆作答；"
    "2) 优先采用权威来源（官方文档、新闻社、学术来源），多来源交叉验证；"
    "3) 回答末尾列出引用的来源链接；"
    "4) 信息不足或来源冲突时明确说明不确定性，不要编造。"
)

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

# ----------------------------- HTTP 客户端 -----------------------------

class DeepSeekError(Exception):
    """用户可见的工具错误。"""


def _post(path, body, timeout=None):
    """POST JSON 到 DeepSeek API，返回 (status, dict)。"""
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT_S) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": {"message": raw[:500]}}
        msg = payload.get("error", {}).get("message", str(e))
        return e.code, {"error": {"message": msg}}
    except urllib.error.URLError as e:
        return 0, {"error": {"message": f"网络错误: {e.reason}"}}


def _extract_messages(output):
    """从 Responses API 的 output 数组提取所有 message 正文（含中间轮次说明文字）。"""
    parts = []
    for item in output or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text" and part.get("text"):
                parts.append(part["text"])
    return parts


def _extract_urls(text):
    """从 markdown 正文提取来源 URL（[text](url) 或裸 http(s) 链接）。"""
    urls = []
    for m in re.finditer(r"\[[^\]]*\]\(((?:https?://)[^)\s]+)\)", text):
        urls.append(m.group(1))
    for m in re.finditer(r"(?<![\w])(https?://[^\s)\]]+)", text):
        u = m.group(1)
        if u not in urls:
            urls.append(u)
    # 去重保序
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def web_search_call(query):
    """调用 DeepSeek Responses API 原生联网搜索，返回 (答案正文, 引用URL列表, usage)。"""
    body = {
        "model": MODEL,
        "instructions": SYS_SEARCH,
        "input": query,
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
        "stream": False,
        "max_output_tokens": MAX_OUTPUT,
    }
    status, resp = _post("/v1/responses", body)
    if status != 200:
        raise DeepSeekError(resp.get("error", {}).get("message", f"HTTP {status}"))
    texts = _extract_messages(resp.get("output", []))
    answer = "\n".join(texts).strip() if texts else resp.get("output_text", "").strip()
    if not answer:
        raise DeepSeekError("DeepSeek 未返回搜索结果，请稍后重试或换个问法")
    urls = _extract_urls(answer)
    return answer, urls, resp.get("usage") or {}


def _chat_once(instructions, user_msg, max_tokens=2048):
    """普通 chat/completions 调用（非搜索），返回文本。"""
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
        "stream": False,
    }
    status, resp = _post("/v1/chat/completions", body)
    if status != 200:
        raise DeepSeekError(resp.get("error", {}).get("message", f"HTTP {status}"))
    return resp["choices"][0]["message"]["content"]


def _parse_queries(raw):
    """解析深搜子查询。优先 JSON 数组，回退到按行/编号提取。"""
    if not raw or not raw.strip():
        raise DeepSeekError("子查询输出为空")
    text = raw.strip()
    # 去掉 ```json ... ``` 代码块标记
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.strip()
    # 尝试 JSON 数组
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            queries = json.loads(m.group(0))
            queries = [str(q).strip() for q in queries if str(q).strip()]
            if queries:
                return queries[:DEEP_QUERIES]
        except json.JSONDecodeError:
            pass
    # 回退：按编号/换行提取
    lines = [ln for ln in re.split(r"\n+", text) if ln.strip()]
    queries = []
    for ln in lines:
        s = re.sub(r"^\s*(?:[0-9]+[.)、]\s*|[-*•]\s*|\"|')", "", ln).strip()
        s = re.sub(r"[\"']$", "", s).strip()
        if s and len(s) > 4:
            queries.append(s)
    if not queries:
        raise DeepSeekError("无法解析子查询输出")
    return queries[:DEEP_QUERIES]

# ----------------------------- 工具实现 -----------------------------

def tool_web_search(args):
    """标准联网搜索。"""
    query = (args.get("query") or "").strip()
    if not query:
        raise DeepSeekError("参数 query 不能为空")
    t0 = time.time()
    answer, urls, usage = web_search_call(query)
    return {
        "query": query,
        "answer": answer,
        "sources": urls,
        "elapsed_s": round(time.time() - t0, 1),
        "usage": usage,
    }


def tool_web_search_deep(args):
    """深度搜索：拆解子查询 -> 多路并行检索 -> 交叉核验综合。"""
    query = (args.get("query") or "").strip()
    if not query:
        raise DeepSeekError("参数 query 不能为空")
    t0 = time.time()

    # 1) 拆解子查询
    plan_raw = _chat_once(SYS_DEEP_PLAN, query, max_tokens=1024)
    sub_queries = _parse_queries(plan_raw)

    # 2) 多路检索（串行执行，避免触发限流；每路独立来源）
    segments = []
    for i, sq in enumerate(sub_queries, 1):
        try:
            ans, urls, _ = web_search_call(f"子问题{i}: {sq}")
            segments.append({"sub_query": sq, "answer": ans, "sources": urls})
        except DeepSeekError as e:
            segments.append({"sub_query": sq, "answer": "", "sources": [], "error": str(e)})

    # 3) 交叉核验综合
    parts = []
    for i, seg in enumerate(segments, 1):
        parts.append(f"--- 子问题{i}: {seg['sub_query']} ---\n{seg['answer'] or '(检索失败)'}")
    synth = _chat_once(SYS_DEEP_SYNTH, f"研究问题：{query}\n\n多路搜索结果：\n" + "\n".join(parts), max_tokens=MAX_OUTPUT)

    all_urls = []
    for seg in segments:
        for u in seg["sources"]:
            if u not in all_urls:
                all_urls.append(u)
    for u in _extract_urls(synth):
        if u not in all_urls:
            all_urls.append(u)

    return {
        "query": query,
        "answer": synth,
        "sub_queries": sub_queries,
        "segments": segments,
        "sources": all_urls,
        "elapsed_s": round(time.time() - t0, 1),
    }


def tool_health(args=None):
    """健康检查：验证 API key 与模型可用性。"""
    try:
        ans, urls, usage = web_search_call("当前时间的最新全球头条新闻是什么？简要回答。")
        return {
            "status": "ok",
            "model": MODEL,
            "base_url": BASE_URL,
            "web_search_ok": True,
            "sample_answer": ans[:300],
            "usage": usage,
        }
    except DeepSeekError as e:
        return {"status": "error", "model": MODEL, "web_search_ok": False, "error": str(e)}

# ----------------------------- MCP 协议（stdio, JSON-RPC 2.0） -----------------------------

class McpParamError(Exception):
    """参数校验错误 -> JSON-RPC -32602。"""

def log(*args):
    if DEBUG:
        print("[deepseek-search-mcp]", *args, file=sys.stderr, flush=True)

HANDLERS = {
    "web_search": tool_web_search,
    "web_search_deep": tool_web_search_deep,
    "health": tool_health,
}

TOOLS = [
    {
        "name": "web_search",
        "description": (
            "联网搜索（标准模式）。基于 DeepSeek V4-Flash 原生联网搜索：自动规划多次搜索、"
            "阅读并核实来源、返回带引用的结构化答案。适合日常查询、新闻、事实确认。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的问题或主题，建议写成完整问题以提高相关性（例：\"2026年8月特斯拉股价走势如何？\"）",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_search_deep",
        "description": (
            "深度联网搜索（高准确性模式）。先把问题拆解为多个子查询分别检索，"
            "再由 DeepSeek V4-Flash 交叉核验、综合成带引用的最终答案。"
            "适合事实核查、研究报告、技术调研、需要多来源验证的场景。耗时更长。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要深度调研的问题，需具体、可拆解（例：\"DeepSeek V4-Flash 正式版相比预览版有哪些能力变化？\"）",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "health",
        "description": "健康检查：验证 DeepSeek API key 与联网搜索能力是否可用。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

def _text_result(rid, obj, is_error=False):
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2)
    return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}}

def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

def write_frame(stream, obj):
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8"))
    stream.write(payload)
    stream.flush()

def read_frame(stream):
    first = stream.readline()
    if not first:
        return None
    headers = {}
    content_length = 0
    if first.strip().lower().startswith(b"content-length:"):
        content_length = int(first.split(b":", 1)[1].strip() or 0)
    else:
        # 首行非 Content-Length（兼容部分客户端发送空行开头），继续读头部
        if b":" in first:
            k, v = first.split(b":", 1)
            headers[k.strip().lower()] = v.strip()
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
            headers[k.strip().lower()] = v.strip()
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
                "serverInfo": {"name": "deepseek-search-mcp", "version": "0.1.0"},
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
            return _error(rid, -32601, f"未知工具: {name}")
        required = tool["inputSchema"].get("required", [])
        missing = [r for r in required if r not in args or args[r] in (None, "")]
        if missing:
            return _error(rid, -32602, f"缺少必需参数: {', '.join(missing)}")
        handler = HANDLERS.get(name)
        if handler is None:
            return _error(rid, -32601, f"工具未实现: {name}")
        try:
            result = handler(args)
            return _text_result(rid, result)
        except McpParamError as e:
            return _error(rid, -32602, f"Invalid params: {e}")
        except DeepSeekError as e:
            return _text_result(rid, f"错误: {e}", is_error=True)
        except Exception as e:
            log("tool crash:", name, e)
            return _text_result(rid, f"内部错误: {e}", is_error=True)
    if rid is not None:
        return _error(rid, -32601, f"未知方法: {method}")
    return None

def main():
    if "--health" in sys.argv:
        print(json.dumps(tool_health(), ensure_ascii=False, indent=2))
        return
    if not API_KEY:
        print("错误: 未设置环境变量 DEEPSEEK_API_KEY", file=sys.stderr)
        sys.exit(1)
    while True:
        msg = read_frame(sys.stdin.buffer)
        if msg is None:
            break
        resp = handle_message(msg)
        if resp is not None:
            write_frame(sys.stdout.buffer, resp)

if __name__ == "__main__":
    main()
