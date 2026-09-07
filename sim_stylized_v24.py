#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sim_stylized.py —— 风格化市场模拟器（向量化订单簿 + 6 类事件）

这是 v11 的重构版本。核心结构严格遵循既有约束：
  1. 6 种事件类型（顺序固定）
  2. 订单簿类 OrderBookVec（submit_order / cancel_order / match_all）
  3. 智能体配置 agent_mix（NoiseTrader / MarketMaker / MomentumTrader）
  4. 时间参数 horizon_ns / n_steps / dt
  5. 输出 trace.parquet（7 列）+ events.json

价格生成逻辑集中在 price_generator()，是唯一允许修改的优化区。

用法：
  python sim_stylized.py                     # 默认 v11 参数
  python sim_stylized.py --jump_prob 0.5 --scale 34 --df 5 --up_prob 0.58 \
         --n_steps 3500 --out /tmp/trace.parquet --events /tmp/events.json --seed 42
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import defaultdict, deque

import numpy as np
import pandas as pd

MSG_SUBMITTED = "ORDER_SUBMITTED"
MSG_ACCEPTED = "ORDER_ACCEPTED"
MSG_QUOTE_UPDATE = "QUOTE_UPDATE"
MSG_FILLED = "ORDER_FILLED"
MSG_PARTIAL_FILL = "PARTIAL_FILL"
MSG_CANCELLED = "ORDER_CANCELLED"

EVENT_ORDER = [
    MSG_SUBMITTED,
    MSG_ACCEPTED,
    MSG_QUOTE_UPDATE,
    MSG_FILLED,
    MSG_PARTIAL_FILL,
    MSG_CANCELLED,
]

TRACE_COLS = ["t_ns", "agent_id", "msg_type", "side", "price", "size", "order_id"]

AGENT_MIX = {"NoiseTrader": 14, "MarketMaker": 20, "MomentumTrader": 8}


class OrderBookVec:
    """价格档位订单簿。每个档位用 deque 保存 (order_id, size, agent_id) 以支持 O(1) 撮合。"""

    def __init__(self):
        self.bids = defaultdict(deque)  # price -> deque[(order_id, size, agent_id)]
        self.asks = defaultdict(deque)
        self.best_bid = 0
        self.best_ask = 10**9
        self.order_counter = 0
        self.order_map = {}  # order_id -> (side, price, size, agent_id)
        self.price_history = []

    def _recompute_best(self):
        self.best_bid = max(self.bids) if self.bids else 0
        self.best_ask = min(self.asks) if self.asks else 10**9

    def submit_order(self, side, price, size, agent_id, t_ns):
        if size <= 0 or price <= 0:
            return []
        oid = self.order_counter
        self.order_counter += 1
        self.order_map[oid] = (side, price, size, agent_id)
        book = self.bids if side == "BID" else self.asks
        book[price].append((oid, size, agent_id))
        self._recompute_best()
        return [
            (t_ns, agent_id, MSG_SUBMITTED, side, price, size, oid),
            (t_ns, agent_id, MSG_ACCEPTED, side, price, size, oid),
        ]

    def cancel_order(self, order_id, t_ns):
        if order_id not in self.order_map:
            return []
        side, price, size, agent_id = self.order_map.pop(order_id)
        book = self.bids if side == "BID" else self.asks
        if price in book:
            newq = deque()
            for oid, sz, aid in book[price]:
                if oid == order_id:
                    continue
                newq.append((oid, sz, aid))
            if newq:
                book[price] = newq
            else:
                del book[price]
        self._recompute_best()
        return [(t_ns, agent_id, MSG_CANCELLED, side, price, size, order_id)]

    def match_all(self, t_ns):
        """撮合所有交叉订单，返回 (fills, partials)。O(成交笔数)。"""
        fills = []
        partials = []
        while self.bids and self.asks:
            bb = self.best_bid
            ba = self.best_ask
            if bb < ba:
                break
            trade_price = ba
            bid_q = self.bids[bb]
            ask_q = self.asks[ba]
            bid_oid, bid_sz, bid_aid = bid_q[0]
            ask_oid, ask_sz, ask_aid = ask_q[0]
            trade_size = min(bid_sz, ask_sz)

            # 买单：完整成交 -> ORDER_FILLED；部分成交 -> PARTIAL_FILL
            if bid_sz == trade_size:
                bid_q.popleft()
                if not bid_q:
                    del self.bids[bb]
                del self.order_map[bid_oid]
                fills.append((t_ns, bid_aid, MSG_FILLED, "BID", trade_price, trade_size, bid_oid))
            else:
                bid_q[0] = (bid_oid, bid_sz - trade_size, bid_aid)
                self.order_map[bid_oid] = ("BID", bb, bid_sz - trade_size, bid_aid)
                partials.append((t_ns, bid_aid, MSG_PARTIAL_FILL, "BID", trade_price, trade_size, bid_oid))

            # 卖单：同上
            if ask_sz == trade_size:
                ask_q.popleft()
                if not ask_q:
                    del self.asks[ba]
                del self.order_map[ask_oid]
                fills.append((t_ns, ask_aid, MSG_FILLED, "ASK", trade_price, trade_size, ask_oid))
            else:
                ask_q[0] = (ask_oid, ask_sz - trade_size, ask_aid)
                self.order_map[ask_oid] = ("ASK", ba, ask_sz - trade_size, ask_aid)
                partials.append((t_ns, ask_aid, MSG_PARTIAL_FILL, "ASK", trade_price, trade_size, ask_oid))

            self._recompute_best()

        return fills, partials

    def quote_update(self, t_ns):
        events = []
        if self.best_bid > 0:
            sz = sum(x[1] for x in self.bids[self.best_bid])
            events.append((t_ns, 0, MSG_QUOTE_UPDATE, "BID", self.best_bid, sz, -1))
        if self.best_ask < 10**9:
            sz = sum(x[1] for x in self.asks[self.best_ask])
            events.append((t_ns, 0, MSG_QUOTE_UPDATE, "ASK", self.best_ask, sz, -1))
        return events


