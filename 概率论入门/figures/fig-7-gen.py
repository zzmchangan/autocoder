"""第 7 章 · 方差与标准差:波动有多大 —— 配图脚本。

产出两张图:
  fig-7-1-two-funds-variance.png   —— 两只同期望不同方差的分布对比(直观"散"的程度)
  fig-7-2-chebyshev.png            —— 切比雪夫不等式:不同 k 下"实际落界比例 vs 下界 1-1/k²"
                                      (用非正态的三点分布演示普适性 + 紧致点)

工具一律 import 自 _plot_utils.py,绝不修改它。
图内标注一律英文,正文用中文。
所有数值先经手算/scipy 核对,模拟固定种子 default_rng(42)。
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

rng = np.random.default_rng(42)


# =====================================================================
# 图 7.1 · 两只同期望不同方差的"基金"对比
#   基金 A: P(+10)=0.5, P(-10)=0.5,  E=0, Var=100, SD=10  (高波动)
#   基金 B: P(+1)=0.5,  P(-1)=0.5,   E=0, Var=1,   SD=1   (低波动)
#   画 PMF 柱状, 两个分布上下错开看, 标注各自的 mu +/- sd 区间。
# =====================================================================
def fig1_two_funds():
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.4))

    # 基金 B(低波动): +-1
    axB = axes[0]
    xsB = np.array([-1.0, 1.0]); psB = np.array([0.5, 0.5])
    muB, sdB = 0.0, 1.0
    plot_pmf(axB, xsB, psB, color=BLUE, label="Fund B PMF", width=0.35)
    vline(axB, muB, color=ORANGE, lw=1.8, label="mean = 0")
    # 画 mu +/- sd 的区间(用 axvspan)
    axB.axvspan(muB - sdB, muB + sdB, color=GREEN, alpha=0.15,
                label="mean +/- SD (width=2)")
    axB.set_title("Fund B:  E=0,  Var=1,  SD=1   (calm)")
    axB.set_xlim(-12, 12); axB.set_ylim(0, 0.62)
    axB.legend(loc="upper center", fontsize=9, framealpha=0.9)
    axB.grid(True, ls=":", alpha=0.4)

    # 基金 A(高波动): +-10
    axA = axes[1]
    xsA = np.array([-10.0, 10.0]); psA = np.array([0.5, 0.5])
    muA, sdA = 0.0, 10.0
    plot_pmf(axA, xsA, psA, color=RED, label="Fund A PMF", width=0.9)
    vline(axA, muA, color=ORANGE, lw=1.8, label="mean = 0")
    axA.axvspan(muA - sdA, muA + sdA, color=GREEN, alpha=0.15,
                label="mean +/- SD (width=20)")
    axA.set_title("Fund A:  E=0,  Var=100,  SD=10   (wild)")
    axA.set_xlim(-12, 12); axA.set_ylim(0, 0.62)
    axA.legend(loc="upper center", fontsize=9, framealpha=0.9)
    axA.grid(True, ls=":", alpha=0.4)

    fig.suptitle("Same expectation, different spread: variance measures the wiggle",
                 fontsize=12, y=1.02)
    save(fig, "fig-7-1-two-funds-variance.png")
    print("[ok] fig-7-1-two-funds-variance.png")
    # 核对
    assert abs((xsA*psA).sum() - 0.0) < 1e-12
    assert abs(((xsA-0)**2*psA).sum() - 100.0) < 1e-12
    assert abs(((xsB-0)**2*psB).sum() - 1.0) < 1e-12
    print("   check: A var=100, B var=1  (ok)")


# =====================================================================
# 图 7.2 · 切比雪夫不等式:实际落界比例 vs 下界 1-1/k²
#   用三点分布演示(mu=0, sigma=sqrt(10)≈3.162):
#     P(X=0)=0.9, P(X=+10)=0.05, P(X=-10)=0.05
#   这是切比雪夫的经典"紧致"反例:k=10/sigma=sqrt(10)≈3.162 时,
#   实际落界 = 0.9, 下界 = 1 - 1/10 = 0.9, 严格相等。
#   左子图: PMF + mu±k*sigma 带(k=1,2,3);右子图: 实际 vs 下界曲线。
# =====================================================================
def fig2_chebyshev():
    # 三点分布参数
    vals  = np.array([0.0, 10.0, -10.0])
    probs = np.array([0.90, 0.05, 0.05])
    mu  = float((vals * probs).sum())          # = 0
    var = float(((vals - mu) ** 2 * probs).sum())  # = 10
    sd  = np.sqrt(var)                          # ≈ 3.1623
    # 理论"实际落界"函数: |val - mu| <= k*sd ?
    def actual_in_band(k):
        return float(probs[np.abs(vals - mu) <= k * sd].sum())

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(11.2, 4.6))

    # ---- 左:PMF + mu ± k*sigma 带 ----
    plot_pmf(axL, vals, probs, color=PURPLE, label="PMF (non-normal)", width=1.8)
    vline(axL, mu, color=ORANGE, lw=1.8, label=f"mean = {mu}")
    band_colors = [GREEN, BLUE, RED]
    for k, c in zip([1, 2, 3], band_colors):
        axL.axvline(mu - k * sd, color=c, ls='--', lw=1.4, alpha=0.85)
        axL.axvline(mu + k * sd, color=c, ls='--', lw=1.4, alpha=0.85,
                    label=f"k={k}:  mean +/- {k}·SD")
    axL.set_xlim(-13, 13); axL.set_ylim(0, 1.0)
    axL.set_title("A non-normal distribution + Chebyshev bands")
    axL.legend(loc="upper center", fontsize=8.5, framealpha=0.92, ncol=2)
    axL.grid(True, ls=":", alpha=0.4)

    # ---- 右:实际 vs 下界,随 k 变化 ----
    ks = np.linspace(1.0, 5.0, 400)
    cheb_bound  = np.maximum(0.0, 1.0 - 1.0 / ks ** 2)
    actual_line = np.array([actual_in_band(k) for k in ks])

    axR.plot(ks, actual_line, color=GREEN, lw=2.4, label="actual P(|X-mu| <= k·SD)")
    axR.plot(ks, cheb_bound,  color=RED,   lw=2.4, ls='--',
             label="Chebyshev lower bound  1 - 1/k²")
    # 标出紧致点 k = sqrt(10)
    k_tight = 10.0 / sd   # = sqrt(10)
    axR.axvline(k_tight, color=ORANGE, ls=':', lw=1.6)
    axR.scatter([k_tight], [0.9], color=ORANGE, s=70, zorder=6)
    axR.annotate(f"tight point\nk=10/SD={k_tight:.3f}\nboth = 0.900",
                 xy=(k_tight, 0.9), xytext=(k_tight + 0.35, 0.62),
                 fontsize=8.5, color=ORANGE,
                 arrowprops=dict(arrowstyle="->", color=ORANGE, lw=1.2))
    # 在 k=1,2,3 标注实际值
    for k in [1, 2, 3]:
        a = actual_in_band(k); b = max(0.0, 1 - 1 / k ** 2)
        axR.scatter([k], [a], color=GREEN, s=42, zorder=6)
        axR.scatter([k], [b], color=RED, s=42, zorder=6)
    axR.set_xlabel("k  (band half-width in units of SD)")
    axR.set_ylabel("probability of landing inside mean +/- k·SD")
    axR.set_xlim(1, 5); axR.set_ylim(-0.02, 1.05)
    axR.set_title("Chebyshev: actual vs lower bound (works for ANY distribution)")
    axR.legend(loc="lower right", fontsize=9, framealpha=0.92)
    axR.grid(True, ls=":", alpha=0.4)

    save(fig, "fig-7-2-chebyshev.png")
    print("[ok] fig-7-2-chebyshev.png")

    # ---- 蒙特卡洛核对(十万次) ----
    samp = rng.choice(vals, p=probs, size=100_000)
    print(f"   sim mean={samp.mean():.4f}, sim sd={samp.std():.4f} (theory SD={sd:.4f})")
    for k in [1, 1.5, 2, 3]:
        in_band_sim = np.mean(np.abs(samp - mu) <= k * sd)
        in_band_th  = actual_in_band(k)
        bound       = max(0.0, 1 - 1 / k ** 2)
        print(f"   k={k}: sim={in_band_sim:.4f}  theory={in_band_th:.4f}  "
              f"cheb_bound={bound:.4f}  gap={in_band_th - bound:+.4f}")
    print(f"   tight k={k_tight:.4f} (10/SD): cheb_bound={1 - 1/k_tight**2:.4f} "
          f"== actual 0.9")


if __name__ == "__main__":
    fig1_two_funds()
    fig2_chebyshev()
    print("done.")
