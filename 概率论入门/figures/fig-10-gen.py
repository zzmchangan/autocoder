"""第 10 章 · 正态深挖 配图脚本。

严禁修改 _plot_utils.py。本脚本独立产出:
  fig-10-1-bell-68-95-997.png  —— 招牌图: 标准正态钟形 + 68/95/99.7 三档填色
  fig-10-2-standardization.png —— 标准化: 不同 mu,sigma 的正态经 Z=(X-mu)/sigma 变同一条曲线
  fig-10-3-clt-foreshadow.png  —— 中心极限伏笔: 均匀/指数的样本均值分布趋近正态
所有标注用英文, 正文用中文。模拟固定 np.random.default_rng(42)。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         plot_pdf, sim_hist, vline, hline)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import norm

OUTDIR = HERE


# ---------------------------------------------------------------
# 图 10.1 招牌: 标准正态钟形 + 68-95-99.7 三档填色
# ---------------------------------------------------------------
def fig1_bell():
    fig, ax = plt.subplots(figsize=(8.4, 4.6))
    xs = np.linspace(-4, 4, 800)
    ys = norm.pdf(xs)

    # 由外到内填色: ±3σ (浅) -> ±2σ (中) -> ±1σ (深), 叠加显示层级
    ax.fill_between(xs, ys, where=(xs >= -3) & (xs <= 3),
                    color=PURPLE, alpha=0.18, label=r'$\pm 3\sigma$: 99.73%')
    ax.fill_between(xs, ys, where=(xs >= -2) & (xs <= 2),
                    color=ORANGE, alpha=0.30, label=r'$\pm 2\sigma$: 95.45%')
    ax.fill_between(xs, ys, where=(xs >= -1) & (xs <= 1),
                    color=GREEN,  alpha=0.55, label=r'$\pm 1\sigma$: 68.27%')

    # 主曲线
    ax.plot(xs, ys, color=BLUE, lw=2.4, zorder=5)

    # 参考竖线: 0, ±1, ±2, ±3
    for k, lab in [(1, r'$\mu\pm\sigma$'), (2, r'$\mu\pm2\sigma$'), (3, r'$\mu\pm3\sigma$')]:
        for s in (-k, k):
            ax.axvline(s, color=GRAY, ls=':', lw=1.0, zorder=1)
    vline(ax, 0, color=RED, ls='--', lw=1.4)

    # 在峰值附近标注三档百分比
    ax.text(0,  norm.pdf(0)*0.62, '68.27%', ha='center', color=GREEN,  fontsize=11, fontweight='bold')
    ax.text(0,  norm.pdf(0)*0.32, '95.45%', ha='center', color=ORANGE, fontsize=10, fontweight='bold')
    ax.text(0,  norm.pdf(0)*0.12, '99.73%', ha='center', color=PURPLE, fontsize=9,  fontweight='bold')

    # 尾部标注
    ax.text(3.25, 0.012, '0.13%', ha='left',  color=PURPLE, fontsize=9)
    ax.text(-3.25, 0.012, '0.13%', ha='right', color=PURPLE, fontsize=9)

    ax.set_xlim(-4, 4)
    ax.set_ylim(0, norm.pdf(0)*1.08)
    ax.set_xlabel(r'$z = (x-\mu)/\sigma$   (standard normal)')
    ax.set_ylabel('density  ' + r'$\phi(z)$')
    ax.set_title('Standard normal: the 68-95-99.7 rule', fontsize=12)
    ax.legend(loc='upper right', framealpha=0.9, fontsize=9)
    save(fig, 'fig-10-1-bell-68-95-997.png')


# ---------------------------------------------------------------
# 图 10.2 标准化: 两个不同正态 -> 同一条标准正态曲线
# ---------------------------------------------------------------
def fig2_standardize():
    fig, axes = plt.subplots(1, 2, figsize=(10.4, 4.4))

    # 左: 原始空间, 两个正态
    ax = axes[0]
    xs = np.linspace(120, 230, 600)
    height = norm(171, 7)      # 身高 N(171, 7^2)
    iq     = norm(100, 15)     # 智商 N(100, 15^2) (压到同坐标轴只为对比形状, 数值无意义)
    ax.plot(xs, height.pdf(xs), color=GREEN, lw=2.3,
            label=r'Height  $N(\mu=171,\ \sigma=7)$')
    # 智商用单独坐标范围画在同一子图(只看"胖瘦"对比), 用 twinx
    ax2 = ax.twinx()
    bx = np.linspace(55, 145, 600)
    ax2.plot(bx, iq.pdf(bx), color=PURPLE, lw=2.3,
             label=r'IQ  $N(\mu=100,\ \sigma=15)$')
    vline(ax, 171, color=GREEN, ls='--', lw=1.2)
    vline(ax2, 100, color=PURPLE, ls='--', lw=1.2)
    ax.set_xlabel('original scale  x')
    ax.set_ylabel('height density', color=GREEN)
    ax2.set_ylabel('IQ density', color=PURPLE)
    ax.tick_params(axis='y', colors=GREEN)
    ax2.tick_params(axis='y', colors=PURPLE)
    ax.set_title('Two normals: different location & spread', fontsize=11)
    lines = ax.get_lines() + ax2.get_lines()
    ax.legend(lines, [l.get_label() for l in lines], loc='upper left', fontsize=9)

    # 右: 标准化后 Z = (X-mu)/sigma, 两者重合成同一条 N(0,1)
    ax = axes[1]
    zs = np.linspace(-4, 4, 600)
    zheight = (xs - 171) / 7
    ziq     = (bx - 100) / 15
    ax.plot(zs, norm.pdf(zs), color=BLUE, lw=2.4, label=r'$N(0,1)$')
    sim_hist(ax, (np.random.default_rng(11).normal(171, 7, 60000) - 171) / 7,
             bins=45, color=GREEN, label='height -> Z')
    sim_hist(ax, (np.random.default_rng(23).normal(100, 15, 60000) - 100) / 15,
             bins=45, color=PURPLE, label='IQ -> Z')
    vline(ax, 0, color=RED, ls='--', lw=1.3)
    for k in (1, 2, 3):
        for s in (-k, k):
            ax.axvline(s, color=GRAY, ls=':', lw=0.9)
    ax.set_xlabel(r'standardized  $z = (x-\mu)/\sigma$')
    ax.set_ylabel('density')
    ax.set_xlim(-4, 4)
    ax.set_title(r'After $Z=(X-\mu)/\sigma$: one universal curve', fontsize=11)
    ax.legend(loc='upper right', fontsize=9)

    fig.tight_layout()
    save(fig, 'fig-10-2-standardization.png')


# ---------------------------------------------------------------
# 图 10.3 中心极限伏笔: 不对称分布的样本均值 -> 钟形
# ---------------------------------------------------------------
def fig3_clt_foreshadow():
    rng = np.random.default_rng(42)
    fig, axes = plt.subplots(1, 2, figsize=(10.4, 4.3))

    # 左: 指数分布 Exp(1) 的样本均值, n=1 / 2 / 30
    ax = axes[0]
    zs = np.linspace(-3.5, 3.5, 400)
    ax.plot(zs, norm.pdf(zs), color=BLUE, lw=2.3, label=r'$N(0,1)$ (theory)')
    for n, col, lab in [(1, GRAY, 'n=1  (single Exp)'),
                        (2, ORANGE, 'n=2'),
                        (30, GREEN, 'n=30  (mean of 30)')]:
        means = rng.exponential(scale=1.0, size=(200_000, n)).mean(axis=1)
        z = (means - means.mean()) / means.std()
        sim_hist(ax, z, bins=55, color=col, label=lab)
    ax.set_xlim(-3.5, 3.5)
    ax.set_xlabel('standardized sample mean')
    ax.set_ylabel('density')
    ax.set_title(r'Exponential: mean of $n$ samples $\to$ normal', fontsize=11)
    ax.legend(loc='upper right', fontsize=9)

    # 右: 均匀分布 U(0,1) 的样本均值, n=1 / 2 / 30
    ax = axes[1]
    ax.plot(zs, norm.pdf(zs), color=BLUE, lw=2.3, label=r'$N(0,1)$ (theory)')
    for n, col, lab in [(1, GRAY, 'n=1  (single U)'),
                        (2, ORANGE, 'n=2'),
                        (30, GREEN, 'n=30  (mean of 30)')]:
        means = rng.uniform(0, 1, size=(200_000, n)).mean(axis=1)
        z = (means - means.mean()) / means.std()
        sim_hist(ax, z, bins=55, color=col, label=lab)
    ax.set_xlim(-3.5, 3.5)
    ax.set_xlabel('standardized sample mean')
    ax.set_ylabel('density')
    ax.set_title(r'Uniform: mean of $n$ samples $\to$ normal', fontsize=11)
    ax.legend(loc='upper right', fontsize=9)

    fig.tight_layout()
    save(fig, 'fig-10-3-clt-foreshadow.png')


if __name__ == '__main__':
    fig1_bell()
    fig2_standardize()
    fig3_clt_foreshadow()
    print('done ->',
          [f for f in os.listdir(OUTDIR) if f.startswith('fig-10-')])
