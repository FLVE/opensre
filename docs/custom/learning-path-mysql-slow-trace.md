# 学习路径：从「MySQL 为什么慢?」到诊断结论

面向第一次读 OpenSRE 代码的人。用一条真实请求把仓库串起来：在交互 shell 里输入
`MySQL为什么慢?`，跟着代码走到 LLM、走到工具、走到数据库，再走回诊断文本。
每一站给出 **读哪几个文件（带行号）、看懂什么、动手验证什么**，最后附一份
**关键点日志审计表** 和 **在本机看到这些事实的方法**。

行号以 `main` 分支 `10d935c6e` 为准，之后可能漂移；文件名和函数名更稳定，优先记函数名。

---

## 0. 先纠正心智模型

动手前先把预期链路和真实链路对齐，否则会在不存在的模块里找代码。

```
预期：main.py → interactive_shell → investigation → agent → LLM → tool selection
      → prometheus tool → prometheus client → agent → mysql tool → mysql client → diagnosis

真实：main.py → interactive_shell → 【action turn（chat 路径）】→ core.agent ReAct 循环 → LLM
      → LLM 自己在全部可用工具里挑（无预选）→ core/tool/execution.py 按名字分发
      → query_grafana_metrics（走 Grafana 数据源代理查 Mimir，没有独立 Prometheus 集成）
      → get_mysql_slow_queries / get_mysql_current_processes（pymysql）
      → 工具结果追加回消息 → 下一轮 LLM → 无工具调用即为最终回答（这就是"diagnosis"）
```

三个必须知道的事实：

| 误区 | 真相 | 证据 |
| --- | --- | --- |
| 自由文本会进入 `tools/investigation/` 六阶段流水线 | **默认不会。** 自由文本走 action turn，直接进共享 ReAct 循环。只有 LLM 主动调用 `investigation_start` 工具、或用户敲 `/investigate` / `opensre investigate` 才进六阶段流水线。 | `core/agent_harness/turns/action_driver.py:694` `_build_action_agent`；`tools/interactive_shell/actions/investigation.py:121-134` 工具描述明确写"没有 investigate 动词不要用" |
| 有 `prometheus tool` / `prometheus client` | **没有。** 仓库里 `PROMETHEUS_URL` 零命中。PromQL 由 `query_grafana_metrics` 通过 Grafana 的 `/api/datasources/proxy/uid/<mimir_uid>/api/v1/query` 发出（instant query，硬编码 10s 超时）。Grafana 上任何 **type=prometheus** 的数据源（Mimir、自建 Prometheus、VictoriaMetrics）都会被当作 `mimir_uid`。分析 MySQL 时 LLM 会用它查 mysqld_exporter 指标——完整流程见"第 5 站 · 补"。 | `integrations/grafana/tools/grafana_metrics_tool/__init__.py:73-144`；`integrations/grafana/mimir.py:14-78`；`integrations/grafana/base.py:188-192, :427-442` |
| 有一个"tool selection"阶段做预选/排序 | **chat 路径没有。** 所有可用工具的 schema 全部发给 LLM，由模型函数调用挑选。只有 investigation 路径有 `select_investigation_tools`（≤32 个）+ `plan_actions` 评分。 | `core/agent_harness/tools/action_tools.py:53-76`；`core/agent/react_loop.py:160`；`tools/investigation/stages/gather_evidence/tools.py:70` |

> 提示：`MySQL为什么慢?` 这个问题本身没有"调查/RCA/根因"动词，所以正常情况下
> 走 **chat 路径**，LLM 会直接在循环里调 MySQL / Grafana 工具，然后给出结论。
> 想看六阶段流水线，敲 `/investigate MySQL为什么慢?`。

---

## 1. 学习顺序（六站）

每站控制在 1–2 小时。**不要一口气读完再动手**，每站结尾都有一个 5 分钟实验。

### 第 1 站：进程入口与 shell 启动（30 分钟）

读这些，按顺序：

| 文件:行 | 函数 | 看懂什么 |
| --- | --- | --- |
| `main.py:47` | `main` | 只是转发到 `surfaces.entrypoint.main`；模块 docstring 是全仓库最好的"嵌入式用法"说明，值得整段读 |
| `surfaces/entrypoint.py:39` | `main` | 把 shell / gateway 作为回调注入 `CliHost`，三个 surface 互不 import |
| `surfaces/cli/app.py:105` | `_run_without_subcommand` | 裸 `opensre` + 双 TTY → 启动 shell；`--verbose/--debug` 只是设 `TRACER_VERBOSE=1`（`:217-218`） |
| `surfaces/interactive_shell/main.py:29` | `run_repl_async` | 启动顺序：静音第三方 logger（`:43`）→ 装 shell 日志处理器（`:52`）→ `create_repl_runtime`（`:55`）→ 打开 session 存储（`:64`）→ 控制器 |
| `surfaces/interactive_shell/controller.py:157, :226` | `InteractiveShellController.__init__` / `start_interactive_shell` | 组装 `TurnRunner`、后台任务池、输入循环 |
| `bootstrap/process.py` | `configure_process` | 谁在启动时把 `tools/` 和 `integrations/` 的适配器注册进 harness（这是工具"可用"的前提） |

实验：
```bash
uv run opensre --help          # 看 Click 组
uv run opensre doctor          # 看本机 LLM / 集成配置是否就绪
ls ~/.opensre/                 # opensre.json、sessions/、operations_log.jsonl 都在这
```

### 第 2 站：一行输入如何变成"一个 turn"（45 分钟）

