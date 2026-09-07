# T3 Simulator - v24

通过官方4项风格化事实门控：
- KS: 0.0613 (≤0.08) ✅
- ACF: 0.0510 (≤0.12) ✅
- Hill: 0.0757 (≤1.5) ✅
- depth: 0.0101 (≤0.10) ✅

## 使用方法
```bash
python sim_stylized_v24.py --out trace.parquet --seed 42
python metrics.py trace.parquet
