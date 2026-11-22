#!/usr/bin/env python3
"""端到端验证：任意时点资格、两种口径差异、下游影响。

用法: python3 scripts/verify.py [base_url]
前置: 后端已启动（默认 http://127.0.0.1:8000）。脚本会重置业务数据并灌入种子场景。
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
A, B, C, D, F, G = ("13800000001", "13900000002", "13900000003",
                    "13900000004", "13900000005", "13900000006")
passed, failed = 0, 0


def call(method, path, body=None, token=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} | ({"Authorization": f"Bearer {token}"} if token else {}),
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def check(name, cond, detail=""):
    global passed, failed
    passed, failed = passed + bool(cond), failed + (not cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  -> {detail}"))


def elig(token, member, biz, semantics="AS_OF", sys_time=None):
    q = f"/api/members/{member}/eligibility?biz_time={biz}&semantics={semantics}"
    if sys_time:
        q += f"&sys_time={sys_time}"
    return call("GET", q, token=token)


def main():
    token = call("POST", "/api/login", {"username": "admin", "password": "admin123"})["token"]
    call("POST", "/api/admin/reset", token=token)
    seed = call("POST", "/api/seed", token=token)
    check("种子灌入 18 个事件", seed["events_created"] == 18, seed)

    # ---------- 1. 任意时点资格 ----------
    # Case1: 2026-02-28 当时可见：误合并仍有效 -> 簇{A,B}，v1 阈值 1000，1800 -> GOLD
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_OF", "2026-02-28T23:59:00Z")
    check("Case1 A@02-28 当时可见 = GOLD", r["tier"] == "GOLD", r)
    check("Case1 簇含 B（合并生效中）", set(r["cluster"]) == {A, B}, r["cluster"])
    check("Case1 消费 1800（含 B 的 600）", r["total_spend"] == 1800.0, r["total_spend"])

    # Case2: 同一业务时点、事后补全：拆分追溯生效 -> 簇{A}；v2 追溯(1500)适用 S001/ONLINE；
    #        v2 对 S002 被部分撤回 -> S002 用 v1。1280 / 350 -> SILVER
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("Case2 A@02-28 事后补全 = SILVER", r["tier"] == "SILVER", r)
    check("Case2 簇仅 A（误合并已追溯拆分）", r["cluster"] == [A], r["cluster"])
    vers = {s["rule_version"]: s["spend"] for s in r["scopes"]}
    check("Case2 v2 范围 1280 / v1(S002 撤回后) 350", vers.get(2) == 1280.0 and vers.get(1) == 350.0, vers)

    # Case0: 2026-01-31 当时可见 = SILVER(900)；事后补全因离线补传 T4(01-28,350) 变 GOLD(1250)
    r = elig(token, A, "2026-01-31T23:59:00Z", "AS_OF", "2026-01-31T23:59:00Z")
    check("Case0 A@01-31 当时可见 = SILVER", r["tier"] == "SILVER" and r["total_spend"] == 900.0, r)
    r = elig(token, A, "2026-01-31T23:59:00Z", "AS_KNOWN_NOW")
    check("Case0b A@01-31 事后补全 = GOLD（离线补传改判）", r["tier"] == "GOLD" and r["total_spend"] == 1250.0, r)

    # Case3/4: 2026-03-31 两个口径均 GOLD，但消费构成不同（2230 vs 1630）
    r = elig(token, A, "2026-03-31T23:59:00Z", "AS_OF", "2026-03-31T23:59:00Z")
    check("Case3 A@03-31 当时可见 = GOLD 2230（合并中+更正后+补传）", r["tier"] == "GOLD" and r["total_spend"] == 2230.0, r)
    r = elig(token, A, "2026-03-31T23:59:00Z", "AS_KNOWN_NOW")
    check("Case4 A@03-31 事后补全 = GOLD 1630（拆分后）", r["tier"] == "GOLD" and r["total_spend"] == 1630.0, r)

    # 更正链：T3 在 03-05 前为 300，之后为 380
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_OF", "2026-03-04T23:59:00Z")
    check("更正前口径含 T3=300（总 1800）", r["total_spend"] == 1800.0, r["total_spend"])
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_OF", "2026-03-06T00:00:00Z")
    check("更正后口径含 T3=380（总 1880）", r["total_spend"] == 1880.0, r["total_spend"])
    check("更正留痕 replaced_events", len(r["replaced_events"]) == 1, r["replaced_events"])

    # 其他会员：B 拆分后回落；D 被撤回保护；F 被规则修订击中；G 被撤回保护
    r = elig(token, B, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("B@02-28 事后补全 = SILVER 600（拆出后独立）", r["tier"] == "SILVER" and r["total_spend"] == 600.0, r)
    r = elig(token, D, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("D@02-28 事后补全 = GOLD 1100（S002 撤回 v2 保护）", r["tier"] == "GOLD" and r["total_spend"] == 1100.0, r)
    r = elig(token, F, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("F@02-28 事后补全 = SILVER（v2 阈值 1500 击中）", r["tier"] == "SILVER", r)
    r = elig(token, G, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("G@02-28 事后补全 = GOLD 1400（撤回保护，否则 SILVER）", r["tier"] == "GOLD", r)

    # DELTA 语义
    r = elig(token, A, "2026-02-28T23:59:00Z", "DELTA", "2026-02-28T23:59:00Z")
    check("DELTA: A@02-28 两种口径 GOLD->SILVER", r["as_of"]["tier"] == "GOLD" and r["as_known_now"]["tier"] == "SILVER" and r["tier_changed"], r)

    # ---------- 2. 发布库 + 差异归因 + 影响索引 ----------
    p1 = call("POST", "/api/publications", {"name": "2026-02月末·当时可见", "biz_as_of": "2026-02-28T23:59:00Z", "sys_as_of": "2026-02-28T23:59:00Z", "semantics_code": "AS_OF"}, token)
    pmid = call("POST", "/api/publications", {"name": "2026-02月末·3月末水位", "biz_as_of": "2026-02-28T23:59:00Z", "sys_as_of": "2026-03-31T23:59:00Z", "semantics_code": "AS_OF"}, token)
    p2 = call("POST", "/api/publications", {"name": "2026-02月末·事后补全", "biz_as_of": "2026-02-28T23:59:00Z", "sys_as_of": "2026-11-22T00:00:00Z", "semantics_code": "AS_KNOWN_NOW"}, token)
    check("发布 P1 冻结证据水位=12（2月末可见最大事件id）", p1["watermark_event_id"] == 12, p1)
    check("发布 P2 冻结规则集合版本>0 且身份图版本>0", p2["rule_set_version"] > 0 and p2["identity_graph_version"] > 0, p2)

    d_full = call("POST", "/api/diffs", {"pub_a": p1["id"], "pub_b": p2["id"]}, token)
    d_mid = call("POST", "/api/diffs", {"pub_a": pmid["id"], "pub_b": p2["id"]}, token)
    diff = call("GET", f"/api/diffs/{d_full['id']}", token=token)
    items = {i["member_key"]: i for i in diff["items"]}
    check("全量差异含 A 的降级记录", A in items and items[A]["before"]["tier"] == "GOLD" and items[A]["after"]["tier"] == "SILVER", items.get(A))
    causes_a = {c["cause"] for c in items[A]["causes"]} if A in items else set()
    check("A 的归因覆盖：迟到交易/更正/身份拆分/规则修订",
          {"LATE_TRANSACTION", "CORRECTION", "IDENTITY_SPLIT", "RULE_REVISION"} <= causes_a, causes_a)
    check("B 的归因含身份拆分", any(c["cause"] == "IDENTITY_SPLIT" for c in items.get(B, {}).get("causes", [])), items.get(B))
    check("F 的归因含规则修订", any(c["cause"] == "RULE_REVISION" for c in items.get(F, {}).get("causes", [])), items.get(F))

    diff_mid = call("GET", f"/api/diffs/{d_mid['id']}", token=token)
    items_mid = {i["member_key"]: i for i in diff_mid["items"]}
    check("中段差异：D 因撤回由 SILVER 升回 GOLD",
          D in items_mid and items_mid[D]["before"]["tier"] == "SILVER" and items_mid[D]["after"]["tier"] == "GOLD", items_mid.get(D))
    check("D 的归因含规则撤回", any(c["cause"] == "RULE_WITHDRAWAL" for c in items_mid.get(D, {}).get("causes", [])), items_mid.get(D))
    check("中段差异：A 因拆分降级", A in items_mid and items_mid[A]["after"]["tier"] == "SILVER", items_mid.get(A))

    # 影响索引
    impacts = diff["impacts"]
    mk = [i for i in impacts if i["dimension"] == "marketing" and i["member_key"] == A]
    bg = [i for i in impacts if i["dimension"] == "budget" and i["member_key"] == A]
    tr = [i for i in impacts if i["dimension"] == "training" and i["member_key"] == A]
    check("影响：A 退出 GOLD 营销名单、进入 SILVER 名单",
          any(i["entity"] == "marketing:list:GOLD" for i in mk) and any(i["entity"] == "marketing:list:SILVER" for i in mk), mk)
    check("影响：A 的门店预算条目（S001/S002/ONLINE）", len(bg) >= 2, bg)
    check("影响：A 的 2026-02 训练样本标签重述 GOLD->SILVER",
          any(i["change_type"] == "LABEL_RESTATED" and i["entity"] == "training:samples:2026-02" for i in tr), tr)
    check("影响条目携带归因事件 id", all(i["attributed_event_ids"] for i in mk + tr), mk + tr)
    ent = call("GET", "/api/impacts?entity=marketing:list:GOLD", token=token)
    check("影响索引可按下游实体反查", len(ent) >= 1, ent)

    # ---------- 3. 账本完整性 / 追加-only / 幂等 / 投影重建 ----------
    v = call("GET", "/api/admin/ledger/verify", token=token)
    check("链式哈希完整", v["ok"] and v["checked"] == 18, v)

    dup = call("POST", "/api/events", {"source": "pos", "event_type": "transaction", "biz_time": "2026-01-05T10:20:00Z",
                                       "payload": {"member_key": A, "store": "S001", "category": "OTC", "amount": 500},
                                       "idempotency_key": "seed-t1"}, token)
    check("幂等键重放不重复入账", dup["created"] is False, dup)

    # 追加-only：直接 UPDATE/DELETE events 应被触发器拒绝（本机有 psql 时验证）
    psql = shutil.which("psql") or "/opt/postgresql-18/bin/psql"
    if os.path.exists(psql):
        sql = "UPDATE events SET actor='hacker' WHERE id=1;"
        r1 = subprocess.run([psql, "postgresql://ccdev:ccdev@127.0.0.1:5432/member_bitemp", "-c", sql],
                            capture_output=True, text=True,
                            env={"PGPASSWORD": "ccdev", "PATH": "/usr/bin:/bin"})
        check("追加-only：UPDATE 被拒绝", "追加式" in (r1.stderr + r1.stdout) or r1.returncode != 0, r1.stderr[:200])
    else:
        print("SKIP  追加-only 触发器直查（无 psql，容器内验证见 README）")

    rb = call("POST", "/api/admin/rebuild", token=token)
    check("投影重建（18 事件重放）", rb["events_replayed"] == 18 and rb["identity_edges"] >= 4 and rb["rule_versions"] == 3, rb)
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("重建后资格结论一致（SILVER）", r["tier"] == "SILVER" and r["total_spend"] == 1630.0, r)

    # 并发修订解决：v2/v3 同链修订 v1，03-15 应适用 v3
    r = elig(token, F, "2026-03-15T23:59:00Z", "AS_KNOWN_NOW")
    check("并发修订确定性解决：03-15 适用 v3(1300)，F 1200 -> SILVER",
          r["tier"] == "SILVER" and all(s["rule_version"] == 3 for s in r["scopes"]), r["scopes"])

    print(f"\n== 验证完成: {passed} 通过, {failed} 失败 ==")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
