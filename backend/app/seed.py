"""演示种子场景（全部通过接入账写入，幂等键固定，可重复执行）。

同一会员 A(13800000001) 贯穿四类历史变更：
1. 加盟店离线补传：T4 业务时间 2026-01-28，平台 2026-03-15 才收到；
2. 误合并后拆分：2026-02-15 人工裁定把 B 并入 A，2026-04-05 追溯拆分 + 撤销；
3. 部分追溯规则撤回：规则 v2（2026-02-01 起追溯生效，GOLD 阈值 1000→1500）
   于 2026-04-10 仅对门店 S002 撤回；
4. 并发修订：v2 与 v3 在 2026-03-01 相隔 5 分钟先后修订 v1，
   由 (effective_from, version) 全序确定性解决，修订链完整保留。
另有更正链：T3 金额 300 → 380（2026-03-05 补录）。
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .services.ledger import ingest_event


def T(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


SEED_EVENTS = [
    # ---- 交易：会员 A ----
    dict(idem="seed-t1", source="pos", type="transaction", biz="2026-01-05T10:20:00", ing="2026-01-05T10:20:05",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "S001", "category": "OTC", "amount": 500, "channel": "pos"}),
    dict(idem="seed-t2", source="online", type="transaction", biz="2026-01-20T21:00:00", ing="2026-01-20T21:00:03",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "ONLINE", "category": "保健", "amount": 400, "channel": "app"}),
    dict(idem="seed-t3", source="pos", type="transaction", biz="2026-02-10T12:00:00", ing="2026-02-10T12:00:04",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "S001", "category": "RX", "amount": 300, "channel": "pos"}),
    # 更正：T3 金额 300 -> 380（修订链 supersedes）
    dict(idem="seed-t3-fix", source="pos", type="transaction", biz="2026-02-10T12:00:00", ing="2026-03-05T09:00:00",
         supersedes="seed-t3",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "S001", "category": "RX", "amount": 380, "channel": "pos", "comment": "收银错单更正"}),
    # 加盟店离线补传：业务时间 2026-01-28，2026-03-15 才到达平台
    dict(idem="seed-t4", source="franchise_offline", type="transaction", biz="2026-01-28T15:30:00", ing="2026-03-15T08:00:00",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "S002", "category": "OTC", "amount": 350, "channel": "offline_batch", "batch": "S002-2026-03"}),
    # ---- 其他会员 ----
    dict(idem="seed-t5", source="pos", type="transaction", biz="2026-02-01T11:00:00", ing="2026-02-01T11:00:02",
         payload={"member_key": "13900000002", "member_name": "李芳", "store": "S001", "category": "OTC", "amount": 600, "channel": "pos"}),
    dict(idem="seed-t9", source="pos", type="transaction", biz="2026-02-15T16:00:00", ing="2026-02-15T16:00:02",
         payload={"member_key": "13900000003", "member_name": "王强", "store": "S001", "category": "OTC", "amount": 100, "channel": "pos"}),
    dict(idem="seed-t7", source="pos", type="transaction", biz="2026-02-05T10:00:00", ing="2026-02-05T10:00:02",
         payload={"member_key": "13900000004", "member_name": "赵敏", "store": "S002", "category": "OTC", "amount": 1100, "channel": "pos"}),
    dict(idem="seed-t8", source="pos", type="transaction", biz="2026-02-03T10:00:00", ing="2026-02-03T10:00:02",
         payload={"member_key": "13900000005", "member_name": "孙磊", "store": "S001", "category": "RX", "amount": 1200, "channel": "pos"}),
    dict(idem="seed-t10", source="pos", type="transaction", biz="2026-02-08T10:00:00", ing="2026-02-08T10:00:02",
         payload={"member_key": "13900000006", "member_name": "钱进", "store": "S002", "category": "OTC", "amount": 1400, "channel": "pos"}),
    # ---- 规则：v1 ----
    dict(idem="seed-r1", source="rule_admin", type="rule_upsert", biz="2026-01-01T00:00:00", ing="2026-01-01T00:00:00",
         payload={"rule_id": "tier-standard", "version": 1, "store_scope": "ALL", "product_scope": "ALL",
                  "window_days": 90, "thresholds": {"SILVER": 300, "GOLD": 1000}, "effective_from": "2026-01-01T00:00:00+00:00"}),
    # ---- 身份：误合并（人工裁定）----
    dict(idem="seed-m1", source="identity_desk", type="identity_merge", biz="2026-02-15T09:00:00", ing="2026-02-15T09:00:00",
         payload={"primary_key": "13800000001", "secondary_key": "13900000002", "reason": "人工裁定：疑似同一人（事后证实为误合并）", "adjudicator": "ops-01"}),
    # ---- 并发规则修订：v2、v3 相隔 5 分钟先后修订 v1 ----
    dict(idem="seed-r2", source="rule_admin", type="rule_upsert", biz="2026-03-01T09:00:00", ing="2026-03-01T09:00:00",
         payload={"rule_id": "tier-standard", "version": 2, "supersedes_version": 1, "store_scope": "ALL", "product_scope": "ALL",
                  "window_days": 90, "thresholds": {"SILVER": 300, "GOLD": 1500}, "effective_from": "2026-02-01T00:00:00+00:00",
                  "comment": "追溯修订：2 月起 GOLD 阈值上调"}),
    dict(idem="seed-r3", source="rule_admin", type="rule_upsert", biz="2026-03-01T09:05:00", ing="2026-03-01T09:05:00",
         payload={"rule_id": "tier-standard", "version": 3, "supersedes_version": 1, "store_scope": "ALL", "product_scope": "ALL",
                  "window_days": 90, "thresholds": {"SILVER": 300, "GOLD": 1300}, "effective_from": "2026-03-01T00:00:00+00:00",
                  "comment": "并发修订：3 月起 GOLD 阈值 1300"}),
    # ---- 身份：待核（不影响资格）----
    dict(idem="seed-p1", source="identity_desk", type="identity_pending", biz="2026-03-20T10:00:00", ing="2026-03-20T10:00:00",
         payload={"key_a": "13800000001", "key_b": "13900000003", "reason": "手机号相似，待核"}),
    # ---- 身份：误合并追溯拆分 + 撤销 ----
    dict(idem="seed-s1", source="identity_desk", type="identity_split", biz="2026-04-05T09:00:00", ing="2026-04-05T09:00:00",
         payload={"primary_key": "13800000001", "secondary_key": "13900000002", "merge_event_key": "seed-m1",
                  "retroactive": True, "reason": "误合并拆分：双方确认为不同自然人"}),
    dict(idem="seed-v1", source="identity_desk", type="identity_revoke", biz="2026-04-05T10:00:00", ing="2026-04-05T10:00:00",
         payload={"target_event_key": "seed-m1", "reason": "撤销申请：原人工裁定证据不足"}),
    # ---- 规则：部分追溯撤回（仅门店 S002）----
    dict(idem="seed-r4", source="rule_admin", type="rule_withdraw", biz="2026-04-10T10:00:00", ing="2026-04-10T10:00:00",
         payload={"rule_id": "tier-standard", "target_version": 2, "store_scope": ["S002"],
                  "withdraw_from": "2026-02-01T00:00:00+00:00",
                  "reason": "加盟店 S002 促销口径争议，v2 对该门店追溯撤回"}),
]


def seed(db: Session) -> dict:
    from sqlalchemy import select

    from . import models

    created, skipped = 0, 0
    key_to_id: dict[str, int] = {}
    for spec in SEED_EVENTS:
        supersedes_id = None
        if spec.get("supersedes"):
            supersedes_id = key_to_id.get(spec["supersedes"])
        payload = dict(spec["payload"])
        # 把业务键引用解析为事件 id（拆分/撤销指向合并事件）
        for ref_in, ref_out in (("merge_event_key", "merge_event_id"), ("target_event_key", "target_event_id")):
            if ref_in in payload:
                payload[ref_out] = key_to_id.get(payload.pop(ref_in))
        ev, was_created = ingest_event(
            db,
            source=spec["source"],
            event_type=spec["type"],
            biz_time=T(spec["biz"]),
            ingested_at=T(spec["ing"]),
            payload=payload,
            actor="seed",
            supersedes_id=supersedes_id,
            idempotency_key=spec["idem"],
        )
        key_to_id[spec["idem"]] = ev.id
        created += 1 if was_created else 0
        skipped += 0 if was_created else 1

    # 时间语义目录
    semantics = [
        ("AS_OF", "当时可见", "query", "query",
         "以查询时点同时作为业务截止与系统截止：只纳入当时已到达平台的证据，重现当时可见的资格结论。"),
        ("AS_KNOWN_NOW", "事后补全", "query", "now",
         "业务时点不变、系统时点为当前：纳入其后到达的补传、更正、身份与规则变更，给出事后补全口径。"),
        ("DELTA", "二者差异", "query", "both",
         "对同一业务时点分别按两种口径计算并归因差异（迟到证据、更正、身份变更、规则修订/撤回）。"),
    ]
    for code, name, biz_p, sys_p, desc in semantics:
        if db.get(models.Semantics, code) is None:
            db.add(models.Semantics(code=code, name=name, biz_policy=biz_p, sys_policy=sys_p, description=desc))
    # 演示账号
    for username, password, role in (("admin", "admin123", "管理员"), ("analyst", "analyst123", "分析师")):
        if db.get(models.User, username) is None:
            db.add(models.User(username=username, password=password, role=role, token=f"token-{username}-2026"))
    db.commit()
    return {"events_created": created, "events_skipped": skipped, "total_events": db.query(models.Event).count()}