| 文件:行 | 函数 | 看懂什么 |
| --- | --- | --- |
| `surfaces/interactive_shell/runtime/turn_host.py:315` | `run_input_loop` | `read()` → `decide_input_action` → `handle_input_action` |
| `surfaces/interactive_shell/runtime/input/actions.py:65` | `decide_input_action` | 非取消/确认 → `SubmitTurn(text)` |
| `surfaces/interactive_shell/runtime/input_policy.py:9, :114` | `_literal_slash_command_text` / `turn_needs_exclusive_stdin` | **这里判断 `/` 只影响 UI（spinner、独占 stdin），不做路由** |
| `surfaces/interactive_shell/controller.py:285-304` | `_handle_input_action` | 把文本放进 `asyncio.Queue` |
| `turn_host.py:358, :195, :254` | `run_agent_turn_queue` / `run_agent_turn` / `_run_agent_turn_loop` | 出队 → `asyncio.to_thread(execute_shell_turn)`：**agent 跑在线程里**，UI 留在事件循环 |
| `surfaces/interactive_shell/runtime/shell_turn_execution.py:31, :71-81` | `execute_shell_turn` | 创建 `ShellOutputSink`，把 logger `opensre.interactive_shell` 传给 `TurnRunner.run` |
| `infrastructure/turn_host/turn_runner.py:109, :164` | `TurnRunner.run` / `_run_turn` | 并发槽位、取消、`agent.handle(text, TurnBinding)`、`output.finalize`、session flush |
| `infrastructure/turn_host/session_agents.py:106, :127` | `SessionAgentPool.session_agent` / `agent_for` | **一个 session 只建一个 `HeadlessAgent`，多轮复用**（README 强调的"不要每条消息重建"就在这实现） |

实验：在 shell 里输入 `MySQL为什么慢?`，然后 `/status`、`/cost`、`/context` 看 turn 的账目。

### 第 3 站：Harness —— 从 `HeadlessAgent.handle` 到 ReAct 循环之前（1 小时）

这一站是自由文本的**真正分岔点**。

| 文件:行 | 函数 | 看懂什么 |
| --- | --- | --- |
| `core/agent_harness/turns/headless_agent.py:105, :128, :177` | `handle` / `run_goal` / `dispatch` | `handle` 是薄包装；`run_goal` 调 `run_until_session_goal` |
| `core/agent_harness/session_goal/run_until.py:199, :233` | `run_until_session_goal` | **总是先跑一次 chat**；没有 goal 就只跑一轮（`:239-245`），不扫描文本 |
| `core/agent_harness/turns/orchestrator.py:80` | `run_turn` | 自动压缩上下文 → 构建 `TurnPlan` → `execute_actions` → 记录对话 → 结算 token |
| `core/agent_harness/turns/action_driver.py:857, :1086` | `ActionTurnRunner.run` / `_run_action_turn` | `component_span("action_turn")` 包裹；`args.tools.action_tools(...)` 取工具；构建 agent；带遥测跑 |
| `action_driver.py:625, :656, :694-786` | `_bang_shell_command` / `_literal_slash_tool_call` / `_build_action_agent` | 只有 `!cmd` 和字面 `/slash` 走静态工具调用绕过 LLM；`MySQL为什么慢?` 落入 `else` 分支：`llm_factory()` + 系统提示 + 用户消息 |
| `core/agent_harness/tools/action_tools.py:53-76` | `get_action_tools_from_integrations_view` | **工具过滤 = ACTION∪CHAT surface ∩ `is_available(已解析集成)` − 排除名单。无上限、无评分** |
| `core/agent_harness/agent_builder.py:44` | `build_agent` | 全仓库唯一的 `core.agent.Agent` 构造点 |
| `tools/interactive_shell/actions/investigation.py:119-134` | `investigation_start_tool` | 六阶段流水线的入口工具；读它的 description，理解 LLM 何时会选它 |

必读规则：`surfaces/interactive_shell/AGENTS.md` "Action Selection And Execution"、`core/agent_harness/AGENTS.md:64-68`。一句话：**不允许对自由文本做关键字/正则路由，意图只在 LLM 的 action turn 里决定**。你以后想"加个判断把 MySQL 问题直接路由到 MySQL 工具"——这是被明令禁止的。

实验：`rg -n "investigation_start" tools/interactive_shell core/agent_harness` 数一数调用方；确认没有任何地方靠字符串匹配触发它。

### 第 4 站：ReAct 循环 —— LLM、工具选择、工具执行、结果回灌（1.5 小时）

整个仓库最值得逐行读的文件是 `core/agent/react_loop.py`（约 770 行）。配合 `core/tool/execution.py`。

| 文件:行 | 函数 | 看懂什么 |
| --- | --- | --- |
| `core/agent/agent.py:82` | `Agent.run` | 解析本轮输入 → `run_react_loop` |
| `core/agent/react_loop.py:159-163` | `ReactLoop.__init__` | `host._filter_tools`（默认恒等）→ **`self._llm.tool_schemas(tools)`：这一行就是"发给 LLM 的工具清单"** → 固定 token 开销 |
| `react_loop.py:203` | `run` 内 `for iteration in range(max_iterations)` | 迭代上限：chat 路径 64（`action_driver.py:212`），investigation 路径 `MAX_INVESTIGATION_LOOPS` |
| `react_loop.py:326-337` | `_think` | 组 `ProviderRequest`，`enforce_context_budget`（`core/context_budget.py`）裁剪历史 |
| `react_loop.py:373` | `self._llm.invoke(...)` | **LLM 请求就在这一行** |
| `core/llm/transports/sdk/agent_clients.py:162, :302-303` | `AnthropicAgentClient.invoke` | 把 provider 响应里的 `tool_use` 块解析成 `ToolCall`；OpenAI 在 `:612`，Bedrock `:413`，CLI 后端 `:815` |
| `react_loop.py:322-324` | 分支 | 无 tool call → `_handle_conclusion`（`:394`，最终回答）；有 → `_observe`（`:421`） |
| `react_loop.py:452` → `core/tool/execution.py:226` | `execute_tool_calls` | `tool_map = {t.name: t}`（`:245`）；多个调用用线程池并行（`:260`） |
| `execution.py:338, :349, :359` | `_execute_one_tool_call` | **按名字分发就在 `tool_map.get(tc.name)`**；校验 `validate_public_input`；前置 hook |
| `execution.py:428-462` | `_invoke_runtime_tool` | `RegisteredTool.run(**{**extract_params, **tc.input})` —— `extract_params` 注入凭据，LLM 永远看不到 |
| `react_loop.py:459-463` | `_observe` 尾部 | 工具结果 → `to_tool_result_runtime_message` → **追加进 `self._messages`，下一轮 LLM 看到它** |
| `react_loop.py:108, :510-527` | `_observation_fingerprint` / 停滞判断 | 连续无新证据 → `stop_reason="stagnation_limit"` |
| `react_loop.py:536` | `_run_safety_handoff` | 撞上限后**禁用工具再问一次**要结论，失败则用固定兜底文案 |
| `react_loop.py:629` | `_finalize` | 产出 `AgentRunResult`（`core/agent/run_io.py:20`） |

