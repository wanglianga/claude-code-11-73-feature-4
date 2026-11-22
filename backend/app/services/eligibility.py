"""资格计算器：按业务时点、系统时点、规则与身份关系计算会员等级。

计算完全基于事件账重放（双时间过滤），不读取任何"当前余额/当前主档"：
- 证据可见性：biz_time <= T_b 且 ingested_at <= T_s；
- 修订链：被 supersede 的事件在 T_s 前已被替代时以最新替代者为准；
- 身份簇：按 (T_b, T_s) 下有效的 merge 边解析；
- 规则：按门店范围解析适用版本，分范围评估后取最高等级。
"""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import identity, rules

TIER_ORDER = ["NONE", "SILVER", "GOLD", "PLATINUM"]


def tier_rank(t: str) -> int:
    return TIER_ORDER.index(t) if t in TIER_ORDER else 0


def tier_for_spend(thresholds: dict, spend: float) -> str:
    best = "NONE"
    for tier, th in thresholds.items():
        if spend >= float(th) and tier_rank(tier) > tier_rank(best):
            best = tier
    return best


def _visible_transactions(db: Session, cluster: set[str], biz_t: datetime, sys_t: datetime):
    """返回 (有效交易, 被替代交易)。修订链在 T_s 口径下取链首。"""
    events = db.execute(
        select(models.Event).where(models.Event.event_type == "transaction")
    ).scalars().all()
    children: dict[int, list[models.Event]] = {}
    for e in events:
        if e.supersedes_id:
            children.setdefault(e.supersedes_id, []).append(e)

    def head(e: models.Event) -> models.Event:
        cur = e
        while True:
            nxt = [c for c in children.get(cur.id, []) if c.ingested_at <= sys_t]
            if not nxt:
                return cur
            cur = max(nxt, key=lambda c: (c.ingested_at, c.id))

    own = [e for e in events if e.payload.get("member_key") in cluster and e.ingested_at <= sys_t]
    visible: dict[int, models.Event] = {}
    replaced: list[dict] = []
    for e in own:
        h = head(e)
        if h.id != e.id:
            replaced.append(
                {"event_id": e.id, "replaced_by": h.id, "reason": "superseded_correction"}
            )
        visible[h.id] = h
    txns = [e for e in visible.values() if e.biz_time <= biz_t]
    return txns, replaced


def compute_eligibility(db: Session, member_key: str, biz_t: datetime, sys_t: datetime) -> dict:
    cluster = identity.cluster_at(db, member_key, biz_t, sys_t)
    txns, replaced = _visible_transactions(db, cluster, biz_t, sys_t)

    # 按门店分组 -> 解析各门店在 (T_b, T_s) 的适用规则版本 -> 按版本聚合评估
    stores = sorted({t.payload.get("store", "UNKNOWN") for t in txns})
    by_version: dict[int, dict] = {}
    for store in stores:
        rv = rules.rule_for_store(db, store, biz_t, sys_t)
        if rv is None:
            continue
        grp = by_version.setdefault(
            rv.id,
            {
                "rule_id": rv.rule_id,
                "rule_version": rv.version,
                "rule_event_id": rv.rule_event_id,
                "window_days": rv.window_days,
                "thresholds": rv.thresholds,
                "product_scope": rv.product_scope,
                "stores": [],
            },
        )
        grp["stores"].append(store)

    scopes = []
    best_tier = "NONE"
    for grp in by_version.values():
        window_start = biz_t - timedelta(days=grp["window_days"])
        counted, spend = [], 0.0
        for t in txns:
            p = t.payload
            if p.get("store") not in grp["stores"]:
                continue
            if not rules.product_in_scope(grp["product_scope"], p.get("category", "")):
                continue
            if not (window_start <= t.biz_time <= biz_t):
                continue
            counted.append(t.id)
            spend += float(p.get("amount", 0))
        tier = tier_for_spend(grp["thresholds"], spend)
        scopes.append(
            {
                "rule_id": grp["rule_id"],
                "rule_version": grp["rule_version"],
                "rule_event_id": grp["rule_event_id"],
                "stores": grp["stores"],
                "window_from": window_start.isoformat(),
                "window_to": biz_t.isoformat(),
                "spend": round(spend, 2),
                "tier": tier,
                "counted_event_ids": sorted(counted),
            }
        )
        if tier_rank(tier) > tier_rank(best_tier):
            best_tier = tier

    return {
        "member_key": member_key,
        "biz_as_of": biz_t.isoformat(),
        "sys_as_of": sys_t.isoformat(),
        "tier": best_tier,
        "total_spend": round(sum(s["spend"] for s in scopes), 2),
        "cluster": sorted(cluster),
        "scopes": scopes,
        "replaced_events": replaced,
    }
