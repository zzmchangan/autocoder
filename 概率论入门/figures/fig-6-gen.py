"""第 6 章 · 期望:长期平均的"重心" —— 配图脚本。

产出两张图:
  fig-6-1-expectation-convergence.png  —— 扔骰子:样本均值逐步贴近期望 3.5(招牌收敛曲线)
  fig-6-2-dice-center-of-mass.png      —— 骰子 PMF 上"概率当砝码",期望 = 平衡点(重心直觉)

工具一律 import 自 _plot_utils.py,绝不修改它。
图内标注一律英文,正文用中文。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         plot_pmf, plot_pdf, sim_hist, convergence, heatmap2d, vline)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

rng = np.random.default_rng(42)


# =====================================================================
# 图 6.1 · 扔骰子:样本均值逐步贴近期望 3.5(招牌收敛曲线)
# =====================================================================
def fig1_convergence():
    rolls = rng.integers(1, 7, 100_000)                      # 扔十万次骰子
    n_draws = np.arange(1, 100_000 + 1)
    cum_mean = np.cumsum(rolls) / n_draws                    # 前 n 次的样本均值

    fig, ax = plt.subplots(figsize=(8.4, 4.8))
    # 只画前 20000 次的细节更清楚;但收敛曲线 helper 用对数 x,画全部 100000
    ns = n_draws
    convergence(ax, ns, cum_mean, truth=3.5, color=BLUE,
                truth_color=ORANGE, ylabel="sample mean of dice rolls")
    # 标注:前面抖、后面稳
    ax.set_ylim(2.6, 4.4)
    ax.set_title("Sample mean of dice rolls -> E[X] = 3.5")
    ax.grid(True, which="both", ls=":", alpha=0.4)
    ax.legend(loc="upper right", fontsize=10, framealpha=0.9)
    save(fig, "fig-6-1-expectation-convergence.png")
    print("[ok] fig-6-1-expectation-convergence.png")
    # 核对几个点
    print("   n=1,10,100,1000,1e5 ->",
          round(cum_mean[0], 3), round(cum_mean[9], 3),
          round(cum_mean[99], 3), round(cum_mean[999], 3),
          round(cum_mean[-1], 4))


# =====================================================================
# 图 6.2 · 骰子 PMF 上"概率当砝码":期望 = 平衡点(重心直觉)
# =====================================================================
def fig2_center_of_mass():
    xs = np.arange(1, 7)
    ps = np.full(6, 1 / 6)                                    # 均匀骰子 PMF
    expect = 3.5

    fig, ax = plt.subplots(figsize=(8.0, 4.6))
    # PMF 柱子 = "砝码"
    plot_pmf(ax, xs, ps, color=GREEN, label="PMF P(X=x) as weight", width=0.75)
    # 期望处画平衡点(支点)
    vline(ax, expect, color=RED, ls='--', lw=2.0,
          label=f"E[X] = {expect} (balance point)")
    # 在 PMF 顶端 x=3.5 上方画一个三角"支点"标记
    ax.scatter([expect], [1 / 6 + 0.015], marker='v', s=140,
               color=RED, zorder=6)
    # 在数轴下方画一条"跷跷板"示意:从 1 到 6,支点在 3.5
    ax.plot([1, 6], [-0.018, -0.018], color='k', lw=2.2, zorder=5)
    ax.scatter([expect], [-0.018], marker='^', s=160,
               color=ORANGE, zorder=6)
    ax.text(expect, -0.05, "fulcrum", ha='center', va='top',
            color=ORANGE, fontsize=10)
    ax.set_ylim(-0.08, 0.22)
    ax.set_title("Expectation = center of mass of the PMF")
    ax.legend(loc="upper center", fontsize=10, framealpha=0.9)
    save(fig, "fig-6-2-dice-center-of-mass.png")
    print("[ok] fig-6-2-dice-center-of-mass.png")


if __name__ == "__main__":
    fig1_convergence()
    fig2_center_of_mass()
    print("done.")