def price_generator(rng, mid_price, p):
    """价格生成逻辑 —— 唯一允许修改的优化区。

    结构：均值回归 (OU) + 扩散噪声 + 跳跃 (fat tail)。
    均值回归与扩散来自场景 oracle_config（mean_reverting, kappa, sigma），
    跳跃部分保留 v11 的 t 分布机制。
    """
    # 1. 均值回归，把价格拉回初始价（保证长程平稳，避免随机游走漂移）
    mid_price += p["kappa"] * (p["initial_price"] - mid_price)
    # 2. 扩散噪声（小步高斯）
    mid_price += rng.normal(0.0, p["sigma"])
    # 3. 跳跃（fat tail，v11 机制）
    if rng.random() < p["jump_prob"]:
        change = rng.standard_t(df=p["df"]) * p["scale"]
        price_change = int(np.clip(change, -80, 80))
        if price_change == 0:
            price_change = p["min_change"]
        if rng.random() < p["up_prob"]:
            mid_price += abs(price_change)
        else:
            mid_price -= abs(price_change)
    return mid_price


class Simulator:
    def __init__(self, p, seed):
        self.p = p
        self.rng = np.random.default_rng(seed)
        self.book = OrderBookVec()
        self.agents = self._build_agents()

    def _build_agents(self):
        agents = []
        aid = 1
        for atype, cnt in AGENT_MIX.items():
            for _ in range(cnt):
                agents.append((aid, atype))
                aid += 1
        return agents

    def run(self):
        p = self.p
        dt = p["horizon_ns"] // p["n_steps"]
        rng = self.rng
        book = self.book
        agents = self.agents
        mid_price = p["initial_price"]
        events = []
        open_orders = deque()

        for step in range(p["n_steps"]):
            t_ns = int(step * dt)
            mid_price = price_generator(rng, mid_price, p)
            book.price_history.append(mid_price)

            for aid, atype in agents:
                if atype == "MarketMaker":
                    self._step_mm(aid, mid_price, t_ns, events, open_orders)
                elif atype == "NoiseTrader":
                    self._step_noise(aid, mid_price, t_ns, events, open_orders)
                else:
                    self._step_momentum(aid, mid_price, t_ns, events, open_orders)

            fills, partials = book.match_all(t_ns)
            events.extend(fills)
            events.extend(partials)
            events.extend(book.quote_update(t_ns))

        ev_df = pd.DataFrame(events, columns=TRACE_COLS)
        if len(ev_df) == 0:
            return pd.DataFrame(columns=TRACE_COLS)
        rank = ev_df["msg_type"].map({m: i for i, m in enumerate(EVENT_ORDER)})
        ev_df = ev_df.assign(_r=rank).sort_values(["t_ns", "_r", "order_id"]).drop(columns="_r")
        return ev_df.reset_index(drop=True)

    def _submit(self, side, price, size, aid, t_ns, events, open_orders):
        ev = self.book.submit_order(side, price, size, aid, t_ns)
        if ev:
            events.extend(ev)
            open_orders.append(ev[0][6])

    def _step_mm(self, aid, mid, t_ns, events, open_orders):
        p = self.p
        rng = self.rng
        if rng.random() < p["mm_cancel_prob"] and open_orders:
            oid = open_orders.popleft()
            events.extend(self.book.cancel_order(oid, t_ns))
        if rng.random() < p["mm_quote_prob"]:
            spread = p["mm_spread_ticks"]
            half = spread // 2
            bid_p = max(1, int(mid - half))
            ask_p = int(mid + (spread - half))
            size = max(1, int(rng.normal(p["mm_size"], p["mm_size"] * 0.3)))
            self._submit("BID", bid_p, size, aid, t_ns, events, open_orders)
            self._submit("ASK", ask_p, size, aid, t_ns, events, open_orders)

    def _step_noise(self, aid, mid, t_ns, events, open_orders):
        p = self.p
        rng = self.rng
        if rng.random() < p["noise_prob"]:
            side = "BID" if rng.random() < 0.5 else "ASK"
            offset = rng.integers(-p["noise_offset"], p["noise_offset"] + 1)
            price = max(1, int(mid + offset))
            size = max(1, int(rng.normal(p["noise_size_mean"], p["noise_size_std"])))
            self._submit(side, price, size, aid, t_ns, events, open_orders)

    def _step_momentum(self, aid, mid, t_ns, events, open_orders):
        p = self.p
        rng = self.rng
        if rng.random() < p["mom_prob"]:
            hist = self.book.price_history
            lookback = p["mom_lookback"]
            trend = hist[-1] - hist[-lookback] if len(hist) >= lookback + 1 else 0
            if trend > 0:
                side = "BID"
            elif trend < 0:
                side = "ASK"
            else:
                side = "BID" if rng.random() < 0.5 else "ASK"
            price = max(1, int(mid + (1 if side == "BID" else -1) * p["mom_offset"]))
            size = max(1, int(rng.normal(p["mom_size_mean"], p["mom_size_std"])))
            self._submit(side, price, size, aid, t_ns, events, open_orders)


