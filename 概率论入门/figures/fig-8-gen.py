"""第 8 章 · 离散三剑客(伯努利/二项/泊松)· 配图脚本。

产出两张 PNG:
  fig-8-1-three-pmfs.png      —— 三种 PMF 并排(伯努利 p=0.5 / 二项 n=10 p=0.5 / 泊松 λ=3)
  fig-8-2-binom-to-poisson.png —— 二项→泊松极限(固定 np=λ, 增大 n 减小 p, 二项 PMF 贴上泊松) +
                                  模拟直方图叠加理论 PMF(验证模拟 ≈ 理论)

工具: 只 import, 绝不修改 _plot_utils.py。模拟固定种子 default_rng(42)。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         plot_pmf, plot_pdf, sim_hist, convergence, heatmap2d, vline, hline)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import bernoulli, binom, poisson


# ============================================================
# 图 8.1 —— 三种 PMF 并排
# ============================================================
def fig_three_pmfs():
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8))

    # (a) 伯努利 p=0.5
    ax = axes[0]
    xs = [0, 1]; ps = [bernoulli.pmf(0, 0.5), bernoulli.pmf(1, 0.5)]
    plot_pmf(ax, xs, ps, color=BLUE, label="Bernoulli(p=0.5)")
    ax.set_title("(a) Bernoulli  p=0.5", fontsize=11)
    ax.set_xticks([0, 1])
    ax.set_ylim(0, 1.0)
    ax.text(0.5, 0.92, "one trial: 0 or 1", transform=ax.transAxes,
            ha='center', fontsize=9, color=GRAY)

    # (b) 二项 n=10 p=0.5
    ax = axes[1]
    xs = np.arange(0, 11)
    ps = binom.pmf(xs, 10, 0.5)
    plot_pmf(ax, xs, ps, color=GREEN, label="Binomial(n=10,p=0.5)")
    ax.set_title("(b) Binomial  n=10, p=0.5", fontsize=11)
    ax.set_xticks(np.arange(0, 11, 2))
    vline(ax, 5.0, color=ORANGE, lw=1.5, label="mean=5")
    ax.set_ylim(0, 0.28)
    ax.legend(fontsize=8, loc='upper left')

    # (c) 泊松 λ=3
    ax = axes[2]
    xs = np.arange(0, 11)
    ps = poisson.pmf(xs, 3)
    plot_pmf(ax, xs, ps, color=PURPLE, label="Poisson(λ=3)")
    ax.set_title("(c) Poisson  λ=3", fontsize=11)
    ax.set_xticks(np.arange(0, 11, 2))
    vline(ax, 3.0, color=ORANGE, lw=1.5, label="mean=var=3")
    ax.set_ylim(0, 0.28)
    ax.legend(fontsize=8, loc='upper right')

    fig.suptitle("Three discrete PMFs", fontsize=12, y=1.02)
    save(fig, "fig-8-1-three-pmfs.png")


# ============================================================
# 图 8.2 —— 二项→泊松极限 + 模拟佐证
# 左: 固定 np=λ=3, 增大 n, 二项 PMF 逐步贴上泊松
# 右: 模拟十万次, 直方图叠加理论 PMF(模拟 ≈ 理论)
# ============================================================
def fig_binom_to_poisson():
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2))

    # ---- 左: 二项 -> 泊松 极限 ----
    ax = axes[0]
    lam = 3
    xs = np.arange(0, 11)
    # 泊松理论 PMF(黑色粗线, 作为目标)
    pois_ps = poisson.pmf(xs, lam)
    ax.plot(xs, pois_ps, 'k-o', ms=5, lw=2.2, label=f"Poisson(λ={lam})", zorder=5)

    # 几组 二项(n,p), np=3, 看逐步贴上泊松
    params = [(10, 0.30), (30, 0.10), (100, 0.03)]
    colors = [RED, ORANGE, GREEN]
    for (n, p), c in zip(params, colors):
        bs = binom.pmf(xs, n, p)
        ax.plot(xs, bs, '^--', color=c, ms=4.5, lw=1.4,
                label=f"Binom(n={n}, p={p})", alpha=0.9)

    ax.set_title("Binomial converges to Poisson  (n·p = λ = 3)", fontsize=11)
    ax.set_xlabel("x")
    ax.set_ylabel("P(X = x)")
    ax.set_xticks(xs)
    ax.set_ylim(0, 0.26)
    ax.legend(fontsize=8, loc='upper right')
    ax.text(0.02, 0.55,
            "as n→∞, p→0,  n·p→λ:\nBinom PMF  ⟶  Poisson PMF",
            transform=ax.transAxes, fontsize=8.5, color=GRAY,
            bbox=dict(boxstyle='round', fc='white', ec=GRAY, alpha=0.8))

    # ---- 右: 模拟直方图 + 理论 PMF (泊松 λ=3) ----
    ax = axes[1]
    rng = np.random.default_rng(42)
    samples = rng.poisson(lam, 100_000)
    sim_hist(ax, samples, bins=np.arange(-0.5, 14.5, 1) - 0, color=RED, label="simulation (100k draws)")
    # 注意: sim_hist 是连续直方图; 这里我们直接画 PMF 柱叠加更清楚
    ax.cla()
    counts = np.bincount(samples, minlength=14)
    freq = counts / 100_000
    xs2 = np.arange(0, 13)
    ax.bar(xs2, freq[xs2], width=0.7, color=RED, alpha=0.40,
           edgecolor='white', linewidth=0.4, label="simulation (100k)")
    plot_pmf(ax, xs2, poisson.pmf(xs2, lam), color=PURPLE, label=f"Poisson(λ={lam}) theory", width=0.35)
    vline(ax, lam, color=ORANGE, lw=1.5, label="mean=3")
    ax.set_title("Simulation matches theory  (Poisson λ=3)", fontsize=11)
    ax.set_xticks(xs2)
    ax.set_ylim(0, 0.26)
    ax.legend(fontsize=8, loc='upper right')

    fig.suptitle("Binomial → Poisson limit  &  simulation check", fontsize=12, y=1.02)
    save(fig, "fig-8-2-binom-to-poisson.png")


if __name__ == "__main__":
    fig_three_pmfs()
    fig_binom_to_poisson()
    print("Generated: fig-8-1-three-pmfs.png, fig-8-2-binom-to-poisson.png")
