"""第 13 章 · 大数定律:扔多了,平均就稳了 —— 配图脚本。

产出两张图:
  fig-13-1-mean-convergence.png   —— 多种分布的样本均值随 n 增大收敛到各自期望
                                     (招牌图:与 P0-01 图 1.1"硬币频率→0.5"视角不同,
                                      这里把"均值收敛到期望"从伯努利推广到均匀/正态/泊松)
  fig-13-2-monte-carlo-pi.png      —— 蒙特卡洛估 π:扔飞镖,内圈比例的 4 倍趋近 π

工具一律 import 自 _plot_utils.py,绝不修改它。
图内标注一律英文,正文用中文。
模拟固定随机种子 np.random.default_rng(42),保证可复现。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         convergence)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats

rng = np.random.default_rng(42)


# =====================================================================
# 图 13.1 · 多种分布的样本均值随 n 增大收敛到期望(招牌图)
# =====================================================================
def fig1_mean_convergence():
    N = 200_000
    ns = np.arange(1, N + 1)

    # 四种分布各扔 N 次, 算前 n 次的样本均值
    # 1) 伯努利(0.5):  P0-01 的硬币, 期望 0.5 (这里画"均值"=正面频率, 跟图 1.1 视角不同)
    bern = rng.random(N) < 0.5
    cm_bern = np.cumsum(bern) / ns
    e_bern = 0.5

    # 2) 均匀(0,1):  期望 0.5
    unif = rng.uniform(0, 1, N)
    cm_unif = np.cumsum(unif) / ns
    e_unif = 0.5

    # 3) 标准正态:  期望 0
    norm = rng.standard_normal(N)
    cm_norm = np.cumsum(norm) / ns
    e_norm = 0.0

    # 4) 泊松(lambda=3):  期望 3
    pois = rng.poisson(lam=3.0, size=N)
    cm_pois = np.cumsum(pois) / ns
    e_pois = 3.0

    fig, axes = plt.subplots(2, 2, figsize=(11.5, 7.6))
    panels = [
        (axes[0, 0], cm_bern, e_bern, BLUE,   "Bernoulli(0.5):  mean = freq of heads"),
        (axes[0, 1], cm_unif, e_unif, GREEN,  "Uniform(0,1)"),
        (axes[1, 0], cm_norm, e_norm, PURPLE, "Standard Normal"),
        (axes[1, 1], cm_pois, e_pois, ORANGE, "Poisson(3)"),
    ]
    for ax, cm, truth, color, title in panels:
        # 为看清抖动, 限制 y 轴范围在期望附近
        convergence(ax, ns, cm, truth=truth, color=color,
                    truth_color=RED, ylabel="sample mean")
        ax.set_title(title, fontsize=11)
        ax.grid(True, which="both", ls=":", alpha=0.4)
        ax.set_ylim(truth - 0.55, truth + 0.55)
        ax.legend(loc="upper right", fontsize=9, framealpha=0.9)

    fig.suptitle("Law of Large Numbers: sample mean -> E[X] as n grows (4 distributions)",
                 fontsize=12.5)
    save(fig, "fig-13-1-mean-convergence.png")
    print("[ok] fig-13-1-mean-convergence.png")
    # 核对收敛点
    print("   末端样本均值:",
          f"Bern={cm_bern[-1]:.4f} (E={e_bern})",
          f"Unif={cm_unif[-1]:.4f} (E={e_unif})",
          f"Norm={cm_norm[-1]:.4f} (E={e_norm})",
          f"Pois={cm_pois[-1]:.4f} (E={e_pois})")
    # 几个中间点(用对数刻度的典型 n)
    for tag, cm, e in [("Bern", cm_bern, e_bern), ("Pois", cm_pois, e_pois)]:
        print(f"   {tag} n=10,100,1e3,1e4,1e5 ->",
              round(cm[9], 3), round(cm[99], 3), round(cm[999], 3),
              round(cm[9999], 3), round(cm[99999], 4))


# =====================================================================
# 图 13.2 · 蒙特卡洛估 π: 扔飞镖, 内圈比例 * 4 趋近 π
# =====================================================================
def fig2_monte_carlo_pi():
    N = 200_000
    # 在 [-1,1]x[-1,1] 正方形里随机扔 N 个点
    xs = rng.uniform(-1, 1, N)
    ys = rng.uniform(-1, 1, N)
    inside = (xs**2 + ys**2) <= 1.0                    # 落在单位圆内
    ns = np.arange(1, N + 1)
    pi_estimate = 4.0 * np.cumsum(inside) / ns          # 4 * 内圈比例 -> π

    fig, axes = plt.subplots(1, 2, figsize=(12.0, 5.2),
                             gridspec_kw={"width_ratios": [1, 1.35]})

    # 左:散点(前 5000 个点, 内圈绿/外圈红)
    ax0 = axes[0]
    show_n = 5000
    theta = np.linspace(0, 2 * np.pi, 400)
    ax0.plot(np.cos(theta), np.sin(theta), color='k', lw=1.6)        # 单位圆
    ax0.scatter(xs[:show_n][inside[:show_n]], ys[:show_n][inside[:show_n]],
                s=2.5, color=GREEN, alpha=0.55, label="inside circle")
    ax0.scatter(xs[:show_n][~inside[:show_n]], ys[:show_n][~inside[:show_n]],
                s=2.5, color=RED, alpha=0.55, label="outside circle")
    ax0.set_xlim(-1.05, 1.05); ax0.set_ylim(-1.05, 1.05)
    ax0.set_aspect('equal')
    ax0.set_xlabel("x"); ax0.set_ylabel("y")
    ax0.set_title(f"Darts on a 2x2 square (first {show_n} of {N:,})", fontsize=11)
    ax0.legend(loc="upper right", fontsize=9, markerscale=4, framealpha=0.9)
    ax0.grid(True, ls=":", alpha=0.4)

    # 右:估计值随 n 收敛到 π
    ax1 = axes[1]
    convergence(ax1, ns, pi_estimate, truth=np.pi, color=BLUE,
                truth_color=ORANGE, ylabel="4 * (fraction inside)  ~  pi")
    ax1.set_ylim(np.pi - 0.35, np.pi + 0.35)
    ax1.set_title("Monte Carlo estimate of pi converges to pi", fontsize=11)
    ax1.grid(True, which="both", ls=":", alpha=0.4)
    ax1.legend(loc="upper right", fontsize=10, framealpha=0.9)

    fig.suptitle("Monte Carlo method = Law of Large Numbers in disguise",
                 fontsize=12.5)
    save(fig, "fig-13-2-monte-carlo-pi.png")
    print("[ok] fig-13-2-monte-carlo-pi.png")
    # 核对
    print("   n=100,1e3,1e4,1e5,2e5 ->",
          round(pi_estimate[99], 4), round(pi_estimate[999], 4),
          round(pi_estimate[9999], 4), round(pi_estimate[99999], 4),
          round(pi_estimate[-1], 5), " (true pi =", round(np.pi, 5), ")")


if __name__ == "__main__":
    fig1_mean_convergence()
    fig2_monte_carlo_pi()
    print("done.")