工具 schema 从哪来：`core/tool/contracts.py:667-671`（`input_model` → JSON schema，否则 `infer_input_schema :73` 从函数签名推断）；`tools/registry_skill_guidance.py:58` 会把"Workflow guidance"**拼进 description**，所以技能指导是通过描述文本影响 LLM 选择，不是过滤。

实验（不需要真实 LLM）：
```bash
uv run pytest tests/core/agent -q -x        # 循环行为的特征测试
uv run pytest tests/core/tool -q -x         # 分发/校验
```
读 `tests/core/agent/test_turn_scenarios.py`，它用假 LLM 把"先调工具、再给结论"的两轮走了一遍，是最便宜的活教材。

### 第 5 站：两个具体工具 —— Grafana/Mimir 与 MySQL（1 小时）

**PromQL（Grafana Mimir）**

| 文件:行 | 看懂什么 |
| --- | --- |
| `integrations/grafana/tools/grafana_metrics_tool/__init__.py:73-98` | `@tool(name="query_grafana_metrics", source="grafana", requires=["metric_name"], injected_params=...)`；输入模型 `:19-27` 只有 `metric_name`（PromQL）和 `service_name` |
| `grafana_metrics_tool/__init__.py:119, :132` | `_resolve_grafana_client` → `client.query_mimir` |
| `integrations/grafana/client.py:69, :137-202` | 客户端构建 + 缓存；先做数据源 UID 发现（`base.py:205`） |
| `integrations/grafana/mimir.py:35-42, :48-65` | URL = `{instance_url}/api/datasources/proxy/uid/{mimir_uid}/api/v1/query`；`service_name` 是**裸字符串拼接**进 label |
| `integrations/grafana/base.py:419-442` | 鉴权（Basic / Bearer）+ `requests.get(..., timeout=10)` |
| `grafana_metrics_tool/__init__.py:55-70` | 证据映射 `_map_grafana_metrics` → `record_evidence_entry(source="grafana_metrics")` |

**MySQL（pymysql）**

| 文件:行 | 看懂什么 |
| --- | --- |
| `integrations/mysql/tools/mysql_slow_queries_tool/__init__.py:47-75` | `get_mysql_slow_queries(host[注入], database, threshold_ms=1000, port=3306)`；schema 由签名推断 |
| `integrations/mysql/tools/mysql_current_processes_tool/__init__.py:38-69` | `get_mysql_current_processes(threshold_seconds=1)` |
| `core/tool_framework/utils/sql_wrapper.py:18-52` | `database` 缺省补 `"mysql"` 并附 `default_db_warning` |
| `integrations/mysql/__init__.py:107-131, :148-169` | `resolve_mysql_config`（store 优先，其次 `MYSQL_*` 环境变量）；`_get_connection`：`pymysql.connect(connect_timeout=10, DictCursor, autocommit)` |
| `integrations/_relational.py:113-161` | `read_only_query` 装饰器：未配置 → `tool_unavailable`；开连接、跑、`finally` 关；**任何异常 → `report_validation_failure` + `tool_unavailable`** |
| `integrations/mysql/__init__.py:421-499` | 慢查询 SQL：先 `SELECT @@performance_schema`，再查 `events_statements_summary_by_digest ... ORDER BY AVG_TIMER_WAIT DESC LIMIT max_results`；`digest_text` 截到 500 字符 |
| `integrations/mysql/__init__.py:310-354` | 进程 SQL：`information_schema.PROCESSLIST WHERE COMMAND != 'Sleep' AND TIME >= %s` |

实验：
```bash
uv run opensre integrations setup mysql     # 配一个本地 MySQL（docker 起一个即可）
uv run opensre integrations verify mysql    # 走 validate_mysql_config → SELECT VERSION()
uv run pytest tests -k mysql -q             # 看工具的单测如何伪造 cursor
```

### 第 5 站 · 补：分析 MySQL 为什么还要查 Prometheus，以及 PromQL 的完整调用流程

**为什么两边都要查。** `performance_schema` 回答的是"**哪条 SQL** 慢"（digest、平均耗时、扫描行数），
但回答不了"**从什么时候开始**慢""慢的时候**资源什么样**"。这两个问题要问 Prometheus 里
mysqld_exporter / node_exporter 的时间序列：

| 想知道 | PromQL（mysqld_exporter 指标名） |
| --- | --- |
| 慢查询是不是最近突然变多 | `rate(mysql_global_status_slow_queries[5m])` |
| 是否在排队（并发执行的线程） | `mysql_global_status_threads_running` |
| 连接数是否打满 | `mysql_global_status_threads_connected / mysql_global_variables_max_connections` |
| QPS 有没有异常 | `rate(mysql_global_status_questions[5m])` |
| 缓冲池命中率是否下降（读盘变多） | `1 - rate(mysql_global_status_innodb_buffer_pool_reads[5m]) / rate(mysql_global_status_innodb_buffer_pool_read_requests[5m])` |
| 主机 CPU / IO 是否饱和 | `1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))`、`rate(node_disk_io_time_seconds_total[5m])` |

一次典型的对话里 LLM 的两轮工具调用长这样：

```
第 1 轮  get_mysql_slow_queries(threshold_ms=1000)          ← "哪条 SQL 慢"
        get_mysql_current_processes(threshold_seconds=5)     ← "现在谁卡着"
第 2 轮  query_grafana_metrics(metric_name="rate(mysql_global_status_slow_queries[5m])")
        query_grafana_metrics(metric_name="mysql_global_status_threads_running")  ← "什么时候开始、有多严重"
第 3 轮  不再请求工具 → 结论
```

**前提。** 仓库没有独立 Prometheus 集成，PromQL 只能经 Grafana 走：你需要 `opensre integrations setup grafana`
配好实例地址和 token，且这个 Grafana 上挂了一个 **type=prometheus** 的数据源（Grafana Cloud 的 Mimir、
自建 Prometheus、VictoriaMetrics 都可以——`base.py:191` 只看类型字符串）。

**逐步流程（file:line）。** 从 LLM 吐出 tool_call 开始：

