# deepseek-search-mcp

基于 DeepSeek V4-Flash 原生联网搜索能力的高准确度搜索 MCP 服务器（Model Context Protocol Server）。

`web_search` 使用 DeepSeek Responses API 内置的 `web_search` 工具，由 V4-Flash 自动完成多轮搜索、来源核实与带引用答案合成；`web_search_deep` 进一步将问题拆解为多个子查询分别检索，再由 V4-Flash 交叉核验、综合成调研级答案。

- **单文件实现**：核心服务仅一个 Python 文件，零第三方运行时依赖（纯 `urllib` + `json` 标准库）
- **协议自实现**：手写 stdio 传输 + JSON-RPC 2.0，不依赖 MCP SDK，兼容 Claude Desktop / Codex / Cherry Studio / Hermes 等任何 MCP 客户端
- **无额外搜索 API**：联网搜索能力由 DeepSeek API 原生提供，只需一个 DeepSeek API Key

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
deepseek_web_search_mcp.py（单文件，零第三方依赖）
    │  DeepSeek Responses API（tools: web_search）
    ▼
DeepSeek V4-Flash（284B MoE，搜索 + 阅读 + 多轮核实 + 带来源引用合成）
```

设计参考了 GitHub 上主流 web search MCP 项目（[mcp-brave-search](https://github.com/thomasvan/mcp-brave-search)、[kindly-web-search-mcp-server](https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server)）的"搜索引擎检索 + LLM 增强合成"模式，但搜索与合成均由 DeepSeek 原生完成，无需第三方搜索 API Key。

## 功能特性

| 工具 | 模式 | 适用场景 | 典型耗时 |
|---|---|---|---|
| `web_search` | 标准 | 日常查询、新闻、事实确认 | 30–60s |
| `web_search_fast` | 快速 | 时效敏感的简单查询、快查 | 3–8s |
| `web_search_deep` | 深度 | 事实核查、研究报告、技术调研 | 2–4 分钟 |
| `health` | 诊断 | 验证 API Key 与搜索链路 | 30–60s |

**`web_search`**：模型自动规划多次搜索 → 阅读并核实来源 → 返回结构化、带来源链接的答案。

**`web_search_fast`**：低推理强度（`reasoning: {"effort": "low"}`）+ 精简指令 + 小输出上限，通常 1–2 轮搜索后直接给出简洁答案。实测相比标准模式提速约 5 倍（5.4s vs 29.5s），答案仍带来源链接。

**`web_search_deep`**：先生成 3–6 个子查询（`DEEPSEEK_DEEP_QUERIES` 控制）分别检索（每个子查询独立搜索、独立来源）→ 由 V4-Flash 交叉核验 → 输出综合答案。实测能主动标注"来源冲突"与"信息不足"的存疑点。

## 快速开始

```bash
# 1. 设置 API Key（也可以直接在 MCP 客户端配置中注入）
export DEEPSEEK_API_KEY="sk-..."

# 2. 健康检查（验证 Key 与搜索链路）
python deepseek_web_search_mcp.py --health

