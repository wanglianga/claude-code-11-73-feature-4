"""规则实验与预算归因 API 路由。

覆盖：实验目录（版本/审批）、样本快照（成员/未决/分层）、影子分群（全量/抽样）、
预算归因（四维拆分/同店多规则/不可判定）、精度评估（CI/全量基准）、发布门禁（检查/门控发布）、
预算数字全链路回查。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .db import get_db
from .deps import current_user
from .services import experiments as exp_svc
from .services import gate as gate_svc
from .services import precision as precision_svc
from .services import shadow as shadow_svc
from .services import snapshots as snap_svc
from .services.experiments import version_json

router = APIRouter(prefix="/api", tags=["规则实验"])


def parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------- 实验目录 ----------------
class ExperimentIn(BaseModel):
    code: str
    name: str
    description: str | None = ""
    formula: str
    thresholds: dict
    formula_params: dict = {}
    store_scope: object = "ALL"
    product_scope: object = "ALL"
    window_days: int = 90
    candidate_effective_from: str
    candidate_effective_to: str | None = None
    benefit_config: dict | None = None
    sampling_mode: str = "FULL"
    sampling_design: dict = {}
    baseline_rule_id: str = "tier-standard"


class ReviseIn(BaseModel):
    formula: str | None = None
    thresholds: dict | None = None
    formula_params: dict | None = None
    store_scope: object | None = None
    product_scope: object | None = None
    window_days: int | None = None
    candidate_effective_from: str | None = None
    candidate_effective_to: str | None = None
    benefit_config: dict | None = None
    sampling_mode: str | None = None
    sampling_design: dict | None = None
    baseline_rule_id: str | None = None
    change_note: str | None = None


class TransitionIn(BaseModel):
    target: str  # SUBMITTED | APPROVED | REJECTED | DRAFT


def exp_json(e: models.RuleExperiment) -> dict:
    return {
        "id": e.id, "code": e.code, "name": e.name, "description": e.description,
        "status": e.status, "current_version_no": e.current_version_no,
        "approved_version_no": e.approved_version_no,
        "created_at": e.created_at.isoformat(), "updated_at": e.updated_at.isoformat(),
    }


@router.post("/experiments", status_code=201)
def create_experiment(body: ExperimentIn, db: Session = Depends(get_db), user=Depends(current_user)):
    try:
        exp, ver = exp_svc.create_experiment(
            db,
            code=body.code, name=body.name, description=body.description or "",
            formula=body.formula, thresholds=body.thresholds, formula_params=body.formula_params,
            store_scope=body.store_scope, product_scope=body.product_scope,
            window_days=body.window_days,
            candidate_effective_from=parse_dt(body.candidate_effective_from),
            candidate_effective_to=parse_dt(body.candidate_effective_to) if body.candidate_effective_to else None,
            benefit_config=body.benefit_config,
            sampling_mode=body.sampling_mode, sampling_design=body.sampling_design,
            baseline_rule_id=body.baseline_rule_id,
            created_by=user.username,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return exp_json(exp) | {"version": version_json(ver)}


@router.get("/experiments")
def list_experiments(db: Session = Depends(get_db), user=Depends(current_user)):
    rows = db.execute(select(models.RuleExperiment).order_by(models.RuleExperiment.id)).scalars().all()
    out = []
    for e in rows:
        latest = exp_svc.latest_version(db, e.id)
        out.append(exp_json(e) | {"latest_sampling_mode": latest.sampling_mode, "latest_formula": latest.formula})
    return out


@router.get("/experiments/{exp_id}")
def get_experiment(exp_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    exp = db.get(models.RuleExperiment, exp_id)
    if not exp:
        raise HTTPException(404, "实验不存在")
    versions = exp_svc.list_versions(db, exp_id)
    return exp_json(exp) | {"versions": [version_json(v) for v in versions]}


@router.post("/experiments/{exp_id}/versions", status_code=201)
def revise_experiment(exp_id: int, body: ReviseIn, db: Session = Depends(get_db), user=Depends(current_user)):
    fields = body.model_dump(exclude_none=True)
    note = fields.pop("change_note", None)
    if "candidate_effective_from" in fields:
        fields["candidate_effective_from"] = parse_dt(fields["candidate_effective_from"])
    if "candidate_effective_to" in fields:
        fields["candidate_effective_to"] = parse_dt(fields["candidate_effective_to"])
    try:
        ver = exp_svc.revise_experiment(db, exp_id, actor=user.username, change_note=note, **fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return version_json(ver)


@router.post("/experiments/{exp_id}/transition")
def transition_experiment(exp_id: int, body: TransitionIn, db: Session = Depends(get_db), user=Depends(current_user)):
    try:
        exp = exp_svc.transition_status(db, exp_id, body.target, actor=user.username)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return exp_json(exp)


@router.get("/experiment-versions/{ver_id}")
def get_version(ver_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    ver = db.get(models.RuleExperimentVersion, ver_id)
    if not ver:
        raise HTTPException(404, "实验版本不存在")
    return version_json(ver)


# ---------------- 样本快照 ----------------
@router.get("/experiment-versions/{ver_id}/snapshots")
def list_version_snapshots(ver_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    series = f"snap-{ver_id}"
    rows = db.execute(
        select(models.SampleSnapshot)
        .where(models.SampleSnapshot.snapshot_series == series)
        .order_by(models.SampleSnapshot.version_no)
    ).scalars().all()
    return [snap_svc.snapshot_json(s) for s in rows]


def member_row_json(m: models.SampleSnapshotMember) -> dict:
    return {
        "member_key": m.member_key, "home_store": m.home_store, "stores": m.stores,
        "stratum": m.stratum, "in_sample": m.in_sample, "is_pending": m.is_pending,
        "pending_reason": m.pending_reason,
        "features": {k: m.features.get(k) for k in
                     ("total_spend", "active_store_count", "txn_count", "cluster", "birthday_eligible")},
    }


@router.get("/snapshots/{snap_id}")
def get_snapshot(snap_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    s = db.get(models.SampleSnapshot, snap_id)
    if not s:
        raise HTTPException(404, "快照不存在")
    members = snap_svc.snapshot_members(db, snap_id)
    return snap_svc.snapshot_json(s) | {"members": [member_row_json(m) for m in members]}


# ---------------- 影子分群 ----------------
class ShadowIn(BaseModel):
    biz_as_of: str
    sys_as_of: str
    mode: str = "AUTO"  # AUTO | FULL | SAMPLED
    version_id: int | None = None
    confidence_level: float = 0.95


@router.post("/experiments/{exp_id}/shadow", status_code=201)
def run_shadow(exp_id: int, body: ShadowIn, db: Session = Depends(get_db), user=Depends(current_user)):
    try:
        run = shadow_svc.run_shadow(
            db, exp_id,
            biz_as_of=parse_dt(body.biz_as_of), sys_as_of=parse_dt(body.sys_as_of),
            mode=body.mode, version_id=body.version_id, confidence_level=body.confidence_level,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return shadow_run_json(db, run.id)


@router.get("/shadow/runs")
def list_runs(experiment_id: int | None = None, db: Session = Depends(get_db), user=Depends(current_user)):
    q = select(models.ShadowRun).order_by(models.ShadowRun.id)
    rows = db.execute(q).scalars().all()
    out = []
    for r in rows:
        if experiment_id:
            ver = db.get(models.RuleExperimentVersion, r.experiment_version_id)
            if not ver or ver.experiment_id != experiment_id:
                continue
        out.append(_run_brief(r))
    return out


def _run_brief(r: models.ShadowRun) -> dict:
    return {
        "id": r.id, "snapshot_id": r.snapshot_id, "experiment_version_id": r.experiment_version_id,
        "kind": r.kind, "status": r.status, "created_at": r.created_at.isoformat(),
        "summary": r.summary,
    }


def member_result_json(m: models.ShadowMemberResult) -> dict:
    return {
        "member_key": m.member_key, "home_store": m.home_store, "stratum": m.stratum,
        "current_tier": m.current_tier, "candidate_tier": m.candidate_tier, "migration": m.migration,
        "candidate_score": (m.candidate_detail or {}).get("score"),
        "current_detail": m.current_detail, "candidate_detail": m.candidate_detail,
        "cost_current": m.cost_current, "cost_candidate": m.cost_candidate, "cost_delta": m.cost_delta,
    }


def attribution_json(a: models.BudgetAttribution) -> dict:
    return {
        "id": a.id, "shadow_run_id": a.shadow_run_id, "snapshot_id": a.snapshot_id,
        "experiment_version_id": a.experiment_version_id, "rule_fingerprint": a.rule_fingerprint,
        "migration": a.migration, "store": a.store, "benefit_type": a.benefit_type,
        "estimate": float(a.estimate) if a.estimate is not None else None,
        "ci_low": float(a.ci_low) if a.ci_low is not None else None,
        "ci_high": float(a.ci_high) if a.ci_high is not None else None,
        "exact_value": float(a.exact_value) if a.exact_value is not None else None,
        "member_count": a.member_count, "decidable": a.decidable, "note": a.note,
    }


def shadow_run_json(db: Session, run_id: int) -> dict:
    r = db.get(models.ShadowRun, run_id)
    if not r:
        raise HTTPException(404, "影子运行不存在")
    members = db.execute(
        select(models.ShadowMemberResult).where(models.ShadowMemberResult.shadow_run_id == run_id)
        .order_by(models.ShadowMemberResult.member_key)
    ).scalars().all()
    attrs = db.execute(
        select(models.BudgetAttribution).where(models.BudgetAttribution.shadow_run_id == run_id)
        .order_by(models.BudgetAttribution.store, models.BudgetAttribution.migration, models.BudgetAttribution.benefit_type)
    ).scalars().all()
    report = db.execute(
        select(models.PrecisionReport).where(models.PrecisionReport.shadow_run_id == run_id)
    ).scalar_one_or_none()
    snapshot = db.get(models.SampleSnapshot, r.snapshot_id)
    ver = db.get(models.RuleExperimentVersion, r.experiment_version_id)
    return _run_brief(r) | {
        "snapshot": snap_svc.snapshot_json(snapshot),
        "experiment_version": version_json(ver),
        "members": [member_result_json(m) for m in members],
        "attributions": [attribution_json(a) for a in attrs],
        "precision": precision_json(report) if report else None,
    }


def precision_json(p: models.PrecisionReport) -> dict:
    return {
        "id": p.id, "shadow_run_id": p.shadow_run_id, "snapshot_id": p.snapshot_id,
        "benchmark_run_id": p.benchmark_run_id, "confidence_level": float(p.confidence_level),
        "covered_members": p.covered_members, "uncovered_members": p.uncovered_members,
        "uncovered_detail": p.uncovered_detail, "metrics": p.metrics,
        "undecidable_stores": p.undecidable_stores, "overall_decidable": p.overall_decidable,
        "created_at": p.created_at.isoformat(),
    }


@router.get("/shadow/runs/{run_id}")
def get_shadow_run(run_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    return shadow_run_json(db, run_id)


@router.post("/shadow/runs/{run_id}/benchmark")
def attach_benchmark(run_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    run = db.get(models.ShadowRun, run_id)
    if not run:
        raise HTTPException(404, "影子运行不存在")
    report = precision_svc.attach_full_benchmark(db, run)
    if report is None:
        raise HTTPException(400, "同快照不存在全量基准运行，请先在该快照上执行 FULL 影子运行")
    return precision_json(report)


# ---------------- 预算归因与回查 ----------------
@router.get("/budget/attributions")
def list_all_attributions(store: str | None = None, benefit_type: str | None = None,
                          experiment_id: int | None = None,
                          db: Session = Depends(get_db), user=Depends(current_user)):
    """跨运行预算归因：同门店被多套候选规则覆盖时按 rule_fingerprint 分列，绝不合并。"""
    q = select(models.BudgetAttribution).order_by(models.BudgetAttribution.id)
    rows = db.execute(q).scalars().all()
    out = []
    for a in rows:
        if store and a.store != store:
            continue
        if benefit_type and a.benefit_type != benefit_type:
            continue
        if experiment_id and a.experiment_version_id:
            ver = db.get(models.RuleExperimentVersion, a.experiment_version_id)
            if not ver or ver.experiment_id != experiment_id:
                continue
        out.append(attribution_json(a))
    return out


@router.get("/shadow/runs/{run_id}/budget")
def get_budget(run_id: int, store: str | None = None, benefit_type: str | None = None,
               migration: str | None = None, db: Session = Depends(get_db), user=Depends(current_user)):
    q = select(models.BudgetAttribution).where(models.BudgetAttribution.shadow_run_id == run_id)
    rows = db.execute(q.order_by(models.BudgetAttribution.id)).scalars().all()
    out = [attribution_json(a) for a in rows]
    if store:
        out = [a for a in out if a["store"] == store]
    if benefit_type:
        out = [a for a in out if a["benefit_type"] == benefit_type]
    if migration:
        out = [a for a in out if a["migration"] == migration]
    return out


@router.get("/budget/trace")
def trace_budget(run_id: int, store: str, benefit_type: str, migration: str | None = None,
                 db: Session = Depends(get_db), user=Depends(current_user)):
    """从任一预算数字回查：实验配置、样本/成员集、规则版本指纹、精度说明与逐人成本。"""
    q = select(models.BudgetAttribution).where(
        models.BudgetAttribution.shadow_run_id == run_id,
        models.BudgetAttribution.store == store,
        models.BudgetAttribution.benefit_type == benefit_type,
    )
    if migration:
        q = q.where(models.BudgetAttribution.migration == migration)
    attrs = db.execute(q).scalars().all()
    if not attrs:
        raise HTTPException(404, "未找到匹配的预算归因行")
    run = db.get(models.ShadowRun, run_id)
    ver = db.get(models.RuleExperimentVersion, run.experiment_version_id)
    exp = db.get(models.RuleExperiment, ver.experiment_id)
    snapshot = db.get(models.SampleSnapshot, run.snapshot_id)
    snap_members = snap_svc.snapshot_members(db, snapshot.id)
    member_results = db.execute(
        select(models.ShadowMemberResult).where(models.ShadowMemberResult.shadow_run_id == run_id)
    ).scalars().all()
    report = db.execute(
        select(models.PrecisionReport).where(models.PrecisionReport.shadow_run_id == run_id)
    ).scalar_one_or_none()
    touched_stores = {a.store for a in attrs}
    lineage_members = []
    for m in member_results:
        if m.home_store in touched_stores and (not migration or m.migration == migration):
            lineage_members.append(member_result_json(m))
    return {
        "budget_rows": [attribution_json(a) for a in attrs],
        "experiment": exp_json(exp),
        "rule_version": version_json(ver),
        "sample_snapshot": snap_svc.snapshot_json(snapshot),
        "member_set": {
            "total_population": snapshot.total_population,
            "sample_size": snapshot.sample_size,
            "sampling_mode": snapshot.sampling_mode,
            "sampling_design": snapshot.sampling_design,
            "pending_members": snapshot.pending_members,
            "frozen_members": [member_row_json(m) for m in snap_members],
        },
        "precision": precision_json(report) if report else None,
        "lineage_members": lineage_members,
    }


# ---------------- 发布门禁 ----------------
@router.post("/shadow/runs/{run_id}/gate", status_code=201)
def run_gate(run_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    try:
        gate = gate_svc.check_gate(db, run_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return gate_svc.gate_json(gate)


@router.get("/gates")
def list_gates(db: Session = Depends(get_db), user=Depends(current_user)):
    rows = db.execute(select(models.ReleaseGate).order_by(models.ReleaseGate.id.desc())).scalars().all()
    return [gate_svc.gate_json(g) for g in rows]


@router.get("/gates/{gate_id}")
def get_gate(gate_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    g = db.get(models.ReleaseGate, gate_id)
    if not g:
        raise HTTPException(404, "门禁记录不存在")
    return gate_svc.gate_json(g)


class ReleaseIn(BaseModel):
    publication_name: str


@router.post("/shadow/runs/{run_id}/release", status_code=201)
def gated_release(run_id: int, body: ReleaseIn, db: Session = Depends(get_db), user=Depends(current_user)):
    try:
        gate, pub = gate_svc.gated_release(db, run_id, body.publication_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not gate.passed:
        raise HTTPException(422, {
            "error": "发布门禁未通过，已阻止正式等级发布",
            "gate": gate_svc.gate_json(gate),
        })
    return {
        "gate": gate_svc.gate_json(gate),
        "publication_id": pub.id,
        "publication_name": pub.name,
        "semantics_code": pub.semantics_code,
    }
