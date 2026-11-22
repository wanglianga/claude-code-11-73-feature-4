"""规则实验演示样本（独立幂等种子，不改变基线 seed 的事件 id 与既有断言）。

内容：
- 10 名额外会员（13900000101..110），覆盖 S001/S002/S003/ONLINE 与多活跃门店、多消费层级；
  其中 S003 仅 1 名会员，作为"稀有门店"触发抽样不可判定场景；
- 1 笔补传交易（业务时间在窗口内、平台 2026-05-10 才收到）：
  系统水位推进到 5 月末后冻结水位变化，样本快照器生成新版本并保留先前预测结论。

全部经接入账写入（幂等键固定，可重复执行）。
"""
from sqlalchemy.orm import Session

from . import models
from .services.ledger import ingest_event
from .seed import T

EXTRA_EVENTS = [
    dict(idem="exp-t11", source="pos", type="transaction", biz="2026-02-12T10:00:00", ing="2026-02-12T10:00:02",
         payload={"member_key": "13900000101", "member_name": "陈一", "store": "S001", "category": "OTC", "amount": 150, "channel": "pos"}),
    dict(idem="exp-t12", source="pos", type="transaction", biz="2026-02-18T10:00:00", ing="2026-02-18T10:00:02",
         payload={"member_key": "13900000102", "member_name": "陈二", "store": "S001", "category": "OTC", "amount": 450, "channel": "pos"}),
    dict(idem="exp-t13", source="online", type="transaction", biz="2026-03-02T20:00:00", ing="2026-03-02T20:00:03",
         payload={"member_key": "13900000102", "member_name": "陈二", "store": "ONLINE", "category": "保健", "amount": 200, "channel": "app"}),
    dict(idem="exp-t14", source="pos", type="transaction", biz="2026-03-05T10:00:00", ing="2026-03-05T10:00:02",
         payload={"member_key": "13900000103", "member_name": "陈三", "store": "S001", "category": "RX", "amount": 800, "channel": "pos"}),
    dict(idem="exp-t15", source="pos", type="transaction", biz="2026-02-22T10:00:00", ing="2026-02-22T10:00:02",
         payload={"member_key": "13900000104", "member_name": "陈四", "store": "S002", "category": "OTC", "amount": 250, "channel": "pos"}),
    dict(idem="exp-t16", source="pos", type="transaction", biz="2026-03-08T10:00:00", ing="2026-03-08T10:00:02",
         payload={"member_key": "13900000105", "member_name": "陈五", "store": "S002", "category": "OTC", "amount": 950, "channel": "pos"}),
    dict(idem="exp-t17", source="pos", type="transaction", biz="2026-03-12T10:00:00", ing="2026-03-12T10:00:02",
         payload={"member_key": "13900000106", "member_name": "陈六", "store": "S001", "category": "OTC", "amount": 1300, "channel": "pos"}),
    dict(idem="exp-t18", source="pos", type="transaction", biz="2026-03-15T10:00:00", ing="2026-03-15T10:00:02",
         payload={"member_key": "13900000106", "member_name": "陈六", "store": "S002", "category": "OTC", "amount": 300, "channel": "pos"}),
    dict(idem="exp-t19", source="online", type="transaction", biz="2026-03-18T20:00:00", ing="2026-03-18T20:00:03",
         payload={"member_key": "13900000107", "member_name": "陈七", "store": "ONLINE", "category": "保健", "amount": 600, "channel": "app"}),
    # 稀有门店 S003：全量仅 1 名会员
    dict(idem="exp-t20", source="pos", type="transaction", biz="2026-03-20T10:00:00", ing="2026-03-20T10:00:02",
         payload={"member_key": "13900000108", "member_name": "陈八", "store": "S003", "category": "OTC", "amount": 700, "channel": "pos"}),
    dict(idem="exp-t21", source="pos", type="transaction", biz="2026-03-22T10:00:00", ing="2026-03-22T10:00:02",
         payload={"member_key": "13900000109", "member_name": "陈九", "store": "S001", "category": "OTC", "amount": 350, "channel": "pos"}),
    dict(idem="exp-t22", source="pos", type="transaction", biz="2026-03-25T10:00:00", ing="2026-03-25T10:00:02",
         payload={"member_key": "13900000110", "member_name": "陈十", "store": "S002", "category": "OTC", "amount": 500, "channel": "pos"}),
    # 补传交易：业务时间 2026-03-26，平台 2026-05-10 才收到 -> 推进 5 月末冻结水位
    dict(idem="exp-backfill-1", source="franchise_offline", type="transaction", biz="2026-03-26T15:00:00", ing="2026-05-10T08:00:00",
         payload={"member_key": "13900000101", "member_name": "陈一", "store": "S001", "category": "OTC", "amount": 400,
                  "channel": "offline_batch", "batch": "S001-2026-05"}),
]


def seed_experiments(db: Session) -> dict:
    created, skipped = 0, 0
    for spec in EXTRA_EVENTS:
        _ev, was_created = ingest_event(
            db,
            source=spec["source"],
            event_type=spec["type"],
            biz_time=T(spec["biz"]),
            ingested_at=T(spec["ing"]),
            payload=spec["payload"],
            actor="seed-experiments",
            idempotency_key=spec["idem"],
        )
        created += 1 if was_created else 0
        skipped += 0 if was_created else 1
    db.commit()
    return {"events_created": created, "events_skipped": skipped,
            "total_events": db.query(models.Event).count()}
