"""第 15 章 · 极大似然估计(MLE)· 配图脚本。

产出两张 PNG:
  fig-15-1-likelihood-curve.png    —— 似然函数曲线(招牌图):
        给定一批伯努利数据(7 正 3 反), 画出对数似然随参数 p 变化的曲线,
        标出 MLE = 0.7 的峰值(vline), 并标出 p=0.5/0.3 对比。
  fig-15-2-mle-consistency.png     —— MLE 一致性:
        真值 p=0.3 的伯努利, 样本量 n 从 10 涨到 100000,
        画 MLE 估计随样本量增大收敛到真值 0.3 的曲线(右子图)。
        左子图给一个小样本(n=20)的似然峰示意, 直观看到"峰在哪"。

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


# ============================================================
# 图 15.1 —— 似然函数曲线(伯努利 7 正 3 反)
# ============================================================
def fig_likelihood_curve():
    fig, ax = plt.subplots(figsize=(8.4, 4.8))

    # 数据: 7 正 3 反(1=正, 0=反), 这是观测到的一批数据, 固定不变
    k, n = 7, 10      # 7 次成功, 共 10 次

    # 参数 p 在 (0,1) 上扫描, 看"哪个 p 最可能产生这批数据"
    p = np.linspace(1e-4, 1 - 1e-4, 1000)
    # 对数似然: log L(p) = k*log(p) + (n-k)*log(1-p)
    loglik = k * np.log(p) + (n - k) * np.log(1 - p)

    # MLE 解析解 = k/n
    p_mle = k / n  # 0.7
    ll_mle = k * np.log(p_mle) + (n - k) * np.log(1 - p_mle)

    # 画对数似然曲线
    ax.plot(p, loglik, color=BLUE, lw=2.6, label=r"$\ell(p) = \log L(p)$  (7 heads / 3 tails)", zorder=4)

    # MLE 峰: 竖虚线 + 峰点
    vline(ax, p_mle, color=RED, lw=2.0, label=f"MLE  p = k/n = {p_mle}")
    ax.plot([p_mle], [ll_mle], 'o', color=RED, ms=9, zorder=6)
    ax.annotate(f"peak at p = {p_mle}\nlog L = {ll_mle:.3f}",
                xy=(p_mle, ll_mle), xytext=(p_mle + 0.06, ll_mle + 0.6),
                fontsize=9.5, color=RED,
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.2))

    # 对比点 p=0.5 和 p=0.3
    for pc, col, txt in [(0.5, ORANGE, "p=0.5"), (0.3, PURPLE, "p=0.3")]:
        llc = k * np.log(pc) + (n - k) * np.log(1 - pc)
        ax.plot([pc], [llc], 's', color=col, ms=8, zorder=6)
        ax.annotate(f"{txt}\nlog L = {llc:.3f}",
                    xy=(pc, llc), xytext=(pc - 0.22, llc - 1.1),
                    fontsize=9, color=col,
                    arrowprops=dict(arrowstyle="->", color=col, lw=1.0))

    ax.set_xlabel("parameter  p   (probability of heads)", fontsize=11)
    ax.set_ylabel("log-likelihood  $\\ell(p)$", fontsize=11)
    ax.set_title("Likelihood function: which p best explains 7 heads / 3 tails?", fontsize=11.5)
    ax.set_xlim(0, 1)
    ax.legend(loc="lower center", fontsize=9)
    ax.text(0.02, 0.04,
            "Question is flipped: data is FIXED,\n"
            "we search over the PARAMETER p.\n"
            "Likelihood ≠ probability (not a density in p).",
            transform=ax.transAxes, fontsize=8.5, color=GRAY,
            bbox=dict(boxstyle='round', fc='white', ec=GRAY, alpha=0.85))

    save(fig, "fig-15-1-likelihood-curve.png")


# ============================================================
# 图 15.2 —— MLE 一致性: 样本量越大, MLE 越贴近真值
# ============================================================
def fig_mle_consistency():
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.4))

    # ---- 左: 小样本(n=20)的似然峰, 直观看到"峰在哪" ----
    ax = axes[0]
    rng0 = np.random.default_rng(7)
    p_true = 0.3
    d20 = rng0.binomial(1, p_true, 20)
    k20 = d20.sum(); n20 = len(d20)
    p = np.linspace(1e-4, 1 - 1e-4, 1000)
    ll20 = k20 * np.log(p) + (n20 - k20) * np.log(1 - p)
    p_mle20 = k20 / n20

    ax.plot(p, ll20, color=BLUE, lw=2.4, label=r"$\ell(p)$  (n=20)", zorder=4)
    vline(ax, p_true, color=ORANGE, lw=1.8, ls='--', label=f"true  p = {p_true}")
    vline(ax, p_mle20, color=RED, lw=1.8, ls='--', label=f"MLE  p = {p_mle20:.3f}")
    # 把 MLE 处的峰点标出
    ll_peak = k20 * np.log(p_mle20) + (n20 - k20) * np.log(1 - p_mle20)
    ax.plot([p_mle20], [ll_peak], 'o', color=RED, ms=8, zorder=6)
    ax.set_xlabel("parameter  p", fontsize=11)
    ax.set_ylabel("log-likelihood", fontsize=11)
    ax.set_title(f"Small sample n=20: MLE = {p_mle20:.3f}  (noisy)", fontsize=11)
    ax.set_xlim(0, 1)
    ax.legend(fontsize=8.5, loc="lower center")
    ax.text(0.02, 0.04,
            f"data: {k20} heads in {n20} flips\n"
            f"true p=0.3, MLE off by {abs(p_mle20-p_true):.3f}",
            transform=ax.transAxes, fontsize=8.5, color=GRAY,
            bbox=dict(boxstyle='round', fc='white', ec=GRAY, alpha=0.85))

    # ---- 右: 样本量增大, MLE 收敛到真值 ----
    ax = axes[1]
    rng = np.random.default_rng(42)
    p_true = 0.3
    ns = np.unique(np.round(np.logspace(1, 5, 60)).astype(int))  # 10 -> 100000
    mles = []
    cum = 0
    total = 0
    # 用累积方式更稳: 逐个来, 累计成功数
    # 但为可复现且直接, 用每个 n 独立抽样
    for n in ns:
        d = rng.binomial(1, p_true, n)
        mles.append(d.mean())
    mles = np.array(mles)

    convergence(ax, ns, mles, truth=p_true, color=BLUE,
                ylabel="MLE of p")
    ax.set_title("MLE consistency: as n grows, MLE → true p = 0.3", fontsize=11)
    ax.legend(fontsize=9, loc="upper right")
    ax.text(0.02, 0.18,
            "Law of Large Numbers guarantees:\n"
            "sample mean → E[X], so\n"
            "MLE (which = sample mean here)\n"
            "converges to the true parameter.",
            transform=ax.transAxes, fontsize=8.5, color=GRAY,
            bbox=dict(boxstyle='round', fc='white', ec=GRAY, alpha=0.85))

    fig.suptitle("MLE is consistent: more data → closer to truth", fontsize=12, y=1.02)
    save(fig, "fig-15-2-mle-consistency.png")


if __name__ == "__main__":
    fig_likelihood_curve()
    fig_mle_consistency()
    print("Generated: fig-15-1-likelihood-curve.png, fig-15-2-mle-consistency.png")
