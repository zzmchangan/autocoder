"""图 3.07 · 黎曼积分(第 3 篇第 7 章)的配图脚本.

两张图:
  1) fig-3-07-1-riemann-sum.png
     2x2: 同一函数 f=x^2 on [0,2], 分别用 4 / 16 / 64 个中点矩形 + 64 个左点矩形,
          看矩形和如何趋近精确积分值 8/3.
  2) fig-3-07-2-darboux-squeeze.png
     左: 一个连续函数 f 的 Darboux 上和(取每段最大值)与下和(取每段最小值),
         当分割变细, 上下和夹拢到积分值.
     右: Dirichlet 病态函数的上下和——无论分割多细, 上和恒=1、下和恒=0, 永不夹拢.

严禁修改 _plot_utils.py. 图内标注一律用英文.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, riemann_rects, marker, setup_axes, save, \
    GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np
import sympy as sp
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


# ---------- 图 1: 黎曼和随 n 增大趋近精确积分值 ----------
def fig1_riemann_sum():
    # 精确积分: sympy 算 ∫_0^2 x^2 dx = 8/3
    x = sp.symbols('x')
    exact = float(sp.integrate(x**2, (x, 0, 2)))   # 2.6666...
    f = lambda t: t**2
    a, b = 0.0, 2.0

    fig, axes = plt.subplots(2, 2, figsize=(10.5, 7.6))
    panels = [
        (axes[0, 0], 4,   'mid',   'n=4 rectangles, midpoint'),
        (axes[0, 1], 16,  'mid',   'n=16 rectangles, midpoint'),
        (axes[1, 0], 64,  'mid',   'n=64 rectangles, midpoint'),
        (axes[1, 1], 64,  'left',  'n=64 rectangles, left endpoint'),
    ]
    for ax, n, pos, title in panels:
        s = riemann_rects(ax, f, a, b, n, pos=pos, color=ORANGE, alpha=0.45,
                          edge=ORANGE)
        plot_fn(ax, f, a, b, color=BLUE, lw=2.4, label='f(x) = x^2')
        # 精确积分值的水平参考线
        ax.axhline(exact, color=GREEN, lw=1.4, ls=':', label='exact = 8/3 = %.4f' % exact)
        setup_axes(ax, xlim=(a, b), ylim=(0, 4.6),
                   title='%s  |  sum = %.4f  (err %.2e)' % (title, s, s - exact),
                   xlabel='x', ylabel='f(x)')
        ax.legend(loc='upper left', fontsize=9)
    save(fig, 'fig-3-07-1-riemann-sum.png')
    print('[ok] fig-3-07-1-riemann-sum.png')


# ---------- 图 2: 达布上和 vs 下和, 以及 Dirichlet 病态 ----------
def fig2_darboux_squeeze():
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(11.5, 5.0))

    # ---- 左图: 连续函数 f = 1 + 0.5*sin(2x) + 0.3x on [0, 4], Darboux 上下和 ----
    f = lambda t: 1.0 + 0.5*np.sin(2*t) + 0.3*t
    a, b = 0.0, 4.0
    n = 16
    xs = np.linspace(a, b, n + 1)
    w = xs[1] - xs[0]
    plot_fn(axL, f, a, b, color=BLUE, lw=2.4, label='f(x) continuous')
    upper, lower = 0.0, 0.0
    for i in range(n):
        xx = np.linspace(xs[i], xs[i+1], 50)
        yy = f(xx)
        hi, lo = yy.max(), yy.min()
        upper += hi * w
        lower += lo * w
        # 上和矩形(红, 半透明)和下和矩形(绿, 半透明)
        axL.add_patch(Rectangle((xs[i], 0), w, hi, facecolor=RED,
                                alpha=0.18, edgecolor=RED, lw=0.4, zorder=2))
        axL.add_patch(Rectangle((xs[i], 0), w, lo, facecolor=GREEN,
                                alpha=0.30, edgecolor=GREEN, lw=0.4, zorder=3))
    # 真积分值的水平线(sympy 精确)
    t = sp.symbols('t')
    exact = float(sp.integrate(1 + sp.Rational(1, 2)*sp.sin(2*t) + sp.Rational(3, 10)*t,
                               (t, 0, 4)))
    axL.axhline(exact, color=PURPLE, lw=1.6, ls='--',
                label='integral = %.3f' % exact)
    setup_axes(axL, xlim=(a, b), ylim=(0, 3.0),
               title='Darboux sums squeeze to the integral\n'
                     'upper=%.3f (red)  lower=%.3f (green)  gap=%.3f'
                     % (upper, lower, upper - lower),
               xlabel='x', ylabel='f(x)')
    axL.legend(loc='upper left', fontsize=9)

    # ---- 右图: Dirichlet 病态函数, 上下和永不夹拢 ----
    # 在 [0,1] 上, 有理点=1, 无理点=0. 任何小区间内都同时含稠密的有理与无理,
    # 所以每个小区间的 sup=1、inf=0, 于是上和恒=1, 下和恒=0, gap 恒=1.
    a2, b2 = 0.0, 1.0
    # 用两个分割(粗 n=4, 细 n=40)画上和(红条)与下和(绿条), 演示"切多细都没用"
    for nn, ybase, alpha in [(4, -0.35, 0.55), (40, 0.05, 0.35)]:
        xs2 = np.linspace(a2, b2, nn + 1)
        w2 = xs2[1] - xs2[0]
        for i in range(nn):
            # 上和: 高=1 的红条(画在 ybase 处, 高 1)
            axR.add_patch(Rectangle((xs2[i], ybase), w2, 1.0, facecolor=RED,
                                    alpha=alpha, edgecolor=RED, lw=0.4, zorder=2))
            # 下和: 高=0 的绿条(就是一条绿线, 用细长矩形示意)
            axR.add_patch(Rectangle((xs2[i], ybase - 0.12), w2, 0.04, facecolor=GREEN,
                                    alpha=0.9, edgecolor=GREEN, lw=0.3, zorder=3))
    # 两条参考线: 上和=1、下和=0
    axR.axhline(1.0, color=RED, lw=2.0, ls='--', label='upper sum = 1.0 (any partition)')
    axR.axhline(0.0, color=GREEN, lw=2.0, ls='--', label='lower sum = 0.0 (any partition)')
    setup_axes(axR, xlim=(a2, b2), ylim=(-0.6, 1.5),
               title='Dirichlet function: upper & lower never meet\n'
                     'upper - lower = 1.0 for ANY partition -> NOT Riemann integrable',
               xlabel='x', ylabel='f(x)')
    axR.legend(loc='center right', fontsize=8.5)
    save(fig, 'fig-3-07-2-darboux-squeeze.png')
    print('[ok] fig-3-07-2-darboux-squeeze.png')


if __name__ == '__main__':
    fig1_riemann_sum()
    fig2_darboux_squeeze()
    print('all figures generated.')
