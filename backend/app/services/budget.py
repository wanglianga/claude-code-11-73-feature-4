"""预算归因器：按 升级降级 × 门店 × 权益类型 × 规则参数 拆分成本变化。

红线：
- 同一门店被两套候选规则覆盖时，按 rule_fingerprint 分别输出影响行，绝不合并为单一成本；
- 全量路线给精确值（exact_value），抽样路线给点估计 + 置信区间（estimate/ci_*）；
- 抽样覆盖达不到门店误差要求时 decidable=false、estimate=null，报告不可判定，不输出精确补贴额；
- 未决（身份审批中）成员不进入估计框架，其成本不计入、单独披露；
- 抽样域估计：某 (迁移,门店) 组的总体总量在全部分层样本上估计，非组成员取值 0，
  避免把入样成员误当成该组总体。
"""
from . import stats
from .scoring import BENEFIT_TYPES, MIGRATIONS

DOMAIN_MIGRATIONS = tuple(m for m in MIGRATIONS if m != "PENDING")


def plan_attributions(
    *,
    mode: str,
    frame_cells: list[dict],
    sampled_cells: list[dict],
    frame_size_by_stratum: dict[str, int],
    confidence_level: float,
    undecidable_stores: set[str],
    rule_fingerprint: str,
) -> list[dict]:
    """生成预算归因行。

    frame_cells：估计框架内全部成员的轻量单元（home_store/stratum），用于确定门店域；
    sampled_cells：实际逐人评估的成员单元（FULL=全体，SAMPLED=入样成员），含 migration/cost_delta。
    分组键：(migration, home_store, benefit_type)。
    """
    all_stores = sorted({c["home_store"] for c in frame_cells})
    enriched = [{**c, "frame_size": frame_size_by_stratum.get(c["stratum"], 1)} for c in sampled_cells]

    rows: list[dict] = []
    for store in all_stores:
        for migration in DOMAIN_MIGRATIONS:
            for benefit in BENEFIT_TYPES:
                decidable = store not in undecidable_stores

                if mode == "FULL":
                    members = [
                        c for c in enriched
                        if c["home_store"] == store and c["migration"] == migration
                    ]
                    total = round(sum(float(c["cost_delta"].get(benefit, 0.0)) for c in members), 2)
                    rows.append({
                        "migration": migration, "store": store, "benefit_type": benefit,
                        "exact_value": total, "estimate": None, "ci_low": None, "ci_high": None,
                        "member_count": len(members), "decidable": True,
                        "note": "全量成员集精确值", "rule_fingerprint": rule_fingerprint,
                    })
                    continue

                # 抽样路线：门店不可判定 -> 不输出精确补贴额
                if not decidable:
                    rows.append({
                        "migration": migration, "store": store, "benefit_type": benefit,
                        "exact_value": None, "estimate": None, "ci_low": None, "ci_high": None,
                        "member_count": 0, "decidable": False,
                        "note": "不可判定：该门店抽样覆盖不足或误差超过容忍阈值，未输出精确补贴额",
                        "rule_fingerprint": rule_fingerprint,
                    })
                    continue

                def domain_value(c, b=benefit, mg=migration, st=store):
                    if c["home_store"] == st and c["migration"] == mg:
                        return float(c["cost_delta"].get(b, 0.0))
                    return 0.0

                def domain_indicator(c, mg=migration, st=store):
                    return 1.0 if c["home_store"] == st and c["migration"] == mg else 0.0

                stat = stats.stratified_total(enriched, domain_value, confidence_level)
                count_stat = stats.stratified_total(enriched, domain_indicator, confidence_level)
                rows.append({
                    "migration": migration, "store": store, "benefit_type": benefit,
                    "exact_value": None,
                    "estimate": stat["estimate"], "ci_low": stat["ci_low"], "ci_high": stat["ci_high"],
                    "member_count": max(int(round(count_stat["estimate"])), 0),
                    "decidable": True,
                    "note": f"分层抽样域估计，{int(confidence_level * 100)}% 置信区间",
                    "rule_fingerprint": rule_fingerprint,
                })
    return rows
