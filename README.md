# 双时间会员资格历史服务（连锁药房）

面向会员分析团队：在**任意历史时点**解释会员为何获得/失去等级资格，并为营销、门店预算、模型训练分别提供「当时可见」「事后补全」及「二者差异」三种口径的数据集。

技术栈：Vue 3 + FastAPI + PostgreSQL。

## 架构选型

题目要求在两种可复演历史方案中二选一，本实现选择 **追加式双时间事件账 + 可重建投影**：

- `events` 接入账是唯一事实来源：追加-only（DB 触发器禁止 UPDATE/DELETE），每行保存
  **来源哈希**（payload 的 sha256）、**链式哈希**（前序行哈希链，防篡改）、**两类时间**
  （`biz_time` 业务发生时间 / `ingested_at` 平台接收时间）、**修订链**（`supersedes_id`）、
  **幂等键**（唯一约束，重放不重复入账）。
- 身份关系图、规则版本、会员名录均为**可重建投影**：由事件重放生成，
  `POST /api/admin/rebuild` 可随时全量重建并验证结论一致。
- 取舍：更正因果直接可审计（每次变化都是一条带双时间戳的事件），代价是长窗口查询需按
  双时间过滤重放——本服务以「按 (业务时点, 系统时点) 过滤后即时重放」实现任意时点资格计算，
  并通过发布库把常用口径物化冻结，避免重复回放。

## 核心组件

| 组件 | 实现 |
| --- | --- |
| 接入账 | `events` 表：追加-only、来源哈希、链式哈希、双时间、修订链、幂等键 |
| 身份关系图 | `identity_edges` 投影：合并/拆分/待核/撤销的双时间有效区间；误合并拆分支持追溯（`retroactive`） |
| 时间语义目录 | `semantics` 表：`AS_OF`（当时可见）/ `AS_KNOWN_NOW`（事后补全）/ `DELTA`（二者差异） |
| 资格计算器 | 按业务时点、系统时点、规则版本、身份簇即时重放计算等级，输出逐范围解释 |
| 发布库 | `publications`：冻结证据水位（最大事件 id）、身份图版本、规则集版本，并物化各会员资格 |
| 差异归因器 | 比较两个发布，把每个会员的变化归因到水位之间的具体事件（迟到交易/更正/身份变更/规则修订/撤回） |
| 影响索引器 | 把等级变化映射到营销名单、门店预算、训练样本，可按下游实体反查 |

规则解析语义（确定性）：对门店 s、业务时点 T_b、系统时点 T_s，取「入账 ≤ T_s、生效区间覆盖
T_b、门店在范围内、未被覆盖 (版本, s, T_b) 的撤回命中」的版本中 (生效起点, 版本号) 最大者。
并发修订（如 v2、v3 相隔 5 分钟先后修订 v1）由该全序确定性解决，修订链完整保留在账本中。

## 规则实验与预算归因（feature-4）

在不改变冻结基线（成员/交易/身份关系历史版本可固定）的前提下，新增**规则实验与预算归因数据服务**，
用于在等级规则由「消费额优先」改为「消费额与活跃门店数并重」正式生效前，估计对下季度等级人数、
积分成本、生日券与门店补贴的影响，并在参数、样本或数据水位变化时解释预测为何改变。

| 组件 | 实现 |
| --- | --- |
| 实验目录 | `rule_experiments` / `rule_experiment_versions`：阈值、计分公式、适用门店、候选生效期、权益参数、抽样设计按版本固化；参数指纹 `param_fingerprint`；审批状态机 DRAFT→SUBMITTED→APPROVED/REJECTED |
| 计分引擎 | `services/scoring.py`：`SPEND_ONLY`（消费额优先）、`SPEND_ACTIVE_COMPOSITE`（消费×w₁+活跃门店数×w₂+笔数×w₃）、`ACTIVE_STORE_GATED`（消费额与活跃门店双门槛）三种确定性公式 + 四类权益成本（积分/生日券/门店补贴/基础） |
| 样本快照器 | `sample_snapshots` / `sample_snapshot_members`：冻结证据水位、身份图版本、规则集版本、成员集、门店归属与 门店×消费层级×活跃度 分层；补传改变水位时追加快照版本并 `supersedes_snapshot_id` 链接前序，旧结论保留 |
| 影子分群器 | `shadow_runs` / `shadow_member_results`：同一快照并行现行规则（正式资格引擎）与候选规则，输出逐人等级迁移；FULL 全量精确、SAMPLED 分层抽样估计 |
| 预算归因器 | `budget_attributions`：按 升级降级×门店×权益类型×规则参数指纹 四维拆分；同店多套候选规则按指纹分列不合并；不可判定门店 estimate/exact 均为 NULL |
| 精度评估器 | `precision_reports`：分层总量估计、置信区间（正态近似，可配 0.90/0.95/0.99）、相对误差、未覆盖成员、边界阈值成员、不可判定门店；可挂接同快照全量基准做误差对比 |
| 发布门禁 | `release_gates`：六项原因码（未批准/参数失效/快照过时/未决成员/不可判定/基准偏差），未通过即阻止正式等级发布；通过后物化 `publications` 并回写实验版本与门禁 |

