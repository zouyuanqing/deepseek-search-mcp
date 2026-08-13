# -*- coding: utf-8 -*-
"""mcp_search: DeepSeek + Xiaomi MiMo 联网搜索 MCP 服务器。

- DeepSeek 全自动联网搜索（Responses API 原生 web_search）
- MiMo raw 搜索（Chat Completions API 内建 web_search 工具，类似 Tavily 的检索模式）
- fetch_page 抓取验证工具
- fallback 链：BACKEND=auto 时 DeepSeek 失败自动降级 MiMo
"""
__version__ = "0.3.0"
