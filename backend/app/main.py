"""双时间资格历史服务 API。"""
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import models
from .db import engine, get_db
from .deps import current_user
from .experiments_api import router as experiments_router
from .seed import seed as run_seed
from .seed_experiments import seed_experiments as run_seed_experiments
from .services import diff as diff_svc
from .services import ledger
from .services.eligibility import compute_eligibility
from .services.identity import cluster_at
from .services.publications import create_publication

app = FastAPI(title="双时间会员资格历史服务", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(experiments_router)


@app.on_event("startup")
def startup():
    models.Base.metadata.create_all(engine)
    # 接入账追加-only：禁止 UPDATE/DELETE（更正只能以新事件 + 修订链追加）
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE OR REPLACE FUNCTION events_append_only() RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'events 账为追加式：禁止 %s，请以新事件修订', TG_OP;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS events_no_update ON events"))
        conn.execute(
            text(
                """
                CREATE TRIGGER events_no_update BEFORE UPDATE OR DELETE ON events
                FOR EACH ROW EXECUTE FUNCTION events_append_only();
                """
            )
        )
    # 演示账号（避免"seed 需登录、账号由 seed 创建"的死锁）
    db = next(get_db())
    try:
        for username, password, role in (("admin", "admin123", "管理员"), ("analyst", "analyst123", "分析师")):
            if db.get(models.User, username) is None:
                db.add(models.User(username=username, password=password, role=role, token=f"token-{username}-2026"))
        db.commit()
        # 一键演示：AUTO_SEED=1 且账本为空时自动灌入演示场景
        import os

        if os.environ.get("AUTO_SEED", "0") == "1" and db.query(models.Event).count() == 0:
            run_seed(db)
    finally:
        db.close()


# ---------------- 认证 ----------------
class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.get(models.User, body.username)
    if not user or user.password != body.password:
        raise HTTPException(401, "账号或密码错误")
    return {"token": user.token, "username": user.username, "role": user.role}


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------- 工具 ----------------
def parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def event_json(e: models.Event) -> dict:
    return {
        "id": e.id,
        "idempotency_key": e.idempotency_key,
        "source": e.source,
        "event_type": e.event_type,
        "source_hash": e.source_hash,
        "chain_hash": e.chain_hash,
        "biz_time": e.biz_time.isoformat(),
        "ingested_at": e.ingested_at.isoformat(),
        "actor": e.actor,
        "supersedes_id": e.supersedes_id,
        "payload": e.payload,
    }


# ---------------- 接入账 ----------------
class EventIn(BaseModel):
    source: str
    event_type: str
    biz_time: str
    payload: dict
    actor: str | None = None
    supersedes_id: int | None = None
    idempotency_key: str | None = None
    ingested_at: str | None = None  # 演示/补录用途；生产由平台时钟生成


@app.post("/api/events", status_code=201)
def post_event(body: EventIn, db: Session = Depends(get_db), user=Depends(current_user)):
    try:
        ev, created = ledger.ingest_event(
            db,
            source=body.source,
            event_type=body.event_type,
            biz_time=parse_dt(body.biz_time),
            ingested_at=parse_dt(body.ingested_at) if body.ingested_at else None,
            payload=body.payload,
            actor=body.actor or user.username,
            supersedes_id=body.supersedes_id,
            idempotency_key=body.idempotency_key,
        )
    except Exception as exc:  # 载荷缺字段等
        raise HTTPException(400, f"事件写入失败: {exc}")
    return {"created": created, "event": event_json(ev)}


@app.get("/api/events")
def list_events(event_type: str | None = None, member: str | None = None, db: Session = Depends(get_db), user=Depends(current_user)):
    q = select(models.Event).order_by(models.Event.id)
    events = db.execute(q).scalars().all()
    out = []
    for e in events:
        if event_type and e.event_type != event_type:
            continue
        if member and member not in str(e.payload):
            continue
        out.append(event_json(e))
    return out


# ---------------- 时间语义目录 ----------------
@app.get("/api/semantics")
def list_semantics(db: Session = Depends(get_db), user=Depends(current_user)):
    rows = db.execute(select(models.Semantics)).scalars().all()
    return [
        {"code": s.code, "name": s.name, "biz_policy": s.biz_policy, "sys_policy": s.sys_policy, "description": s.description}
        for s in rows
    ]


# ---------------- 会员与资格 ----------------
@app.get("/api/members")
def list_members(db: Session = Depends(get_db), user=Depends(current_user)):
    rows = db.execute(select(models.MemberDirectory).order_by(models.MemberDirectory.member_key)).scalars().all()
    return [{"member_key": m.member_key, "display_name": m.display_name} for m in rows]


@app.get("/api/members/{key}/eligibility")
def get_eligibility(
    key: str,
    biz_time: str,
    sys_time: str | None = None,
    semantics: str = "AS_OF",
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    biz_t = parse_dt(biz_time)
    now = datetime.now(timezone.utc)
    if semantics == "AS_OF":
        sys_t = parse_dt(sys_time) if sys_time else biz_t
        return compute_eligibility(db, key, biz_t, sys_t) | {"semantics": "AS_OF"}
    if semantics == "AS_KNOWN_NOW":
        sys_t = parse_dt(sys_time) if sys_time else now
        return compute_eligibility(db, key, biz_t, sys_t) | {"semantics": "AS_KNOWN_NOW"}
    if semantics == "DELTA":
        sys_then = parse_dt(sys_time) if sys_time else biz_t
        a = compute_eligibility(db, key, biz_t, sys_then)
        b = compute_eligibility(db, key, biz_t, now)
        return {
            "semantics": "DELTA",
            "member_key": key,
            "biz_as_of": biz_t.isoformat(),
            "as_of": a,
            "as_known_now": b,
            "tier_changed": a["tier"] != b["tier"],
            "spend_delta": round(b["total_spend"] - a["total_spend"], 2),
        }
    raise HTTPException(400, f"未知语义口径: {semantics}")


@app.get("/api/members/{key}/timeline")
def get_timeline(
    key: str,
    biz_from: str,
    biz_to: str,
    sys_time: str | None = None,
    points: int = 12,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    start, end = parse_dt(biz_from), parse_dt(biz_to)
    sys_t = parse_dt(sys_time) if sys_time else datetime.now(timezone.utc)
    step = (end - start) / max(points - 1, 1)
    out = []
    for i in range(points):
        t = start + step * i
        res = compute_eligibility(db, key, t, sys_t)
        out.append({"biz_time": t.isoformat(), "tier": res["tier"], "spend": res["total_spend"]})
    return out


@app.get("/api/members/{key}/cluster")
def get_cluster(key: str, biz_time: str, sys_time: str | None = None, db: Session = Depends(get_db), user=Depends(current_user)):
    biz_t = parse_dt(biz_time)
    sys_t = parse_dt(sys_time) if sys_time else datetime.now(timezone.utc)
    return {"member_key": key, "biz_as_of": biz_t.isoformat(), "sys_as_of": sys_t.isoformat(), "cluster": sorted(cluster_at(db, key, biz_t, sys_t))}


# ---------------- 身份关系图 ----------------
@app.get("/api/identity/edges")
def list_edges(db: Session = Depends(get_db), user=Depends(current_user)):
    edges = db.execute(select(models.IdentityEdge).order_by(models.IdentityEdge.id)).scalars().all()
    return [
        {
            "id": e.id,
            "relation_event_id": e.relation_event_id,
            "member_a": e.member_a,
            "member_b": e.member_b,
            "relation": e.relation,
            "status": e.status,
            "biz_from": e.biz_from.isoformat(),
            "biz_to": e.biz_to.isoformat() if e.biz_to else None,
            "sys_from": e.sys_from.isoformat(),
            "sys_to": e.sys_to.isoformat() if e.sys_to else None,
            "target_edge_id": e.target_edge_id,
            "reason": e.reason,
        }
        for e in edges
    ]


# ---------------- 规则 ----------------
@app.get("/api/rules/versions")
def list_rule_versions(db: Session = Depends(get_db), user=Depends(current_user)):
    rows = db.execute(select(models.RuleVersion).order_by(models.RuleVersion.id)).scalars().all()
    return [
        {
            "id": r.id,
            "rule_event_id": r.rule_event_id,
            "rule_id": r.rule_id,
            "version": r.version,
            "supersedes_version": r.supersedes_version,
            "store_scope": r.store_scope,
            "product_scope": r.product_scope,
            "window_days": r.window_days,
            "thresholds": r.thresholds,
            "effective_from": r.effective_from.isoformat(),
            "effective_to": r.effective_to.isoformat() if r.effective_to else None,
            "sys_from": r.sys_from.isoformat(),
        }
        for r in rows
    ]


@app.get("/api/rules/withdrawals")
def list_withdrawals(db: Session = Depends(get_db), user=Depends(current_user)):
    rows = db.execute(select(models.RuleWithdrawal).order_by(models.RuleWithdrawal.id)).scalars().all()
    return [
        {
            "id": w.id,
            "withdraw_event_id": w.withdraw_event_id,
            "rule_id": w.rule_id,
            "target_version": w.target_version,
            "store_scope": w.store_scope,
            "withdraw_from": w.withdraw_from.isoformat(),
            "withdraw_to": w.withdraw_to.isoformat() if w.withdraw_to else None,
            "sys_from": w.sys_from.isoformat(),
            "reason": w.reason,
        }
        for w in rows
    ]


# ---------------- 发布库 / 差异 / 影响 ----------------
class PublicationIn(BaseModel):
    name: str
    biz_as_of: str
    sys_as_of: str
    semantics_code: str = "AS_OF"


@app.post("/api/publications", status_code=201)
def post_publication(body: PublicationIn, db: Session = Depends(get_db), user=Depends(current_user)):
    pub = create_publication(db, body.name, parse_dt(body.biz_as_of), parse_dt(body.sys_as_of), body.semantics_code)
    return pub_json(pub)


def pub_json(p: models.Publication) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "created_at": p.created_at.isoformat(),
        "biz_as_of": p.biz_as_of.isoformat(),
        "sys_as_of": p.sys_as_of.isoformat(),
        "watermark_event_id": p.watermark_event_id,
        "identity_graph_version": p.identity_graph_version,
        "rule_set_version": p.rule_set_version,
        "semantics_code": p.semantics_code,
    }


@app.get("/api/publications")
def list_publications(db: Session = Depends(get_db), user=Depends(current_user)):
    rows = db.execute(select(models.Publication).order_by(models.Publication.id)).scalars().all()
    return [pub_json(p) for p in rows]


@app.get("/api/publications/{pid}")
def get_publication(pid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    pub = db.get(models.Publication, pid)
    if not pub:
        raise HTTPException(404, "发布不存在")
    rows = db.execute(
        select(models.PublicationRow).where(models.PublicationRow.publication_id == pid)
    ).scalars().all()
    return pub_json(pub) | {
        "rows": [
            {"member_key": r.member_key, "tier": r.tier, "spend": float(r.spend), "detail": r.detail}
            for r in rows
        ]
    }


class DiffIn(BaseModel):
    pub_a: int
    pub_b: int


@app.post("/api/diffs", status_code=201)
def post_diff(body: DiffIn, db: Session = Depends(get_db), user=Depends(current_user)):
    try:
        d = diff_svc.create_diff(db, body.pub_a, body.pub_b)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"id": d.id, "pub_a": d.pub_a, "pub_b": d.pub_b, "created_at": d.created_at.isoformat()}


@app.get("/api/diffs/{did}")
def get_diff(did: int, db: Session = Depends(get_db), user=Depends(current_user)):
    d = db.get(models.Diff, did)
    if not d:
        raise HTTPException(404, "差异不存在")
    items = db.execute(select(models.DiffItem).where(models.DiffItem.diff_id == did)).scalars().all()
    impacts = db.execute(select(models.Impact).where(models.Impact.diff_id == did)).scalars().all()
    return {
        "id": d.id,
        "pub_a": d.pub_a,
        "pub_b": d.pub_b,
        "created_at": d.created_at.isoformat(),
        "items": [
            {"member_key": i.member_key, "before": i.before, "after": i.after, "causes": i.causes}
            for i in items
        ],
        "impacts": [impact_json(x) for x in impacts],
    }


def impact_json(x: models.Impact) -> dict:
    return {
        "id": x.id,
        "diff_id": x.diff_id,
        "dimension": x.dimension,
        "entity": x.entity,
        "member_key": x.member_key,
        "change_type": x.change_type,
        "before": x.before,
        "after": x.after,
        "attributed_event_ids": x.attributed_event_ids,
    }


@app.get("/api/impacts")
def list_impacts(dimension: str | None = None, entity: str | None = None, db: Session = Depends(get_db), user=Depends(current_user)):
    q = select(models.Impact).order_by(models.Impact.id)
    rows = db.execute(q).scalars().all()
    return [
        impact_json(x)
        for x in rows
        if (not dimension or x.dimension == dimension) and (not entity or x.entity == entity)
    ]


# ---------------- 管理：投影重建 / 链校验 / 种子 ----------------
@app.post("/api/admin/rebuild")
def rebuild(db: Session = Depends(get_db), user=Depends(current_user)):
    return ledger.rebuild_projections(db)


@app.get("/api/admin/ledger/verify")
def verify_ledger(db: Session = Depends(get_db), user=Depends(current_user)):
    return ledger.verify_chain(db)


@app.post("/api/seed")
def seed_endpoint(db: Session = Depends(get_db), user=Depends(current_user)):
    return run_seed(db)


@app.post("/api/seed/experiments")
def seed_experiments_endpoint(db: Session = Depends(get_db), user=Depends(current_user)):
    """规则实验演示样本：额外会员（含稀有门店 S003）与改变冻结水位的补传交易。"""
    return run_seed_experiments(db)


@app.post("/api/admin/reset")
def reset_all(db: Session = Depends(get_db), user=Depends(current_user)):
    """演示/验证用途：清空业务数据（保留账号与语义目录）。

    events 账受追加-only 触发器保护，此处显式以维护角色临时停用触发器后清空。
    """
    db.execute(text("SET session_replication_role = 'replica'"))
    db.execute(
        text(
            "TRUNCATE events, identity_edges, rule_versions, rule_withdrawals, member_directory, "
            "publications, publication_rows, diffs, diff_items, impacts, "
            "rule_experiments, rule_experiment_versions, sample_snapshots, sample_snapshot_members, "
            "shadow_runs, shadow_member_results, budget_attributions, precision_reports, release_gates "
            "RESTART IDENTITY CASCADE"
        )
    )
    db.execute(text("SET session_replication_role = 'origin'"))
    db.commit()
    return {"ok": True}


@app.get("/api/overview")
def overview(db: Session = Depends(get_db), user=Depends(current_user)):
    return {
        "events": db.query(models.Event).count(),
        "members": db.query(models.MemberDirectory).count(),
        "identity_edges": db.query(models.IdentityEdge).count(),
        "rule_versions": db.query(models.RuleVersion).count(),
        "rule_withdrawals": db.query(models.RuleWithdrawal).count(),
        "publications": db.query(models.Publication).count(),
        "diffs": db.query(models.Diff).count(),
        "impacts": db.query(models.Impact).count(),
        "experiments": db.query(models.RuleExperiment).count(),
        "experiment_versions": db.query(models.RuleExperimentVersion).count(),
        "snapshots": db.query(models.SampleSnapshot).count(),
        "shadow_runs": db.query(models.ShadowRun).count(),
        "budget_attributions": db.query(models.BudgetAttribution).count(),
        "precision_reports": db.query(models.PrecisionReport).count(),
        "release_gates": db.query(models.ReleaseGate).count(),
        "ledger": ledger.verify_chain(db),
    }
