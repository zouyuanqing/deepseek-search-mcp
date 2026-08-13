# deepseek-search-mcp

多后端联网搜索 MCP 服务器（Model Context Protocol Server）：**DeepSeek V4-Flash 全自动搜索** + **小米 MiMo 原始检索**（类似 Tavily 的"检索不思考"）+ **fetch_page 来源验证**，支持自动降级链与位置定制化搜索。

`web_search` 使用 DeepSeek Responses API 内置的 `web_search` 工具，由 V4-Flash 自动完成多轮搜索、来源核实与带引用答案合成；`web_search_deep` 进一步将问题拆解为多个子查询分别检索，再由 V4-Flash 交叉核验、综合成调研级答案。MiMo 提供 raw 检索与本地化搜索，可作为 DeepSeek 的补充来源或独立 fallback 后端；`fetch_page` 让 agent 自行抓取并核实任何来源的真实内容。

- **零第三方依赖**：纯 `urllib` + `json` 标准库（Python 3.9+），多文件包结构便于维护与 debug
- **协议自实现**：手写 stdio 传输 + JSON-RPC 2.0，兼容 Claude Desktop / Codex / Cherry Studio / Hermes 等任何 MCP 客户端（Content-Length 帧，自动兼容 NDJSON）
- **降级链**：`BACKEND=auto` 时 DeepSeek 失败自动降级 MiMo，结果带 `backend` 字段标注实际后端
- **位置定制化**：所有搜索工具支持 `location` 参数（结构化对象或自由文本），MiMo 映射 `user_location`，DeepSeek 注入查询上下文

---

## 目录