| # | 位置 | 发生了什么 |
| --- | --- | --- |
| 1 | `core/agent/react_loop.py:452` → `core/tool/execution.py:349` | `tool_map.get("query_grafana_metrics")` 按名字找到工具 |
| 2 | `execution.py:447-462` | `RegisteredTool.run(**{**extract_params, **tc.input})`。`extract_params`（`grafana_metrics_tool/__init__.py:41-48`）注入 `grafana_endpoint / grafana_api_key / …` 凭据，还带一个默认 `metric_name="pipeline_runs_total"`——**被 LLM 给的 `tc.input["metric_name"]` 覆盖**，LLM 永远看不到凭据 |
| 3 | `grafana_metrics_tool/__init__.py:99-117` | 工具函数体。若 `grafana_backend` 非空走 benchmark 假后端（`:112-117`），真实路径继续 |
| 4 | `__init__.py:119` → `tools/_helpers.py:22-40` → `integrations/grafana/client.py:69` | `get_grafana_client_from_credentials`：按凭据做 cache key，**首次**才 `_build_and_cache_client`（`client.py:137-202`） |
| 5 | `client.py:164` → `base.py:205` `discover_datasource_uids` | `GET {instance_url}/api/datasources`，按 `_TYPE_MAP`（`base.py:188-192`）把 `type=="prometheus"` 的数据源归到 `mimir_uid`；多个候选时优先级：`isDefault` → 名字含 `prom/metrics` → 不含 `alert/state-history/ml-/usage-insights` → 第一个（`base.py:262-297`）。发现不到就退回环境变量 `GRAFANA_MIMIR_DATASOURCE_UID`（`client.py:165-168`）。这一步有 INFO 日志（`base.py:253, :294`；`client.py:185`） |
| 6 | `__init__.py:124-130` | `client.is_configured` / `client.mimir_datasource_uid` 任一为空 → `tool_unavailable("grafana_mimir", …)`，LLM 会看到一条"不可用"的工具结果 |
| 7 | `__init__.py:132` → `integrations/grafana/mimir.py:35-44` | `query_mimir`：URL = `{instance_url}/api/datasources/proxy/uid/{mimir_uid}/api/v1/query`（`base.py:128-129`）；`service_name` 非空时**裸拼**成 `metric{service_name="…"}`；`params={"query": …}` |
| 8 | `mimir.py:47` → `base.py:427-442` | `requests.get(url, headers=鉴权, params, timeout=10, verify=ssl_verify)`。鉴权顺序：有用户名密码走 Basic，否则 `Bearer <read_token>`，否则无头（`base.py:419-425`）。**这是一个 instant query**，没有 `start/end/step` |
| 9 | Grafana → Prometheus | Grafana 的数据源代理把请求原样转给后端 Prometheus API，返回标准 `{"status":"success","data":{"resultType":"vector","result":[…]}}` |
| 10 | `mimir.py:48-65` | 只取 `data.result[]` 的 `metric` 和 `value`，返回 `{success, metrics, total_series, query, account_id}`；任何异常走 `:66-78`，被吞成 `{"success": False, "error": …}`（**不打日志、不进 Sentry**） |
| 11 | `__init__.py:133-144` | 失败 → `tool_unavailable`；成功 → `{source:"grafana_mimir", available:true, metrics, total_series, metric_name, service_name, account_id}` |
| 12 | `execution.py:396/482` `_normalize_result` → `__init__.py:55-70` `_map_grafana_metrics` | 结果规范化；证据映射器写 `evidence["grafana_metric_results"][metric_name]` 并 `record_evidence_entry(source="grafana_metrics", summary="<expr>, N series")` |
| 13 | `react_loop.py:459-463` | 打包成 tool-result 消息追加进 `messages`，下一轮 LLM 看到它 |

**LLM 实际看到的请求与响应（示例）**

```jsonc
// 第 2 轮 LLM 吐出的 tool_call（Anthropic tool_use 块解析后）
{"name": "query_grafana_metrics",
 "input": {"metric_name": "rate(mysql_global_status_slow_queries[5m])"}}

// 出站 HTTP（base.py:433）
GET https://grafana.example.com/api/datasources/proxy/uid/prom-main/api/v1/query
    ?query=rate(mysql_global_status_slow_queries[5m])
Authorization: Bearer ****   timeout=10

// 回灌给 LLM 的工具结果（__init__.py:137-144）
{"source": "grafana_mimir", "available": true,
 "metric_name": "rate(mysql_global_status_slow_queries[5m])",
 "total_series": 1,
 "metrics": [{"metric": {"instance": "db-1:9104", "job": "mysqld"},
              "value": [1756563000.123, "4.7"]}]}
```

**读代码时的四个坑**

1. **只有 instant query**：拿不到曲线，"最近 30 分钟趋势"必须由 LLM 自己把窗口写进表达式（`rate(...[30m])`、`max_over_time(...[30m])`）。`infrastructure/evidence/metric_summary.py:98` 那个会算 min/max/avg 的汇总器读的是 range 形状（`values`），和这里的 `value` 对不上，所以实时路径不走它。
2. **工具描述没提 MySQL / Prometheus**：`description="Query Grafana Cloud Mimir for pipeline metrics."`，`use_cases` 全是 pipeline 措辞（`__init__.py:78-83`）。LLM 是靠这段文本决定要不要用它的——想让它在 MySQL 场景里更主动地查指标，最直接的改法是给 `use_cases` / `examples` 加上 mysqld_exporter 的例子（改的是元数据，不是逻辑）。
3. **失败是静默的**：数据源没配、token 过期、PromQL 语法错，全部变成 `available:false` 的工具结果回给 LLM，本地日志和 Sentry 都没有；只有数据源发现阶段有 INFO/WARNING。
4. **`service_name` 裸拼**：`metric{service_name="<值>"}` 不转义，LLM 传了引号就语法错，白白浪费一轮。对 mysqld_exporter 指标更常用的是 `instance` 标签，直接写进 `metric_name` 表达式更稳。

实验：
```bash
uv run opensre integrations setup grafana
uv run opensre integrations verify grafana                 # 会跑 discover_datasource_uids
uv run pytest tests -k "grafana and metrics" -q            # 看假后端 / 假 client 如何伪造响应
```

### 第 6 站：结论如何产生（30 分钟）

