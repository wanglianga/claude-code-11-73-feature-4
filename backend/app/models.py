"""数据模型。

架构选型：追加式双时间事件账 + 可重建投影。
- events 为唯一事实来源（append-only，DB 触发器禁止 UPDATE/DELETE）。
- identity_edges / rule_versions / rule_withdrawals / member_directory 均为可重建投影，
  由事件重放生成，可随时通过 /api/admin/rebuild 全量重建。
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Event(Base):
    """接入账：追加式双时间事件账。

    每行保存：来源哈希(source_hash)、链式哈希(chain_hash)、两类时间
    (biz_time 业务发生时间 / ingested_at 平台接收时间)、修订链(supersedes_id)、
    幂等键(idempotency_key)。
    """

    __tablename__ = "events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    idempotency_key = Column(String(128), unique=True, nullable=False)
    source = Column(String(32), nullable=False)  # pos | online | franchise_offline | identity_desk | rule_admin
    event_type = Column(String(32), nullable=False)
    source_hash = Column(String(64), nullable=False)  # sha256(canonical payload)
    chain_hash = Column(String(64), nullable=False)  # sha256(prev chain_hash + row) 防篡改链
    biz_time = Column(DateTime(timezone=True), nullable=False)
    ingested_at = Column(DateTime(timezone=True), nullable=False)
    actor = Column(String(64))
    supersedes_id = Column(BigInteger, ForeignKey("events.id"), nullable=True)
    payload = Column(JSONB, nullable=False)


class IdentityEdge(Base):
    """身份关系图投影：合并/拆分/待核/撤销的有效区间（双时间区间）。"""

    __tablename__ = "identity_edges"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    relation_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    member_a = Column(String(64), nullable=False)
    member_b = Column(String(64), nullable=False)
    relation = Column(String(16), nullable=False)  # merge | split | pending | revoke
    status = Column(String(16), nullable=False)  # active | split | revoked | pending
    biz_from = Column(DateTime(timezone=True), nullable=False)
    biz_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)
    sys_to = Column(DateTime(timezone=True), nullable=True)
    target_edge_id = Column(BigInteger, nullable=True)  # split/revoke 指向的 merge 边
    reason = Column(Text)


class RuleVersion(Base):
    """等级规则版本投影：按门店、商品范围与生效区间修订。"""

    __tablename__ = "rule_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    rule_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    rule_id = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)
    supersedes_version = Column(Integer, nullable=True)
    store_scope = Column(JSONB, nullable=False)  # "ALL" 或门店列表
    product_scope = Column(JSONB, nullable=False)  # "ALL" 或品类列表
    window_days = Column(Integer, nullable=False)
    thresholds = Column(JSONB, nullable=False)  # {"SILVER": 300, "GOLD": 1000}
    effective_from = Column(DateTime(timezone=True), nullable=False)
    effective_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)


class RuleWithdrawal(Base):
    """规则撤回投影：支持部分（按门店范围）追溯撤回。"""

    __tablename__ = "rule_withdrawals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    withdraw_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    rule_id = Column(String(64), nullable=False)
    target_version = Column(Integer, nullable=False)
    store_scope = Column(JSONB, nullable=False)
    withdraw_from = Column(DateTime(timezone=True), nullable=False)
    withdraw_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)
    reason = Column(Text)


class MemberDirectory(Base):
    """会员名录投影（仅索引已知会员键，不保存会覆盖历史的“当前主档”）。"""

    __tablename__ = "member_directory"

    member_key = Column(String(64), primary_key=True)
    display_name = Column(String(128))
    first_seen_event_id = Column(BigInteger)


class Semantics(Base):
    """时间语义目录：定义查询口径。"""

    __tablename__ = "semantics"

    code = Column(String(32), primary_key=True)
    name = Column(String(64), nullable=False)
    biz_policy = Column(String(16), nullable=False)  # query
    sys_policy = Column(String(16), nullable=False)  # query | now | both
    description = Column(Text)


class Publication(Base):
    """发布库：冻结证据水位、身份关系图版本与规则集合版本。"""

    __tablename__ = "publications"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    biz_as_of = Column(DateTime(timezone=True), nullable=False)
    sys_as_of = Column(DateTime(timezone=True), nullable=False)
    watermark_event_id = Column(BigInteger, nullable=False)  # 证据水位
    identity_graph_version = Column(BigInteger, nullable=False)  # 关系图冻结版本(最大身份事件id)
    rule_set_version = Column(BigInteger, nullable=False)  # 规则集合冻结版本(最大规则事件id)
    semantics_code = Column(String(32), nullable=False)
    # 发布门禁：正式等级发布若由已批准实验驱动，记录放行的实验版本与门禁检查
    experiment_version_id = Column(BigInteger, ForeignKey("rule_experiment_versions.id"), nullable=True)
    gate_id = Column(BigInteger, nullable=True)


class PublicationRow(Base):
    __tablename__ = "publication_rows"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    publication_id = Column(BigInteger, ForeignKey("publications.id"), nullable=False)
    member_key = Column(String(64), nullable=False)
    tier = Column(String(16), nullable=False)
    spend = Column(Numeric(14, 2), nullable=False)
    detail = Column(JSONB, nullable=False)


class Diff(Base):
    __tablename__ = "diffs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pub_a = Column(BigInteger, ForeignKey("publications.id"), nullable=False)
    pub_b = Column(BigInteger, ForeignKey("publications.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class DiffItem(Base):
    """差异归因：把资格变化连到具体事件。"""

    __tablename__ = "diff_items"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    diff_id = Column(BigInteger, ForeignKey("diffs.id"), nullable=False)
    member_key = Column(String(64), nullable=False)
    before = Column(JSONB, nullable=False)
    after = Column(JSONB, nullable=False)
    causes = Column(JSONB, nullable=False)  # [{event_id, cause, detail}]


class Impact(Base):
    """影响索引：变化对营销名单、门店预算、训练样本的下游影响。"""

    __tablename__ = "impacts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    diff_id = Column(BigInteger, ForeignKey("diffs.id"), nullable=False)
    dimension = Column(String(16), nullable=False)  # marketing | budget | training
    entity = Column(String(128), nullable=False)  # 下游实体键
    member_key = Column(String(64), nullable=False)
    change_type = Column(String(32), nullable=False)
    before = Column(JSONB)
    after = Column(JSONB)
    attributed_event_ids = Column(JSONB)


class User(Base):
    __tablename__ = "users"

    username = Column(String(64), primary_key=True)
    password = Column(String(128), nullable=False)
    role = Column(String(32), nullable=False)
    token = Column(String(128), nullable=False)


# ==================== 规则实验与预算归因（feature-4） ====================
class RuleExperiment(Base):
    """实验目录：阈值/计分公式/适用门店/候选生效期/审批状态的版本化容器。

    审批状态机：DRAFT -> SUBMITTED -> APPROVED / REJECTED。
    每次修改阈值或公式都生成新的 RuleExperimentVersion（参数指纹变化），
    已产生的影子结果在参数不匹配时由发布门禁判为 STALE（失效但保留，不删除）。
    """

    __tablename__ = "rule_experiments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(64), unique=True, nullable=False)
    name = Column(String(128), nullable=False)
    description = Column(Text)
    status = Column(String(16), nullable=False)  # DRAFT | SUBMITTED | APPROVED | REJECTED
    current_version_no = Column(Integer, nullable=False, default=1)
    approved_version_no = Column(Integer, nullable=True)  # 最近一次批准的版本（改参后落后于当前版本）
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class RuleExperimentVersion(Base):
    """实验版本：不可变参数快照 + 参数指纹（阈值/公式/门店/生效期/权益参数/抽样设计）。"""

    __tablename__ = "rule_experiment_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    experiment_id = Column(BigInteger, ForeignKey("rule_experiments.id"), nullable=False)
    version_no = Column(Integer, nullable=False)
    # 候选规则参数
    formula = Column(String(32), nullable=False)  # SPEND_ONLY | SPEND_ACTIVE_COMPOSITE | ACTIVE_STORE_GATED
    thresholds = Column(JSONB, nullable=False)  # 分等级阈值（按 formula 解释为消费额/综合分/双门槛）
    formula_params = Column(JSONB, nullable=False)  # 计分权重、活跃门店口径等
    store_scope = Column(JSONB, nullable=False)  # "ALL" 或门店列表
    product_scope = Column(JSONB, nullable=False, default="ALL")
    window_days = Column(Integer, nullable=False)
    candidate_effective_from = Column(DateTime(timezone=True), nullable=False)
    candidate_effective_to = Column(DateTime(timezone=True), nullable=True)
    benefit_config = Column(JSONB, nullable=False)  # 积分率/生日券/门店补贴参数
    # 抽样设计（写入实验版本）：FULL=全量成员集；STRATIFIED=分层抽样
    sampling_mode = Column(String(16), nullable=False)  # FULL | STRATIFIED
    sampling_design = Column(JSONB, nullable=False)  # 分层定义/每层样本量/目标误差/随机种子
    param_fingerprint = Column(String(64), nullable=False)  # sha256(规范化参数)
    baseline_rule_id = Column(String(64), nullable=False)  # 对照（现行）规则 rule_id
    created_at = Column(DateTime(timezone=True), nullable=False)
    created_by = Column(String(64), nullable=False)
    change_note = Column(Text)


class SampleSnapshot(Base):
    """样本快照器：冻结成员集合、交易可见性、身份关系与门店归属。

    水位变化（补传交易改变冻结水位）时生成新快照版本并保留先前预测结论：
    snapshot_series 相同的多版本构成同一实验样本的演进链。
    """

    __tablename__ = "sample_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_series = Column(String(64), nullable=False)  # 同一系列（实验维度），水位演进产生新版本
    version_no = Column(Integer, nullable=False)
    experiment_version_id = Column(BigInteger, ForeignKey("rule_experiment_versions.id"), nullable=False)
    biz_as_of = Column(DateTime(timezone=True), nullable=False)
    sys_as_of = Column(DateTime(timezone=True), nullable=False)
    watermark_event_id = Column(BigInteger, nullable=False)  # 冻结证据水位
    identity_graph_version = Column(BigInteger, nullable=False)
    rule_set_version = Column(BigInteger, nullable=False)
    sampling_mode = Column(String(16), nullable=False)  # FULL | STRATIFIED
    sampling_design = Column(JSONB, nullable=False)  # 实际落地的抽样设计（分层/样本量/种子）
    total_population = Column(Integer, nullable=False)  # 冻结成员全集大小
    sample_size = Column(Integer, nullable=False)  # 实际入样成员数（全量=全集）
    pending_members = Column(JSONB, nullable=False)  # 身份拆分审批中的未决成员清单与原因
    supersedes_snapshot_id = Column(BigInteger, nullable=True)  # 水位变化时的前序快照
    created_at = Column(DateTime(timezone=True), nullable=False)


class SampleSnapshotMember(Base):
    """快照成员行：冻结门店归属、分层、入样标记与未决标记（逐人可回查）。"""

    __tablename__ = "sample_snapshot_members"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id = Column(BigInteger, ForeignKey("sample_snapshots.id"), nullable=False)
    member_key = Column(String(64), nullable=False)
    home_store = Column(String(64), nullable=False)  # 冻结门店归属（主消费门店）
    stores = Column(JSONB, nullable=False)  # 活跃门店全集
    stratum = Column(String(128), nullable=False)  # 门店×消费层级×活跃度 分层键
    in_sample = Column(Boolean, nullable=False)
    is_pending = Column(Boolean, nullable=False, default=False)
    pending_reason = Column(Text)
    features = Column(JSONB, nullable=False)  # 冻结特征：消费额/活跃门店数/层级/活跃度


class ShadowRun(Base):
    """影子分群运行：在同一快照上并行现行与候选规则，生成等级迁移。"""

    __tablename__ = "shadow_runs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id = Column(BigInteger, ForeignKey("sample_snapshots.id"), nullable=False)
    experiment_version_id = Column(BigInteger, ForeignKey("rule_experiment_versions.id"), nullable=False)
    kind = Column(String(16), nullable=False)  # FULL | SAMPLED
    status = Column(String(16), nullable=False)  # DONE
    created_at = Column(DateTime(timezone=True), nullable=False)
    # 汇总结果（全量为精确值；抽样为点估计 + 置信区间）
    summary = Column(JSONB, nullable=False)


class ShadowMemberResult(Base):
    """逐人影子结果：现行 vs 候选等级、迁移方向、计分明细、权益成本。

    全量运行覆盖冻结全集；抽样运行仅覆盖入样成员，未入样成员不产生精确结论。
    """

    __tablename__ = "shadow_member_results"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    shadow_run_id = Column(BigInteger, ForeignKey("shadow_runs.id"), nullable=False)
    snapshot_id = Column(BigInteger, ForeignKey("sample_snapshots.id"), nullable=False)
    member_key = Column(String(64), nullable=False)
    home_store = Column(String(64), nullable=False)
    stratum = Column(String(128), nullable=False)
    current_tier = Column(String(16), nullable=False)
    candidate_tier = Column(String(16), nullable=False)
    migration = Column(String(16), nullable=False)  # UPGRADE | DOWNGRADE | RETAIN | PENDING
    current_detail = Column(JSONB, nullable=False)
    candidate_detail = Column(JSONB, nullable=False)
    cost_current = Column(JSONB, nullable=False)  # 四类权益成本（现行）
    cost_candidate = Column(JSONB, nullable=False)  # 四类权益成本（候选）
    cost_delta = Column(JSONB, nullable=False)  # 逐权益类型差额


class BudgetAttribution(Base):
    """预算归因：按升级降级 × 门店 × 权益类型 × 规则参数 拆分成本变化。

    同一门店被两套候选规则覆盖时分别输出影响行（rule_fingerprint 不同），绝不合并。
    抽样口径的估计值携带置信区间；不可判定时 estimate 为 null 且 decidable=false。
    """

    __tablename__ = "budget_attributions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    shadow_run_id = Column(BigInteger, ForeignKey("shadow_runs.id"), nullable=False)
    snapshot_id = Column(BigInteger, ForeignKey("sample_snapshots.id"), nullable=False)
    experiment_version_id = Column(BigInteger, ForeignKey("rule_experiment_versions.id"), nullable=False)
    rule_fingerprint = Column(String(64), nullable=False)  # 规则参数身份（同店多规则分列依据）
    migration = Column(String(16), nullable=False)  # UPGRADE | DOWNGRADE | RETAIN
    store = Column(String(64), nullable=False)
    benefit_type = Column(String(32), nullable=False)  # POINTS | BIRTHDAY | STORE_SUBSIDY | BASE
    estimate = Column(Numeric(14, 2), nullable=True)  # 抽样不可判定时为 NULL
    ci_low = Column(Numeric(14, 2), nullable=True)
    ci_high = Column(Numeric(14, 2), nullable=True)
    exact_value = Column(Numeric(14, 2), nullable=True)  # 全量精确值
    member_count = Column(Integer, nullable=False)
    decidable = Column(Boolean, nullable=False, default=True)
    note = Column(Text)


class PrecisionReport(Base):
    """精度评估器：预测误差、置信范围、未覆盖成员与不可判定门店。"""

    __tablename__ = "precision_reports"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    shadow_run_id = Column(BigInteger, ForeignKey("shadow_runs.id"), nullable=False)
    snapshot_id = Column(BigInteger, ForeignKey("sample_snapshots.id"), nullable=False)
    benchmark_run_id = Column(BigInteger, nullable=True)  # 同快照全量基准运行（抽样对比用）
    confidence_level = Column(Numeric(6, 4), nullable=False)
    covered_members = Column(Integer, nullable=False)
    uncovered_members = Column(Integer, nullable=False)  # 未覆盖成员数
    uncovered_detail = Column(JSONB, nullable=False)  # 未覆盖成员/分层清单
    metrics = Column(JSONB, nullable=False)  # 各指标点估计/CI/相对误差/是否达标
    undecidable_stores = Column(JSONB, nullable=False)  # 误差要求无法满足的门店
    overall_decidable = Column(Boolean, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class ReleaseGate(Base):
    """发布门禁检查记录：阻止未批准或参数不匹配的实验结果进入正式等级发布。"""

    __tablename__ = "release_gates"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    experiment_id = Column(BigInteger, ForeignKey("rule_experiments.id"), nullable=False)
    experiment_version_id = Column(BigInteger, ForeignKey("rule_experiment_versions.id"), nullable=False)
    shadow_run_id = Column(BigInteger, ForeignKey("shadow_runs.id"), nullable=False)
    passed = Column(Boolean, nullable=False)
    checks = Column(JSONB, nullable=False)  # 逐项检查 {code, passed, detail}
    reason_codes = Column(JSONB, nullable=False)  # 失败原因码
    created_at = Column(DateTime(timezone=True), nullable=False)
