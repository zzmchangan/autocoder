"""P0-01 概率,到底在量什么 · 配图生成(定调章)。

两张图:
  fig-1-1-coin-convergence.png  — 扔硬币,正面频率从剧烈抖动 -> 趋近 0.5
                                  (大数定律的字面演示, 全书"扔很多次趋稳"主锚)
  fig-1-2-clt-preview.png       — 把 100 个均匀随机数加起来, 重复 5 万次,
                                  标准化后的直方图 -> 钟形(中心极限定理预告)

独立脚本, 只 import 自 _plot_utils, 绝不修改共享文件。固定随机种子可复现。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import save, GREEN, RED, BLUE, ORANGE, convergence, sim_hist, plot_pdf
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ============================================================
# Fig 1.1  扔硬币: 正面频率 -> 0.5  (单次盲 vs 大量稳 的字面演示)
# ============================================================
rng = np.random.default_rng(42)
N = 200000
flips = rng.integers(0, 2, N)                 # 0=反, 1=正
cum_prop = np.cumsum(flips) / np.arange(1, N + 1)

# 对数间隔抽样, 既能看到开头剧烈抖动, 又能看到后面贴着 0.5
idx = np.unique(np.logspace(0, np.log10(N), 2500).astype(int))
idx = idx[idx < N]   # 防止 logspace 末点越界

fig, ax = plt.subplots(figsize=(8.4, 5.2))
convergence(ax, idx, cum_prop[idx], truth=0.5,
            ylabel="proportion of heads", color=BLUE, truth_color=ORANGE)
ax.set_ylim(0.25, 0.78)
ax.legend(loc="upper right", fontsize=11)
ax.set_title("Single flips are blind,  but many flips obey a law:\n"
             "proportion of heads -> 0.5", fontsize=12.5)
save(fig, "fig-1-1-coin-convergence.png")
print("Fig 1.1: final proportion over", N, "flips =", round(cum_prop[-1], 5))

# ============================================================
# Fig 1.2  中心极限预告: 100 个均匀分布求和 -> 钟形
# ============================================================
rng2 = np.random.default_rng(7)
trials, n_sum = 50000, 100
sums = rng2.uniform(0, 1, size=(trials, n_sum)).sum(axis=1)
# 标准化: U(0,1) 的 E=0.5, Var=1/12 -> 和的 E=n/2, Var=n/12
mu, sigma = n_sum * 0.5, np.sqrt(n_sum / 12.0)
z = (sums - mu) / sigma

fig, ax = plt.subplots(figsize=(8.4, 5.2))
sim_hist(ax, z, bins=60, color=RED, label="standardized sum of 100 uniforms (sim)")
xs = np.linspace(-4, 4, 400)
plot_pdf(ax, xs, np.exp(-xs ** 2 / 2) / np.sqrt(2 * np.pi),
         color=GREEN, label="standard normal", lw=2.6)
ax.set_xlim(-4, 4)
ax.legend(loc="upper left", fontsize=11)
ax.set_title("Preview of the Central Limit Theorem:\n"
             "sum of many random things -> a bell curve", fontsize=12.5)
save(fig, "fig-1-2-clt-preview.png")
print("Fig 1.2: simulated standardized sum, mean =", round(z.mean(), 4),
      "std =", round(z.std(), 4), "(both should be ~0 and ~1)")
print("OK both figures saved.")