**chat 路径**：没有"diagnosis 阶段"。LLM 某一轮不再请求工具，`_handle_conclusion`
（`react_loop.py:394`）把 `response.content` 当最终文本，`stop_reason="completed"`，
经 `action_driver.py:1086` 后半段 compose/show 输出到终端。

**investigation 路径**（`/investigate` 时）：

| 文件:行 | 阶段 |
| --- | --- |
| `tools/investigation/agent_pipeline.py:113, :28-33` | `run_agent_investigation`，阶段顺序 SoT：resolve_integrations → intake → plan_evidence → gather_evidence → diagnose → deliver |
| `tools/investigation/stages/gather_evidence/tools.py:70` | `select_investigation_tools`：有 `planned_actions` 用它；否则 ≤32 不动；否则按告警源相关度分层截断 |
| `tools/investigation/stages/gather_evidence/agent.py:227, :436` | `_run_seed_calls`：**LLM 开口前先确定性地跑几个"必查"工具** |
| `tools/investigation/stages/diagnose/node.py:47, :25, :95` | `diagnose` → `parse_diagnosis` 取最后一条 assistant 文本 → 再调一次 LLM 结构化抽取 root_cause / causal_chain / remediation |
| `tools/investigation/reporting/node.py:34, :44` | `deliver` / `generate_report`：证据目录（`E1`… 引用）、终端渲染、投递 |

配套文档：`docs/investigation-pipeline-architecture.md`（有 mermaid 流程图）、`docs/investigation-tool-calling.md`。

---

## 2. 全链路一页纸（真实顺序）

```
main.py:47 main
└─ surfaces/entrypoint.py:39 main
   └─ surfaces/cli/app.py:105 _run_without_subcommand
      └─ surfaces/interactive_shell/main.py:96 run_repl → :29 run_repl_async
         └─ controller.py:226 start_interactive_shell
            └─ runtime/turn_host.py:315 run_input_loop        ← 读到 "MySQL为什么慢?"
               └─ input/actions.py:65 decide_input_action → SubmitTurn
                  └─ controller.py:304 queue.put
                     └─ turn_host.py:358 run_agent_turn_queue → :195 run_agent_turn
                        └─ shell_turn_execution.py:31 execute_shell_turn   (to_thread)
                           └─ infrastructure/turn_host/turn_runner.py:164 _run_turn
                              └─ session_agents.py:127 agent_for (缓存的 HeadlessAgent)
                                 └─ core/agent_harness/turns/headless_agent.py:105 handle
                                    └─ session_goal/run_until.py:199 run_until_session_goal
                                       └─ turns/orchestrator.py:80 run_turn
                                          └─ turns/action_driver.py:1086 _run_action_turn
                                             ├─ tools/action_tools.py:53 (工具 = 可用集成的 ACTION∪CHAT 工具)
                                             ├─ :694 _build_action_agent (else 分支：真 LLM)
                                             └─ core/agent/agent.py:82 Agent.run
                                                └─ core/agent/react_loop.py:203 for iteration…
                                                   ├─ :373 llm.invoke(messages, tools=schemas)
                                                   ├─ 有 tool_calls → :452 execute_tool_calls
                                                   │   └─ core/tool/execution.py:338 _execute_one_tool_call
                                                   │       ├─ [第 1 轮] get_mysql_slow_queries → mysql/__init__.py:450 pymysql cursor.execute
                                                   │       ├─ [第 1 轮] get_mysql_current_processes → mysql/__init__.py:321
                                                   │       └─ [第 2 轮] query_grafana_metrics("rate(mysql_global_status_slow_queries[5m])")
                                                   │            → grafana/client.py:137 首次发现数据源(type=prometheus)
                                                   │            → grafana/mimir.py:47 GET …/proxy/uid/<uid>/api/v1/query → Prometheus
                                                   │   └─ :459-463 结果追加进 messages → 下一轮
                                                   └─ [第 3 轮] 无 tool_calls → :394 _handle_conclusion → 最终文本
```

---

## 3. 关键点日志审计

问题："每个关键点是否都有日志打印？" 答案：**大多数没有 `logger` 日志，但有三套非 logger 的事实记录**。先分清四种"痕迹"：

> **2026-09-01 更新**：本节的审计结论已被后续改动推翻大半。`OPENSRE_LOG_LEVEL`
> 与 `/loglevel` 已经实现，§5 的 1–6 条也已全部补上。下表保留原始审计作为对照，
> 每条后面加了「现状」。当前实际做法见 §3.1。

| 痕迹 | 落在哪 | 默认可见？ |
| --- | --- | --- |
| A. stdlib `logging` | shell 根处理器 **只放 ERROR+**（`infrastructure/logging/shell_handler.py`），根 logger 级别从未设置（默认 WARNING）。~~没有任何环境变量能调级别。~~ **现状：`OPENSRE_LOG_LEVEL` 或 `/loglevel` 可调，见 §3.1；没设这个变量时根 logger 一律不动，宿主进程自己的日志配置不受影响** | ⚠️ 默认仍 ERROR，可调 |
| B. 运行时事件 `core/events.py` | `AgentStartEvent` / `ProviderRequestStart/EndEvent` / `ToolExecutionStart/EndEvent` / `TurnEndEvent` / `AgentEndEvent`，由 shell 的事件 sink 渲染成屏幕上的进度行 | ✅（UI） |
| C. trace span → `~/.opensre/sessions/<session_id>.jsonl` 里 `"type": "trace_span"` | `component:action_turn` / `loop:react_loop` / `loop_iteration` / `llm:<model>` / `tool:<name>`，带 `duration_ms`、`tool_call_count`、`content_chars`、`tool_call_id`。**仅交互 shell 注册了 JSONL 存储**（`surfaces/interactive_shell/runtime/context.py:145`），其它宿主是 Noop | ✅（文件） |
| D. `~/.opensre/operations_log.jsonl` | `agent_loop_started` / `agent_loop_iteration`（iteration、outcome、requested_tool_count、tool_error_count）/ `agent_loop_finished`（stop_reason、iterations_used、tool_result_count）+ `model/provider/tool_names` | ✅（文件） |

逐点审计（✅ 有 logger 日志 / 🟡 只有 B/C/D 痕迹 / ❌ 什么都没有）：

