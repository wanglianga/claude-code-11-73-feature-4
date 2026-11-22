"""样本快照器：固定成员集合、交易可见性、身份关系和门店归属。

职责与红线：
- 冻结证据水位（最大事件 id）、身份图版本、规则集版本，与发布库同一口径；
- 逐人冻结特征（窗口消费、活跃门店数、门店归属、消费层级、活跃度、身份簇）；
- 身份拆分/合并审批中的成员标记为未决样本（is_pending），影子分群不得按合并消费计算；
- 抽样设计（FULL 全量成员集 / STRATIFIED 分层抽样）随实验版本落地并写入快照；
- 补传交易改变冻结水位时生成新快照版本（version_no+1），先前快照与预测结论原样保留。

抽样为确定性抽样：随机种子来自抽样设计，同快照重跑得到同一入样集合。
"""
import hashlib
import math
import random
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import identity
from .publications import IDENTITY_TYPES, RULE_TYPES
from .scoring import activity_band, member_features, spend_band


def _watermark(db: Session, sys_as_of) -> tuple[int, int, int, int]:
    events = db.execute(select(models.Event)).scalars().all()
    visible = [e for e in events if e.ingested_at <= sys_as_of]
    watermark = max((e.id for e in visible), default=0)
    id_ver = max((e.id for e in visible if e.event_type in IDENTITY_TYPES), default=0)
    rule_ver = max((e.id for e in visible if e.event_type in RULE_TYPES), default=0)
    return watermark, id_ver, rule_ver, len(visible)


def _stratum_key(home_store: str, spend: float, active_stores: int) -> str:
    return f"{home_store}|{spend_band(spend)}|{activity_band(active_stores)}"


def latest_snapshot(db: Session, series: str) -> models.SampleSnapshot | None:
    rows = db.execute(
        select(models.SampleSnapshot)
        .where(models.SampleSnapshot.snapshot_series == series)
        .order_by(models.SampleSnapshot.version_no.desc())
    ).scalars().all()
    return rows[0] if rows else None