**两条估计路线（抽样设计或全量成员集写入实验版本）**：全量逐人执行得到精确迁移与门店成本，
消耗更多计算与快照存储；分层抽样按门店/消费层级/活跃度外推总体并附置信区间，迭代更快，
但对稀有门店与边界阈值成员更不稳定——精度评估器据此要求改用全量或直接报告「不可判定」，
绝不输出精确补贴额。

**强制红线（均有端到端断言）**：

- 身份拆分/合并审批中的成员 → 未决样本（`migration=PENDING`），不按合并消费计算、不进估计框架；
- 两套候选规则覆盖同一门店 → 预算归因按规则指纹分别输出，不合并为单一成本；
- 审批中修改阈值或公式 → 生成新版本、实验回到 DRAFT，发布门禁以 `GATE_STALE_PARAMS` 使旧结果失效；
- 补传交易改变冻结水位 → 样本快照器生成新快照版本并保留先前预测结论，旧运行门禁判 `GATE_SNAPSHOT_STALE`；
- 抽样覆盖达不到某门店误差要求 → 精度评估器标记不可判定，预算归因器不输出精确补贴额；
- 任一预算数字可经 `/api/budget/trace` 回查实验配置、样本/成员集、规则版本指纹与精度说明。

## 快速启动

### 方式一：Docker Compose（一键）

```bash
docker compose up --build
```

- 前端：http://localhost:8080
- 后端 API：http://localhost:8000 （Swagger UI: http://localhost:8000/docs）
- 数据库：localhost:15432（ccdev/ccdev，库 member_bitemp）

compose 启动时 `AUTO_SEED=1` 会自动灌入演示场景（仅当账本为空）。

### 方式二：本地开发

前置：容器内 PostgreSQL（127.0.0.1:5432，ccdev/ccdev）已运行。

```bash
# 后端（首次会自动创建 member_bitemp 库与表结构）
cd backend
/opt/python-3.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# 前端（另开终端）
cd frontend
/opt/node-22/bin/npm install
/opt/node-22/bin/npm run dev        # http://localhost:5173，已配置 /api 代理

# 灌入演示数据（或在前端「总览」页点击"灌入演示场景"）
curl -X POST http://127.0.0.1:8000/api/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}'
curl -X POST http://127.0.0.1:8000/api/seed -H 'Authorization: Bearer token-admin-2026'
# 规则实验演示样本：额外 10 名会员（含稀有门店 S003）+ 1 笔改变冻结水位的补传交易
curl -X POST http://127.0.0.1:8000/api/seed/experiments -H 'Authorization: Bearer token-admin-2026'
```

## 测试账号

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| admin | admin123 | 管理员 |
| analyst | analyst123 | 分析师 |

## 演示场景（种子数据）

同一会员 A（13800000001）贯穿四类历史变更，另有多名对照会员：

1. **加盟店离线补传**：交易 T4 业务时间 2026-01-28，2026-03-15 才到达平台；
2. **误合并后拆分**：2026-02-15 人工裁定把 B 并入 A，2026-04-05 追溯拆分并撤销裁定；
3. **部分追溯规则撤回**：规则 v2（2026-02-01 起追溯生效，GOLD 阈值 1000→1500）于
   2026-04-10 仅对门店 S002 撤回；
4. **并发修订**：v2、v3 相隔 5 分钟先后修订 v1（另有交易 T3 的更正链 300→380）。

关键结论（验证脚本逐项断言）：

| 查询 | 当时可见 (AS_OF) | 事后补全 (AS_KNOWN_NOW) |
| --- | --- | --- |
| A @ 2026-01-31 | SILVER（¥900） | **GOLD**（¥1250，补传改判） |
| A @ 2026-02-28 | GOLD（¥1800，簇 {A,B}） | **SILVER**（v2 范围 ¥1280 + S002 撤回后 v1 范围 ¥350） |
| A @ 2026-03-31 | GOLD（¥2230） | GOLD（¥1630，构成不同） |
| D @ 2026-02-28 | GOLD | GOLD（**被 S002 撤回保护**，否则落入 v2 的 1500 线） |
| F @ 2026-02-28 | GOLD | **SILVER**（被 v2 追溯阈值击中） |

## 验证方式

### 一键端到端验证

```bash
# 后端运行中执行（两脚本均会重置业务数据并重新灌入种子，可独立重复运行）：
python3 scripts/verify.py                # 基线：双时间资格/差异归因/影响（39 项断言）
python3 scripts/verify_experiments.py    # 规则实验与预算归因（50 项断言）
python3 scripts/verify.py http://localhost:8000
```

`verify.py` 断言：任意时点资格、更正链前后口径、DELTA 语义、发布冻结水位、差异归因
（迟到交易/更正/身份拆分/规则修订/规则撤回）、下游影响、链式哈希完整性、幂等重放、
追加-only 触发器、投影重建一致性、并发修订确定性解决。

`verify_experiments.py` 断言（本批次交付）：