| # | 关键点 | 位置 | logger 日志 | 其它痕迹 | 结论 |
| --- | --- | --- | --- | --- | --- |
| 1 | 进程启动、shell 启动 | `main.py` → `interactive_shell/main.py` | 无 | — | ❌ 全程静默（`bootstrap/process.py:31` 定义了 `_LOG` 但零调用） |
| 2 | 读到用户输入、入队 | `turn_host.py:315`, `actions.py:65`, `controller.py:304` | 无 | — | ❌ |
| 3 | turn 开始 / 结束 | `turn_runner.py:231` | DEBUG `gateway_turn done intent=%s answered=%s outbound_chars=%s` | span `component:action_turn`；PostHog `gateway_turn_started/completed` | 🟡 结束有 DEBUG，开始没有 |
| 4 | action turn 拿到哪些工具 | `action_driver.py:1106` | DEBUG `action_turn start tools=%s integrations=%s` | — | 🟡 DEBUG 级，shell 看不到 |
| 5 | 进入 ReAct 循环 | `react_loop.py:189` | 无 | `AgentStartEvent`（tool_count, max_iterations）；ops-log `agent_loop_started`；span `loop:react_loop` | 🟡 |
| 6 | **LLM 请求发出** | `react_loop.py` `_think` | ~~无~~ DEBUG `llm request model=… iteration=… kind=… messages=… tools=…` | `ProviderRequestStartEvent`（message_count, tool_schema_count）；span `llm:<model>` | ✅ 只记形状不记内容（提示词不落日志） |
| 7 | **LLM 响应收到** | 同上 | ~~无~~ DEBUG `llm response model=… tools=[名字列表] content_chars=…` | `ProviderRequestEndEvent`（tool_call_count, content_chars）；span attrs `has_tool_calls` | ✅ 模型选了哪些工具可见；响应文本仍不落日志 |
| 8 | 工具被选中（名称+参数名） | `core/tool/execution.py` `_log_tool_call_start` | DEBUG `tool_call start name=… id=… source=… args=[…]` | `ToolExecutionStartEvent`；span `tool:<name>`；ops-log 只有计数 | ✅ 只记参数名，值不落（见 §3.1 末） |
| 9 | 工具参数 | `core/agent_harness/tools/tool_provider.py:163` | **INFO** `tool action name=%s input=%s`（截 500 字符） | — | 🟡 shell 路径已接线（logger `opensre.interactive_shell`），但 INFO 被 ERROR 门槛挡住 |
| 10 | 工具结果大小 / 成败 | `tool_provider.py:174`；`execution.py:407` | INFO `tool result name=%s ok=%s size=%d`；DEBUG `tool_call done … outcome=%s` | `ToolExecutionEndEvent` | 🟡 同上 |
| 11 | 工具执行异常 | `execution.py:416`；`contracts.py:177` | WARNING `[tool:%s] failed: %s`；`report_exception` → Sentry | — | ✅（但 WARNING 在 shell 仍不可见） |
| 12 | **Grafana/Mimir HTTP 请求** | `integrations/grafana/mimir.py` | ~~无~~ DEBUG `mimir query endpoint=… uid=… promql=… window=…→… step=…`；异常分支 WARNING `mimir query failed`（**不再静默吞掉**）；截断 INFO | — | ✅ 打在 `mimir.py` 而非 `base.py`：那层看不见这是 range 还是 instant、窗口多大、截断没有 |
| 13 | **MySQL 连接与 SQL** | `integrations/_relational.py` `read_only_query` | ~~无~~ DEBUG `mysql query start <fn> target=host:port/db` / `… done … in Nms`；失败 WARNING 带目标与耗时 | — | ✅ 打在装饰器上，MySQL/Postgres/MariaDB 一处覆盖；SQL 本身不记（静态可 grep） |
| 14 | MySQL 失败 | `_relational.py:151` → `boundary.py:59-74` | WARNING `[mysql] get_slow_queries validation failed`（**无 traceback**）+ Sentry `capture_exception` | — | ✅ 唯一有日志的 MySQL 点 |
| 15 | 工具结果回灌消息 | `react_loop.py:459-463` | 无 | 只反映在下一轮 `message_count` | ❌ |
| 16 | 循环迭代计数 | `react_loop.py:272` | 无 | ops-log `agent_loop_iteration`；span `loop_iteration` | 🟡 |
| 17 | 停止原因 / 撞上限 / 停滞 | `react_loop.py` `run` / `_observe` | ~~无~~ WARNING `react loop hit the iteration limit of N` / `react loop stopped: no new evidence for N iterations` | ops-log `agent_loop_finished.stop_reason`；`AgentEndEvent` | ✅ WARNING 级，默认 ERROR 门槛下仍需开 `/loglevel warning` |
| 18 | 上下文预算裁剪 | `core/context_budget.py:423/429/437` | WARNING ×3 | — | ✅ |
| 19 | 最终回答产出 | `react_loop.py:394`, `action_driver.py:1203` | DEBUG `action_turn done planned=%s executed=%s …` | `TurnEndEvent(accepted=True)` | 🟡 |
| 20 | investigation 六阶段 | `agent_pipeline.py:196` | **无 logger**，只 `capture_exception`；`intake/node.py:120` INFO 原始告警；`diagnose/node.py:43/76/184` WARNING；`reporting/node.py` 无 | span `stage:<name>` | 🟡 |

原始结论：**链路上"发生了什么"有痕迹（B/C/D），"具体内容是什么"（请求体、PromQL、SQL、参数）几乎没有痕迹。**

### 3.1 现状（2026-09-01 之后）

上表 #6、#7、#12、#13、#17 已补上日志，并且新增了调级别的入口。**先开级别，否则仍然什么都看不见**：

```bash
OPENSRE_LOG_LEVEL=DEBUG uv run opensre        # 进程启动即生效
# 或在 shell 里随时切换：
/loglevel debug
/loglevel                                      # 查看当前级别
```

**日志落在终端，不落文件**——除非你指定文件。全仓库没有任何默认的文件日志：
交互 shell 走 Rich console（stdout），无 console 时兜底 stderr，gateway 独立进程走 stderr。
`~/.opensre/sessions/*.jsonl` 和 `operations_log.jsonl` 是另外两套机制，**别把它们当成"只有计数"**：

