"""第 5 章 · 随机变量与分布 · 配图生成脚本。

产出三张图:
  fig-5-1-dice-pmf.png       离散 PMF(骰子点数)+ 扔十万次的模拟直方图叠加
  fig-5-2-pdf-area.png       连续 PDF(均匀 U(0,2))+ 正态 N(0,1), fill 显示"面积=概率"
  fig-5-3-cdf.png            CDF 与 PDF 对照(累积到 x 的概率)

依赖: numpy, scipy, matplotlib, 本目录的 _plot_utils.py(只 import, 不修改)。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         plot_pmf, plot_pdf, sim_hist)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats


# ============================================================
# 图 5.1 · 离散 PMF: 骰子点数 + 扔十万次模拟
# ============================================================
def fig1_dice_pmf():
    rng = np.random.default_rng(42)
    rolls = rng.integers(1, 7, 100_000)          # 扔十万次骰子
    xs = np.arange(1, 7)
    ps = np.array([1/6] * 6)                     # 理论 PMF
    sim_p = np.array([(rolls == i).mean() for i in xs])   # 模拟频率

    fig, ax = plt.subplots(figsize=(7.6, 4.3))
    # 模拟频率(半透明红柱, 稍窄, 叠在后面)
    ax.bar(xs - 0.18, sim_p, width=0.32, color=RED, alpha=0.45,
           edgecolor='white', linewidth=0.4, label="simulation (n=100,000)")
    # 理论 PMF(绿柱)
    ax.bar(xs + 0.18, ps, width=0.32, color=GREEN, alpha=0.85,
           edgecolor='k', linewidth=0.5, label="theory PMF = 1/6")
    ax.axhline(1/6, color=ORANGE, ls='--', lw=1.5, label="truth = 1/6 ≈ 0.1667")
    ax.set_xticks(xs)
    ax.set_xlabel("x  (die face)")
    ax.set_ylabel("P(X = x)")
    ax.set_ylim(0, 0.23)
    ax.legend(loc='upper right', fontsize=9)
    ax.set_title("Discrete random variable: die roll,  PMF vs simulation")
    save(fig, "fig-5-1-dice-pmf.png")


# ============================================================
# 图 5.2 · 连续 PDF: 均匀 U(0,2) 和正态 N(0,1), fill 显示"面积=概率"
# ============================================================
def fig2_pdf_area():
    rng = np.random.default_rng(42)

    fig, axes = plt.subplots(1, 2, figsize=(10.8, 4.3))

    # --- 左: 均匀 U(0,2), 阴影面积 = P(0.5<X<1.5)=0.5 ---
    ax = axes[0]
    x = np.linspace(-0.5, 2.5, 400)
    y = np.where((x >= 0) & (x <= 2), 0.5, 0.0)
    plot_pdf(ax, x, y, color=GREEN, label="Uniform(0,2)  f(x)=1/2")
    # 填阴影区间
    xm = np.linspace(0.5, 1.5, 100)
    ym = np.full_like(xm, 0.5)
    ax.fill_between(xm, ym, color=BLUE, alpha=0.45,
                    label="area = P(0.5<X<1.5) = 0.5")
    # 模拟直方图叠加
    sims = rng.uniform(0, 2, 100_000)
    sim_hist(ax, sims, bins=40, color=RED, label="simulation (n=100,000)")
    ax.set_ylim(0, 0.9)
    ax.legend(loc='upper center', fontsize=8.5)
    ax.set_title("Uniform:  area under f(x) = probability")

    # --- 右: 标准正态 N(0,1), 阴影 = P(-1<Z<1)≈0.6827 ---
    ax = axes[1]
    x = np.linspace(-4, 4, 400)
    y = stats.norm.pdf(x)
    plot_pdf(ax, x, y, color=GREEN, label="Normal(0,1)  f(x)")
    xm = np.linspace(-1, 1, 200)
    ax.fill_between(xm, stats.norm.pdf(xm), color=BLUE, alpha=0.45,
                    label="area = P(-1<Z<1) ≈ 0.6827")
    sims = rng.standard_normal(100_000)
    sim_hist(ax, sims, bins=60, color=RED, label="simulation (n=100,000)")
    ax.set_ylim(0, 0.5)
    ax.legend(loc='upper right', fontsize=8.5)
    ax.set_title("Normal:  area under f(x) = probability")

    save(fig, "fig-5-2-pdf-area.png")


# ============================================================
# 图 5.3 · CDF 与 PDF 对照(以标准正态为例)
# ============================================================
def fig3_cdf():
    fig, axes = plt.subplots(1, 2, figsize=(10.8, 4.3))

    # --- 左: PDF, 标出累积到 x=1 的面积 ---
    ax = axes[0]
    x = np.linspace(-4, 4, 400)
    y = stats.norm.pdf(x)
    plot_pdf(ax, x, y, color=GREEN, label="PDF  f(x)")
    xm = np.linspace(-4, 1, 200)
    ax.fill_between(xm, stats.norm.pdf(xm), color=BLUE, alpha=0.35,
                    label="area = P(X ≤ 1) = Φ(1) ≈ 0.8413")
    ax.axvline(1, color=RED, ls='--', lw=1.6)
    ax.text(1.05, 0.30, "x = 1", color=RED, fontsize=9)
    ax.set_ylim(0, 0.46)
    ax.legend(loc='upper right', fontsize=9)
    ax.set_title("PDF:  shaded area = P(X ≤ 1)")

    # --- 右: CDF 单调上升到 1 ---
    ax = axes[1]
    cdf = stats.norm.cdf(x)
    ax.plot(x, cdf, color=PURPLE, lw=2.4, label="CDF  F(x) = P(X ≤ x)")
    ax.axhline(1.0, color=GRAY, ls=':', lw=1.2)
    ax.axhline(stats.norm.cdf(1), color=BLUE, ls='--', lw=1.4)
    ax.axvline(1, color=RED, ls='--', lw=1.6)
    ax.scatter([1], [stats.norm.cdf(1)], color=RED, zorder=5, s=35)
    ax.text(-3.8, stats.norm.cdf(1) + 0.03,
            f"F(1) = Φ(1) ≈ {stats.norm.cdf(1):.4f}", color=BLUE, fontsize=9)
    ax.set_ylim(0, 1.08)
    ax.set_xlabel("x")
    ax.set_ylabel("F(x) = P(X ≤ x)")
    ax.legend(loc='lower right', fontsize=9)
    ax.set_title("CDF:  monotone,  from 0 to 1")

    save(fig, "fig-5-3-cdf.png")


if __name__ == "__main__":
    fig1_dice_pmf()
    fig2_pdf_area()
    fig3_cdf()
    print("generated: fig-5-1-dice-pmf.png, fig-5-2-pdf-area.png, fig-5-3-cdf.png")
