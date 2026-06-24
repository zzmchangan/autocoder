"""P1-03《连续与一致连续:为什么"不断"还不够》配图生成脚本.

两张图:
  fig-1-03-1-uniform-vs-not.png   —— 一致连续 vs 不一致连续:delta 随点变化(以 1/x 为例)
  fig-1-03-2-weierstrass.png      —— Weierstrass 处处连续处处不可导函数(锯齿)
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, plot_curve, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, sympy as sp, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ========== 图 1 · 一致连续 vs 不一致连续:delta 随点变化 ==========
# f(x) = 1/x  在 (0, 1] 上不一致连续, delta 依赖点位置
# 对固定的 eps=0.2, 在不同点 x0 处反解满足 |1/x - 1/x0|<eps 的最大 delta
fig, axes = plt.subplots(1, 2, figsize=(12.6, 5.0))

eps = 0.2

def f(x):
    return 1.0 / x

# 左图: 不一致连续 f=1/x, 三个不同点处 delta 差异巨大
ax = axes[0]
xs = np.linspace(0.18, 2.5, 500)
ax.plot(xs, f(xs), color=BLUE, lw=2.4, label=r'$f(x)=1/x$  on $(0,2]$')

# 三个点 x0 = 1.5, 0.5, 0.2, 在每个点画 eps 横带和所需 delta 竖带
pts = [1.5, 0.5, 0.22]
colors_pt = [GREEN, ORANGE, RED]
for x0, c in zip(pts, colors_pt):
    y0 = f(x0)
    # |1/x - y0| < eps  <=>  x 在 (1/(y0+eps), 1/(y0-eps)) (取 x<x0 一侧的反解)
    # 更稳妥地: 数值找最大 delta 使 |x-x0|<delta 内 |f(x)-f(x0)|<eps
    # 对 1/x 单调, x = x0-d 处误差最大 (分母变小函数变大), 解 1/(x0-d) - 1/x0 = eps
    # => 1/(x0-d) = y0 + eps => x0 - d = 1/(y0+eps) => d = x0 - 1/(y0+eps)
    d_left = x0 - 1.0/(y0 + eps)
    # 右侧 d: 1/x0 - 1/(x0+d) = eps => 1/(x0+d) = y0 - eps => d = 1/(y0-eps) - x0
    if y0 - eps > 0:
        d_right = 1.0/(y0 - eps) - x0
    else:
        d_right = 5.0
    d = min(d_left, d_right)   # 取两侧最小, 保证整段满足
    # eps 横带
    ax.plot([x0 - d, x0 + d], [y0, y0], color=c, lw=2.2, alpha=0.9)
    ax.plot([x0 - d, x0 - d], [y0 - eps, y0 + eps], color=c, lw=1.0, ls=':', alpha=0.7)
    ax.plot([x0 + d, x0 + d], [y0 - eps, y0 + eps], color=c, lw=1.0, ls=':', alpha=0.7)
    ax.scatter([x0], [y0], color=c, s=42, zorder=6)
    # 标 delta
    ax.annotate(r'$x_0=%.2f,\ \delta\approx%.3f$' % (x0, d),
                (x0, y0), xytext=(x0 + 0.08, y0 + 0.25),
                color=c, fontsize=9.5)

ax.axhline(0, color='gray', lw=0.6, alpha=0.4)
ax.axvline(0, color='gray', lw=0.6, alpha=0.4)
setup_axes(ax, xlim=(0, 2.6), ylim=(0, 6.0),
           title=r'NOT uniformly continuous:  $f=1/x$  (for fixed $\epsilon=0.2$, $\delta$ shrinks near $0$)',
           xlabel='x', ylabel='f(x)', grid=True)
ax.legend(fontsize=10, loc='upper right')

# 右图: 一致连续 f=sqrt(x) 在 [0,1] (或 f=x^2 在 [0,1])
# 用 f(x) = x (最简单一致连续) 演示: 对 eps=0.2, 任何点 delta 都 = eps = 0.2
ax = axes[1]

def g(x):
    return x

xs = np.linspace(-0.1, 1.3, 400)
ax.plot(xs, g(xs), color=BLUE, lw=2.4, label=r'$g(x)=x$  on $[0,1]$')

eps2 = 0.2
delta2 = 0.2   # 对 f(x)=x, |f(x)-f(x0)|=|x-x0|, delta=eps 任何点都一样
for x0 in [0.1, 0.5, 0.9]:
    y0 = g(x0)
    c = GREEN
    ax.plot([x0 - delta2, x0 + delta2], [y0, y0], color=c, lw=2.2, alpha=0.9)
    ax.plot([x0 - delta2, x0 - delta2], [y0 - eps2, y0 + eps2], color=c, lw=1.0, ls=':', alpha=0.7)
    ax.plot([x0 + delta2, x0 + delta2], [y0 - eps2, y0 + eps2], color=c, lw=1.0, ls=':', alpha=0.7)
    ax.scatter([x0], [y0], color=c, s=42, zorder=6)

ax.annotate(r'same $\delta=0.2$ works at every $x_0$',
            (0.5, 0.5), xytext=(0.15, 1.0),
            color=GREEN, fontsize=10,
            arrowprops=dict(arrowstyle='->', color=GREEN, lw=1.0))
ax.axhline(0, color='gray', lw=0.6, alpha=0.4)
ax.axvline(0, color='gray', lw=0.6, alpha=0.4)
setup_axes(ax, xlim=(-0.1, 1.3), ylim=(-0.2, 1.4),
           title=r'Uniformly continuous:  $g=x$  (one $\delta$ fits all points)',
           xlabel='x', ylabel='g(x)', grid=True)
ax.legend(fontsize=10, loc='lower right')

fig.tight_layout()
fig.savefig(os.path.join(HERE, 'fig-1-03-1-uniform-vs-not.png'), dpi=150)
plt.close(fig)
print('[done] fig-1-03-1-uniform-vs-not.png')
# 报告三个点的 delta(对 1/x), 给正文核对
for x0 in [1.5, 0.5, 0.22]:
    y0 = 1.0/x0
    d_left = x0 - 1.0/(y0 + 0.2)
    d_right = 1.0/(y0 - 0.2) - x0 if (y0 - 0.2) > 0 else 5.0
    print('   1/x: x0=%.2f -> delta(min of L,R) = %.4f' % (x0, min(d_left, d_right)))


# ========== 图 2 · Weierstrass 处处连续处处不可导函数 ==========
# W(x) = sum_{k=0}^{N} a^k * cos(b^k * pi * x),  0<a<1, b odd integer, ab > 1+3pi/2
# 经典取 a=0.5, b=11 (满足 a*b>1+3pi/2)
fig, ax = plt.subplots(figsize=(9.5, 4.8))

def weierstrass(x, a=0.5, b=11.0, N=80):
    total = np.zeros_like(x, dtype=float)
    for k in range(N):
        total += (a ** k) * np.cos((b ** k) * np.pi * x)
    return total

xs = np.linspace(-2.0, 2.0, 8000)
ys = weierstrass(xs)
ax.plot(xs, ys, color=PURPLE, lw=0.7, label=r'$W(x)=\sum_{k=0}^{\infty} a^k \cos(b^k \pi x),\ a=0.5,\ b=11$')

setup_axes(ax, xlim=(-2.0, 2.0), ylim=(-2.6, 2.6),
           title='Weierstrass function:  continuous everywhere, differentiable nowhere',
           xlabel='x', ylabel='W(x)', grid=True)
ax.legend(fontsize=10, loc='upper right')
save(fig, 'fig-1-03-2-weierstrass.png')
print('[done] fig-1-03-2-weierstrass.png')

print('\n[all done] two figures generated.')
