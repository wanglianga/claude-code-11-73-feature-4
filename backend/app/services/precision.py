"""精度评估器：预测误差、置信范围、未覆盖成员与不可判定门店。

规则（对应题目红线）：
- 全量路线：无抽样误差，相对误差 0，所有门店可判定；
- 抽样路线：逐门店检查 入样数下限、相对误差（半宽/点估计）与边界阈值成员覆盖；
  任一项不满足 -> 该门店标记不可判定，预算归因器不得输出精确补贴额；
- 未覆盖成员（未入样的框架成员）与未决成员分别披露；
- 同一快照存在全量基准运行时，记录点估计与精确值的偏差/相对误差，供"全量 vs 抽样"验收。
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import stats
from .scoring import BENEFIT_TYPES, MIGRATIONS

DEFAULT_MAX_RSE = 0.25
DEFAULT_MIN_STORE_SAMPLE = 2
FLOOR = 1.0


def _rse(stat: dict) -> float:
    denom = abs(stat["estimate"]) if abs(stat["estimate"]) >= FLOOR else FLOOR
    return round(abs(stat["half_width"]) / denom, 4)


def evaluate_store_precision(
    kind: str,
    cells: list[dict],
    frame_cells: list[dict],
    frame_size: dict[str, int],
    sampling_design: dict,
    confidence_level: float,
    frame_boundary: dict[str, int],
    sampled_boundary: dict[str, int],
) -> tuple[set[str], list[dict]]:
    design = sampling_design or {}
    max_rse = float(design.get("max_relative_error", DEFAULT_MAX_RSE))
    min_sample = int(design.get("min_store_sample", DEFAULT_MIN_STORE_SAMPLE))
    stores = sorted({c["home_store"] for c in frame_cells})
    enriched = [{**c, "frame_size": frame_size.get(c["stratum"], 1)} for c in cells]

    undecidable: set[str] = set()
    out: list[dict] = []
    for store in stores:
        frame_n = sum(1 for c in frame_cells if c["home_store"] == store)
        sampled_n = sum(1 for c in cells if c["home_store"] == store)
        boundary_total = frame_boundary.get(store, 0)
        boundary_sampled = sampled_boundary.get(store, 0)

        if kind == "FULL":
            out.append({
                "store": store, "frame_members": frame_n, "sampled_members": frame_n,
                "rse": 0.0, "decidable": True, "reasons": [],
                "boundary_members": boundary_total, "boundary_sampled": boundary_total,
            })
            continue

        reasons = []
        stat = stats.stratified_total(
            enriched,
            lambda c, st=store: float(c["cost_delta"]["TOTAL"]) if c["home_store"] == st else 0.0,
            confidence_level,
        )
        rse = _rse(stat)
        if sampled_n < min_sample:
            reasons.append(f"入样成员 {sampled_n} < 最低要求 {min_sample}")
        if frame_n > 0 and sampled_n == 0:
            reasons.append("稀有门店：该门店无任何入样成员")
        if rse > max_rse:
            reasons.append(f"相对误差 {rse} 超过容忍阈值 {max_rse}")
        if boundary_total > 0 and boundary_sampled == 0:
            reasons.append("边界阈值成员未被抽样覆盖，迁移结论不稳定")
        decidable = not reasons
        if not decidable:
            undecidable.add(store)
        out.append({
            "store": store, "frame_members": frame_n, "sampled_members": sampled_n,
            "estimate": stat["estimate"], "ci_low": stat["ci_low"], "ci_high": stat["ci_high"],
            "rse": rse, "max_rse": max_rse, "decidable": decidable, "reasons": reasons,
            "boundary_members": boundary_total, "boundary_sampled": boundary_sampled,
        })
    return undecidable, out


def build_report(
    db: Session,
    run: models.ShadowRun,
    snapshot: models.SampleSnapshot,
    kind: str,
    cells: list[dict],
    frame_cells: list[dict],
    frame_size: dict[str, int],
    pending_cells: list[dict],
    undecidable_stores: set[str],
    store_stats: list[dict],
    sampling_design: dict,
    confidence_level: float,
) -> models.PrecisionReport:
    design = sampling_design or {}
    max_rse = float(design.get("max_relative_error", DEFAULT_MAX_RSE))
    enriched = [{**c, "frame_size": frame_size.get(c["stratum"], 1)} for c in cells]
    frame_pop = sum(frame_size.values())

    metrics: dict = {"kind": kind, "tier_population": {}, "migration": {}, "cost_delta": {}}
    for tier in ("NONE", "SILVER", "GOLD", "PLATINUM"):
        if kind == "FULL":
            n_cur = sum(1 for c in cells if c["current_tier"] == tier)
            n_cand = sum(1 for c in cells if c["candidate_tier"] == tier)
            metrics["tier_population"][tier] = {
                "current": n_cur, "candidate": n_cand,
                "candidate_ci": [n_cand, n_cand], "rse": 0.0, "pass": True,
            }
        else:
            st = stats.stratified_total(
                enriched, lambda c, t=tier: 1.0 if c["candidate_tier"] == t else 0.0, confidence_level
            )
            rse = _rse(st)
            metrics["tier_population"][tier] = {
                "candidate_estimate": st["estimate"], "ci": [st["ci_low"], st["ci_high"]],
                "rse": rse, "pass": rse <= max_rse,
            }

    for mg in MIGRATIONS:
        if mg == "PENDING":
            metrics["migration"][mg] = {"count": len(pending_cells), "in_frame": False}
            continue
        if kind == "FULL":
            n = sum(1 for c in cells if c["migration"] == mg)
            metrics["migration"][mg] = {"estimate": n, "ci": [n, n], "rse": 0.0, "pass": True}
        else:
            st = stats.stratified_total(
                enriched, lambda c, m=mg: 1.0 if c["migration"] == m else 0.0, confidence_level
            )
            rse = _rse(st)
            metrics["migration"][mg] = {
                "estimate": st["estimate"], "ci": [st["ci_low"], st["ci_high"]],
                "rse": rse, "pass": rse <= max_rse,
            }

    for b in BENEFIT_TYPES + ("TOTAL",):
        if kind == "FULL":
            total = round(sum(float(c["cost_delta"].get(b, 0.0)) for c in cells), 2)
            metrics["cost_delta"][b] = {
                "estimate": total, "ci": [total, total], "rse": 0.0, "pass": True,
            }
        else:
            st = stats.stratified_total(
                enriched, lambda c, k=b: float(c["cost_delta"].get(k, 0.0)), confidence_level
            )
            rse = _rse(st)
            metrics["cost_delta"][b] = {
                "estimate": st["estimate"], "ci": [st["ci_low"], st["ci_high"]],
                "se": st["se"], "rse": rse, "pass": rse <= max_rse,
            }

    # 未覆盖成员：分层 N_h / n_h
    strata_detail: dict[str, dict] = {}
    for c in frame_cells:
        strata_detail.setdefault(c["stratum"], {"N_h": 0, "n_h": 0})
        strata_detail[c["stratum"]]["N_h"] += 1
    for c in cells:
        strata_detail.setdefault(c["stratum"], {"N_h": 0, "n_h": 0})
        strata_detail[c["stratum"]]["n_h"] += 1
    uncovered = frame_pop - len(cells)
    uncovered_detail = [
        {"stratum": h, "N_h": v["N_h"], "n_h": v["n_h"], "uncovered": v["N_h"] - v["n_h"]}
        for h, v in sorted(strata_detail.items()) if v["N_h"] - v["n_h"] > 0
    ]

    report = models.PrecisionReport(
        shadow_run_id=run.id,
        snapshot_id=snapshot.id,
        benchmark_run_id=None,
        confidence_level=confidence_level,
        covered_members=len(cells),
        uncovered_members=max(uncovered, 0),
        uncovered_detail=uncovered_detail,
        metrics=metrics,
        undecidable_stores=store_stats,
        overall_decidable=not undecidable_stores,
        created_at=datetime.now(timezone.utc),
    )
    db.add(report)
    db.flush()
    return report


def attach_full_benchmark(db: Session, sampled_run: models.ShadowRun) -> models.PrecisionReport | None:
    """同快照全量基准对比：把抽样点估计与全量精确值的偏差写回精度报告。"""
    full_run = db.execute(
        select(models.ShadowRun).where(
            models.ShadowRun.snapshot_id == sampled_run.snapshot_id,
            models.ShadowRun.kind == "FULL",
            models.ShadowRun.id != sampled_run.id,
        ).order_by(models.ShadowRun.id)
    ).scalars().first()
    if full_run is None:
        return None
    report = db.execute(
        select(models.PrecisionReport).where(
            models.PrecisionReport.shadow_run_id == sampled_run.id
        )
    ).scalar_one_or_none()
    if report is None:
        return None

    full_members = db.execute(
        select(models.ShadowMemberResult).where(models.ShadowMemberResult.shadow_run_id == full_run.id)
    ).scalars().all()
    full_tier = {}
    for tier in ("NONE", "SILVER", "GOLD", "PLATINUM"):
        full_tier[tier] = sum(1 for m in full_members if m.candidate_tier == tier and m.migration != "PENDING")
    full_cost = {}
    for b in BENEFIT_TYPES + ("TOTAL",):
        full_cost[b] = round(
            sum(float(m.cost_delta.get(b, 0.0)) for m in full_members if m.migration != "PENDING"), 2
        )

    bench = {"benchmark_run_id": full_run.id, "tier_population": {}, "cost_delta": {}}
    # 必须先复制：直接改 report.metrics 会污染 SQLAlchemy 已加载基线，导致变更不被跟踪
    metrics = dict(report.metrics)
    for tier, exact in full_tier.items():
        est = metrics["tier_population"].get(tier, {}).get("candidate_estimate", exact)
        bench["tier_population"][tier] = {
            "exact": exact, "estimate": est,
            "abs_error": round(est - exact, 2),
            "rel_error": round(abs(est - exact) / exact, 4) if exact else 0.0,
            "inside_ci": metrics["tier_population"].get(tier, {}).get("ci", [exact, exact])[0]
            <= exact <= metrics["tier_population"].get(tier, {}).get("ci", [exact, exact])[1],
        }
    for b, exact in full_cost.items():
        st = metrics["cost_delta"].get(b, {})
        est = st.get("estimate", exact)
        ci = st.get("ci", [exact, exact])
        bench["cost_delta"][b] = {
            "exact": exact, "estimate": est,
            "abs_error": round(est - exact, 2),
            "rel_error": round(abs(est - exact) / abs(exact), 4) if abs(exact) >= FLOOR else 0.0,
            "inside_ci": ci[0] <= exact <= ci[1],
        }
    metrics["benchmark_vs_full"] = bench
    # JSONB 需赋新对象才能被 SQLAlchemy 标记为脏（原地修改不触发变更跟踪）
    report.metrics = dict(metrics)
    report.benchmark_run_id = full_run.id
    db.commit()
    db.refresh(report)
    return report