- `operations_log.jsonl` 确实只有生命周期事件的计数和耗时。
- `sessions/*.jsonl` **存完整内容**——`append_message` 落 `role` + 整段 `content`，
  `append_tool_call` 落工具的整个 `arguments` **和** `result`
  （`core/agent_harness/session/persistence/memory.py:80, :100`）。
  也就是说本节的日志 sink 特意只记参数名、还设成 `0600` 的那些东西，
  会话文件里**早就有了，而且是 `0644`**。要收紧得单独处理，本节的改动管不到它。

要留档就指定一个文件。文件**持有自己的级别**，所以可以屏幕干净、文件全量：

```bash
OPENSRE_LOG_FILE=~/opensre-debug.log \
OPENSRE_LOG_FILE_LEVEL=DEBUG \
uv run opensre                                 # 终端仍是默认 ERROR，文件记全量

tail -f ~/opensre-debug.log
```

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `OPENSRE_LOG_FILE` | 无 | **不设就完全不写盘**，也不建目录 |
| `OPENSRE_LOG_FILE_LEVEL` | `DEBUG` | 独立于终端；`/loglevel` 和 `OPENSRE_LOG_LEVEL` 都不影响它 |

两个变量都可以写进仓库根目录的 `.env`（在 `BootStep.ENV`，即启动第一步被读入，早于装 handler）。
加载用 `override=False`，所以 **shell 里临时 `export` 的值优先于 `.env`**。
`.env` 里配了也不影响测试：pytest / CI 下这个 sink 一律不装，免得每次跑测试都往工作区写文件。

文件权限是 `0600`（DEBUG 下它装着工具入参和出站 URL），按 5 MB × 3 份滚动。
路径解析失败、目录建不了、文件打不开——都只降级成终端日志并报一条 **ERROR**（不是 WARNING：
终端 handler 默认就在 ERROR，WARNING 会被它自己丢掉），**不会让 shell 起不来**。
装好之后磁盘才满、或滚动重命名失败，sink 会自我关闭并只抱怨一次。

`/loglevel` 不带参数时会把**真正装上的**文件路径和级别报出来——不是读环境变量，
所以打开失败时它不会骗你说在写。

**只有交互 shell 装这个 sink**（`surfaces/interactive_shell/main.py` 是唯一调用方）。
不带子命令的 `uv run opensre` 有；`opensre ask` / `opensre investigate` 这些纯 CLI 子命令**没有**，
它们的日志只走终端。（`run_repl` 的 `initial_input` 参数没有任何生产调用方传值。）

补在哪、记了什么：

| 关键点 | 位置 | 日志 |
| --- | --- | --- |
| LLM 请求 | `core/agent/react_loop.py` `_think` | DEBUG `llm request model=… iteration=… kind=… messages=… tools=…` |
| LLM 响应 | 同上 | DEBUG `llm response model=… tools=[名字列表] content_chars=…` |
| 撞迭代上限 | `react_loop.py` `run` | WARNING `react loop hit the iteration limit of N` |
| 停滞退出 | `react_loop.py` `_observe` | WARNING `react loop stopped: no new evidence for N iterations` |
| 工具入参 | `core/tool/execution.py` `_log_tool_call_start` | DEBUG `tool_call start name=… id=… source=… args=[metric_name, start, end, step, timeout=30]` |
| PromQL 出站 | `integrations/grafana/mimir.py` | DEBUG `mimir query endpoint=query_range uid=… promql=… window=…→… step=…` |
| PromQL 失败 | 同上 | WARNING `mimir query failed promql=… error=…`（原来被静默吞掉） |
| 序列被截断 | 同上 | INFO `mimir query capped at 50 of 4894 series for …` |
| 数据源来源 | `integrations/grafana/client.py` | DEBUG `datasource mimir uid=… origin=configured\|discovered\|default` |
| 数据库查询 | `integrations/_relational.py` | DEBUG `mysql query start <fn> target=host:port/db` / `… done … in Nms`；失败 WARNING 带目标与耗时 |

关系型那条打在 `read_only_query` 装饰器上，**MySQL / Postgres / MariaDB 共用一处**。
SQL 语句本身没有记——它在源码里是静态的、可 grep；记不到的是目标、耗时和成败。

**工具入参只记参数名，不记值**（数字和布尔除外）。`redact_sensitive` 按 **key 名**脱敏，
看不见嵌在普通字段里的凭据——`{"command": "curl -H 'Authorization: Bearer …'"}` 的 key
是 `command`，会原样落盘，而这条日志在托管 gateway 上会进服务端日志。
真正要看的 PromQL 由 `mimir.py` 那条单独打出，不依赖这里。

一个真实样例：

```
DEBUG integrations.grafana.mimir: mimir query endpoint=query_range uid=wloGDWa4k
      promql=cdb_cpu_use_rate{instanceid=~"cdbro-ov6a6jkc"}
      window=2026-08-31T05:00:00Z→2026-08-31T05:30:00Z step=15s
WARNING integrations.grafana.mimir: mimir query failed promql=up error=401 Unauthorized
```

---

## 4. 在本机看到这些事实的方法

### 4.1 让 DEBUG/INFO 日志落到文件（推荐，不改仓库代码）

`install_shell_log_handler` 在根 logger **已经有处理器时会直接跳过**（`shell_handler.py:80-83`），
所以在进程入口前先配好根 logger 即可。把下面脚本放在仓库外任意位置：

```python
# opensre_debug.py
import logging, os, sys

logging.basicConfig(
    filename=os.environ.get("OPENSRE_DEBUG_LOG", "/tmp/opensre-debug.log"),
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
for noisy in ("httpx", "httpcore", "urllib3", "openai", "anthropic", "asyncio", "filelock"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from surfaces.entrypoint import main
sys.exit(main())
```

```bash
uv run python /path/to/opensre_debug.py            # 正常进 shell
tail -f /tmp/opensre-debug.log                     # 另开终端
```

之后你会看到审计表里 #3、#4、#8–#11、#14、#19 的记录，包括
`tool action name=get_mysql_slow_queries input={...}` 和 `tool result … size=…`。
（已用 `--help` 冒烟验证脚本能跑；真实一轮对话未实测，待你本机跑。）

