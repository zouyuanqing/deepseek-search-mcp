# deepseek-search-mcp 测试报告

- **测试日期**：2026-08-12（v0.3.0，MiMo 集成版）
- **测试环境**：Windows，Python 3.14.4，零第三方依赖
- **真实 API**：DeepSeek（`deepseek-v4-flash`）+ 小米 MiMo（`mimo-v2.5-pro`）
- **执行人**：项目作者

---

## 1. 测试范围与结果总览

| 编号 | 用例 | 方法 | 结果 |
|---|---|---|---|
| T1 | 单元：`Location` 双格式解析（结构化/中文文本/英文/边界） | `tests/test_models.py` | ✅ 25 通过 |
| T2 | 单元：URL 提取（markdown 链接/裸链接/中文标点/去重） | `tests/test_models.py` | ✅ 4 通过 |
| T3 | 单元：`_http_post` 成功/HTTP 错误/网络错误/超时 | `tests/test_providers.py` | ✅ 3 通过 |
| T4 | 单元：DeepSeekClient 请求构造/响应解析/错误分支 | `tests/test_providers.py` | ✅ 6 通过 |
| T5 | 单元：MimoClient 请求构造（tools/force_search/max_keyword/limit/user_location）/响应解析/错误分支 | `tests/test_providers.py` | ✅ 9 通过 |
| T6 | 单元：FallbackChain 降级链（首成功/全败聚合/chat 降级/空链） | `tests/test_providers.py` | ✅ 5 通过 |
| T7 | 单元：`build_backends` 三种模式与 key 缺失处理 | `tests/test_providers.py` | ✅ 7 通过 |
| T8 | 集成：fetch_page 本地 HTTP（HTML 提取/JSON/纯文本/404/重定向/超时/二进制拒绝） | `tests/test_fetch.py` | ✅ 7 通过 |
| T9 | 集成：fetch_page SSRF 防护（内网/保留地址拒绝 + 白名单开关） | `tests/test_fetch.py` | ✅ 5 通过 |
| T10 | 集成：stdio 协议冒烟（无 key：initialize/ping/tools-list/schema/参数校验/友好错误） | `tests/test_stdio_mcp.py` | ✅ 12 通过 |
| T11 | 集成：stdio 真实搜索链路（有 key：`mimo_search` + `health`） | `tests/test_stdio_mcp.py` | ✅ 2 通过 |
| T12 | 真实验证：`--health`（BACKEND=auto） | 命令行 | ✅ 通过 |
| T13 | 真实验证：`mimo_search`（武汉天气 + user_location） | `mcp_client.py` | ✅ 通过（32.6s） |
| T14 | 真实验证：`web_search` auto 链降级（backend=mimo） | `mcp_client.py` | ✅ 通过（39.8s） |
| T15 | 真实验证：`fetch_page`（python.org 静态页/gzip/中文 URL/SPA 提示/404） | `mcp_client.py` | ✅ 通过 |
| T16 | 真实验证：`web_search_deep` + `enrich_mimo`（6 路子查询 + 交叉核验） | `mcp_client.py` | ✅ 通过（210s） |

**合计：75 个自动化用例全部通过**（无 key 环境跳过 2 个 live 用例，配置 key 后自动包含）。

---

## 2. MiMo API 实测关键结论

### 2.1 能力验证（2026-08-12 真实调用）

| 项目 | 结果 |
|---|---|
| Key 鉴权 | `api-key: $MIMO_API_KEY` 头可用；`GET /v1/models` 返回 `mimo-v2.5` / `mimo-v2.5-pro` / ASR / TTS 系列 |
| 联网搜索 | `tools: [{"type": "web_search", "force_search": true, "max_keyword", "limit"}]` 生效，无需额外控制台配置 |
| **位置定制化** | `user_location: {"type":"approximate","country","region","city"}` 生效：武汉天气返回中央气象台（nmc.cn）、天气网（tianqi.com）、京报网等**本地权威来源** |
| 响应结构 | 来源在 `choices[].message.annotations[]`（`type: url_citation`，含 `url/title/site_name/publish_time/summary/logo_url`）；`tool_calls` 为 null（内建工具不出现） |
| 用量 | `usage.web_search_usage: {tool_usage, page_usage}`；prompt 因拼接搜索内容较大（实测 2661–8306 token） |

### 2.2 实测发现的问题（已在代码中修复）

