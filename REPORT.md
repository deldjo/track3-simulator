# T3 市场模拟器优化 — v24 交付报告

> 目标：在保持订单簿完整性与 6 种事件类型的前提下，调整价格生成机制，让模拟器通过风格化事实门控。

---

## 一、先说最重要的三个发现

### 1. 缺失文件已全部找回（不再是阻塞项）

之前判断"缺两个关键文件"，现在已从公开仓库 `Agenthon-2026/track3-simulation-public` 找回：

| 文件 | 状态 |
|---|---|
| `units/t3-as01-base-mix/trace.parquet`（参考轨迹，263 KB，24695 事件） | ✅ 已下载 |
| `scenario.json` / `card.toml` / `events.json`（场景配置） | ✅ 已下载 |
| 官方评分源码 `stylized_facts.py` / `semantics.py` / `scoring.py` | ✅ 已下载 |

### 2. 你在调错的收益口径（核心问题）

官方门控用的是 **QUOTE_UPDATE 重建的 mid-price 收益**，不是你在用的 **ORDER_FILLED 成交收益**。两者是不同序列，参考值完全不同：

| 指标 | mid-price（官方门控） | ORDER_FILLED（你的代理指标） |
|---|---|---|
| 零收益占比 | **50.8%** | 25.2% |
| std | 4.28e-05 | 7.59e-05 |
| 偏度 | 0.57 | 0.149 |

所以「零收益占比降至 25%」这个目标本身就是错的——**门控对象的 mid-price 零收益是 50.8%，不是 25%**。你 v11 的 KS=0.1721 是成交口径的数字，而门控算的是 mid-price 口径的 KS。

官方 4 项门控（阈值）：

| 指标 | 阈值 |
|---|---|
| 收益分布 KS | ≤ 0.08 |
| ACF(\|r\|) RMS 误差（滞后 1/5/10/20/50） | ≤ 0.12 |
| Hill 尾部指数绝对误差（top-100） | ≤ 1.5 |
| 深度分布 JS 散度（20-bin） | ≤ 0.10 |

### 3. 你的 agent_mix 与真实场景不符

你提示词里写的 `agent_mix = {'NoiseTrader': 14, 'MarketMaker': 20, 'MomentumTrader': 8}`（42 个、3 类），而 `t3-as01-base-mix` 的真实配置是：

```json
{"NoiseTrader": 12, "ValueTrader": 4, "MomentumTrader": 2, "MarketMaker": 2}
```

（20 个、4 类，含 ValueTrader；seed=1806084051，Tier-B 统计校验。）

---

## 二、我做了什么

1. **重构了可运行的 `sim_stylized.py`**：完整 6 事件类型 + `OrderBookVec`（submit/cancel/match）+ 3 类智能体，价格生成逻辑独立为 `price_generator()`。
2. **修复了一个撮合 bug**：部分成交被错误记为 `ORDER_FILLED`（同一 order_id 出现两次 FILLED），导致同价成对、零收益虚高。改为完整成交发 FILLED、部分成交发 PARTIAL_FILL。
3. **补上了均值回归**：v11 的价格生成是「随机游走 + 跳跃」，3500 步后价格漂移、分布偏移（KS 从 n_steps=500 的 0.039 退化到 3500 的 0.144）。场景 oracle 本身就是 `mean_reverting`（kappa=0.05），这是 v11 缺失的关键一块。
4. **以官方门控 KS 为目标扫参**，并做了跨 5 种子的鲁棒性验证。

---

## 三、结果

### 最终 v24 参数（`sim_stylized_v24.py` 默认值）

```python
kappa      = 0.015   # 均值回归强度（新增）
sigma      = 3.5     # 扩散噪声 tick（新增）
jump_prob  = 0.30    # 跳跃概率（v11=0.40）
scale      = 8       # 跳跃尺度（v11=25）
df         = 5       # t 分布自由度（v11=4）
up_prob    = 0.55    # 上涨概率（v11=0.58）
```

### 官方 4 项门控（5 种子，全部通过）

| seed | KS(≤0.08) | ACF(≤0.12) | Hill(≤1.5) | depth(≤0.10) |
|---|---|---|---|---|
| 42 | 0.0613 | 0.0510 | 0.0757 | 0.0101 |
| 7 | 0.0627 | 0.0541 | 0.0420 | 0.0106 |
| 123 | 0.0676 | 0.0560 | 0.0583 | 0.0247 |
| 999 | 0.0606 | 0.0676 | 0.3091 | 0.0171 |
| 2024 | 0.0609 | 0.0496 | 0.0545 | 0.0272 |

**结论：✅ 全部通过，最差种子 KS 仅 0.0676（阈值 0.08），余量充足。**

### 关键对比：scale 的取值方向

你建议 `scale 30–45`（不要超 40），这是按**成交口径**校准的；对 **mid-price 门控**来说过大了。实测：

- `scale=25`（v11）→ 官方 KS ≈ 0.144–0.186（不达标）
- `scale=8–12` + 均值回归 → 官方 KS ≈ 0.06（达标，余量足）

---

## 四、还没解决的（真实提交的差距）

我的 v24 通过了**风格化事实门控**，但距「可提交的 Docker 镜像」还有三件事（这些不在你本次「调价格生成」的范围内，但必须知道）：

1. **`message_trace.parquet` 缺失**：`card.toml` 里 `requires_message_ledger = true`（59/72 个 unit 都要求），我的模拟器目前只产出 `trace.parquet` + `events.json`，没产出消息级账本。这是 g3.5 协议保真门控的输入。
2. **提交形态是 Docker 镜像**（`simulate` / `simulate-batch` 两个 verb），不是 ZIP 脚本。README 的 `--network none` 意味着数据全从 `/input/` 挂载读。
3. **agent_mix 要对齐真实场景**：官方门控只看统计分布所以我的 14/20/8 也能过 mid-price 门控，但若要对齐语义回归（Tier-B 还有 spread ±10bps 检查），最好改用真实的 12/4/2/2 四类智能体。

---

## 五、交付物清单

| 文件 | 说明 |
|---|---|
| `sim_stylized.py` | 重构的完整模拟器（参数可配置） |
| `sim_stylized_v24.py` | v24 版（调优参数已烘焙为默认值） |
| `metrics.py` | 双口径指标：官方 4 项门控 + 你的 5 项代理指标 |
| `scan.py` / `robust.py` / `verify.py` | 扫参 / 鲁棒性 / 最终验证脚本 |
| `units/t3-as01-base-mix/` | 参考轨迹 + 场景配置 |
| `official/` | 官方评分源码（stylized_facts / semantics / scoring） |
| `final/v24_seed*.parquet` + `events.json` | 最终生成的 5 组轨迹与事件文件 |

---

## 六、下一步建议

1. **立即切换优化口径**：弃用 ORDER_FILLED 零收益作为目标，改用 `metrics.py` 里的 `official_gate()`（mid-price 口径）。
2. **对齐 agent_mix**：改成 `12/4/2/2` 四类智能体（含 ValueTrader），这样 spread/depth 也更贴近。
3. **补 message_trace.parquet**：这是过 g3.5 门控的硬要求。
4. **封装 Docker 镜像**：`simulate` / `simulate-batch` 两个入口 + `--network none`。

如果你愿意，我可以接着做第 2、3 步（对齐四类智能体 + 生成 message_trace 账本），把 v24 推进到「能跑通 run_regression.py 的候选镜像」。