### 4.2 打开 `TRACER_VERBOSE`（仓库自带的 print 级调试）

```bash
uv run opensre --verbose          # 等价于 TRACER_VERBOSE=1
# 或在 shell 里：/verbose on
```

只影响 `debug_print`（`infrastructure/observability/render/debug.py:21-36`）：investigation 路径的
`[tool] → summary` / `[seed:tool] → …`（`gather_evidence/agent.py:391, :498`）。**不改 logger 级别。**

### 4.3 读 session JSONL（对话 + span，最完整）

```bash
ls -t ~/.opensre/sessions/*.jsonl | head -1                     # 最近一个 session
S=$(ls -t ~/.opensre/sessions/*.jsonl | head -1)

# 每个 LLM 调用 / 工具调用的耗时与计数
jq -c 'select(.type=="trace_span") | {span_kind,name,status,duration_ms,attributes}' "$S"

# 只看工具 span
jq -c 'select(.type=="trace_span" and .span_kind=="tool")' "$S"

# 对话记录（含 assistant 的 tool_calls 和 tool 结果）
jq -c 'select(.type!="trace_span") | {type, kind, timestamp}' "$S" | head -40
```

### 4.4 读 operations_log.jsonl（循环级摘要）

```bash
jq -c 'select(.event|startswith("agent_loop")) | {ts, event, data:{iteration:.data.iteration, outcome:.data.outcome, stop_reason:.data.stop_reason, iterations_used:.data.iterations_used, tool_names:.data.tool_names}}' ~/.opensre/operations_log.jsonl | tail -20
```

环境变量：`OPENSRE_OPERATIONS_LOG_PATH`（改路径）、`OPENSRE_OPERATIONS_LOG_DISABLED=1`（关闭）、
`OPENSRE_OPERATIONS_LOG_MAX_BYTES`（默认 5 MiB 滚动）。`OPENSRE_HOME` 可整体改根目录。

### 4.5 非交互复现（脚本化 / 反复实验）

```bash
uv run opensre ask "MySQL为什么慢?"                 # 一轮 agent turn，读工具自动放行
uv run opensre --json ask "MySQL为什么慢?"           # 机器可读输出
```

注意 `ask` 路径没有注册 JSONL trace store，4.3 的 span 不会出现；4.1 和 4.4 仍然有效。

---

## 5. 如果要补日志，补在哪（~~建议，未实施~~ **已全部实施，见 §3.1**）

按"一处改动覆盖最多调用方"的原则，优先级从高到低：

1. **`core/agent/react_loop.py:373` 前后**（LLM 请求/响应）：DEBUG 记 `model`、`message_count`、`tool_schema_count`、响应 `tool_calls` 的名字列表和 `content_chars`。这是整条链路唯一的 LLM 咽喉，一处覆盖 chat 与 investigation 两条路径。
2. ~~**`core/tool/execution.py:386`**：把 `public_tool_input(tc)`（`:581`，已存在但没被日志用）加进 `tool_call start` 那行。~~ **这条实施后被评审否掉**：`redact_sensitive` 只按 key 名脱敏，落完整入参会把嵌在 `command` / 连接串里的凭据写进日志。改为只记**参数名**（数字和布尔保留值）。
3. **`react_loop.py:510-527, :536`**：撞迭代上限 / 停滞触发时打一条 WARNING，现在只有 ops-log 有 `stop_reason`。
4. **`integrations/grafana/mimir.py:47` 与 `:66-78`**：DEBUG 记 URL + PromQL；异常分支至少 `logger.warning` 一次（目前静默吞掉）。
5. **`integrations/mysql/__init__.py:148, :321, :450`**：DEBUG 记 host:port/database、SQL 模板和参数、返回行数（不要记密码；`redact_sensitive` 在 `infrastructure/observability/trace/redaction.py`）。
6. 给 shell 一个调级别的入口（例如 `OPENSRE_LOG_LEVEL` 或 `/loglevel`），否则 1–5 加了也只有 4.1 的方式能看见。

补日志前先读 `AGENTS.md` 的 "Information exposure through an exception" 条目：异常细节只能进本地日志和 Sentry，不能进 gateway 的外部输出。

---

## 6. 顺手发现的问题（未改动，仅记录）

- **文档过期**：`core/agent_harness/AGENTS.md:53,60` 与根 `AGENTS.md:315` 引用的 `turns/evidence_kind.py` 不存在；`evidence_kind:` handoff 只在 `tests/core/agent/test_turn_scenarios.py:65` 出现。
- **Goal reviewer 未被消费**：`action_driver.py:765 build_goal_reviewer` 和 `gather_evidence/agent.py:323 _build_goal` 构造的 `Goal` 存在 `Agent` 上（`agent.py:74`），但 `react_loop.py` 从不调用 `_should_accept_conclusion` / `should_accept_with_goal`（`core/agent/goals.py:48` 只有测试调用）。
- ~~**`summarize_prometheus_metrics` 与实际 Mimir 输出形状不一致**~~ **已修**：range 查询产出 `values`，形状对上了；证据引用现在也调它算 min/max。
- ~~**`service_name` 裸拼进 PromQL**~~ **已修**：只有裸指标名可用 `service_name`，表达式会返回明确错误而不是生成非法 PromQL。

### 6.1 §0 心智模型里已经过期的三条（2026-09-01）

读第 0 节和第 5 站时注意，下面几条**已不再成立**：

- ~~"只有 instant query，拿不到曲线"~~ —— `query_grafana_metrics` 现在接受 `start` / `end` / `step`，
  有时间窗就走 `/api/v1/query_range`。窗口默认取 `state.incident_window`，经 `_meta` 通道注入。
  半开窗（只给一端）会被明确拒绝，右端点按 `[since, until)` 契约排除。
- ~~"`extract_params` 带默认 `metric_name="pipeline_runs_total"`"~~ —— 已删除。
  `build_seed_calls` 现在会跳过"凑不齐必填参数"的 seed，所以指标工具不再被盲目 seed。
- ~~"Grafana 数据源发现挑中哪个就是哪个"~~ —— `GRAFANA_*_DATASOURCE_UID` 现在**优先于**自动发现
  （顺序：显式配置 > 发现 > Grafana Cloud 默认）。一个实例挂 14 个 prometheus 数据源时这条是必需的。
