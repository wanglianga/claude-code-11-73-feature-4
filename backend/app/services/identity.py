"""身份关系图：合并、拆分、待核、撤销的双时间有效区间维护与任意时点解析。

语义：
- merge：在 [biz_from, biz_to) × [sys_from, sys_to) 区间内把 secondary 并入 primary。
- split：关闭目标 merge 边；retroactive=true 时 biz_to 回收到 merge 起点
  （误合并视为自始不成立），否则从拆分的业务时间起生效。
- revoke：撤销申请，关闭目标 merge 边并标记 revoked（撤销 = 自始无效）。
- pending：待核关系，仅记录，不参与资格计算。
所有关闭动作只写新区间端点，不删除历史行。
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models


def keys_of(ev) -> list[str]:
    p = ev.payload
    keys = []
    for k in ("primary_key", "secondary_key", "key_a", "key_b"):
        if p.get(k):
            keys.append(p[k])
    return keys


def apply_identity_event(db: Session, ev: models.Event) -> None:
    p = ev.payload
    if ev.event_type == "identity_merge":
        db.add(
            models.IdentityEdge(
                relation_event_id=ev.id,
                member_a=p["secondary_key"],
                member_b=p["primary_key"],
                relation="merge",
                status="active",
                biz_from=ev.biz_time,
                sys_from=ev.ingested_at,
                reason=p.get("reason"),
            )
        )
    elif ev.event_type == "identity_pending":
        db.add(
            models.IdentityEdge(
                relation_event_id=ev.id,
                member_a=p["key_a"],
                member_b=p["key_b"],
                relation="pending",
                status="pending",
                biz_from=ev.biz_time,
                sys_from=ev.ingested_at,
                reason=p.get("reason"),
            )
        )
    elif ev.event_type in ("identity_split", "identity_revoke"):
        # 找到目标 merge 边
        target = None
        target_event_id = p.get("merge_event_id") or p.get("target_event_id")
        if target_event_id:
            target = db.execute(
                select(models.IdentityEdge).where(
                    models.IdentityEdge.relation_event_id == target_event_id,
                    models.IdentityEdge.relation == "merge",
                )
            ).scalar_one_or_none()
        if target is None:
            a, b = p.get("secondary_key"), p.get("primary_key")
            target = db.execute(
                select(models.IdentityEdge).where(
                    models.IdentityEdge.relation == "merge",
                    models.IdentityEdge.status == "active",
                    models.IdentityEdge.member_a == a,
                    models.IdentityEdge.member_b == b,
                )
            ).scalar_one_or_none()
        if target is not None and target.sys_to is None:
            target.sys_to = ev.ingested_at
            if ev.event_type == "identity_revoke" or p.get("retroactive"):
                # 误合并拆分 / 撤销：关系自始不成立
                target.biz_to = target.biz_from
            else:
                target.biz_to = ev.biz_time
            target.status = "revoked" if ev.event_type == "identity_revoke" else "split"
        db.add(
            models.IdentityEdge(
                relation_event_id=ev.id,
                member_a=p.get("secondary_key") or p.get("key_a") or "",
                member_b=p.get("primary_key") or p.get("key_b") or "",
                relation="split" if ev.event_type == "identity_split" else "revoke",
                status="applied",
                biz_from=ev.biz_time,
                sys_from=ev.ingested_at,
                target_edge_id=target.id if target else None,
                reason=p.get("reason"),
            )
        )


def merge_edges_at(db: Session, biz_t: datetime, sys_t: datetime) -> list[models.IdentityEdge]:
    """在 (业务时点 biz_t, 系统时点 sys_t) 双时间口径下有效的 merge 边。"""
    edges = db.execute(select(models.IdentityEdge).where(models.IdentityEdge.relation == "merge")).scalars().all()
    out = []
    for e in edges:
        if e.status == "pending":
            continue
        if e.sys_from > sys_t:
            continue
        if e.biz_from > biz_t:
            continue
        # 闭合（拆分/撤销）只在闭合记录已可见、且业务终点覆盖查询时点时生效：
        # sys_t 早于闭合记录时间 => 当时该边仍然有效；biz_t 早于 biz_to => 区间内仍有效。
        if (
            e.sys_to is not None
            and e.sys_to <= sys_t
            and e.biz_to is not None
            and e.biz_to <= biz_t
        ):
            continue
        out.append(e)
    return out


def pending_edges_at(db: Session, biz_t: datetime, sys_t: datetime) -> list[models.IdentityEdge]:
    """在 (biz_t, sys_t) 口径下仍然有效的待核关系边（identity_pending，未被拆分/撤销闭合）。"""
    edges = db.execute(
        select(models.IdentityEdge).where(models.IdentityEdge.relation == "pending")
    ).scalars().all()
    out = []
    for e in edges:
        if e.sys_from > sys_t or e.biz_from > biz_t:
            continue
        if (
            e.sys_to is not None
            and e.sys_to <= sys_t
            and e.biz_to is not None
            and e.biz_to <= biz_t
        ):
            continue
        out.append(e)
    return out


def pending_members_at(db: Session, biz_t: datetime, sys_t: datetime) -> dict[str, list[dict]]:
    """返回 {member_key: [待核关系说明]}；成员身份正在拆分/合并审批时列为未决。

    待核(pending)边仅记录不参与资格计算；实验快照必须把这些成员标记为未决样本，
    不能按合并消费计算影子分群。
    """
    result: dict[str, list[dict]] = {}
    for e in pending_edges_at(db, biz_t, sys_t):
        note = {
            "edge_id": e.id,
            "relation_event_id": e.relation_event_id,
            "peer": e.member_b if e.member_a else e.member_a,
            "reason": e.reason or "身份关系待核/拆分审批中",
        }
        result.setdefault(e.member_a, []).append(note)
        result.setdefault(e.member_b, []).append({**note, "peer": e.member_a})
    return result


def cluster_at(db: Session, member_key: str, biz_t: datetime, sys_t: datetime) -> set[str]:
    """并查集求 member 在指定双时点的身份簇。"""
    edges = merge_edges_at(db, biz_t, sys_t)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for e in edges:
        union(e.member_a, e.member_b)
    root = find(member_key)
    cluster = {k for k in parent if find(k) == root}
    cluster.add(member_key)
    return cluster