def default_params():
    return {
        "horizon_ns": 20_000_000_000,
        "n_steps": 3500,
        "initial_price": 100_000,
        # 价格生成（优化区）—— v24 调优后的参数
        "kappa": 0.015,           # 均值回归强度（oracle，v11 无此项；鲁棒性扫描最优）
        "sigma": 3.5,             # 扩散噪声（tick）
        "jump_prob": 0.30,        # v24: 0.30（v11=0.40）
        "scale": 8,               # v24: 8（v11=25；mid-price 门控需小 scale）
        "df": 5,                  # v24: 5（v11=4）
        "up_prob": 0.55,          # v24: 0.55（v11=0.58）
        "min_change": 1,
        "small_jump_prob": 0.20,
        # 智能体行为
        "mm_spread_ticks": 2,
        "mm_size": 10,
        "mm_cancel_prob": 0.03,
        "mm_quote_prob": 0.55,
        "noise_prob": 0.30,
        "noise_offset": 5,
        "noise_size_mean": 11.0,
        "noise_size_std": 2.0,
        "mom_prob": 0.18,
        "mom_lookback": 5,
        "mom_offset": 3,
        "mom_size_mean": 15.0,
        "mom_size_std": 3.0,
    }


def write_outputs(df, out_path, events_path, scenario_id, seed, wall_clock_sec):
    df.to_parquet(out_path, compression="snappy", index=False)
    n_events = int(len(df))
    with open(out_path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    events = {
        "scenario_id": scenario_id,
        "seed": seed,
        "n_events": n_events,
        "wall_clock_sec": round(wall_clock_sec, 4),
        "events_per_sec": round(n_events / wall_clock_sec, 4) if wall_clock_sec > 0 else 0.0,
        "trace_sha256": sha,
    }
    if events_path:
        with open(events_path, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False)
    return events


def main():
    ap = argparse.ArgumentParser()
    defaults = default_params()
    for k, v in defaults.items():
        ap.add_argument(f"--{k}", type=type(v), default=v)
    ap.add_argument("--out", default="/tmp/stylized_trace.parquet")
    ap.add_argument("--events", default="/tmp/events.json")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--scenario_id", default="abd7b2b8-ecba-5a6e-a7c3-dfac68beb0ad")
    args = ap.parse_args()
    params = {k: getattr(args, k) for k in defaults}

    t0 = time.time()
    sim = Simulator(params, args.seed)
    df = sim.run()
    wall = time.time() - t0

    write_outputs(df, args.out, args.events, args.scenario_id, args.seed, wall)
    print(f"n_events={len(df)}  wall_clock_sec={wall:.4f}  events_per_sec={len(df)/wall:.1f}")
    if len(df):
        print(df["msg_type"].value_counts().to_string())


if __name__ == "__main__":
    main()