# 3. 作为 MCP 服务器运行（stdio）
python deepseek_web_search_mcp.py
```

## 工具说明

### `web_search`

- **参数**：`query`（必填，string）— 要搜索的问题或主题，建议写成完整问题以提高相关性
- **返回**：JSON，含 `answer`（带来源链接的综合答案）、`sources`（提取的引用 URL 列表）、`elapsed_s`、`usage`

### `web_search_fast`

- **参数**：`query`（必填，string）— 要搜索的问题或主题
- **返回**：JSON，含 `answer`（简洁答案）、`mode: "fast"`、`sources`、`elapsed_s`、`usage`
- **实现**：`reasoning: {"effort": "low"}` 降低推理 token 消耗 + 精简系统指令 + 输出上限默认 2048

### `web_search_deep`

- **参数**：`query`（必填，string）— 需要深度调研的问题，需具体、可拆解
- **返回**：JSON，含 `answer`（交叉核验后的综合答案）、`sub_queries`（拆解出的子查询）、`segments`（每路子查询的检索结果）、`sources`、`elapsed_s`

### `health`

- **参数**：无
- **返回**：`status`、模型/端点信息、搜索链路可用性、示例答案片段

## 配置环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | — | DeepSeek API Key |
| `DEEPSEEK_BASE_URL` | | `https://api.deepseek.com` | API 端点 |
| `DEEPSEEK_MODEL` | | `deepseek-v4-flash` | 模型名（Responses API 目前仅支持该模型） |
| `DEEPSEEK_TIMEOUT_S` | | `180` | HTTP 超时（秒）。深搜模式多轮调用耗时较长，建议 ≥180 |
| `DEEPSEEK_MAX_OUTPUT` | | `8192` | 单轮搜索合成答案的最大输出 token |
| `DEEPSEEK_MAX_OUTPUT_FAST` | | `2048` | 快速模式的最大输出 token |
| `DEEPSEEK_DEEP_QUERIES` | | `4` | 深搜子查询数量（范围 1–6） |
| `DEEPSEEK_DEBUG` | | `0` | 设为 `1` 输出调试日志到 stderr |

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
        "DEEPSEEK_TIMEOUT_S": "180"
      }
    }
  }
}
```

其他支持 stdio 的 MCP 客户端（Codex、Cursor、Cherry Studio、Hermes 等）配置方式类似。

## 开发与测试

仓库内包含两套测试/对比工具：

| 文件 | 用途 |
|---|---|
| `test_stdio.py` | 端到端 stdio 协议测试：模拟 MCP 客户端完成 `initialize` → `tools/list` → `tools/call(web_search)` → `tools/call(web_search_deep)` 全链路 |
| `mcp_client.py` | 通用 MCP stdio 客户端，可连接任意 stdio MCP 服务器并调用工具 |
| `bench_search.py` | 同题对比基准：DeepSeek MCP vs Tavily MCP 的耗时与输出对比 |

完整测试用例、三方对比数据与发现的问题见 [TESTING.md](TESTING.md)。

运行方式：

```bash
# stdio 全链路测试（--deep 附加深搜用例）
python test_stdio.py [--deep]

# 调用任意 stdio MCP 服务器（列出工具）
python mcp_client.py --cmd "npx.cmd" --args "-y tavily-mcp" --list --jsonl

# 三方对比（需 DEEPSEEK_API_KEY / TAVILY_API_KEY 环境变量）
python bench_search.py --deep "你的调研问题"
```

> **协议注意**：`tavily-mcp` 等新版 MCP SDK 实现的服务器使用 **NDJSON 行协议**（每行一个 JSON），而本项目手写的服务器使用 **Content-Length 帧协议**（兼容 `2024-11-05` / `2025-06-18` 等协议版本的经典实现）。`mcp_client.py` 用 `--jsonl` 参数切换两种协议；`initialize` 请求对新版 SDK 服务器必须携带 `capabilities` 与 `clientInfo` 字段。

## 基准对比

实测（2026-08-09，DeepSeek V4-Flash 正式版）三方搜索能力对比：

| 维度 | 内置 WebSearch | Tavily MCP | `web_search` | `web_search_fast` | `web_search_deep` |
|---|---|---|---|---|---|
| 耗时 | ~3s | 1–7s | 29.5s | **5.4s** | 245s |
| 输出 | 5 条摘要 | 原始片段 | 带引用综合答案 | 带引用简洁答案 | 交叉核验报告 |
| 来源甄别 | 无 | 无 | 有 | 有 | 有（主动标注存疑点） |

要点：

- **Tavily MCP** 响应最快，但只做"检索不思考"：实测中把过时价格与官方价格并列返回而未甄别，准确性依赖使用方自行判断。
- **`web_search`** 返回带来源链接的综合答案，多轮搜索自动核实关键事实。
- **`web_search_fast`** 与标准模式同架构，仅降推理强度、精简指令与输出上限，实测 5.4s（提速约 5.5 倍），答案仍带来源链接与时效性提示。
- **`web_search_deep`** 质量最高：交叉验证多来源，能主动指出"某信息系某媒体披露、官方未点名"等存疑点；代价是耗时明显更长。

**结论**：对时效敏感的简单快查用 `web_search_fast`；日常查询用 Tavily 或内置搜索；需要高准确度的调研与事实核查场景，建议使用 `web_search_deep`。

## 已知限制

1. **模型支持**：DeepSeek Responses API 目前仅支持 `deepseek-v4-flash`，`deepseek-v4-pro` 尚不支持（官方计划 2026 年 8 月初加入）。
2. **响应提取**：Responses API 的顶层 `output_text` 字段为空，正文需从 `output[]` 数组的 `message.content[].output_text` 中提取（本项目已处理）。
3. **耗时**：联网搜索为多轮推理任务，单轮 30–180s、深搜 2–4 分钟，客户端侧超时需配置足够长。
4. **`notifications/initialized`**：该通知无响应，MCP 客户端不能同步等待（测试脚本中用线程 + 超时处理）。

## 许可证

MIT
