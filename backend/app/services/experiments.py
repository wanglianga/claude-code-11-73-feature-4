"""实验目录：阈值/计分公式/适用门店/候选生效期/审批状态的版本化管理。

设计红线：
- 实验参数不可变：修改阈值或公式 -> 生成新实验版本（version_no+1），旧版本与旧影子结果保留；
- 参数指纹 fingerprint = sha256(规范化参数)，发布门禁据此判断"实验结果是否与当前参数匹配"；
- 审批状态机 DRAFT -> SUBMITTED -> APPROVED/REJECTED；批准绑定具体版本号，
  批准后再改参，approved_version_no 落后于 current_version_no，旧结果自动判为 STALE。
"""
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .scoring import FORMULAS, default_benefit_config

VALID_TRANSITIONS = {
    "DRAFT": {"SUBMITTED"},
    "SUBMITTED": {"APPROVED", "REJECTED", "DRAFT"},
    "APPROVED": {"DRAFT"},  # 批准后改参自动回到 DRAFT
    "REJECTED": {"DRAFT"},
}


def fingerprint_of(params: dict) -> str:
    return hashlib.sha256(
        json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def create_experiment(
    db: Session,
    *,
    code: str,
    name: str,
    description: str,
    formula: str,
    thresholds: dict,
    formula_params: dict,
    store_scope,
    product_scope,
    window_days: int,
    candidate_effective_from: datetime,
    candidate_effective_to: datetime | None,
    benefit_config: dict | None,
    sampling_mode: str,
    sampling_design: dict,
    baseline_rule_id: str,
    created_by: str,
    change_note: str | None = None,
) -> tuple[models.RuleExperiment, models.RuleExperimentVersion]:
    if formula not in FORMULAS:
        raise ValueError(f"不支持的计分公式 {formula}，可选 {FORMULAS}")
    if sampling_mode not in ("FULL", "STRATIFIED"):
        raise ValueError("sampling_mode 仅支持 FULL / STRATIFIED")
    if db.execute(select(models.RuleExperiment).where(models.RuleExperiment.code == code)).scalar_one_or_none():
        raise ValueError(f"实验编码已存在: {code}")

    now = datetime.now(timezone.utc)
    exp = models.RuleExperiment(
        code=code, name=name, description=description,
        status="DRAFT", current_version_no=1, approved_version_no=None,
        created_at=now, updated_at=now,
    )
    db.add(exp)
    db.flush()
    ver = _add_version(
        db, exp, formula=formula, thresholds=thresholds, formula_params=formula_params,
        store_scope=store_scope, product_scope=product_scope, window_days=window_days,
        candidate_effective_from=candidate_effective_from,
        candidate_effective_to=candidate_effective_to,
        benefit_config=benefit_config or default_benefit_config(),
        sampling_mode=sampling_mode, sampling_design=sampling_design,
        baseline_rule_id=baseline_rule_id, created_by=created_by, change_note=change_note or "初始版本",
    )
    db.commit()
    db.refresh(exp)
    return exp, ver


def revise_experiment(
    db: Session,
    experiment_id: int,
    *,
    actor: str,
    change_note: str | None = None,
    **fields,
) -> models.RuleExperimentVersion:
    """审批中/已批准后修改阈值或公式（或任何参数）：追加新版本，旧结果保留但门禁判失效。"""
    exp = db.get(models.RuleExperiment, experiment_id)
    if exp is None:
        raise ValueError("实验不存在")
    latest = latest_version(db, experiment_id)

    def pick(key, default):
        return fields[key] if key in fields and fields[key] is not None else default

    new_no = exp.current_version_no + 1
    ver = _add_version(
        db, exp,
        formula=pick("formula", latest.formula),
        thresholds=pick("thresholds", latest.thresholds),
        formula_params=pick("formula_params", latest.formula_params),
        store_scope=pick("store_scope", latest.store_scope),
        product_scope=pick("product_scope", latest.product_scope),
        window_days=pick("window_days", latest.window_days),
        candidate_effective_from=pick("candidate_effective_from", latest.candidate_effective_from),
        candidate_effective_to=pick("candidate_effective_to", latest.candidate_effective_to),
        benefit_config=pick("benefit_config", latest.benefit_config),
        sampling_mode=pick("sampling_mode", latest.sampling_mode),
        sampling_design=pick("sampling_design", latest.sampling_design),
        baseline_rule_id=pick("baseline_rule_id", latest.baseline_rule_id),
        created_by=actor,
        change_note=change_note or f"第 {new_no} 版参数修订",
        version_no_override=new_no,
    )
    # 改参即失效：回到 DRAFT，已批准版本号停留在旧版
    exp.current_version_no = new_no
    exp.status = "DRAFT"
    exp.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(ver)
    return ver


def _add_version(db: Session, exp: models.RuleExperiment, *, version_no_override: int | None = None, **v) -> models.RuleExperimentVersion:
    no = version_no_override or exp.current_version_no
    fp_payload = {
        "formula": v["formula"],
        "thresholds": v["thresholds"],
        "formula_params": v["formula_params"],
        "store_scope": v["store_scope"],
        "product_scope": v["product_scope"],
        "window_days": v["window_days"],
        "candidate_effective_from": v["candidate_effective_from"].isoformat()
        if isinstance(v["candidate_effective_from"], datetime) else v["candidate_effective_from"],
        "candidate_effective_to": v["candidate_effective_to"].isoformat()
        if isinstance(v["candidate_effective_to"], datetime) else v["candidate_effective_to"],
        "benefit_config": v["benefit_config"],
        "baseline_rule_id": v["baseline_rule_id"],
    }
    ver = models.RuleExperimentVersion(
        experiment_id=exp.id,
        version_no=no,
        formula=v["formula"],
        thresholds=v["thresholds"],
        formula_params=v["formula_params"],
        store_scope=v["store_scope"],
        product_scope=v["product_scope"],
        window_days=v["window_days"],
        candidate_effective_from=v["candidate_effective_from"],
        candidate_effective_to=v["candidate_effective_to"],
        benefit_config=v["benefit_config"],
        sampling_mode=v["sampling_mode"],
        sampling_design=v["sampling_design"],
        param_fingerprint=fingerprint_of(fp_payload),
        baseline_rule_id=v["baseline_rule_id"],
        created_at=datetime.now(timezone.utc),
        created_by=v["created_by"],
        change_note=v["change_note"],
    )
    db.add(ver)
    db.flush()
    return ver


def transition_status(db: Session, experiment_id: int, target: str, *, actor: str) -> models.RuleExperiment:
    exp = db.get(models.RuleExperiment, experiment_id)
    if exp is None:
        raise ValueError("实验不存在")
    if target not in VALID_TRANSITIONS.get(exp.status, set()):
        raise ValueError(f"非法审批流转: {exp.status} -> {target}")
    exp.status = target
    if target == "APPROVED":
        exp.approved_version_no = exp.current_version_no
    exp.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(exp)
    return exp


def latest_version(db: Session, experiment_id: int) -> models.RuleExperimentVersion:
    rows = db.execute(
        select(models.RuleExperimentVersion)
        .where(models.RuleExperimentVersion.experiment_id == experiment_id)
        .order_by(models.RuleExperimentVersion.version_no.desc())
    ).scalars().all()
    if not rows:
        raise ValueError("实验无版本")
    return rows[0]


def list_versions(db: Session, experiment_id: int) -> list[models.RuleExperimentVersion]:
    return db.execute(
        select(models.RuleExperimentVersion)
        .where(models.RuleExperimentVersion.experiment_id == experiment_id)
        .order_by(models.RuleExperimentVersion.version_no)
    ).scalars().all()


def version_json(v: models.RuleExperimentVersion) -> dict:
    return {
        "id": v.id,
        "experiment_id": v.experiment_id,
        "version_no": v.version_no,
        "formula": v.formula,
        "thresholds": v.thresholds,
        "formula_params": v.formula_params,
        "store_scope": v.store_scope,
        "product_scope": v.product_scope,
        "window_days": v.window_days,
        "candidate_effective_from": v.candidate_effective_from.isoformat(),
        "candidate_effective_to": v.candidate_effective_to.isoformat() if v.candidate_effective_to else None,
        "benefit_config": v.benefit_config,
        "sampling_mode": v.sampling_mode,
        "sampling_design": v.sampling_design,
        "param_fingerprint": v.param_fingerprint,
        "baseline_rule_id": v.baseline_rule_id,
        "created_at": v.created_at.isoformat(),
        "created_by": v.created_by,
        "change_note": v.change_note,
    }