- [架构](#架构)
- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [工具说明](#工具说明)
- [配置环境变量](#配置环境变量)
- [MCP 客户端接入](#mcp-客户端接入)
- [开发与测试](#开发与测试)
- [基准对比](#基准对比)
- [已知限制](#已知限制)
- [许可证](#许可证)

## 架构

```
纯文本 LLM（决策与提问）
    │  MCP 协议（stdio, JSON-RPC 2.0, Content-Length 帧）
    ▼
deepseek_web_search_mcp.py（兼容入口）→ mcp_search 包
    ├─ models.py      统一模型：Location / SearchResult / Citation
    ├─ providers.py   DeepSeekClient（全自动）· MimoClient（raw）· FallbackChain
    ├─ fetch.py       fetch_page（HTML/JSON → 文本，SSRF 防护）
    └─ server.py      MCP 协议 + 工具注册 + 深搜编排
        │
        ├─ BACKEND=deepseek → DeepSeek Responses API（tools: web_search）
        ├─ BACKEND=mimo     → MiMo Chat Completions API（tools: web_search）
        └─ BACKEND=auto     → DeepSeek 优先，失败自动降级 MiMo
```

设计参考 GitHub 主流 web search MCP 项目（[mcp-brave-search](https://github.com/thomasvan/mcp-brave-search)、[kindly-web-search-mcp-server](https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server)）的"搜索引擎检索 + LLM 增强合成"模式：DeepSeek 负责全自动搜索与合成，MiMo 承担 raw 检索基础设施（与 Tavily 定位一致），fetch_page 提供白盒验证能力。

## 功能特性

| 工具 | 模式 | 适用场景 | 典型耗时 |
|---|---|---|---|
| `web_search` | 标准 | 日常查询、新闻、事实确认 | 30–60s |
| `web_search_fast` | 快速 | 时效敏感的简单查询、快查 | 3–8s |
| `web_search_deep` | 深度 | 事实核查、研究报告、技术调研 | 2–4 分钟 |
| `mimo_search` | raw 检索 | 补充来源、本地化检索、agent 自行核实 | 20–40s |
| `fetch_page` | 基础 | 抓取来源 URL 验证真实内容 | 1–10s |
| `health` | 诊断 | 验证各后端与 fetch 链路 | 30–60s |

**`web_search`**：模型自动规划多次搜索 → 阅读并核实来源 → 返回结构化、带来源链接的答案。`BACKEND=auto` 时 DeepSeek 失败自动降级 MiMo。

**`web_search_fast`**：低推理强度（`reasoning: {"effort": "low"}`）+ 精简指令 + 小输出上限，通常 1–2 轮搜索后直接给出简洁答案。

**`web_search_deep`**：先生成 3–6 个子查询分别检索（每个子查询独立搜索、独立来源）→ 交叉核验 → 输出综合答案。`enrich_mimo=true` 时每路子查询额外注入 MiMo raw 检索结果作为补充来源（子查询已由 MiMo 完成时自动跳过，避免重复检索）。

**`mimo_search`**：直接调用小米 MiMo 联网搜索，返回结构化来源列表（标题/链接/站点/发布时间/摘要）+ 简短摘要，不做多轮编排。支持 `location`（映射 `user_location`，实测可显著提升本地结果质量）、`max_keyword` / `limit` 控制成本。使用前需在 [小米 MiMo 控制台](https://platform.xiaomimimo.com/#/console/plugin) 确认已启用联网插件。

**`fetch_page`**：抓取 URL 提取为纯文本（HTML 去噪 / JSON 原样），内置 SSRF 防护（拒绝内网/保留地址）。注意：JS 动态渲染页面（SPA）无法静态提取，会返回明确提示。

## 快速开始

```bash
# 1. 设置 API Key（也可以直接在 MCP 客户端配置中注入）
export DEEPSEEK_API_KEY="sk-..."          # DeepSeek 全自动搜索
export MIMO_API_KEY="sk-..."              # MiMo raw 检索 / fallback
export BACKEND="auto"                     # auto = DeepSeek 优先，失败降级 MiMo

# 2. 健康检查（验证 Key 与搜索链路）
python deepseek_web_search_mcp.py --health

# 3. 作为 MCP 服务器运行（stdio）
python deepseek_web_search_mcp.py
```

## 工具说明

### `web_search` / `web_search_fast`

- **参数**：
  - `query`（必填，string）— 要搜索的问题或主题，建议写成完整问题以提高相关性
  - `location`（可选）— 位置定制化搜索，两种格式：
    - 结构化：`{"country": "中国", "region": "湖北", "city": "武汉"}`
    - 自由文本：`"湖北省武汉市"` / `"武汉"` / `"Wuhan, Hubei, China"`（自动拆分行政层级）
- **返回**：JSON，含 `answer`、`sources`、`citations`（结构化来源）、`backend`（实际后端）、`usage`、`elapsed_s`

### `web_search_deep`

- **参数**：`query`（必填）、`location`（可选）、`enrich_mimo`（可选，默认 false — 用 MiMo raw 检索补充来源）
- **返回**：JSON，含 `answer`（交叉核验综合答案）、`sub_queries`、`segments`（每路子查询结果与 backend）、`sources`、`elapsed_s`

### `mimo_search`

- **参数**：`query`（必填）、`location`（可选）、`fast`（可选，减少关键词与结果数）、`max_keyword` / `limit`（可选，1–20）
- **返回**：JSON，含 `answer`（简短摘要）、`sources`、`citations`（title/url/site_name/publish_time/summary）、`usage.web_search_usage`、`elapsed_s`

### `fetch_page`

- **参数**：`url`（必填）、`max_chars`（可选，默认 20000，上限 100000）
- **返回**：JSON，含 `title`、`text`、`content_type`、`final_url`（跟随重定向后）、`truncated`

### `health`

- **参数**：无
- **返回**：`status`、后端链、各后端可用性与示例、fetch 链路状态

## 配置环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `BACKEND` | | `deepseek` | `deepseek`（仅 DeepSeek）/ `mimo`（仅 MiMo）/ `auto`（DeepSeek 优先，失败自动降级 MiMo） |
| `DEEPSEEK_API_KEY` | deepseek/auto | — | DeepSeek API Key |
| `DEEPSEEK_BASE_URL` | | `https://api.deepseek.com` | API 端点 |
| `DEEPSEEK_MODEL` | | `deepseek-v4-flash` | 模型名（Responses API 目前仅支持该模型） |
| `DEEPSEEK_TIMEOUT_S` | | `180` | HTTP 超时（秒） |
| `DEEPSEEK_MAX_OUTPUT` | | `8192` | 单轮搜索合成答案的最大输出 token |
| `DEEPSEEK_MAX_OUTPUT_FAST` | | `2048` | 快速模式的最大输出 token |
| `MIMO_API_KEY` | mimo/auto | — | 小米 MiMo API Key（https://platform.xiaomimimo.com） |
| `MIMO_BASE_URL` | | `https://api.xiaomimimo.com` | MiMo API 端点 |
| `MIMO_MODEL` | | `mimo-v2.5-pro` | MiMo 模型（`mimo-v2.5` / `mimo-v2.5-pro` 支持联网搜索） |
| `MIMO_TIMEOUT_S` | | `180` | MiMo HTTP 超时（秒） |
| `MIMO_FORCE_SEARCH` | | `true` | 是否强制联网搜索（不依赖模型意图判断） |
| `MIMO_MAX_KEYWORD` | | `5` | 每轮最大搜索关键词数（fast 模式减半） |
| `MIMO_LIMIT` | | `5` | 返回搜索结果条数（fast 模式减半） |
| `MIMO_MAX_OUTPUT` | | `4096` | MiMo 最大输出 token（实测搜索时 prompt 可达数千 token） |
| `MIMO_MAX_OUTPUT_FAST` | | `2048` | MiMo fast 模式最大输出 token |
| `MCP_FETCH_TIMEOUT_S` | | `20` | fetch_page HTTP 超时（秒） |
| `MCP_FETCH_MAX_CHARS` | | `20000` | fetch_page 提取文本上限 |
| `MCP_FETCH_ALLOW_PRIVATE` | | `0` | 设为 `1` 跳过 SSRF 防护（仅本地开发/测试） |
| `DEBUG` | | `0` | 设为 `1` 输出调试日志到 stderr |

## MCP 客户端接入

以 Claude Desktop 的 `claude_desktop_config.json` 为例：

```json
{
  "mcpServers": {
    "deepseek-search": {
      "command": "python",
      "args": ["/absolute/path/to/deepseek_web_search_mcp.py"],
      "env": {
        "DEEPSEEK_API_KEY": "sk-...",
        "MIMO_API_KEY": "sk-...",
        "BACKEND": "auto",
        "MCP_FETCH_TIMEOUT_S": "20"
      }
    }
  }
}
```

其他支持 stdio 的 MCP 客户端（Codex、Cursor、Cherry Studio、Hermes 等）配置方式类似。旧配置（仅 `DEEPSEEK_API_KEY`）无需任何改动。

## 开发与测试

```
mcp_search/
  __init__.py      # 版本
  models.py        # Location 双格式解析 / Citation / SearchResult
  providers.py     # DeepSeekClient / MimoClient / FallbackChain / 配置解析
  fetch.py         # fetch_page（HTML/JSON → 文本，SSRF 防护，gzip 解压）
  server.py        # MCP 协议 + 工具注册 + 深搜编排
tests/
  test_models.py       # Location 解析（结构化/文本/英文/边界）+ URL 提取
  test_providers.py    # 请求构造/响应解析/错误分支/降级链（monkeypatch，不发真实请求）
  test_fetch.py        # 本地 http.server：HTML 提取/JSON/404/超时/SSRF/gzip
  test_stdio_mcp.py    # stdio 端到端：协议冒烟（无 key）+ 真实搜索链路（有 key）
```

运行方式：

```bash
# 全量测试（无 key 时自动跳过真实搜索用例；配置任一 key 后自动包含）
python -m unittest discover -s tests -p "test_*.py"

# 真实 MiMo 搜索链路
$env:MIMO_API_KEY="sk-..." ; $env:BACKEND="auto"
python -m unittest discover -s tests -p "test_*.py"

# 通用 MCP stdio 客户端调用任意工具（--timeout 深搜建议 900）
python mcp_client.py --cmd "python" --args "deepseek_web_search_mcp.py" \
  --tool mimo_search --params-file params.json --env "MIMO_API_KEY=sk-..." --timeout 600

# 老版 stdio 冒烟脚本（仍可用）
python test_stdio.py [--deep]
```

> **协议注意**：`tavily-mcp` 等新版 MCP SDK 实现的服务器使用 **NDJSON 行协议**，而本项目手写服务器使用 **Content-Length 帧协议**（自动兼容两种）。`mcp_client.py` 用 `--jsonl` 参数切换；`initialize` 请求对新版 SDK 服务器必须携带 `capabilities` 与 `clientInfo` 字段。

## 基准对比

实测（2026-08-12，真实 MiMo API）各搜索路径对比：

| 维度 | Tavily MCP | `web_search` (DS) | `web_search_fast` (DS) | `mimo_search` | `web_search_deep` |
|---|---|---|---|---|---|
| 耗时 | 1–7s | 30–60s | 3–8s | 20–40s | 2–4 分钟 |
| 输出 | 原始片段 | 带引用综合答案 | 带引用简洁答案 | 来源列表 + 摘要 | 交叉核验报告 |
| 来源甄别 | 无 | 有 | 有 | 结构化 citations | 有（标注存疑点） |
| 位置定制 | 无 | 查询上下文注入 | 查询上下文注入 | `user_location`（原生） | 支持 |

要点：

- **`mimo_search`** 定位与 Tavily 相同（检索不思考），但返回结构化 `citations`（标题/站点/发布时间/摘要），实测 `user_location` 可显著提升本地结果质量（如武汉天气返回中央气象台/天气网等本地权威来源）。
- **`fetch_page`** 提供"检索 → 抓取原文验证"的白盒链路：搜索给出的每条来源都可直接抓取核实，弥补黑盒合成的盲区。
- **`BACKEND=auto` 降级**：DeepSeek 不可用时自动切 MiMo（raw 模式），结果带 `backend` 字段标注，不中断工作流。

## 已知限制

1. **模型支持**：DeepSeek Responses API 目前仅支持 `deepseek-v4-flash`；MiMo 联网搜索仅 `mimo-v2.5-pro` / `mimo-v2.5` 支持，且需在控制台启用联网插件（启用/关闭有约 5 分钟缓存期）。
2. **MiMo thinking 模式**：MiMo 默认启用思维链，实测思考模式下 `content` 可能为空（间歇性）；本项目所有请求显式 `thinking: {"type": "disabled"}` 规避。
3. **MiMo 输出截断**：`max_completion_tokens` 过小时 `finish_reason` 为 `length`，答案被截断；默认 4096，如需更短答案请用 `fast` 模式。
4. **响应提取**：DeepSeek Responses API 顶层 `output_text` 为空，正文从 `output[].message.content[].output_text` 提取（已处理）。
5. **fetch_page 限制**：JS 动态渲染页面（SPA）无法静态提取正文，返回明确提示；SSRF 防护默认拒绝内网地址。
6. **耗时**：深搜模式多轮调用耗时较长（实测 MiMo 链 ~210s），客户端超时需配置足够长（`mcp_client.py --timeout 900`）。
7. **`notifications/initialized`**：该通知无响应，MCP 客户端不能同步等待（测试脚本中用线程 + 超时处理）。

## 许可证

MIT
