"""发布库：冻结证据水位、身份关系图版本与规则集合版本，并物化各会员资格结果。"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .eligibility import compute_eligibility

IDENTITY_TYPES = ("identity_merge", "identity_split", "identity_pending", "identity_revoke")
RULE_TYPES = ("rule_upsert", "rule_withdraw")


def create_publication(db: Session, name: str, biz_as_of: datetime, sys_as_of: datetime, semantics_code: str) -> models.Publication:
    events = db.execute(select(models.Event)).scalars().all()
    visible = [e for e in events if e.ingested_at <= sys_as_of]
    watermark = max((e.id for e in visible), default=0)
    id_ver = max((e.id for e in visible if e.event_type in IDENTITY_TYPES), default=0)
    rule_ver = max((e.id for e in visible if e.event_type in RULE_TYPES), default=0)

    pub = models.Publication(
        name=name,
        created_at=datetime.now(timezone.utc),
        biz_as_of=biz_as_of,
        sys_as_of=sys_as_of,
        watermark_event_id=watermark,
        identity_graph_version=id_ver,
        rule_set_version=rule_ver,
        semantics_code=semantics_code,
    )
    db.add(pub)
    db.flush()

    members = db.execute(select(models.MemberDirectory)).scalars().all()
    for m in members:
        res = compute_eligibility(db, m.member_key, biz_as_of, sys_as_of)
        db.add(
            models.PublicationRow(
                publication_id=pub.id,
                member_key=m.member_key,
                tier=res["tier"],
                spend=res["total_spend"],
                detail=res,
            )
        )
    db.commit()
    db.refresh(pub)
    return pub
