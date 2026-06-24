"""第 14 章 · 中心极限定理 配图脚本(独立, 只 import, 不改 _plot_utils)。

产出两张图:
  fig-14-1-sum-evolution.png —— 招牌图: 指数分布的样本均值, n=1/2/5/30
                                标准化后随 n 增大逐步长成正态钟形(4 子图)
  fig-14-2-clt-breaks.png    —— CLT 的边界: 左=均匀/指数求和趋正态(对照);
                                右=柯西分布求和不趋正态(CLT 失效, 方差无限)
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         plot_pdf, sim_hist)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import norm


# ============================================================
# 图 14.1 · 招牌: 指数分布求平均, n=1/2/5/30 标准化后逐步变钟形
# ============================================================
# 取 Exponential(1): 明显右偏、有长尾, 长得最不像正态。
# mu=1, sigma^2=1。每次抽 n 个独立样本求平均 X_bar, 再标准化
#   Z = (X_bar - mu) / sqrt(sigma^2/n) = (sum - n*mu)/sqrt(n*sigma^2)
# 标准化是为了把"中心移到 0、宽度压到 1", 直接和标准正态曲线比形状。

def fig1_sum_evolution():
    rng = np.random.default_rng(42)
    mu, var = 1.0, 1.0           # Exponential(scale=1)
    trials = 200_000
    ns = [1, 2, 5, 30]

    fig, axes = plt.subplots(2, 2, figsize=(11, 7.2))
    x_grid = np.linspace(-4, 4, 400)

    for ax, n in zip(axes.flat, ns):
        # 模拟: 每次抽 n 个 Exp(1) 求平均
        means = rng.exponential(scale=1.0, size=(trials, n)).mean(axis=1)
        # 标准化(让形状可和标准正态直接对比)
        z = (means - mu) / np.sqrt(var / n)

        sim_hist(ax, z, bins=60, color=RED, label="standardized sample mean")
        plot_pdf(ax, x_grid, norm.pdf(x_grid), color=GREEN,
                 label="standard normal N(0,1)", lw=2.4)
        ax.set_title(f"n = {n}  (average of {n} Exp(1))", fontsize=11)
        ax.set_xlim(-4, 4)
        ax.set_ylim(0, 0.62)
        ax.legend(fontsize=8, loc="upper left")

    fig.suptitle("Central Limit Theorem: sample mean of Exp(1) -> Normal as n grows",
                 fontsize=12)
    save(fig, "fig-14-1-sum-evolution.png")
    print("[ok] fig-14-1-sum-evolution.png")


# ============================================================
# 图 14.2 · CLT 的边界: 均匀求和趋正态(左), 柯西求和不趋正态(右)
# ============================================================
# 左: Uniform(0,1), mu=0.5, var=1/12。n=30 标准化和死死贴住正态。
# 右: Cauchy(0,1), 方差无限。无论 n 多大, 标准化和都贴不住正态——
#     重尾吞噬一切, CLT 失效的铁证。

def fig2_clt_breaks():
    rng = np.random.default_rng(7)
    trials = 100_000
    n = 30
    x_grid = np.linspace(-4, 4, 400)

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.6))

    # ---- 左: 均匀分布求和, CLT 成立 ----
    mu_u, var_u = 0.5, 1.0 / 12.0
    sums_u = rng.uniform(0, 1, size=(trials, n)).sum(axis=1)
    z_u = (sums_u - n * mu_u) / np.sqrt(n * var_u)
    sim_hist(axL, z_u, bins=60, color=RED, label="standardized sum (n=30)")
    plot_pdf(axL, x_grid, norm.pdf(x_grid), color=GREEN,
             label="N(0,1)", lw=2.4)
    axL.set_title("Uniform(0,1):  CLT holds  (finite variance)", fontsize=10.5)
    axL.set_xlim(-4, 4); axL.set_ylim(0, 0.55)
    axL.legend(fontsize=8.5, loc="upper left")

    # ---- 右: 柯西分布求和, CLT 失效 ----
    # Cauchy 无均值无方差, 这里"标准化"只能减样本均值除样本标准差(经验标准化),
    # 只是为了画图可看——即便如此, 形状仍贴不住正态, 重尾依旧。
    sums_c = rng.standard_cauchy(size=(trials, n)).sum(axis=1)
    # 限制画图范围(柯西尾巴太重, 不限会塌成一条平线), 但形状用经验标准化展示
    z_c = (sums_c - sums_c.mean()) / sums_c.std()
    # 画直方图时, 把落在 [-4,4] 之外的尾部概率也标出来
    in_range = z_c[(z_c >= -4) & (z_c <= 4)]
    axR.hist(in_range, bins=60, range=(-4, 4), density=True,
             color=RED, alpha=0.45, edgecolor='white', linewidth=0.3,
             label=f"standardized sum (n=30)\ntail mass |Z|>4 = {np.mean(np.abs(z_c)>4):.3f}")
    plot_pdf(axR, x_grid, norm.pdf(x_grid), color=GREEN, label="N(0,1)", lw=2.4)
    axR.set_title("Cauchy(0,1):  CLT FAILS  (infinite variance)", fontsize=10.5)
    axR.set_xlim(-4, 4); axR.set_ylim(0, 0.55)
    axR.legend(fontsize=8.5, loc="upper center")

    fig.suptitle("When CLT works and when it breaks: finite vs infinite variance",
                 fontsize=12)
    save(fig, "fig-14-2-clt-breaks.png")
    print("[ok] fig-14-2-clt-breaks.png")


if __name__ == "__main__":
    fig1_sum_evolution()
    fig2_clt_breaks()
    print("done.")
