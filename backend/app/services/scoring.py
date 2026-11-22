"""候选规则计分引擎（确定性）与权益成本模型。

三种计分公式（实验版本 formula 字段）：
- SPEND_ONLY：消费额优先（现行口径），score = 窗口消费额，阈值即消费额门槛；
- SPEND_ACTIVE_COMPOSITE：消费额与活跃门店数并重，
  score = w_spend*消费额 + w_store*活跃门店数 (+ w_activity*交易笔数)，阈值为综合分门槛；
- ACTIVE_STORE_GATED：双门槛，等级阈值同时给出消费额与活跃门店数，两项都达标才晋级。

全部计算只依赖快照口径下可见的事件重放结果，不读取任何"当前主档"；
同样的输入（特征 + 参数）必然得到同样的分数、等级与成本。
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .eligibility import TIER_ORDER, tier_for_spend, tier_rank, _visible_transactions
from .identity import cluster_at

FORMULAS = ("SPEND_ONLY", "SPEND_ACTIVE_COMPOSITE", "ACTIVE_STORE_GATED")
BENEFIT_TYPES = ("POINTS", "BIRTHDAY", "STORE_SUBSIDY", "BASE")
MIGRATIONS = ("UPGRADE", "DOWNGRADE", "RETAIN", "PENDING")
DEFAULT_SPEND_BANDS = [0, 300, 1000, 1500]


def member_features(
    db: Session,
    member_key: str,
    biz_t: datetime,
    sys_t: datetime,
    window_days: int,
    product_scope="ALL",
) -> dict:
    """冻结口径下的成员特征：身份簇、窗口消费、活跃门店数、门店归属与消费层级。"""
    cluster = cluster_at(db, member_key, biz_t, sys_t)
    txns, replaced = _visible_transactions(db, cluster, biz_t, sys_t)
    window_start = biz_t - timedelta(days=window_days)

    in_window = [
        t
        for t in txns
        if window_start <= t.biz_time <= biz_t
        and _product_ok(product_scope, t.payload.get("category", ""))
    ]
    spend_by_store: dict[str, float] = {}
    txn_count = 0
    for t in in_window:
        store = t.payload.get("store", "UNKNOWN")
        spend_by_store[store] = spend_by_store.get(store, 0.0) + float(t.payload.get("amount", 0))
        txn_count += 1

    total_spend = round(sum(spend_by_store.values()), 2)
    stores = sorted(spend_by_store)
    home_store = max(spend_by_store, key=lambda s: (spend_by_store[s], s)) if spend_by_store else "UNKNOWN"
    return {
        "member_key": member_key,
        "cluster": sorted(cluster),
        "total_spend": total_spend,
        "spend_by_store": spend_by_store,
        "active_store_count": len(stores),
        "stores": stores,
        "home_store": home_store,
        "txn_count": txn_count,
        "window_from": window_start.isoformat(),
        "window_to": biz_t.isoformat(),
        "replaced_events": replaced,
        "birthday_eligible": _birthday_eligible(in_window),
    }


def _product_ok(product_scope, category: str) -> bool:
    return product_scope == "ALL" or (isinstance(product_scope, list) and category in product_scope)


def _birthday_eligible(txns: list) -> bool:
    """生日券资格：交易载荷显式标记 birthday_flag 时按标记，否则默认有生日资格（模拟假设写在权益配置）。"""
    for t in txns:
        if t.payload.get("birthday_flag") is False:
            return False
    return True


def spend_band(spend: float, bands: list[float] | None = None) -> str:
    """消费层级分层键。"""
    bands = bands or DEFAULT_SPEND_BANDS
    label = f"<{bands[1]}" if len(bands) > 1 else f"<{bands[0]}"
    for i in range(len(bands) - 1):
        if bands[i] <= spend < bands[i + 1]:
            label = f"[{bands[i]},{bands[i + 1]})"
    if spend >= bands[-1]:
        label = f">={bands[-1]}"
    return label


def activity_band(active_stores: int) -> str:
    """活跃度分层键（按活跃门店数）。"""
    if active_stores <= 0:
        return "A0"
    if active_stores == 1:
        return "A1"
    if active_stores == 2:
        return "A2"
    return "A3+"


def score_member(features: dict, formula: str, formula_params: dict, thresholds: dict) -> dict:
    """按候选公式计分并定级。返回分数、构成分量与等级。"""
    spend = features["total_spend"]
    stores = features["active_store_count"]
    txn_count = features["txn_count"]

    if formula == "SPEND_ONLY":
        score = spend
        tier = tier_for_spend(thresholds, score)
        components = {"spend": round(spend, 2)}
    elif formula == "SPEND_ACTIVE_COMPOSITE":
        w_spend = float(formula_params.get("weight_spend", 1.0))
        w_store = float(formula_params.get("weight_active_store", 100.0))
        w_activity = float(formula_params.get("weight_txn", 0.0))
        score = round(w_spend * spend + w_store * stores + w_activity * txn_count, 2)
        tier = tier_for_spend(thresholds, score)
        components = {
            "spend_term": round(w_spend * spend, 2),
            "active_store_term": round(w_store * stores, 2),
            "txn_term": round(w_activity * txn_count, 2),
            "weights": {"spend": w_spend, "active_store": w_store, "txn": w_activity},
        }
    elif formula == "ACTIVE_STORE_GATED":
        # thresholds: {"SILVER": {"spend": 300, "stores": 1}, "GOLD": {"spend": 1000, "stores": 2}, ...}
        score = spend  # 主分仍为消费额，门店数作为门槛
        tier = "NONE"
        for t in TIER_ORDER:
            if t not in thresholds:
                continue
            gate = thresholds[t]
            need_spend = float(gate.get("spend", 0))
            need_stores = int(gate.get("stores", 1))
            if spend >= need_spend and stores >= need_stores and tier_rank(t) > tier_rank(tier):
                tier = t
        components = {"spend": round(spend, 2), "active_stores": stores, "gates": thresholds}
    else:
        raise ValueError(f"未知计分公式: {formula}（支持 {FORMULAS}）")

    return {
        "formula": formula,
        "score": round(float(score), 2),
        "tier": tier,
        "components": components,
        "thresholds": thresholds,
    }


def is_boundary_score(score: float, thresholds: dict, margin_ratio: float = 0.05) -> bool:
    """边界阈值成员：综合分落在任一等级阈值 ±margin_ratio 比例带内（迁移结论对参数最敏感）。"""
    for v in thresholds.values():
        th = float(v.get("spend", 0.0)) if isinstance(v, dict) else float(v)
        if th <= 0:
            continue
        if abs(score - th) <= th * margin_ratio:
            return True
    return False


def benefit_cost(tier: str, features: dict, benefit_config: dict) -> dict:
    """按等级与成员特征计算四类权益成本（确定性模拟，参数来自实验版本）。

    POINTS：积分成本 = 积分率(等级) × 窗口消费 × 积分单价；
    BIRTHDAY：生日券面额（按生日资格开关）；
    STORE_SUBSIDY：门店补贴 = 补贴率(等级) × 窗口消费；
    BASE：等级基础运营成本（固定）。
    """
    spend = features["total_spend"]
    points_cfg = benefit_config.get("points", {})
    birthday_cfg = benefit_config.get("birthday", {})
    subsidy_cfg = benefit_config.get("store_subsidy", {})
    base_cfg = benefit_config.get("base", {})

    point_rate = float(points_cfg.get("rates", {}).get(tier, 0))
    point_value = float(points_cfg.get("point_value", 1.0))
    points = round(point_rate * spend * point_value, 2)

    voucher = float(birthday_cfg.get("voucher", {}).get(tier, 0))
    eligible_rate = float(birthday_cfg.get("eligible_rate", 1.0))
    birthday = round(voucher * eligible_rate, 2) if features.get("birthday_eligible", True) else 0.0

    subsidy_rate = float(subsidy_cfg.get("rates", {}).get(tier, 0))
    store_subsidy = round(subsidy_rate * spend, 2)

    base = round(float(base_cfg.get("fee", {}).get(tier, 0)), 2)
    return {
        "POINTS": points,
        "BIRTHDAY": birthday,
        "STORE_SUBSIDY": store_subsidy,
        "BASE": base,
        "TOTAL": round(points + birthday + store_subsidy + base, 2),
    }


def cost_delta(cost_current: dict, cost_candidate: dict) -> dict:
    """逐权益类型差额（候选 - 现行）。"""
    out = {}
    for k in BENEFIT_TYPES + ("TOTAL",):
        out[k] = round(float(cost_candidate.get(k, 0)) - float(cost_current.get(k, 0)), 2)
    return out


def default_benefit_config() -> dict:
    """演示用权益参数：等级越高积分率/补贴率/券面额越高。"""
    return {
        "points": {
            "point_value": 1.0,
            "rates": {"NONE": 0.0, "SILVER": 0.01, "GOLD": 0.02, "PLATINUM": 0.03},
        },
        "birthday": {
            "eligible_rate": 1.0,
            "voucher": {"NONE": 0, "SILVER": 20, "GOLD": 50, "PLATINUM": 100},
        },
        "store_subsidy": {
            "rates": {"NONE": 0.0, "SILVER": 0.005, "GOLD": 0.01, "PLATINUM": 0.02},
        },
        "base": {
            "fee": {"NONE": 0, "SILVER": 10, "GOLD": 25, "PLATINUM": 50},
        },
    }
