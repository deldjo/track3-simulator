#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
metrics.py —— 两套指标：用户 5 项代理指标 + 官方 4 项风格化事实门控。

官方公式逐行复刻自 qfbench2_common/scoring/stylized_facts.py 与
qfbench2_track_simulation/semantics.py（mid_price_series / depth_histogram）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

FILL_MSG_TYPES = {"ORDER_FILLED", "PARTIAL_FILL"}


# ---------------------------------------------------------------------------
# 收益序列抽取
# ---------------------------------------------------------------------------
def order_filled_returns(df: pd.DataFrame) -> np.ndarray:
    """用户口径：ORDER_FILLED 价格按时间排序后的对数收益。"""
    fills = df[df["msg_type"] == "ORDER_FILLED"].sort_values("t_ns")
    prices = fills["price"].values
    if len(prices) < 2:
        return np.array([])
    return np.diff(np.log(prices))


def mid_price_series(df: pd.DataFrame) -> pd.Series:
    """官方口径：QUOTE_UPDATE bid/ask ffill 配对取中价；fill 作 fallback。"""
    quotes = df[df["msg_type"] == "QUOTE_UPDATE"]
    if len(quotes) >= 2:
        bids = quotes[quotes["side"] == "BID"].set_index("t_ns")["price"]
        asks = quotes[quotes["side"] == "ASK"].set_index("t_ns")["price"]
        combined = pd.DataFrame({"bid": bids, "ask": asks}).ffill().dropna()
        if len(combined) >= 2:
            mid = (combined["bid"].astype(float) + combined["ask"].astype(float)) / 2.0
            return mid.reset_index(drop=True)
    fills = df[df["msg_type"].isin(FILL_MSG_TYPES)]
    return fills["price"].dropna().astype(float).reset_index(drop=True)


def mid_returns(df: pd.DataFrame) -> np.ndarray:
    mid = mid_price_series(df)
    return np.diff(np.log(mid.values))


def depth_histogram(df: pd.DataFrame, n_bins: int = 20) -> np.ndarray | None:
    quotes = df[df["msg_type"] == "QUOTE_UPDATE"]
    sizes = quotes["size"].dropna().astype(float).to_numpy()
    if len(sizes) < 10:
        return None
    counts, _ = np.histogram(sizes, bins=n_bins, density=False)
    total = counts.sum()
    return counts.astype(float) / total if total else None


# ---------------------------------------------------------------------------
# 官方风格化事实
# ---------------------------------------------------------------------------
def _acf(x: np.ndarray, lags):
    x = x - x.mean()
    denom = np.dot(x, x)
    if denom == 0.0:
        raise ValueError("acf undefined for constant series")
    out = np.empty(len(lags))
    for i, k in enumerate(lags):
        out[i] = 1.0 if k == 0 else np.dot(x[:-k], x[k:]) / denom
    return out


def _hill_estimator(r: np.ndarray, k: int = 100) -> float:
    a = np.sort(np.abs(r))[::-1]
    k = min(k, a.size - 1)
    top = a[: k + 1]
    xi = np.mean(np.log(top[:-1]) - np.log(top[-1]))
    return float(1.0 / xi) if xi > 0 else np.inf


def official_gate(gen_df: pd.DataFrame, ref_df: pd.DataFrame) -> dict:
    """官方 4 项指标（mid-price 收益口径）。"""
    gr = mid_returns(gen_df)
    rr = mid_returns(ref_df)
    lags = (1, 5, 10, 20, 50)

    def acf_abs_l2(a, b):
        d = _acf(np.abs(a), lags) - _acf(np.abs(b), lags)
        return float(np.sqrt(np.mean(d**2)))

    gh = depth_histogram(gen_df)
    rh = depth_histogram(ref_df)

    rep = {
        "ks": float(ks_2samp(gr, rr).statistic),
        "acf_abs_l2": acf_abs_l2(gr, rr),
        "hill_abs": abs(_hill_estimator(gr) - _hill_estimator(rr)),
    }
    if gh is not None and rh is not None:
        p = gh + 1e-12
        q = rh + 1e-12
        p /= p.sum()
        q /= q.sum()
        m = 0.5 * (p + q)

        def kl(a, b):
            return np.sum(a * np.log(a / b))

        rep["depth_js"] = float(0.5 * kl(p, m) + 0.5 * kl(q, m))
    return rep


# ---------------------------------------------------------------------------
# 用户 5 项代理指标（ORDER_FILLED 口径）
# ---------------------------------------------------------------------------
def proxy_metrics(gen_df: pd.DataFrame, ref_df: pd.DataFrame) -> dict:
    gr = order_filled_returns(gen_df)
    rr = order_filled_returns(ref_df)
    std_ratio = np.std(gr) / np.std(rr)
    zero_ratio = float((gr == 0).mean())
    ks = float(ks_2samp(gr, rr).statistic)
    skew = float(pd.Series(gr).skew())
    q25 = float(np.percentile(gr, 25))
    return {
        "std_ratio": std_ratio,
        "zero_ratio": zero_ratio,
        "ks": ks,
        "skew": skew,
        "q25": q25,
    }


# ---------------------------------------------------------------------------
# 达标判定
# ---------------------------------------------------------------------------
CEILINGS = {"ks": 0.08, "acf_abs_l2": 0.12, "hill_abs": 1.5, "depth_js": 0.10}


def gate_pass(report: dict) -> tuple[bool, list[str]]:
    breaches = [f"{k}={report[k]:.4g}>{CEILINGS[k]:.4g}" for k in CEILINGS if k in report and report[k] > CEILINGS[k]]
    return len(breaches) == 0, breaches


if __name__ == "__main__":
    import sys

    ref = pd.read_parquet("units/t3-as01-base-mix/trace.parquet")
    gen = pd.read_parquet(sys.argv[1] if len(sys.argv) > 1 else "out/smoke.parquet")

    print("=== 用户 5 项代理指标（ORDER_FILLED 口径） ===")
    pm = proxy_metrics(gen, ref)
    for k, v in pm.items():
        print(f"  {k}: {v:.4f}")

    print("\n=== 官方 4 项门控（mid-price 口径） ===")
    og = official_gate(gen, ref)
    for k, v in og.items():
        ceil = CEILINGS.get(k)
        flag = f"  (<= {ceil})" if ceil is not None else ""
        print(f"  {k}: {v:.4f}{flag}")
    ok, br = gate_pass(og)
    print(f"  => {'✅ 通过' if ok else '❌ 未通过: ' + ', '.join(br)}")
