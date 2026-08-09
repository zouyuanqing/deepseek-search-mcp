# deepseek-search-mcp 测试报告

- **测试日期**：2026-08-09
- **模型**：DeepSeek V4-Flash 正式版（`deepseek-v4-flash`，即 DeepSeek-V4-Flash-0731）
- **API 端点**：`https://api.deepseek.com`
- **测试环境**：Windows，Python 3.11，零第三方依赖
- **执行人**：项目作者

---

## 1. 测试范围

| 编号 | 用例 | 方法 | 结果 |
|---|---|---|---|
| T1 | 健康检查 `--health` | 命令行直连 | ✅ 通过 |
| T2 | stdio 协议握手 `initialize` | `test_stdio.py` | ✅ 通过 |
| T3 | `tools/list` 工具清单 | `test_stdio.py` | ✅ 通过 |
| T4 | `tools/call` `web_search` | `test_stdio.py` | ✅ 通过 |
| T5 | `tools/call` `web_search_deep` | `test_stdio.py --deep` | ✅ 通过 |
| T6 | 同题三方对比 | `bench_search.py` | ✅ 完成 |

## 2. 测试用例明细

### T1 健康检查

```bash
DEEPSEEK_API_KEY=sk-... python deepseek_web_search_mcp.py --health
```

返回 `status: ok`、`web_search_ok: true`，模型 `deepseek-v4-flash`，并附一条真实联网搜索的示例回答（全球头条新闻），验证了 API Key 有效性与搜索链路连通性。

### T2–T5 stdio 全链路

`test_stdio.py` 模拟 MCP 客户端依次发送 `initialize` → `notifications/initialized` → `tools/list` → `tools/call`，验证服务器按 JSON-RPC 2.0 正确响应。两个工具均返回带真实来源链接的搜索结果。

**深搜用例输出示例**（问题：DeepSeek V4-Flash 正式版相比预览版有哪些能力变化？价格如何？）：
- 交叉验证发现 **API 价格存在来源冲突**：部分报道称"价格不变"，但多篇官方文档转载显示"缓存命中单价从 ¥0.2 降至 ¥0.02（降 90%）"，最终以多来源确认的 ¥0.02/¥1/¥2 为准。
- 明确标注**信息缺口**：官方未公布正式版数学专项基准（AIME/HMMT）对比数据。

这验证了深搜模式的核心价值：多来源交叉核验 + 主动披露冲突与不确定性。

## 3. 三方对比

同一组问题分别调用内置 WebSearch、Tavily MCP（`tavily-search`）、本项目的 `web_search` 与 `web_search_deep`。

### 3.1 问题一：DeepSeek V4-Flash API 定价

| 方案 | 耗时 | 结果质量 |
|---|---|---|
| Tavily MCP `tavily_search` | **1.3s** | 返回原始片段；将过时价格（$0.069）与官方当前价格（$0.14）**并列返回，未甄别时效性** |
| 本项目 `web_search` | 29.5s | 带引用综合答案，明确标注"2026-08-06 DeepSeek 宣布将整体上调 API 价格，新价未公布"的时效性信息 |
| 内置 WebSearch | ~3s | 5 条摘要，需自行整合 |

### 3.2 问题二：Meta Muse Spark 1.1 入侵事件官方说法

| 方案 | 耗时 | 结果质量 |
|---|---|---|
| Tavily MCP `tavily_search` | 1.3–5.9s | 返回 2 篇新闻报道原始内容（TVB、大纪元），无综合无核验 |
| 本项目 `web_search_deep` | 245s | 交叉核验多来源，主动标注存疑点："Muse Spark 1.1 模型名系 The Information 援引知情人士披露，Meta 官方声明中未点名具体模型" |

### 3.3 结论

- **Tavily**：快但"检索不思考"，准确性依赖使用方判断，适合日常快查。
- **本项目 `web_search`**：带引用综合答案，多轮搜索自动核实。
- **本项目 `web_search_deep`**：准确性最高，适合事实核查与调研；代价是耗时 2–4 分钟。

## 4. 复现方式

```bash
# stdio 全链路测试
python test_stdio.py --deep

# 三方对比（需 DEEPSEEK_API_KEY 与 TAVILY_API_KEY 环境变量）
python bench_search.py --deep "你的调研问题"

# 直接调用
python mcp_client.py --cmd "npx.cmd" --args "-y tavily-mcp" --tool tavily_search --jsonl \
  --params-file <(echo '{"query":"...","search_depth":"advanced","max_results":5}')
```

## 5. 测试中发现的问题与修复

| 问题 | 根因 | 修复 |
|---|---|---|
| Responses API 顶层 `output_text` 为空 | DeepSeek 实现未填充该字段 | 改为从 `output[].message.content[].output_text` 提取 |
| `web_search_deep` 子查询解析失败 | 模型输出偶发非标准 JSON | 增加容错解析：先 JSON 数组，回退按行/编号提取 |
| MCP 客户端同步等待 `notifications/initialized` 卡死 | 该通知无响应 | 测试脚本用线程 + join 超时处理 |
| Windows 下 `select` 对管道不可靠 | Windows 管道不支持 select | 改用线程读取 + 超时 |
| tavily-mcp initialize 400 | 新版 SDK 要求 `capabilities`/`clientInfo` | 客户端补齐字段 |