| 问题 | 根因 | 修复 |
|---|---|---|
| **MiMo 间歇性返回空 content** | `thinking` 参数默认 enabled，思考模式下 `content` 可能为空 | 所有 MiMo 请求显式 `"thinking": {"type": "disabled"}`；空内容错误附带 reasoning 提示 |
| 答案截断 | `max_completion_tokens` 过小 → `finish_reason: "length"` | 默认 4096；文档说明用 `fast` 模式控制成本 |
| 同 URL 重复出现在 sources | MiMo annotations 可能含重复引用 | sources 去重保序（citations 保留完整） |
| `finish_reason` 暴露 | 响应含该字段但未透出 | SearchResult 增加 `finish_reason` 字段 |

### 2.3 深搜路径（BACKEND=auto，仅有 MiMo key）

实测 `web_search_deep` 全链路（规划 → 6 路子查询 → 交叉核验综合）210s 完成：

- 规划输出为多行 JSON 数组，`_parse_queries` 正确解析出 6 个子查询（首版实现曾因单行 JSON 数组走 fallback 导致未解析，已修复为优先 JSON 解析）
- 每路子查询独立搜索、独立来源（实测每路 20–35 个来源）
- 综合答案主动标注数据差异（如销量预测 1900 万 vs 1730 万辆、渗透率 54% vs 59%）与信息缺口
- `enrich_mimo=true` 且子查询已由 MiMo 完成时自动跳过重复检索（节省约一半调用）

---

## 3. fetch_page 实测

| 目标 | 结果 |
|---|---|
| python.org（静态 HTML + gzip） | ✅ title/正文提取正常（修复 gzip 解压 + 截断时机问题后） |
| 中央气象台武汉页（JS 渲染） | ✅ 返回"无可提取的静态文本"明确提示，title 正常 |
| 腾讯新闻/天气网（SPA） | ✅ 同上前置提示 |
| 中国消费者报（模板未渲染） | ✅ 页面本身是 PHP 模板，提取极少文本（站点问题，非工具问题） |
| 中文 URL（`/wiki/武汉`） | ✅ percent-encoding 修复后正常（初版报 ascii 编码错误） |
| 404 / 超时 / 二进制 / SSRF | ✅ 均返回明确错误 |

**初版发现并修复的 bug**：

1. **HTML 截断时机错误**：原实现在解析前就把 body 截断到 `max_chars`，导致解析不完整、正文/标题全空。修复：抓取预算放大（≥256KB）、解析完整 HTML、仅对**提取出的文本**截断。
2. **gzip 未解压**：部分站点（如 python.org）返回 `Content-Encoding: gzip`，urllib 不自动解压，提取出二进制乱码。修复：按响应头解压（gzip/deflate），读取预算按压缩比放大。
3. **中文 URL 报 ascii 错误**：urllib 不自动处理非 ASCII URL。修复：path/query 做 percent-encoding。
4. **socket 超时未捕获**：Python 3.10+ socket 超时直接抛 `TimeoutError` 而非包装为 URLError。修复：显式捕获 `TimeoutError`（providers 同步修复）。
5. **`<head>` 误入跳过标签**：导致 `<title>` 提取不到。修复：从跳过集移除 head。

---

## 4. 降级链（FallbackChain）验证

| 场景 | 结果 |
|---|---|
| `BACKEND=deepseek` 无 DeepSeek key | 启动即报"未设置环境变量 DEEPSEEK_API_KEY"（友好错误） |
| `BACKEND=mimo` 无 MiMo key | 同上（MIMO_API_KEY） |
| `BACKEND=auto` 双 key | 链 = `[deepseek, mimo]`，DeepSeek 优先 |
| `BACKEND=auto` 单 key | 自动跳过缺失后端，链只含可用后端 |
| `BACKEND=auto` 无 key | 报"未配置任何 API Key：需要 DEEPSEEK_API_KEY 或 MIMO_API_KEY" |
| 首后端失败 → 次后端成功 | 结果带 `backend` 字段标注实际后端（实测 web_search 降级 mimo） |
| 全后端失败 | 聚合各后端错误信息一并抛出 |

---

## 5. 复现方式

```bash
# 全量自动化测试（有 key 时自动包含 live 用例）
python -m unittest discover -s tests -p "test_*.py"

# 健康检查（BACKEND=auto，双后端状态）
MIMO_API_KEY=sk-... BACKEND=auto python deepseek_web_search_mcp.py --health

# mimo_search 真实调用（位置定制化）
python mcp_client.py --cmd "python" --args "deepseek_web_search_mcp.py" \
  --tool mimo_search --params-file params.json \
  --env "MIMO_API_KEY=sk-..." --env "BACKEND=auto" --timeout 600

# web_search_deep + enrich_mimo（耗时 3-4 分钟）
python mcp_client.py --cmd "python" --args "deepseek_web_search_mcp.py" \
  --tool web_search_deep --params-file deep.json \
  --env "MIMO_API_KEY=sk-..." --env "BACKEND=auto" --timeout 900

# fetch_page
python mcp_client.py --cmd "python" --args "deepseek_web_search_mcp.py" \
  --tool fetch_page --params-file fetch.json
```

