"""影子分群器：在同一冻结快照上并行运行现行与候选规则并生成等级迁移。

两条路线（写入实验版本的抽样设计决定默认路线，也可在同快照上对照执行）：
- FULL：对冻结成员集合逐人执行候选规则，得到精确迁移与门店成本；
- SAMPLED：按 门店×消费层级×活跃度 分层抽样，用分层估计量外推总体并附置信区间。

红线：
- 身份拆分/合并审批中的成员只能作为未决样本（migration=PENDING），不按合并消费计算、不进估计框架；
- 全量与抽样在同一快照上的结果可直接对比（精度评估器挂接同快照全量基准）。
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import budget as budget_svc
from . import precision as precision_svc
from .eligibility import compute_eligibility, tier_rank
from .scoring import BENEFIT_TYPES, MIGRATIONS, benefit_cost, cost_delta, is_boundary_score, score_member
from .snapshots import create_snapshot, snapshot_members


def _migration_of(current: str, candidate: str) -> str:
    if tier_rank(candidate) > tier_rank(current):
        return "UPGRADE"
    if tier_rank(candidate) < tier_rank(current):
        return "DOWNGRADE"
    return "RETAIN"


def _boundary_counts(snap_members, ver, margin_ratio: float) -> tuple[dict, dict]:
    """返回每门店 (全框架边界成员数, 入样边界成员数)。在冻结特征上轻量算分，无需现行资格。"""
    frame_b: dict[str, int] = {}
    sampled_b: dict[str, int] = {}
    for sm in snap_members:
        if sm.is_pending:
            continue
        cand = _scoped_features(sm.features, ver.store_scope)
        scored = score_member(cand, ver.formula, ver.formula_params, ver.thresholds)
        if is_boundary_score(scored["score"], ver.thresholds, margin_ratio):
            frame_b[sm.home_store] = frame_b.get(sm.home_store, 0) + 1
            if sm.in_sample:
                sampled_b[sm.home_store] = sampled_b.get(sm.home_store, 0) + 1
    return frame_b, sampled_b


def _scoped_features(features: dict, store_scope) -> dict:
    """候选规则适用门店范围外的交易不计入候选计分（现行规则口径保持全门店）。"""
    if store_scope == "ALL":
        return features
    allowed = set(store_scope if isinstance(store_scope, list) else [])
    spend_by_store = {s: v for s, v in features["spend_by_store"].items() if s in allowed}
    return {
        **features,
        "spend_by_store": spend_by_store,
        "total_spend": round(sum(spend_by_store.values()), 2),
        "active_store_count": len(spend_by_store),
        "stores": sorted(spend_by_store),
    }


def run_shadow(
    db: Session,
    experiment_id: int,
    *,
    biz_as_of,
    sys_as_of,
    mode: str = "AUTO",
    version_id: int | None = None,
    confidence_level: float = 0.95,
) -> models.ShadowRun:
    exp = db.get(models.RuleExperiment, experiment_id)
    if exp is None:
        raise ValueError("实验不存在")
    ver = (
        db.get(models.RuleExperimentVersion, version_id)
        if version_id
        else _latest_version(db, experiment_id)
    )
    if ver is None or ver.experiment_id != experiment_id:
        raise ValueError("实验版本不存在")

    snapshot, _created = create_snapshot(db, ver, biz_as_of=biz_as_of, sys_as_of=sys_as_of)
    snap_members = snapshot_members(db, snapshot.id)
    kind = ver.sampling_mode if mode == "AUTO" else mode  # FULL | SAMPLED
    if kind not in ("FULL", "SAMPLED"):
        raise ValueError("影子运行模式仅支持 FULL / SAMPLED / AUTO")

    # 层总量 N_h：估计框架只含非未决成员
    frame_size: dict[str, int] = {}
    for sm in snap_members:
        if not sm.is_pending:
            frame_size[sm.stratum] = frame_size.get(sm.stratum, 0) + 1

    run = models.ShadowRun(
        snapshot_id=snapshot.id,
        experiment_version_id=ver.id,
        kind=kind,
        status="DONE",
        created_at=datetime.now(timezone.utc),
        summary={},
    )
    db.add(run)
    db.flush()

    cells: list[dict] = []          # 进入估计框架的逐人单元
    pending_cells: list[dict] = []  # 未决单元
    for sm in snap_members:
        if sm.is_pending:
            cell = _persist_pending(db, run, snapshot, sm)
            pending_cells.append(cell)
            continue
        if kind == "SAMPLED" and not sm.in_sample:
            continue  # 抽样路线：未入样成员不产生精确结论
        cell = _evaluate_member(db, run, snapshot, ver, sm)
        cells.append(cell)

    frame_cells = [
        {"home_store": sm.home_store, "stratum": sm.stratum}
        for sm in snap_members if not sm.is_pending
    ]

    # 边界阈值成员：在全框架（含未入样）上用冻结特征轻量算分统计，避免抽样漏判
    margin_ratio = float((ver.sampling_design or {}).get("boundary_margin_ratio", 0.05))
    frame_boundary, sampled_boundary = _boundary_counts(snap_members, ver, margin_ratio)

    undecidable_stores, store_stats = precision_svc.evaluate_store_precision(
        kind, cells, frame_cells, frame_size, ver.sampling_design, confidence_level,
        frame_boundary, sampled_boundary,
    )

    # ---- 预算归因（升级降级 × 门店 × 权益类型 × 规则参数指纹）----
    attr_rows = budget_svc.plan_attributions(
        mode=kind,
        frame_cells=frame_cells,
        sampled_cells=cells,
        frame_size_by_stratum=frame_size,
        confidence_level=confidence_level,
        undecidable_stores=undecidable_stores,
        rule_fingerprint=ver.param_fingerprint,
    )
    for r in attr_rows:
        db.add(
            models.BudgetAttribution(
                shadow_run_id=run.id,
                snapshot_id=snapshot.id,
                experiment_version_id=ver.id,
                rule_fingerprint=r["rule_fingerprint"],
                migration=r["migration"],
                store=r["store"],
                benefit_type=r["benefit_type"],
                estimate=r["estimate"],
                ci_low=r["ci_low"],
                ci_high=r["ci_high"],
                exact_value=r["exact_value"],
                member_count=r["member_count"],
                decidable=r["decidable"],
                note=r["note"],
            )
        )

    summary = _aggregate(kind, cells, frame_cells, frame_size, attr_rows, confidence_level, undecidable_stores)
    report = precision_svc.build_report(
        db, run, snapshot, kind, cells, frame_cells, frame_size,
        pending_cells, undecidable_stores, store_stats, ver.sampling_design, confidence_level
    )
    summary["precision_report_id"] = report.id
    summary["overall_decidable"] = report.overall_decidable
    run.summary = summary
    db.commit()
    db.refresh(run)
    return run


def _evaluate_member(
    db: Session,
    run: models.ShadowRun,
    snapshot: models.SampleSnapshot,
    ver: models.RuleExperimentVersion,
    sm: models.SampleSnapshotMember,
) -> dict:
    features = sm.features
    # 现行规则：与正式发布同一双时间口径的资格引擎
    current = compute_eligibility(db, sm.member_key, snapshot.biz_as_of, snapshot.sys_as_of)
    current_tier = current["tier"]
    # 候选规则：在冻结特征上按适用门店裁剪后计分
    cand_features = _scoped_features(features, ver.store_scope)
    scored = score_member(cand_features, ver.formula, ver.formula_params, ver.thresholds)
    candidate_tier = scored["tier"]
    migration = _migration_of(current_tier, candidate_tier)

    cost_cur = benefit_cost(current_tier, features, ver.benefit_config)
    cost_cand = benefit_cost(candidate_tier, features, ver.benefit_config)
    delta = cost_delta(cost_cur, cost_cand)

    db.add(
        models.ShadowMemberResult(
            shadow_run_id=run.id,
            snapshot_id=snapshot.id,
            member_key=sm.member_key,
            home_store=sm.home_store,
            stratum=sm.stratum,
            current_tier=current_tier,
            candidate_tier=candidate_tier,
            migration=migration,
            current_detail={"tier": current_tier, "eligibility": current},
            candidate_detail=scored,
            cost_current=cost_cur,
            cost_candidate=cost_cand,
            cost_delta=delta,
        )
    )
    return {
        "member_key": sm.member_key,
        "home_store": sm.home_store,
        "stratum": sm.stratum,
        "current_tier": current_tier,
        "candidate_tier": candidate_tier,
        "migration": migration,
        "cost_current": cost_cur,
        "cost_candidate": cost_cand,
        "cost_delta": delta,
    }


def _persist_pending(
    db: Session, run: models.ShadowRun, snapshot: models.SampleSnapshot, sm: models.SampleSnapshotMember
) -> dict:
    db.add(
        models.ShadowMemberResult(
            shadow_run_id=run.id,
            snapshot_id=snapshot.id,
            member_key=sm.member_key,
            home_store=sm.home_store,
            stratum=sm.stratum,
            current_tier="PENDING",
            candidate_tier="PENDING",
            migration="PENDING",
            current_detail={"pending_reason": sm.pending_reason},
            candidate_detail={"pending_reason": sm.pending_reason},
            cost_current={b: 0.0 for b in (*BENEFIT_TYPES, "TOTAL")},
            cost_candidate={b: 0.0 for b in (*BENEFIT_TYPES, "TOTAL")},
            cost_delta={b: 0.0 for b in (*BENEFIT_TYPES, "TOTAL")},
        )
    )
    return {
        "member_key": sm.member_key, "home_store": sm.home_store, "stratum": sm.stratum,
        "migration": "PENDING", "pending_reason": sm.pending_reason,
    }


def _aggregate(kind, cells, frame_cells, frame_size, attr_rows, confidence_level, undecidable_stores) -> dict:
    from . import stats

    tiers = ["NONE", "SILVER", "GOLD", "PLATINUM"]
    enriched = [{**c, "frame_size": frame_size.get(c["stratum"], 1)} for c in cells]

    def tier_pop(which_tier: str, tier_key: str):
        if kind == "FULL":
            n = sum(1 for c in cells if c[which_tier] == tier_key)
            return {"estimate": n, "ci_low": n, "ci_high": n, "se": 0.0, "half_width": 0.0}
        return stats.stratified_total(
            enriched, lambda c: 1.0 if c[which_tier] == tier_key else 0.0, confidence_level
        )

    tier_summary = {
        "current": {t: tier_pop("current_tier", t) for t in tiers},
        "candidate": {t: tier_pop("candidate_tier", t) for t in tiers},
    }
    migration_summary = {}
    for mg in MIGRATIONS:
        if kind == "FULL":
            n = sum(1 for c in cells if c["migration"] == mg)
            migration_summary[mg] = {"estimate": n, "ci_low": n, "ci_high": n, "se": 0.0, "half_width": 0.0}
        else:
            migration_summary[mg] = stats.stratified_total(
                enriched, lambda c, m=mg: 1.0 if c["migration"] == m else 0.0, confidence_level
            )

    cost_summary = {}
    decidable_attr = [r for r in attr_rows if r["decidable"]]
    for b in BENEFIT_TYPES:
        rows = [r for r in decidable_attr if r["benefit_type"] == b]
        if kind == "FULL":
            total = round(sum(float(r["exact_value"] or 0) for r in rows), 2)
            cost_summary[b] = {"estimate": total, "ci_low": total, "ci_high": total, "se": 0.0, "half_width": 0.0}
        else:
            est = round(sum(float(r["estimate"] or 0) for r in rows), 2)
            lo = round(sum(float(r["ci_low"] or 0) for r in rows), 2)
            hi = round(sum(float(r["ci_high"] or 0) for r in rows), 2)
            cost_summary[b] = {"estimate": est, "ci_low": lo, "ci_high": hi,
                               "se": None, "half_width": round((hi - lo) / 2, 2)}

    return {
        "kind": kind,
        "evaluated_members": len(cells),
        "frame_population": sum(frame_size.values()),
        "tier_population": tier_summary,
        "migration": migration_summary,
        "cost_delta_by_benefit": cost_summary,
        "undecidable_stores": sorted(undecidable_stores),
        "confidence_level": confidence_level,
    }


def _latest_version(db: Session, experiment_id: int) -> models.RuleExperimentVersion:
    return db.execute(
        select(models.RuleExperimentVersion)
        .where(models.RuleExperimentVersion.experiment_id == experiment_id)
        .order_by(models.RuleExperimentVersion.version_no.desc())
    ).scalars().first()
