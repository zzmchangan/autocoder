"""第 17 章 · 贝叶斯推断 配图脚本(独立, 只 import _plot_utils, 绝不修改它)。

产出两张图:
  fig-17-1-posterior-shrinkage.png  招牌图: 先验 Beta(1,1), 随数据累积,
                                    后验 Beta(1+k, 1+n-k) 曲线族逐步收紧聚焦真值
                                    (画 95% 可信区间竖线对, 直观看'信念被数据夹紧')
  fig-17-2-priors-converge.png      三个不同先验的人, 看同一批硬币数据,
                                    后验曲线逐步趋近真值 (先验被冲刷的可视化)

固定随机种子 (np.random.default_rng(42)), 保证可复现。所有图内标注英文。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         plot_pdf, vline, hline)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats


def fig1_posterior_shrinkage():
    """招牌图: 先验 -> 后验, 曲线族随数据收紧。"""
    rng = np.random.default_rng(42)
    true_p = 0.6
    obs = (rng.random(200) < true_p).astype(int)   # 200 次虚拟抛硬币

    a0, b0 = 1, 1                                  # 均匀先验 Beta(1,1)
    # 选几个里程碑: 0, 2, 5, 20, 100 次观测
    milestones = [0, 2, 5, 20, 100]
    colors = [GRAY, PURPLE, BLUE, ORANGE, GREEN]
    labels_full = []

    fig, ax = plt.subplots(figsize=(8.4, 5.2))
    xs = np.linspace(0, 1, 600)
    cum_h = 0
    cum_t = 0
    curves = []   # (n, a, b, color)
    for i, x in enumerate(obs):
        if x == 1:
            cum_h += 1
        else:
            cum_t += 1
        n = i + 1
        if n in milestones:
            a = a0 + cum_h
            b = b0 + cum_t
            curves.append((n, a, b))

    # 画曲线族
    for (n, a, b), color in zip(curves, colors):
        ys = stats.beta.pdf(xs, a, b)
        if n == 0:
            label = f"prior Beta({a},{b})  (0 data)"
        else:
            label = f"posterior Beta({a},{b})  (n={n}, {cum_h if n==cum_h+ (0) else 0})"
            label = f"posterior Beta({a},{b})  (n={n})"
        plot_pdf(ax, xs, ys, color=color, label=label, lw=2.4)

    # 画真值竖线
    vline(ax, true_p, color=RED, ls='--', lw=2.0, label=f"true p = {true_p}")

    # 标 n=100 的 95% 可信区间(用阴影)
    a_last, b_last = curves[-1][1], curves[-1][2]
    lo = stats.beta.ppf(0.025, a_last, b_last)
    hi = stats.beta.ppf(0.975, a_last, b_last)
    ax.axvspan(lo, hi, color=GREEN, alpha=0.10, zorder=1)
    ax.annotate(f"95% credible interval\nn=100: ({lo:.3f}, {hi:.3f})",
                xy=((lo+hi)/2, stats.beta.pdf((lo+hi)/2, a_last, b_last)*0.55),
                ha='center', fontsize=9, color=GREEN,
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=GREEN, alpha=0.9))

    ax.set_xlim(0, 1)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("p  (coin's heads probability)")
    ax.set_ylabel("posterior density")
    ax.set_title("Bayesian updating: prior -> posterior sharpens as data grows")
    ax.legend(loc='upper left', fontsize=8.5, framealpha=0.9)
    save(fig, "fig-17-1-posterior-shrinkage.png")
    print("saved fig-17-1-posterior-shrinkage.png")
    print(f"  真值 p={true_p}; n=100 时后验 Beta({a_last},{b_last}), "
          f"95% CI=({lo:.4f},{hi:.4f}), width={hi-lo:.4f}")


def fig2_priors_converge():
    """第二张图: 三个不同先验, 看同一批数据, 后验曲线趋同。"""
    rng = np.random.default_rng(42)
    true_p = 0.6
    obs = (rng.random(200) < true_p).astype(int)

    priors = [
        ("prior: Beta(1,1)  uniform",     1,   1,  GRAY),
        ("prior: Beta(2,8)  skeptical",   2,   8,  PURPLE),
        ("prior: Beta(50,50)  strong",   50,  50,  BLUE),
    ]

    fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.6), sharey=True)
    xs = np.linspace(0, 1, 600)
    snapshots = [10, 50, 200]      # 三个时间快照

    cum_h = 0
    cum_t = 0
    # 预算每个 n 的累计
    cum_heads = np.concatenate([[0], np.cumsum(obs)])
    cum_tails = np.arange(len(obs)+1) - cum_heads

    for ax, n in zip(axes, snapshots):
        h = int(cum_heads[n])
        t = int(cum_tails[n])
        for name, a0, b0, color in priors:
            a = a0 + h
            b = b0 + t
            ys = stats.beta.pdf(xs, a, b)
            label = f"Beta({a0},{b0}) -> Beta({a},{b})"
            plot_pdf(ax, xs, ys, color=color, label=label, lw=2.2)
        vline(ax, true_p, color=RED, ls='--', lw=1.8, label=f"true p={true_p}")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 18)
        ax.set_xlabel("p")
        ax.set_title(f"after n={n} flips  ({h}H / {t}T)")
        ax.legend(loc='upper left', fontsize=7.5, framealpha=0.9)
    axes[0].set_ylabel("posterior density")
    fig.suptitle("Different priors, same data: posteriors converge as data washes out the prior",
                 fontsize=11, y=1.02)
    save(fig, "fig-17-2-priors-converge.png")
    print("saved fig-17-2-priors-converge.png")
    # 打印 n=200 时三个后验的均值, 核对正文
    for name, a0, b0, color in priors:
        a = a0 + int(cum_heads[200])
        b = b0 + int(cum_tails[200])
        lo = stats.beta.ppf(0.025, a, b)
        hi = stats.beta.ppf(0.975, a, b)
        print(f"  {name:30s}: n=200 均值={a/(a+b):.4f}, 95%CI=({lo:.4f},{hi:.4f}), width={hi-lo:.4f}")


if __name__ == "__main__":
    fig1_posterior_shrinkage()
    print()
    fig2_priors_converge()
