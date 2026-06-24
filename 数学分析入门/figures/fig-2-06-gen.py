"""P2-06《中值定理与泰勒展开》配图脚本(独立,不修改 _plot_utils.py)。

生成三张图:
  fig-2-06-1-mvt.png            拉格朗日中值定理:切线平行于割线
  fig-2-06-2-taylor-orders.png  1/3/5/7 阶泰勒多项式逼近 sin x
  fig-2-06-3-remainder.png      泰勒余项(误差)随阶数与距离衰减
"""
import sys, os, math
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (plot_fn, plot_curve, tangent, marker, band,
                         setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ----------------------------------------------------------------------
# 图 1 · 拉格朗日中值定理:曲线上某点切线平行于端点割线
# ----------------------------------------------------------------------
def fig_mvt():
    fig, ax = plt.subplots(figsize=(7.0, 4.6))
    f = lambda x: 0.5 * x ** 2 + 0.3 * x          # 一条光滑凸曲线
    a, b = -1.0, 2.0
    fa, fb = f(a), f(b)
    slope_sec = (fb - fa) / (b - a)               # 割线斜率
    # 中值定理保证存在 c,使 f'(c)=slope_sec。f'(x)=x+0.3 => c=slope_sec-0.3
    c = slope_sec - 0.3
    fc = f(c)

    plot_fn(ax, f, -1.6, 2.6, color=BLUE, lw=2.4,
            label=r"$f(x)=\frac{1}{2}x^2+\frac{3}{10}x$")
    # 割线(端点连线)
    plot_curve(ax, np.array([a, b]), np.array([fa, fb]),
               color=ORANGE, lw=2.2, label="secant (chord)")
    # 中值定理给出的切线:过 (c,f(c)) 且平行于割线
    tangent(ax, c, slope_sec, fc, span=1.4, color=RED, lw=2.0, ls='--',
            label="tangent at c, parallel to secant")
    # 端点与中点
    marker(ax, a, fa, color=GREEN, text="A(a,f(a))", dx=-0.35, dy=-0.45, fs=10)
    marker(ax, b, fb, color=GREEN, text="B(b,f(b))", dx=0.08, dy=-0.25, fs=10)
    marker(ax, c, fc, color=RED, text=f"c={c:.3f}", dx=0.05, dy=0.20, fs=10)
    # 辅助标注:f'(c)=secant slope
    ax.text(0.5, 3.1,
            r"$f'(c)=\dfrac{f(b)-f(a)}{b-a}=$" + f"{slope_sec:.3f}",
            color=RED, fontsize=11)
    setup_axes(ax, xlim=(-1.7, 2.7), ylim=(-0.6, 3.6),
               title="Mean Value Theorem: tangent at c is parallel to the chord")
    ax.legend(loc='lower right', fontsize=9)
    save(fig, "fig-2-06-1-mvt.png")


# ----------------------------------------------------------------------
# 图 2 · 1/3/5/7 阶泰勒多项式逼近 sin x
# ----------------------------------------------------------------------
def fig_taylor_orders():
    fig, ax = plt.subplots(figsize=(7.0, 4.6))
    plot_fn(ax, np.sin, -5, 5, color=BLUE, lw=2.6, label=r"$\sin x$ (true)")

    def taylor_sin(N):
        # 阶数 N(多项式最高次),奇数次
        def g(x):
            s = np.zeros_like(np.atleast_1d(x), dtype=float)
            sign = 1.0
            k = 1
            while k <= N:
                s = s + sign * x ** k / math.factorial(k)
                sign *= -1
                k += 2
            return s
        return g

    colors = {1: ORANGE, 3: GREEN, 5: PURPLE, 7: RED}
    for N in [1, 3, 5, 7]:
        plot_fn(ax, taylor_sin(N), -5, 5, color=colors[N], lw=1.8,
                label=f"Taylor order {N}")

    setup_axes(ax, xlim=(-5, 5), ylim=(-2.2, 2.2),
               title="Taylor polynomials approximate sin x (more orders, wider fit)")
    ax.legend(loc='lower right', fontsize=9, ncol=2)
    save(fig, "fig-2-06-2-taylor-orders.png")


# ----------------------------------------------------------------------
# 图 3 · 余项(误差):随阶数升高、距离原点增大如何衰减
# ----------------------------------------------------------------------
def fig_remainder():
    fig, axes = plt.subplots(1, 2, figsize=(9.4, 4.3))

    # 左:固定 x=1,误差 vs 阶数(对数纵轴)
    ax = axes[0]
    true = np.sin(1.0)
    orders = list(range(1, 16, 2))
    errs = []
    for N in orders:
        s = 0.0
        sign = 1.0
        k = 1
        while k <= N:
            s += sign / math.factorial(k)
            sign *= -1
            k += 2
        errs.append(abs(s - true))
    bounds = [1.0 / math.factorial(N + 2) for N in orders]  # 拉格朗日余项界
    ax.semilogy(orders, errs, 'o-', color=RED, lw=2, label="actual error")
    ax.semilogy(orders, bounds, 's--', color=GREEN, lw=1.6,
                label=r"Lagrange bound $1/(N+2)!$")
    setup_axes(ax, xlabel="Taylor order N", ylabel="error at x=1",
               title="Error at x=1 drops fast as order grows")
    ax.legend(fontsize=9)
    ax.grid(True, which='both', alpha=0.2)

    # 右:固定阶数 1/3/5/7,误差绝对值 vs |x|
    ax = axes[1]
    xs = np.linspace(0.01, 5.0, 200)

    def err_curve(N):
        out = []
        for xv in xs:
            s = 0.0
            sign = 1.0
            k = 1
            while k <= N:
                s += sign * xv ** k / math.factorial(k)
                sign *= -1
                k += 2
            out.append(abs(s - np.sin(xv)))
        return np.array(out)

    for N, col in zip([1, 3, 5, 7], [ORANGE, GREEN, PURPLE, RED]):
        ax.semilogy(xs, err_curve(N), color=col, lw=1.8,
                    label=f"order {N}")
    setup_axes(ax, xlabel="|x|", ylabel="|Taylor - sin x|",
               title="Error grows with distance from 0 (lower order faster)")
    ax.legend(fontsize=9, ncol=2)
    ax.grid(True, which='both', alpha=0.2)

    save(fig, "fig-2-06-3-remainder.png")


if __name__ == "__main__":
    fig_mvt()
    fig_taylor_orders()
    fig_remainder()
    print("generated: fig-2-06-1-mvt.png, fig-2-06-2-taylor-orders.png, fig-2-06-3-remainder.png")
