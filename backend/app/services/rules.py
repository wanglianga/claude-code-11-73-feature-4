"""等级规则：版本化、按门店/商品范围与生效区间修订、部分追溯撤回。

解析语义（确定性，写入语义目录）：
- 对门店 s、业务时点 T_b、系统时点 T_s：
  1) 候选 = sys_from <= T_s 且 effective_from <= T_b 且 (effective_to 为空或 > T_b)
     且门店在 store_scope 内的版本；
  2) 剔除在 T_s 前可见、且覆盖 (版本, s, T_b) 的撤回所命中的版本（部分撤回只影响其门店范围）；
  3) 取 (effective_from, version) 最大者 —— 并发修订由该全序确定性地解决，
     修订链(supersedes_version)完整保留在账本中供审计。
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models


def store_in_scope(scope, store: str) -> bool:
    return scope == "ALL" or (isinstance(scope, list) and store in scope)


def product_in_scope(scope, category: str) -> bool:
    return scope == "ALL" or (isinstance(scope, list) and category in scope)


def apply_rule_event(db: Session, ev: models.Event) -> None:
    p = ev.payload
    if ev.event_type == "rule_upsert":
        db.add(
            models.RuleVersion(
                rule_event_id=ev.id,
                rule_id=p["rule_id"],
                version=p["version"],
                supersedes_version=p.get("supersedes_version"),
                store_scope=p.get("store_scope", "ALL"),
                product_scope=p.get("product_scope", "ALL"),
                window_days=p["window_days"],
                thresholds=p["thresholds"],
                effective_from=_parse(p["effective_from"]),
                effective_to=_parse(p["effective_to"]) if p.get("effective_to") else None,
                sys_from=ev.ingested_at,
            )
        )
    elif ev.event_type == "rule_withdraw":
        db.add(
            models.RuleWithdrawal(
                withdraw_event_id=ev.id,
                rule_id=p["rule_id"],
                target_version=p["target_version"],
                store_scope=p.get("store_scope", "ALL"),
                withdraw_from=_parse(p["withdraw_from"]),
                withdraw_to=_parse(p["withdraw_to"]) if p.get("withdraw_to") else None,
                sys_from=ev.ingested_at,
                reason=p.get("reason"),
            )
        )


def _parse(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt


def rule_for_store(db: Session, store: str, biz_t: datetime, sys_t: datetime) -> models.RuleVersion | None:
    versions = db.execute(select(models.RuleVersion)).scalars().all()
    withdrawals = db.execute(select(models.RuleWithdrawal)).scalars().all()

    def withdrawn(v: models.RuleVersion) -> bool:
        for w in withdrawals:
            if w.sys_from > sys_t:
                continue
            if w.rule_id != v.rule_id or w.target_version != v.version:
                continue
            if not store_in_scope(w.store_scope, store):
                continue
            if w.withdraw_from <= biz_t and (w.withdraw_to is None or w.withdraw_to > biz_t):
                return True
        return False

    cands = []
    for v in versions:
        if v.sys_from > sys_t:
            continue
        if v.effective_from > biz_t:
            continue
        if v.effective_to is not None and v.effective_to <= biz_t:
            continue
        if not store_in_scope(v.store_scope, store):
            continue
        if withdrawn(v):
            continue
        cands.append(v)
    if not cands:
        return None
    return max(cands, key=lambda v: (v.effective_from, v.version))