## 6. 历史问题与修复记录（v0.2.0 保留）

| 问题 | 根因 | 修复 |
|---|---|---|
| Responses API 顶层 `output_text` 为空 | DeepSeek 实现未填充该字段 | 从 `output[].message.content[].output_text` 提取 |
| `web_search_deep` 子查询解析失败 | 模型输出偶发非标准 JSON | 先 JSON 数组，回退按行/编号提取 |
| MCP 客户端同步等待 `notifications/initialized` 卡死 | 该通知无响应 | 测试脚本用线程 + join 超时处理 |
| Windows 下 `select` 对管道不可靠 | Windows 管道不支持 select | 改用线程读取 + 超时 |
| tavily-mcp initialize 400 | 新版 SDK 要求 `capabilities`/`clientInfo` | 客户端补齐字段 |

## 7. 双后端全链路验证（DeepSeek + MiMo，2026-08-12 补充）

本轮使用真实 DeepSeek key 补测 v0.3.0 初版未覆盖的路径：

| 场景 | 结果 |
|---|---|
| `--health`（BACKEND=auto 双 key） | ✅ 链 = `[deepseek, mimo]`，双后端 health 均 ok（DS 8 来源 / MiMo 22 来源） |
| `web_search`（DeepSeek 真实路径） | ✅ backend=deepseek，19.8s；人民币汇率多来源交叉核实（央行官网/证券时报/上证报一致） |
| `web_search_fast`（DeepSeek fast） | ✅ 6.3s，reasoning_tokens 118（低推理生效），A股收盘带来源 |
| `web_search_deep` + `enrich_mimo`（双后端） | ✅ 602.7s；6 路子查询；**enrich_mimo 真实生效**（DS 子查询 + MiMo 补充）；**fallback 实测生效**（政策子查询 DS 失败自动降级 mimo）；综合答案标注数据差异 |
| 长链路网络抖动 | ✅ `ConnectionResetError`（WinError 10054）/ DNS 抖动（getaddrinfo failed）不再抛内部错误：归一化为 ProviderError → fallback 链自动降级 → 全败时聚合各后端错误 |

### 7.1 本轮发现并修复

| 问题 | 根因 | 修复 |
|---|---|---|
| `web_search_deep` 报"内部错误: WinError 10054" | `ConnectionResetError`（OSError 子类）未捕获，绕过 fallback 链 | `_http_post` 增加 `OSError` / 兜底 `Exception` 捕获，断连归一化为 ProviderError，链式降级生效 |
| 瞬时 DNS 故障（getaddrinfo failed）导致双后端全败 | 网络环境抖动 | 降级链正确聚合双后端错误信息，重试即恢复（无需代码改动，验证了错误聚合路径） |

### 7.2 双后端 deep 模式实测数据

- 子查询 6 路中 5 路 backend=deepseek + `mimo_extra`（MiMo 补充 10–25 个额外来源），1 路 DS 失败自动降级 backend=mimo
- 单次综合答案来源 100+ 条；交叉核验主动标注：恒指收盘数据多源冲突（新华社 25440.17 vs 网络转载 25392.74，判定转载误差）、成交额口径差异（2.15/2.16/2.17 万亿）
- 成本提示：deep + enrich 双后端串行约 10 分钟，建议仅在高准确性调研场景使用，常规场景用 `web_search` 或 `web_search_fast`

## 8. 遗留风险

- **DeepSeek 真实调用已回归**：本轮完成 `web_search` / `web_search_fast` / `web_search_deep` 真实调用（见 §7），v0.3.0 初版"仅 mock 验证"的遗留项已消除。
- **MiMo 行为漂移**：MiMo API 处于快速迭代期（V2.5 系列 2026-06 切换），模型名/字段可能变化；`finish_reason: length` 的截断在部分查询下仍可能出现。
- **fetch_page 提取质量**：正文提取为轻量 HTMLParser（无正文识别），对复杂布局页可能含导航噪声或漏内容；如需更高提取质量可评估引入 trafilatura。
- **长链路网络韧性**：deep 模式串行 8–10 次 API 调用，偶发断连虽已被降级链兜底，但会损失对应子查询的 DeepSeek 质量（降级为 MiMo raw）。
