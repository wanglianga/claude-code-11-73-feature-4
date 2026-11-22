"""分层抽样统计：总体总量估计、方差与正态近似置信区间。

抽样框架：按 门店×消费层级×活跃度 分层，层内简单随机抽样；
未决（身份审批中）成员强制入样但不进入估计框架，单独披露。
"""
import math

Z_BY_LEVEL = {0.90: 1.6449, 0.95: 1.9600, 0.99: 2.5758}


def z_of(confidence_level: float) -> float:
    if confidence_level in Z_BY_LEVEL:
        return Z_BY_LEVEL[confidence_level]
    # 逆正态 CDF 的 rational 近似（Acklam），覆盖任意置信水平
    p = 1 - (1 - confidence_level) / 2
    return _inv_norm_cdf(p)


def _inv_norm_cdf(p: float) -> float:
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
           ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def stratified_total(members: list[dict], value_fn, confidence_level: float = 0.95) -> dict:
    """分层总量估计。

    members: [{"stratum", "frame_size", ...}] 仅入样且在估计框架内（非未决）的成员，
    每条样本标注 frame_size=该层全体成员数 N_h。
    返回 {estimate, ci_low, ci_high, se, strata: [...]}。
    """
    strata: dict[str, list[float]] = {}
    sizes: dict[str, int] = {}
    for m in members:
        h = m["stratum"]
        strata.setdefault(h, []).append(float(value_fn(m)))
        sizes[h] = int(m["frame_size"])

    estimate = 0.0
    variance = 0.0
    detail = []
    for h, ys in strata.items():
        n_h = len(ys)
        N_h = sizes[h]
        mean = sum(ys) / n_h
        s2 = sum((y - mean) ** 2 for y in ys) / (n_h - 1) if n_h > 1 else 0.0
        est_h = N_h * mean
        # 有限总体修正 (1 - n_h/N_h)
        var_h = (N_h ** 2) * (1 - n_h / N_h) * s2 / n_h if N_h > 0 else 0.0
        estimate += est_h
        variance += max(var_h, 0.0)
        detail.append({"stratum": h, "N_h": N_h, "n_h": n_h, "mean": round(mean, 4),
                       "estimate": round(est_h, 2), "variance": round(var_h, 4)})

    se = math.sqrt(variance)
    z = z_of(confidence_level)
    hw = z * se
    return {
        "estimate": round(estimate, 2),
        "se": round(se, 2),
        "ci_low": round(estimate - hw, 2),
        "ci_high": round(estimate + hw, 2),
        "half_width": round(hw, 2),
        "confidence_level": confidence_level,
        "strata": detail,
    }


def relative_half_width(stat: dict, floor: float = 1.0) -> float:
    denom = abs(stat["estimate"]) if abs(stat["estimate"]) >= floor else floor
    return abs(stat["half_width"]) / denom


def exact_total(members: list[dict], value_fn) -> dict:
    """全量精确总量（无抽样误差）。"""
    total = round(sum(float(value_fn(m)) for m in members), 2)
    return {"estimate": total, "se": 0.0, "ci_low": total, "ci_high": total,
            "half_width": 0.0, "confidence_level": 1.0, "strata": []}
