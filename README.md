# deepseek-search-mcp

多后端联网搜索 MCP 服务器：**DeepSeek V4-Flash 全自动搜索** + **小米 MiMo 原始检索** + **fetch_page 来源验证**，带自动降级链与位置定制化搜索。

- **零依赖**：纯 Python 标准库（≥3.9），无第三方包
- **协议自实现**：stdio + JSON-RPC 2.0，兼容 Claude Desktop / Codex / Cherry Studio / Hermes 等客户端
- **自动降级**：`BACKEND=auto` 时 DeepSeek 失败自动切 MiMo，结果标注实际后端
- **位置搜索**：`location` 参数（结构化对象或自由文本）本地化检索

## 架构

```
MCP 客户端（LLM 决策与提问）
    │  stdio, JSON-RPC 2.0
    ▼
deepseek_web_search_mcp.py（入口）→ mcp_search 包
    ├─ providers.py   DeepSeekClient（全自动）· MimoClient（raw）· FallbackChain
    ├─ models.py      统一模型：Location / Citation / SearchResult
    ├─ fetch.py       fetch_page（HTML/JSON → 文本，SSRF 防护）
    └─ server.py      MCP 协议 + 工具注册 + 深搜编排
    │
    ├─ DeepSeek Responses API（原生 web_search 工具，多轮核实 + 带引用合成）
    └─ MiMo Chat Completions API（web_search 工具，结构化来源 + user_location）
```

## 工具

| 工具 | 模式 | 用途 | 耗时 |
|---|---|---|---|
| `web_search` | DeepSeek 全自动 | 日常查询、新闻、事实确认 | 30–60s |
| `web_search_fast` | 快速 | 时效敏感的快查 | 3–8s |
| `web_search_deep` | 深搜 | 事实核查、调研报告（子查询拆解 + 交叉核验） | 3–10 分钟 |
| `mimo_search` | raw 检索 | 类似 Tavily：结构化来源列表 + 摘要，不编排 | 20–40s |
| `fetch_page` | 基础 | 抓取 URL 验证来源真实内容 | 1–10s |
| `health` | 诊断 | 验证各后端与 fetch 链路 | 30–60s |

### 参数

- **`location`**（三个搜索工具通用，可选）：`{"country":"中国","region":"湖北","city":"武汉"}` 或 `"湖北省武汉市"` / `"Wuhan, Hubei, China"`。MiMo 映射 `user_location`（实测显著提升本地结果），DeepSeek 注入查询上下文。
- **`web_search_deep` 的 `enrich_mimo`**（可选，默认 false）：每路子查询额外注入 MiMo raw 结果作为补充来源，DeepSeek 定"准确性"、MiMo 补"覆盖面"。
- **`mimo_search` 的 `fast` / `max_keyword` / `limit`**（可选）：控制检索成本（默认 5/5，fast 减半）。
- **`fetch_page` 的 `max_chars`**（可选，默认 20000）：提取文本上限。

## 快速开始

```bash
export DEEPSEEK_API_KEY="sk-..."     # DeepSeek 全自动搜索（必填其一）
export MIMO_API_KEY="sk-..."         # MiMo raw 检索 / 降级兜底

python deepseek_web_search_mcp.py --health   # 健康检查
python deepseek_web_search_mcp.py            # 作为 MCP 服务器运行（stdio）
```

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `BACKEND` | `deepseek` | `deepseek` / `mimo` / `auto`（DeepSeek 优先，失败自动降级 MiMo） |
| `DEEPSEEK_API_KEY` | — | DeepSeek API Key |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | Responses API 目前仅支持该模型 |
| `DEEPSEEK_TIMEOUT_S` | `180` | HTTP 超时（秒） |
| `DEEPSEEK_MAX_OUTPUT` | `8192` | 标准/深搜合成答案的最大输出 token |
| `DEEPSEEK_MAX_OUTPUT_FAST` | `2048` | 快速模式最大输出 token |
| `MIMO_API_KEY` | — | 小米 MiMo API Key（[platform.xiaomimimo.com](https://platform.xiaomimimo.com)） |
| `MIMO_MODEL` | `mimo-v2.5-pro` | 联网搜索支持 `mimo-v2.5` / `mimo-v2.5-pro` |
| `MIMO_FORCE_SEARCH` | `true` | 强制联网搜索（不依赖模型意图判断） |
| `MIMO_MAX_KEYWORD` / `MIMO_LIMIT` | `5` / `5` | 每轮搜索关键词数 / 结果条数（fast 减半） |
| `MIMO_MAX_OUTPUT` | `4096` | 搜索时 prompt 会拼接结果，过小会截断（`finish_reason: length`） |
| `MCP_FETCH_TIMEOUT_S` / `MCP_FETCH_MAX_CHARS` | `20` / `20000` | fetch_page 超时与提取上限 |
| `MCP_FETCH_ALLOW_PRIVATE` | `0` | 设为 `1` 跳过 SSRF 防护（仅本地开发） |
| `DEBUG` | `0` | 设为 `1` 输出调试日志到 stderr |

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
        "BACKEND": "auto"
      }
    }
  }
}
```

> **超时**：标准模式 30–60s、深搜最长 10 分钟，客户端侧超时需配置足够长（opencode 为 `experimental.mcp_timeout`，单位毫秒）。

## 开发与测试

```
mcp_search/      # models / providers / fetch / server
tests/           # 99 个用例：单元（mock）+ 集成（本地 HTTP）+ stdio 端到端
mcp_client.py    # 通用 MCP stdio 客户端，可调任意工具
```

```bash
# 全量测试（配置任一 API Key 后自动包含真实搜索用例）
python -m unittest discover -s tests -p "test_*.py"

# 手动调用示例
python mcp_client.py --cmd "python" --args "deepseek_web_search_mcp.py" \
  --tool mimo_search --params-file params.json \
  --env "MIMO_API_KEY=sk-..." --env "BACKEND=auto" --timeout 600
```

可靠性设计：网络错误/5xx/429/空结果自动重试；深搜规划失败自动回退默认子查询拆解、综合失败降级为结果汇总；断连自动走降级链。详见 [TESTING.md](TESTING.md)。

## 已知限制

1. DeepSeek Responses API 仅支持 `deepseek-v4-flash`；MiMo 联网搜索需 `mimo-v2.5` 系列并在控制台启用联网插件（启停有约 5 分钟缓存期）。
2. MiMo 请求显式禁用 thinking（默认开启时偶发返回空 content）。
3. `fetch_page` 无法提取 JS 动态渲染页（SPA）正文，返回明确提示；SSRF 防护默认拒绝内网地址。
4. `mimo_search` 的答案摘要可能被截断（`finish_reason: length`），需更长输出请调大 `MIMO_MAX_OUTPUT`。
5. `notifications/initialized` 无响应，客户端不能同步等待。

## 许可证

MIT
