#!/usr/bin/env python3
"""规则实验与预算归因 端到端验证。

覆盖验收点：
1. 全量与抽样在同一快照的结果对比（含全量基准、置信区间、误差）；
2. 身份待核成员列为未决样本，不按合并消费计算；
3. 审批中修改阈值/公式 -> 旧实验结果门禁失效；
4. 补传交易改变冻结水位 -> 新快照版本且保留先前预测结论；
5. 两套候选规则覆盖同一门店 -> 预算归因分别输出、不合并；
6. 抽样覆盖达不到门店误差要求 -> 不可判定且不输出精确补贴额；
7. 任一预算数字可回查实验配置/样本成员集/规则版本指纹/精度说明；
8. 发布门禁拦截未批准/参数不匹配/未决成员，并在满足条件时放行正式发布；
9. 三种计分公式确定性定级；抽样设计写入实验版本与快照；同水位快照幂等复用。

用法: python3 scripts/verify_experiments.py [base_url]
前置: 后端已启动（默认 http://127.0.0.1:8000）。脚本会重置业务数据。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
passed, failed = 0, 0
A, C = "13800000001", "13900000003"
M_CHEN6 = "13900000106"


def call(method, path, body=None, token=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} | ({"Authorization": f"Bearer {token}"} if token else {}),
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    global passed, failed
    passed, failed = passed + bool(cond), failed + (not cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  -> {detail}"))


def make_exp(token, code, formula, thresholds, *, mode="FULL", design=None, params=None, name=None):
    body = {
        "code": code, "name": name or code, "formula": formula, "thresholds": thresholds,
        "formula_params": params or ({"weight_spend": 1.0, "weight_active_store": 100.0}
                                     if formula == "SPEND_ACTIVE_COMPOSITE" else {}),
        "window_days": 90, "candidate_effective_from": "2026-07-01T00:00:00Z",
        "sampling_mode": mode, "sampling_design": design or {},
    }
    _, e = call("POST", "/api/experiments", body, token)
    return e


def shadow(token, eid, biz="2026-04-30T23:59:00Z", sys_t="2026-04-30T23:59:00Z", mode="FULL"):
    _, r = call("POST", f"/api/experiments/{eid}/shadow",
                {"biz_as_of": biz, "sys_as_of": sys_t, "mode": mode}, token)
    return r


def main():
    token = call("POST", "/api/login", {"username": "admin", "password": "admin123"})[1]["token"]
    call("POST", "/api/admin/reset", token=token)
    call("POST", "/api/seed", token=token)
    seed_exp = call("POST", "/api/seed/experiments", token=token)[1]
    check("灌入实验演示样本 13 个事件（16 名会员）", seed_exp["events_created"] == 13, seed_exp)

    # ========== 1. 全量影子分群 + 身份待核隔离 ==========
    e_full = make_exp(token, "E-FULL", "SPEND_ACTIVE_COMPOSITE",
                      {"SILVER": 400, "GOLD": 1200}, name="并重·全量")
    r_full = shadow(token, e_full["id"], mode="FULL")
    check("全量运行 kind=FULL", r_full["kind"] == "FULL", r_full["kind"])
    check("冻结成员全集=16", r_full["snapshot"]["total_population"] == 16, r_full["snapshot"])
    pending_keys = {p["member_key"] for p in r_full["snapshot"]["pending_members"]}
    check("待核成员 A、C 进入未决清单", {A, C} <= pending_keys, pending_keys)
    pend = [m for m in r_full["members"] if m["migration"] == "PENDING"]
    check("未决样本迁移=PENDING 且不计分", {m["member_key"] for m in pend} == {A, C}
          and all(m["candidate_score"] is None for m in pend), pend)
    check("未决成员不进估计框架（框架 14 人）", r_full["summary"]["frame_population"] == 14, r_full["summary"])
    check("全量评估 14 名非未决成员", r_full["summary"]["evaluated_members"] == 14, r_full["summary"])
    check("全量精度可判定、零未覆盖", r_full["precision"]["overall_decidable"] is True
          and r_full["precision"]["uncovered_members"] == 0, r_full["precision"])

    # ========== 2. 预算四维归因（迁移×门店×权益×规则指纹）==========
    attrs = r_full["attributions"]
    keys = {(a["migration"], a["store"], a["benefit_type"], a["rule_fingerprint"]) for a in attrs}
    check("预算归因行四维唯一且覆盖四类权益",
          all(k[2] in ("POINTS", "BIRTHDAY", "STORE_SUBSIDY", "BASE") for k in keys) and len(keys) == len(attrs),
          len(attrs))
    check("全量归因给精确值 exact_value", all(a["exact_value"] is not None for a in attrs), attrs[:2])
    # 逐人成本差额之和 = 归因精确值（按门店+权益+迁移核对单元；TOTAL 由四类权益相加核对）
    member_results = r_full["members"]
    sample_store = "S001"
    for benefit in ("POINTS", "BIRTHDAY", "STORE_SUBSIDY", "BASE"):
        for mig in ("UPGRADE", "RETAIN"):
            expect = round(sum(float(m["cost_delta"].get(benefit, 0)) for m in member_results
                               if m["home_store"] == sample_store and m["migration"] == mig), 2)
            row = next(a for a in attrs if a["store"] == sample_store and a["migration"] == mig
                       and a["benefit_type"] == benefit)
            check(f"归因精确值可回算 {sample_store}/{mig}/{benefit}={expect}",
                  abs(float(row["exact_value"]) - expect) < 1e-6, (row["exact_value"], expect))
    total_expect = round(sum(float(m["cost_delta"].get("TOTAL", 0)) for m in member_results), 2)
    total_attr = round(sum(float(a["exact_value"]) for a in attrs), 2)
    check("四类权益归因之和 = 逐人 TOTAL 成本差额", abs(total_attr - total_expect) < 1e-6,
          (total_attr, total_expect))

    # ========== 3. 三种计分公式确定性定级（陈六：消费1600、2 家活跃门店）==========
    e_spend = make_exp(token, "E-F-SPEND", "SPEND_ONLY", {"SILVER": 300, "GOLD": 1000})
    e_comp = make_exp(token, "E-F-COMP", "SPEND_ACTIVE_COMPOSITE", {"SILVER": 400, "GOLD": 1700})
    e_gate_pass = make_exp(token, "E-F-GATE1", "ACTIVE_STORE_GATED",
                           {"SILVER": {"spend": 300, "stores": 1}, "GOLD": {"spend": 1500, "stores": 2}})
    e_gate_block = make_exp(token, "E-F-GATE2", "ACTIVE_STORE_GATED",
                            {"SILVER": {"spend": 300, "stores": 1}, "GOLD": {"spend": 1500, "stores": 3}})
    tier = lambda r: next(m["candidate_tier"] for m in r["members"] if m["member_key"] == M_CHEN6)
    check("SPEND_ONLY：1600≥1000 -> GOLD", tier(shadow(token, e_spend["id"])) == "GOLD")
    r_comp = shadow(token, e_comp["id"])
    m_comp = next(m for m in r_comp["members"] if m["member_key"] == M_CHEN6)
    check("并重公式分=1600+2*100=1800 且阈值1700 -> GOLD",
          m_comp["candidate_score"] == 1800.0 and m_comp["candidate_tier"] == "GOLD", m_comp)
    check("双门槛达标(门店2≥2) -> GOLD", tier(shadow(token, e_gate_pass["id"])) == "GOLD")
    check("双门槛不达标(门店2<3) 回落 SILVER", tier(shadow(token, e_gate_block["id"])) == "SILVER")

    # ========== 4. 同水位快照幂等复用 ==========
    r_again = shadow(token, e_full["id"], mode="FULL")
    check("同参数重复运行复用同一快照（幂等）", r_again["snapshot_id"] == r_full["snapshot_id"],
          (r_again["snapshot_id"], r_full["snapshot_id"]))

    # ========== 5. 抽样路线 + 同快照全量基准对比 ==========
    # 5a 普查式抽样（fraction=1.0）：估计量必须等于全量精确值
    e_census = make_exp(token, "E-CENSUS", "SPEND_ACTIVE_COMPOSITE", {"SILVER": 400, "GOLD": 1200},
                        mode="STRATIFIED", design={"seed": 11, "sample_fraction": 1.0,
                                                   "max_relative_error": 0.25, "min_store_sample": 1})
    check("抽样设计写入实验版本", e_census["version"]["sampling_mode"] == "STRATIFIED"
          and e_census["version"]["sampling_design"]["sample_fraction"] == 1.0, e_census["version"])
    r_samp_census = shadow(token, e_census["id"], mode="SAMPLED")
    check("普查式抽样入样=框架 14 人", r_samp_census["snapshot"]["sample_size"] >= 14, r_samp_census["snapshot"])
    r_samp_full = shadow(token, e_census["id"], mode="FULL")  # 同实验同水位 -> 同快照
    check("抽样与全量落在同一快照", r_samp_census["snapshot_id"] == r_samp_full["snapshot_id"],
          (r_samp_census["snapshot_id"], r_samp_full["snapshot_id"]))
    _, bench = call("POST", f"/api/shadow/runs/{r_samp_census['id']}/benchmark", token=token)
    bm = bench["metrics"]["benchmark_vs_full"]
    check("基准对比已挂接全量运行", bench["benchmark_run_id"] == r_samp_full["id"], bench["benchmark_run_id"])
    check("普查抽样成本估计=全量精确（相对误差0）",
          all(v["rel_error"] == 0.0 and v["inside_ci"] for v in bm["cost_delta"].values()), bm["cost_delta"])
    check("普查抽样等级人数=全量精确",
          all(v["rel_error"] == 0.0 for v in bm["tier_population"].values()), bm["tier_population"])

    # 5b 部分抽样：产生真实未覆盖成员与置信区间
    e_part = make_exp(token, "E-PART", "SPEND_ACTIVE_COMPOSITE", {"SILVER": 400, "GOLD": 1200},
                      mode="STRATIFIED", design={"seed": 7, "sample_fraction": 0.45, "min_per_stratum": 1,
                                                 "max_relative_error": 0.25, "min_store_sample": 2})
    r_part = shadow(token, e_part["id"], mode="SAMPLED")
    check("部分抽样存在未覆盖成员", r_part["precision"]["uncovered_members"] > 0, r_part["precision"])
    check("抽样汇总带置信区间",
          all(v["ci_low"] <= v["estimate"] <= v["ci_high"]
              for v in r_part["summary"]["migration"].values() if isinstance(v["estimate"], (int, float))),
          r_part["summary"]["migration"])

    # ========== 6. 稀有门店/误差不达标 -> 不可判定，不输出精确补贴额 ==========
    e_rare = make_exp(token, "E-RARE", "SPEND_ACTIVE_COMPOSITE", {"SILVER": 400, "GOLD": 1200},
                      mode="STRATIFIED", design={"seed": 3, "sample_fraction": 0.35, "min_per_stratum": 1,
                                                 "max_relative_error": 0.25, "min_store_sample": 3})
    r_rare = shadow(token, e_rare["id"], mode="SAMPLED")
    s003 = [s for s in r_rare["precision"]["undecidable_stores"] if s["store"] == "S003"]
    check("稀有门店 S003 判定不可判定（框架仅1人，低于每店3人要求）",
          s003 and s003[0]["decidable"] is False, s003)
    check("整体精度 overall_decidable=false", r_rare["precision"]["overall_decidable"] is False)
    s003_rows = [a for a in r_rare["attributions"] if a["store"] == "S003"]
    check("不可判定门店不输出精确补贴额（estimate/exact 均为 null）",
          s003_rows and all(a["decidable"] is False and a["estimate"] is None and a["exact_value"] is None
                            for a in s003_rows), s003_rows[:2])

    # ========== 7. 同店两套候选规则 -> 预算分别输出、不合并 ==========
    e_r1 = make_exp(token, "E-RULE-A", "SPEND_ONLY", {"SILVER": 300, "GOLD": 1000})
    e_r2 = make_exp(token, "E-RULE-B", "SPEND_ONLY", {"SILVER": 400, "GOLD": 1300})
    shadow(token, e_r1["id"], mode="FULL")
    shadow(token, e_r2["id"], mode="FULL")
    _, cross = call("GET", "/api/budget/attributions?store=S001&benefit_type=POINTS", token=token)
    fingerprints = {a["rule_fingerprint"] for a in cross}
    check("同一门店 S001 被多套规则覆盖时按指纹分列（>=2），不合并", len(fingerprints) >= 2, fingerprints)

    # ========== 8. 发布门禁：未批准 / 参数失效 / 未决成员 / 放行 ==========
    # 8a 未批准
    _, g0 = call("POST", f"/api/shadow/runs/{r_full['id']}/gate", token=token)
    check("门禁拦截未批准实验", g0["passed"] is False and "GATE_NOT_APPROVED" in g0["reason_codes"], g0["reason_codes"])
    # 8b 批准后仍因未决成员拦截（4/30 水位含待核 A、C）
    call("POST", f"/api/experiments/{e_full['id']}/transition", {"target": "SUBMITTED"}, token=token)
    call("POST", f"/api/experiments/{e_full['id']}/transition", {"target": "APPROVED"}, token=token)
    _, g1 = call("POST", f"/api/shadow/runs/{r_full['id']}/gate", token=token)
    check("门禁拦截含未决成员的批准结果", g1["passed"] is False and "GATE_PENDING_MEMBERS" in g1["reason_codes"], g1["reason_codes"])
    # 8c 审批中改阈值 -> 旧结果参数失效
    call("POST", f"/api/experiments/{e_full['id']}/versions",
         {"thresholds": {"SILVER": 500, "GOLD": 1400}, "change_note": "审批后调阈值"}, token=token)
    _, g2 = call("POST", f"/api/shadow/runs/{r_full['id']}/gate", token=token)
    check("改阈值后旧结果门禁失效 GATE_STALE_PARAMS", "GATE_STALE_PARAMS" in g2["reason_codes"], g2["reason_codes"])
    exp_now = call("GET", f"/api/experiments/{e_full['id']}", token=token)[1]
    check("改参后实验回到 DRAFT 且批准版本号落后", exp_now["status"] == "DRAFT"
          and exp_now["approved_version_no"] < exp_now["current_version_no"], exp_now)

    # 8d 满足全部条件的放行发布（待核关系出现前的 3/10 水位）
    e_happy = make_exp(token, "E-HAPPY", "SPEND_ONLY", {"SILVER": 300, "GOLD": 1000})
    r_happy = shadow(token, e_happy["id"], biz="2026-03-10T23:59:00Z", sys_t="2026-03-10T23:59:00Z", mode="FULL")
    check("放行路径无未决成员", all(m["migration"] != "PENDING" for m in r_happy["members"]), r_happy["members"])
    call("POST", f"/api/experiments/{e_happy['id']}/transition", {"target": "SUBMITTED"}, token=token)
    call("POST", f"/api/experiments/{e_happy['id']}/transition", {"target": "APPROVED"}, token=token)
    code, rel = call("POST", f"/api/shadow/runs/{r_happy['id']}/release",
                     {"publication_name": "2026Q3 正式等级发布"}, token=token)
    check("门禁放行并生成正式发布", code == 201 and rel["gate"]["passed"] is True and rel["publication_id"], rel)
    pub = call("GET", f"/api/publications/{rel['publication_id']}", token=token)[1]
    check("正式发布回写实验版本/门禁且逐人物化", pub["semantics_code"] == "EXPERIMENT_RELEASE"
          and len(pub["rows"]) == r_happy["summary"]["evaluated_members"], pub)

    # ========== 9. 补传交易改变水位 -> 新快照版本，旧结论保留 ==========
    e_bf = make_exp(token, "E-BACKFILL", "SPEND_ONLY", {"SILVER": 300, "GOLD": 1000})
    rb1 = shadow(token, e_bf["id"], sys_t="2026-04-30T23:59:00Z", mode="FULL")
    rb2 = shadow(token, e_bf["id"], sys_t="2026-05-31T23:59:00Z", mode="FULL")  # 5/10 补传可见
    ver_id = e_bf["version"]["id"]
    snaps = call("GET", f"/api/experiment-versions/{ver_id}/snapshots", token=token)[1]
    check("补传后生成快照 v2 且水位推进、链接前序",
          [(s["version_no"], s["watermark_event_id"]) for s in snaps] ==
          [(1, rb1["snapshot"]["watermark_event_id"]), (2, rb2["snapshot"]["watermark_event_id"])]
          and rb2["snapshot"]["watermark_event_id"] > rb1["snapshot"]["watermark_event_id"]
          and snaps[1]["supersedes_snapshot_id"] == snaps[0]["id"], snaps)
    check("先前影子运行与预测结论保留", rb1["id"] != rb2["id"] and rb1["snapshot_id"] != rb2["snapshot_id"])
    _, g_stale = call("POST", f"/api/shadow/runs/{rb1['id']}/gate", token=token)
    check("旧快照运行门禁判 GATE_SNAPSHOT_STALE", "GATE_SNAPSHOT_STALE" in g_stale["reason_codes"], g_stale["reason_codes"])

    # ========== 10. 预算数字全链路回查 ==========
    one = next(a for a in r_full["attributions"] if a["store"] == "S001" and a["benefit_type"] == "POINTS")
    _, trace = call("GET", f"/api/budget/trace?run_id={r_full['id']}&store=S001&benefit_type=POINTS"
                           f"&migration={one['migration']}", token=token)
    check("回查含实验配置/规则版本/样本快照/成员集/精度/逐人链路",
          {"experiment", "rule_version", "sample_snapshot", "member_set", "precision", "lineage_members"} <= set(trace),
          trace.keys())
    check("回查规则指纹与运行版本一致",
          trace["rule_version"]["param_fingerprint"] == r_full["experiment_version"]["param_fingerprint"])
    check("回查成员集含全集规模与抽样设计",
          trace["member_set"]["total_population"] == 16 and "sampling_design" in trace["member_set"])
    check("回查精度说明非空且含未决披露", trace["precision"] is not None
          and {p["member_key"] for p in trace["member_set"]["pending_members"]} == {A, C})

    print(f"\n== 规则实验验证完成: {passed} 通过, {failed} 失败 ==")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
