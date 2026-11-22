"""发布门禁：阻止未经批准或与当前参数不匹配的实验结果进入正式等级发布。

检查项（任一不过即拒绝发布，原因码随 ReleaseGate 留痕）：
- GATE_NOT_APPROVED        实验未处于 APPROVED；
- GATE_STALE_PARAMS        运行所基于的实验版本不是当前批准版本（审批中改过阈值/公式，旧结果失效）；
- GATE_SNAPSHOT_STALE      运行快照已被更新水位的快照版本取代（补传交易改变冻结水位）；
- GATE_PENDING_MEMBERS     运行中仍有身份拆分/合并审批中的未决成员；
- GATE_UNDECIDABLE         精度评估存在不可判定门店（抽样覆盖达不到误差要求）；
- GATE_BENCHMARK_DEVIATION 同快照全量基准对比中，抽样估计偏差超出容忍或真值落在置信区间外。
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import experiments as exp_svc
from . import snapshots as snap_svc


def check_gate(db: Session, run_id: int) -> models.ReleaseGate:
    run = db.get(models.ShadowRun, run_id)
    if run is None:
        raise ValueError("影子运行不存在")
    ver = db.get(models.RuleExperimentVersion, run.experiment_version_id)
    exp = db.get(models.RuleExperiment, ver.experiment_id)
    snapshot = db.get(models.SampleSnapshot, run.snapshot_id)
    report = db.execute(
        select(models.PrecisionReport).where(models.PrecisionReport.shadow_run_id == run.id)
    ).scalar_one_or_none()
    member_results = db.execute(
        select(models.ShadowMemberResult).where(models.ShadowMemberResult.shadow_run_id == run.id)
    ).scalars().all()
    latest_ver = exp_svc.latest_version(db, exp.id)

    checks: list[dict] = []
    reasons: list[str] = []

    def add(code: str, passed: bool, detail: str):
        checks.append({"code": code, "passed": passed, "detail": detail})
        if not passed:
            reasons.append(code)

    # 1) 审批状态
    add("GATE_NOT_APPROVED", exp.status == "APPROVED",
        f"实验状态={exp.status}，批准版本号={exp.approved_version_no}")

    # 2) 参数匹配：运行版本必须是当前版本且为被批准版本（指纹一致）
    is_current = ver.id == latest_ver.id and ver.param_fingerprint == latest_ver.param_fingerprint
    is_approved = exp.approved_version_no == ver.version_no
    add("GATE_STALE_PARAMS", is_current and is_approved,
        f"运行版本=v{ver.version_no}（指纹 {ver.param_fingerprint[:12]}…），"
        f"当前版本=v{latest_ver.version_no}，批准版本=v{exp.approved_version_no}")

    # 3) 快照水位：同一系列不得存在更新的快照版本
    newest_snap = snap_svc.latest_snapshot(db, snapshot.snapshot_series)
    snap_ok = newest_snap is not None and newest_snap.id == snapshot.id
    add("GATE_SNAPSHOT_STALE", snap_ok,
        f"运行快照=v{snapshot.version_no}(#{snapshot.id})，最新快照=v{newest_snap.version_no if newest_snap else '-'}")

    # 4) 未决成员
    pending_n = sum(1 for m in member_results if m.migration == "PENDING")
    add("GATE_PENDING_MEMBERS", pending_n == 0, f"未决成员 {pending_n} 名（身份拆分/合并审批中）")

    # 5) 精度可判定
    decidable = bool(report and report.overall_decidable)
    undec = [] if report is None else [s["store"] for s in report.undecidable_stores if not s["decidable"]]
    add("GATE_UNDECIDABLE", decidable, f"不可判定门店={undec or '无'}")

    # 6) 全量基准对比（抽样运行且存在同快照全量基准时）
    bench_ok, bench_detail = True, "无全量基准或为全量运行，跳过"
    if report and report.metrics.get("benchmark_vs_full"):
        bench = report.metrics["benchmark_vs_full"]
        bad = []
        for group, items in (("tier_population", bench.get("tier_population", {})),
                             ("cost_delta", bench.get("cost_delta", {}))):
            for key, m in items.items():
                if not m.get("inside_ci", True):
                    bad.append(f"{group}.{key} 真值落在 CI 外")
        bench_ok = not bad
        bench_detail = "；".join(bad) or "全部指标真值落在置信区间内"
    add("GATE_BENCHMARK_DEVIATION", bench_ok, bench_detail)

    gate = models.ReleaseGate(
        experiment_id=exp.id,
        experiment_version_id=ver.id,
        shadow_run_id=run.id,
        passed=not reasons,
        checks=checks,
        reason_codes=reasons,
        created_at=datetime.now(timezone.utc),
    )
    db.add(gate)
    db.commit()
    db.refresh(gate)
    return gate


def gate_json(g: models.ReleaseGate) -> dict:
    return {
        "id": g.id,
        "experiment_id": g.experiment_id,
        "experiment_version_id": g.experiment_version_id,
        "shadow_run_id": g.shadow_run_id,
        "passed": g.passed,
        "checks": g.checks,
        "reason_codes": g.reason_codes,
        "created_at": g.created_at.isoformat(),
    }


def gated_release(db: Session, run_id: int, publication_name: str) -> tuple[models.ReleaseGate, models.Publication | None]:
    """门禁通过后执行正式等级发布：以候选影子结果物化 Publication，并回写实验版本与门禁 id。"""
    gate = check_gate(db, run_id)
    if not gate.passed:
        return gate, None

    run = db.get(models.ShadowRun, run_id)
    snapshot = db.get(models.SampleSnapshot, run.snapshot_id)
    results = db.execute(
        select(models.ShadowMemberResult)
        .where(models.ShadowMemberResult.shadow_run_id == run.id, models.ShadowMemberResult.migration != "PENDING")
    ).scalars().all()

    pub = models.Publication(
        name=publication_name,
        created_at=datetime.now(timezone.utc),
        biz_as_of=snapshot.biz_as_of,
        sys_as_of=snapshot.sys_as_of,
        watermark_event_id=snapshot.watermark_event_id,
        identity_graph_version=snapshot.identity_graph_version,
        rule_set_version=snapshot.rule_set_version,
        semantics_code="EXPERIMENT_RELEASE",
        experiment_version_id=run.experiment_version_id,
        gate_id=gate.id,
    )
    db.add(pub)
    db.flush()
    for m in results:
        detail = m.candidate_detail if isinstance(m.candidate_detail, dict) else {}
        db.add(
            models.PublicationRow(
                publication_id=pub.id,
                member_key=m.member_key,
                tier=m.candidate_tier,
                spend=float(detail.get("score", 0.0)),
                detail={
                    "source": "experiment_gated_release",
                    "shadow_run_id": run.id,
                    "gate_id": gate.id,
                    "candidate_detail": detail,
                    "current_tier": m.current_tier,
                    "migration": m.migration,
                    "cost_delta": m.cost_delta,
                },
            )
        )
    db.commit()
    db.refresh(pub)
    return gate, pub
