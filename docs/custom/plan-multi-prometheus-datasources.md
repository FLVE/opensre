# 方案：支持多个 Prometheus 数据源

状态：**待实施**（前置的方案 A 已落地）
提出日期：2026-09-01

## 背景

OpenSRE 的 Grafana 客户端对每种数据源类型只保存**一个** UID：

```python
# integrations/grafana/config.py
loki_datasource_uid: str = ""
tempo_datasource_uid: str = ""
mimir_datasource_uid: str = ""
```

`query_grafana_metrics` 只能打这一个 Mimir UID。当 Grafana 实例挂着多个
Prometheus 数据源、且指标**按数据源分片**时，这个模型就不够用了。

### 触发本方案的实测证据

某生产 Grafana 实例：91 个数据源，其中 **14 个是 prometheus 类型**。
两个关键数据源的指标覆盖**完全不重叠**：

| 数据源 | `cdb_cpu_use_rate` | `kube_pod_info` | `node_cpu_seconds_total` |
| --- | --- | --- | --- |
| 基础监控 `wloGDWa4k` | 333 | 0 | 0 |
| 现网环境 `f304245b…`（`isDefault`） | 0 | 11690 | 59264 |

自动发现的第一优先级是 `isDefault`（`integrations/grafana/base.py`），
因此数据库指标永远查不到——这是一次真实故障调查失败的直接原因之一。

## 已落地的前置方案 A

配置优先于自动发现：

```
GRAFANA_*_DATASOURCE_UID（显式设置） > 自动发现 > Grafana Cloud 默认值
```

**A 解决了什么**：运维可以把 Mimir 钉到正确的数据源。

**A 没解决什么**：钉死之后，另一个数据源上的指标就查不到了。上表中钉
`wloGDWa4k` 可以查数据库，但 K8s / 节点指标全部失效。**一次调查无法同时
覆盖两类故障。**

## 目标

一次调查能够查询多个 Prometheus 数据源，且不要求调用方预先知道指标在哪个
数据源上。

## 非目标

- 跨数据源联邦查询（PromQL 层面的 join）。
- Loki / Tempo 的多数据源支持——等指标侧模型验证后再推广。
- 自动推断指标与数据源的归属关系。

## 待定的设计问题

按重要性排序，实施前需要逐条定论。

### 1. 配置形状

单值 UID 需要扩展成有序集合。候选：

- 逗号分隔的 UID 列表：`GRAFANA_MIMIR_DATASOURCE_UIDS=uid-a,uid-b`
- 保留自动发现的**全部** prometheus 数据源，配置只用于排序或过滤

保留全部数据源不需要运维列举 UID，但会把候选集扩大到 14 个，放大下面的
扇出问题。

### 2. 选择策略

给定一个查询和 N 个候选数据源：

| 策略 | 代价 |
| --- | --- |
| 依次尝试直到非空 | N 次串行往返；"空结果"与"数据源不含该指标"无法区分 |
| 并发全查后合并 | N 倍负载；同名指标在多个数据源上会产生歧义序列 |
| 用 `/api/v1/label/__name__/values` 建立 指标→数据源 索引 | 一次性建索引，之后 O(1)；但索引会过期，且实测在大数据源上 10 秒超时 |
| 让模型在工具入参里指定 `datasource_uid` | 零猜测；但模型需要知道有哪些 UID 及其含义 |

**倾向**：模型指定 + 提供一个列出数据源（uid、名称、指标名前缀样本）的
发现型工具。理由是它符合本仓库既有的原则——意图归模型，宿主不做启发式
路由（见 `AGENTS.md` 的 Footguns 一节关于禁止确定性意图路由的条款）。

### 3. 工具契约

若采用模型指定：

- `QueryGrafanaMetricsInput` 增加 `datasource_uid: str | None`
- 新增发现型工具（例如 `list_grafana_datasources`），可作为确定性 seed 调用——
  它无必填参数，符合 `build_seed_calls` 的可 seed 条件
- 未指定 `datasource_uid` 时的行为需明确：用默认 UID，还是报错要求先发现？

### 4. 缓存与失效

`_grafana_client_cache` 按账号缓存客户端。多数据源会引入按 UID 的状态；
需要确定缓存键的形状，以及数据源在 Grafana 侧变更后如何失效。

## 影响面

| 文件 | 变更 |
| --- | --- |
| `config/grafana_cloud.py` | 多 UID 配置读取 |
| `config/constants/grafana.py` | 新的环境变量名 |
| `integrations/grafana/config.py` | `GrafanaAccountConfig` 数据结构 |
| `integrations/grafana/base.py` | 发现逻辑返回全部候选而非单个 |
| `integrations/grafana/client.py` | UID 解析与缓存 |
| `integrations/grafana/mimir.py` | 按 UID 查询 |
| `integrations/grafana/tools/grafana_metrics_tool/` | 工具 schema |
| `docs/grafana*.mdx` | 用户文档 |

跨 `config` / `integrations` 两层并改动公共工具 schema，属于 **L 规模**：
需要先出设计文档收敛，再进入实施。

## 验收标准

1. 一次调查能在一个数据源上取到 `cdb_cpu_use_rate`、在另一个上取到
   `kube_pod_info`，无需重新配置。
2. 只配置了单个 UID 的既有安装行为不变。
3. 每次调查对 Grafana 的请求数有上界，且不随数据源总数线性增长。
4. 返回的证据能标明数据来自哪个数据源——否则跨数据源的相关性分析无法追溯。
