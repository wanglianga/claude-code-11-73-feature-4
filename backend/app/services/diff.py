"""差异归因器 + 影响索引器。

差异归因：比较两个发布（或两种时间语义口径），把每个会员的资格/消费差异
归因到水位之间的具体事件（迟到交易、更正、身份合并/拆分/撤销、规则修订、规则撤回）。
影响索引：把资格变化映射到下游——营销名单、门店预算、模型训练样本。
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .eligibility import tier_rank
from .identity import cluster_at
from .publications import IDENTITY_TYPES, RULE_TYPES

CAUSE_BY_TYPE = {
    "identity_merge": "IDENTITY_MERGE",
    "identity_split": "IDENTITY_SPLIT",
    "identity_revoke": "IDENTITY_REVOKE",
    "identity_pending": "IDENTITY_PENDING",
    "rule_upsert": "RULE_REVISION",
    "rule_withdraw": "RULE_WITHDRAWAL",
}


def _member_stores(rows: list[models.PublicationRow], member: str) -> set[str]:
    stores = set()
    for r in rows:
        if r.member_key != member:
            continue
        for scope in (r.detail or {}).get("scopes", []):
            stores.update(scope.get("stores", []))
    return stores


def _attribute(
    db: Session,
    member: str,
    events_between: list[models.Event],
    biz_as_of: datetime,
    sys_a: datetime,
    sys_b: datetime,
    stores: set[str],
) -> list[dict]:
    causes = []
    cluster_a = cluster_at(db, member, biz_as_of, sys_a)
    cluster_b = cluster_at(db, member, biz_as_of, sys_b)
    related_members = cluster_a | cluster_b | {member}
    for e in events_between:
        p = e.payload or {}
        if e.event_type == "transaction":
            if p.get("member_key") not in related_members:
                continue
            if e.supersedes_id:
                cause = "CORRECTION"
                detail = f"更正事件#{e.supersedes_id}，金额 {p.get('amount')}"
            elif e.biz_time <= biz_as_of:
                cause = "LATE_TRANSACTION"
                detail = f"迟到补传交易，业务时间 {e.biz_time.date()}，金额 {p.get('amount')}，门店 {p.get('store')}"
            else:
                cause = "NEW_TRANSACTION"
                detail = f"新业务交易，金额 {p.get('amount')}"
            causes.append({"event_id": e.id, "cause": cause, "detail": detail})
        elif e.event_type in IDENTITY_TYPES:
            keys = {p.get("primary_key"), p.get("secondary_key"), p.get("key_a"), p.get("key_b")}
            if member in keys or keys & related_members:
                causes.append(
                    {
                        "event_id": e.id,
                        "cause": CAUSE_BY_TYPE[e.event_type],
                        "detail": p.get("reason") or e.event_type,
                    }
                )
        elif e.event_type in RULE_TYPES:
            scope = p.get("store_scope", "ALL")
            hit = scope == "ALL" or bool(stores & set(scope if isinstance(scope, list) else []))
            if hit:
                if e.event_type == "rule_upsert":
                    detail = f"规则 {p.get('rule_id')} v{p.get('version')} 生效自 {p.get('effective_from')}，阈值 {p.get('thresholds')}"
                else:
                    detail = f"撤回 {p.get('rule_id')} v{p.get('target_version')}，范围 {scope}，自 {p.get('withdraw_from')}"
                causes.append({"event_id": e.id, "cause": CAUSE_BY_TYPE[e.event_type], "detail": detail})
    return causes


def create_diff(db: Session, pub_a_id: int, pub_b_id: int) -> models.Diff:
    pub_a = db.get(models.Publication, pub_a_id)
    pub_b = db.get(models.Publication, pub_b_id)
    if not pub_a or not pub_b:
        raise ValueError("publication not found")

    rows_a = db.execute(
        select(models.PublicationRow).where(models.PublicationRow.publication_id == pub_a_id)
    ).scalars().all()
    rows_b = db.execute(
        select(models.PublicationRow).where(models.PublicationRow.publication_id == pub_b_id)
    ).scalars().all()
    map_a = {r.member_key: r for r in rows_a}
    map_b = {r.member_key: r for r in rows_b}

    lo, hi = sorted([pub_a.sys_as_of, pub_b.sys_as_of])
    events_between = [
        e
        for e in db.execute(select(models.Event)).scalars().all()
        if lo < e.ingested_at <= hi
    ]

    diff = models.Diff(pub_a=pub_a_id, pub_b=pub_b_id, created_at=datetime.now(timezone.utc))
    db.add(diff)
    db.flush()

    all_members = sorted(set(map_a) | set(map_b))
    for member in all_members:
        ra, rb = map_a.get(member), map_b.get(member)
        before = {"tier": ra.tier if ra else "NONE", "spend": float(ra.spend) if ra else 0.0}
        after = {"tier": rb.tier if rb else "NONE", "spend": float(rb.spend) if rb else 0.0}
        if before == after:
            continue
        stores = _member_stores(rows_a, member) | _member_stores(rows_b, member)
        causes = _attribute(
            db, member, events_between, pub_b.biz_as_of, pub_a.sys_as_of, pub_b.sys_as_of, stores
        )
        item = models.DiffItem(
            diff_id=diff.id, member_key=member, before=before, after=after, causes=causes
        )
        db.add(item)
        db.flush()
        _index_impacts(db, diff.id, member, before, after, causes, stores, pub_b.biz_as_of)

    db.commit()
    db.refresh(diff)
    return diff


def _index_impacts(
    db: Session,
    diff_id: int,
    member: str,
    before: dict,
    after: dict,
    causes: list[dict],
    stores: set[str],
    biz_as_of: datetime,
) -> None:
    """影响索引器：营销名单 / 门店预算 / 训练样本。"""
    event_ids = [c["event_id"] for c in causes]
    if before["tier"] != after["tier"]:
        bt, at = before["tier"], after["tier"]
        change = "TIER_UPGRADE" if tier_rank(at) > tier_rank(bt) else "TIER_DOWNGRADE"
        # 营销名单
        for tier, action in ((bt, "REMOVE"), (at, "ADD")):
            if tier == "NONE":
                continue
            db.add(
                models.Impact(
                    diff_id=diff_id,
                    dimension="marketing",
                    entity=f"marketing:list:{tier}",
                    member_key=member,
                    change_type=change,
                    before={"list": f"marketing:list:{bt}"},
                    after={"list": f"marketing:list:{at}", "action": action},
                    attributed_event_ids=event_ids,
                )
            )
        # 门店预算（该会员消费涉及门店的等级人数结构变化）
        for store in sorted(stores) or ["UNKNOWN"]:
            db.add(
                models.Impact(
                    diff_id=diff_id,
                    dimension="budget",
                    entity=f"budget:store:{store}",
                    member_key=member,
                    change_type=change,
                    before={"tier": bt},
                    after={"tier": at},
                    attributed_event_ids=event_ids,
                )
            )
        # 训练样本（按业务月份重述标签）
        db.add(
            models.Impact(
                diff_id=diff_id,
                dimension="training",
                entity=f"training:samples:{biz_as_of:%Y-%m}",
                member_key=member,
                change_type="LABEL_RESTATED",
                before={"label": bt, "spend": before["spend"]},
                after={"label": at, "spend": after["spend"]},
                attributed_event_ids=event_ids,
            )
        )
    elif before["spend"] != after["spend"]:
        db.add(
            models.Impact(
                diff_id=diff_id,
                dimension="training",
                entity=f"training:samples:{biz_as_of:%Y-%m}",
                member_key=member,
                change_type="FEATURE_RESTATED",
                before={"label": before["tier"], "spend": before["spend"]},
                after={"label": after["tier"], "spend": after["spend"]},
                attributed_event_ids=event_ids,
            )
        )