def create_snapshot(
    db: Session,
    ver: models.RuleExperimentVersion,
    *,
    biz_as_of,
    sys_as_of,
) -> tuple[models.SampleSnapshot, bool]:
    """创建（或幂等复用）样本快照。返回 (snapshot, created)。

    同一系列下水位（证据/身份/规则）未变化时复用最新快照；水位变化则追加新版本，
    并通过 supersedes_snapshot_id 保留演进链与先前预测结论。
    """
    series = f"snap-{ver.id}"
    watermark, id_ver, rule_ver, _ = _watermark(db, sys_as_of)
    prior = latest_snapshot(db, series)
    if (
        prior is not None
        and prior.watermark_event_id == watermark
        and prior.identity_graph_version == id_ver
        and prior.rule_set_version == rule_ver
        and prior.sampling_mode == ver.sampling_mode
    ):
        return prior, False

    pending_map = identity.pending_members_at(db, biz_as_of, sys_as_of)
    members = db.execute(select(models.MemberDirectory)).scalars().all()

    rows: list[dict] = []
    for m in members:
        feat = member_features(
            db, m.member_key, biz_as_of, sys_as_of, ver.window_days, ver.product_scope
        )
        is_pending = m.member_key in pending_map
        rows.append(
            {
                "member_key": m.member_key,
                "home_store": feat["home_store"],
                "stores": feat["stores"],
                "stratum": _stratum_key(feat["home_store"], feat["total_spend"], feat["active_store_count"]),
                "is_pending": is_pending,
                "pending_reason": _pending_reason(pending_map.get(m.member_key)),
                "features": feat,
            }
        )

    in_sample_set = _draw_sample(rows, ver.sampling_mode, ver.sampling_design)

    snapshot = models.SampleSnapshot(
        snapshot_series=series,
        version_no=(prior.version_no + 1) if prior else 1,
        experiment_version_id=ver.id,
        biz_as_of=biz_as_of,
        sys_as_of=sys_as_of,
        watermark_event_id=watermark,
        identity_graph_version=id_ver,
        rule_set_version=rule_ver,
        sampling_mode=ver.sampling_mode,
        sampling_design=ver.sampling_design,
        total_population=len(rows),
        sample_size=len(in_sample_set),
        pending_members=[
            {"member_key": k, "reasons": pending_map[k]} for k in sorted(pending_map)
        ],
        supersedes_snapshot_id=prior.id if prior else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(snapshot)
    db.flush()
    for r in rows:
        db.add(
            models.SampleSnapshotMember(
                snapshot_id=snapshot.id,
                member_key=r["member_key"],
                home_store=r["home_store"],
                stores=r["stores"],
                stratum=r["stratum"],
                in_sample=(r["member_key"] in in_sample_set),
                is_pending=r["is_pending"],
                pending_reason=r["pending_reason"],
                features=r["features"],
            )
        )
    db.commit()
    db.refresh(snapshot)
    return snapshot, True


def _pending_reason(notes: list[dict] | None) -> str | None:
    if not notes:
        return None
    return "；".join(n.get("reason", "身份待核") for n in notes)


def _draw_sample(rows: list[dict], mode: str, design: dict) -> set[str]:
    """确定性入样。FULL 全量；STRATIFIED 按分层（门店×消费层级×活跃度）独立抽样。"""
    if mode == "FULL":
        return {r["member_key"] for r in rows}

    design = design or {}
    seed = int(design.get("seed", 20260101))
    strata: dict[str, list[dict]] = {}
    for r in rows:
        strata.setdefault(r["stratum"], []).append(r)

    picked: set[str] = set()
    for stratum, group in strata.items():
        keys = sorted(r["member_key"] for r in group)
        n = len(keys)
        # 稳定分层种子：sha256 摘要取整，避免内置 hash() 的进程级随机化
        stratum_seed = int(hashlib.sha256(stratum.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed + stratum_seed % 1_000_003)
        shuffled = keys[:]
        rng.shuffle(shuffled)

        if "sample_per_stratum" in design:
            k = min(int(design["sample_per_stratum"]), n)
        elif "sample_fraction" in design:
            k = min(max(math.ceil(float(design["sample_fraction"]) * n), 1), n)
        else:
            k = n
        min_per = int(design.get("min_per_stratum", 1))
        k = max(min(k, n), min(min_per, n))
        # 未决成员强制入样（需跟踪审批结论），不计入分层估计自由度
        for r in group:
            if r["is_pending"]:
                picked.add(r["member_key"])
        picked.update(shuffled[:k])
    return picked


def snapshot_members(db: Session, snapshot_id: int) -> list[models.SampleSnapshotMember]:
    return db.execute(
        select(models.SampleSnapshotMember)
        .where(models.SampleSnapshotMember.snapshot_id == snapshot_id)
        .order_by(models.SampleSnapshotMember.member_key)
    ).scalars().all()


def snapshot_json(s: models.SampleSnapshot) -> dict:
    return {
        "id": s.id,
        "snapshot_series": s.snapshot_series,
        "version_no": s.version_no,
        "experiment_version_id": s.experiment_version_id,
        "biz_as_of": s.biz_as_of.isoformat(),
        "sys_as_of": s.sys_as_of.isoformat(),
        "watermark_event_id": s.watermark_event_id,
        "identity_graph_version": s.identity_graph_version,
        "rule_set_version": s.rule_set_version,
        "sampling_mode": s.sampling_mode,
        "sampling_design": s.sampling_design,
        "total_population": s.total_population,
        "sample_size": s.sample_size,
        "pending_members": s.pending_members,
        "supersedes_snapshot_id": s.supersedes_snapshot_id,
        "created_at": s.created_at.isoformat(),
    }