1. 全量与抽样在**同一快照**的结果对比（普查式抽样估计=全量精确值，并挂接全量基准 CI 对比）；
2. 身份待核成员列为未决样本、不按合并消费计算、不进估计框架；
3. 三种计分公式确定性定级（消费优先 / 并重综合分 / 活跃门店双门槛）；
4. 预算归因按 迁移×门店×权益×规则指纹 四维拆分，逐人成本差额可回算，四类权益之和=TOTAL；
5. 同店两套候选规则按指纹分别输出、不合并；
6. 稀有门店 S003 抽样覆盖不达标 → 不可判定且不输出精确补贴额；
7. 审批中改阈值 → 旧结果门禁 `GATE_STALE_PARAMS` 失效、实验回到 DRAFT；
8. 补传交易 → 新快照版本（水位推进、链接前序）且旧预测保留、旧运行判快照过时；
9. 发布门禁拦截未批准/未决成员，并在全部条件满足时放行、物化正式发布；
10. 任一预算数字经 `/api/budget/trace` 回查实验配置、样本成员集、规则版本指纹与精度说明。

**本仓库交付前已实际执行：基线 `39 通过, 0 失败`；规则实验 `50 通过, 0 失败`。**

### 手工验证示例

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# 任意双时点资格：A 在 2026-02-28 的两种口径
curl -s "http://127.0.0.1:8000/api/members/13800000001/eligibility?biz_time=2026-02-28T23:59:00Z&semantics=DELTA&sys_time=2026-02-28T23:59:00Z" \
  -H "Authorization: Bearer $TOKEN"

# 账本完整性
curl -s http://127.0.0.1:8000/api/admin/ledger/verify -H "Authorization: Bearer $TOKEN"

# 追加-only：直接改账应被拒绝
psql postgresql://ccdev:ccdev@127.0.0.1:5432/member_bitemp -c "UPDATE events SET actor='x' WHERE id=1"
# => ERROR: events 账为追加式：禁止 UPDATE，请以新事件修订
```

前端页面同样可完成全部验证：总览（语义目录/重建投影）、接入账（写入与查看哈希链）、
资格查询（三种口径 + 时间线）、身份关系图（任意时点簇解析）、规则中心、发布与差异
（冻结发布 → 两两比较 → 查看归因与下游影响）、**规则实验**（实验目录与审批 → 影子分群
→ 迁移/预算四维归因 → 精度 CI 与全量基准对比 → 发布门禁与门控发布 → 预算数字回查）。

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/login | 登录取令牌 |
| POST | /api/events | 写入事件（幂等键去重） |
| GET | /api/events | 账本流水查询 |
| GET | /api/semantics | 时间语义目录 |
| GET | /api/members | 会员名录 |
| GET | /api/members/{key}/eligibility | 任意双时点资格（semantics=AS_OF/AS_KNOWN_NOW/DELTA） |
| GET | /api/members/{key}/timeline | 等级时间线 |
| GET | /api/members/{key}/cluster | 任意时点身份簇 |
| GET | /api/identity/edges | 身份关系边（双时间区间） |
| GET | /api/rules/versions · /api/rules/withdrawals | 规则版本与撤回 |
| POST/GET | /api/publications | 冻结发布（证据水位/身份图/规则集版本） |
| POST/GET | /api/diffs | 差异归因 |
| GET | /api/impacts | 下游影响（可按 dimension/entity 反查） |
| POST | /api/seed/experiments | 灌入规则实验演示样本（额外会员/稀有门店/补传交易） |
| POST/GET | /api/experiments · /api/experiments/{id} | 实验目录与版本（参数指纹/审批状态） |
| POST | /api/experiments/{id}/versions · /transition | 修订参数生成新版本 · 审批状态流转 |
| GET | /api/experiment-versions/{id}/snapshots · /api/snapshots/{id} | 样本快照版本与冻结成员/未决/分层 |
| POST | /api/experiments/{id}/shadow · GET /api/shadow/runs/{id} | 影子分群（FULL/SAMPLED）与迁移/预算/精度详情 |
| POST | /api/shadow/runs/{id}/benchmark | 挂接同快照全量基准对比 |
| GET | /api/shadow/runs/{id}/budget · /api/budget/attributions | 预算归因（可按门店/权益/迁移过滤，跨运行看同店多规则分列） |
| GET | /api/budget/trace | 预算数字全链路回查（实验配置/样本/规则版本/精度/逐人） |
| POST | /api/shadow/runs/{id}/gate · /release · GET /api/gates | 发布门禁检查、门控正式发布 |
| POST | /api/admin/rebuild | 从事件账重建全部投影 |
| GET | /api/admin/ledger/verify | 链式哈希校验 |
| POST | /api/admin/reset | 清空业务数据（演示用） |

完整交互式文档见 `/docs`（Swagger UI，FastAPI 自动生成）。

## 设计红线

- **不得以当前余额或当前主档覆盖历史**：系统不保存"当前等级/当前余额"字段；任何时点的
  资格都由事件账按双时间口径重放得出。误合并拆分、规则撤回等更正只追加新事件并闭合
  旧区间，历史行永不修改、永不删除。
