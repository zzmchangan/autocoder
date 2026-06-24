"""第 7 篇 · 第 19 章 · 距离空间与完备化 —— 配图脚本.

两张图:
  fig-7-19-1-metric-convergence.png  —— 度量空间中的柯西列(收敛)vs 柯西列(扑空)
  fig-7-20-2-fixed-point.png         —— 压缩映射迭代逼近 cos x = x 的不动点 0.7390851...
注意: 图内标注一律英文, 正文用中文.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, plot_curve, marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, sympy as sp, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# =====================================================================
# 图 1: 度量空间里的柯西列 —— 完备(收敛) vs 不完备(扑空)
# =====================================================================
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.6))

# 左图: 在完备空间中, 柯西列收敛到一点 x*
# 用 1/k^2 部分和逼近 pi^2/6 的意象, 改写为一条"越来越挤在一起"的点列
target = 0.7390851
# 造一个显式 x_n -> x* 的点列, 差距按几何衰减
seq = []
x = 1.0
for _ in range(12):
    seq.append(x)
    x = np.cos(x)
seq = np.array(seq)

axL.axhline(target, color=GREEN, lw=1.5, ls='--', label='limit $x^*$')
axL.plot(range(len(seq)), seq, 'o-', color=BLUE, lw=1.4, ms=5, label='$x_n = \\cos(x_{n-1})$')
axL.axhspan(target - 0.04, target + 0.04, color=ORANGE, alpha=0.15, zorder=0)
marker(axL, len(seq) - 1, seq[-1], color=RED, text='Cauchy -> $x^*$', dx=-3.0, dy=-0.02, fs=10)
setup_axes(axL, xlim=(-0.5, len(seq) - 0.5), ylim=(0.5, 1.05),
           title='complete space: Cauchy sequence converges',
           xlabel='n', ylabel='$x_n$', grid=True)
axL.legend(loc='upper right', fontsize=9)

# 右图: 不完备空间(如有理数)中, 柯西列"该收敛却扑空"
# 用 sqrt(2) 的有理逼近点列 1, 1.4, 1.41, ... 演示
sqrt2 = np.sqrt(2)
trunc = [np.floor(sqrt2 * 10**k) / 10**k for k in range(1, 9)]
axR.axhline(sqrt2, color=RED, lw=1.6, ls='--', label='true limit $\\sqrt{2}\\notin\\mathbb{Q}$')
axR.plot(range(len(trunc)), trunc, 's-', color=PURPLE, lw=1.4, ms=5, label='rationals $\\in\\mathbb{Q}$')
# 在极限位置打一个空心点, 表示"该点不在空间里"
axR.scatter([len(trunc) - 1 + 0.6], [sqrt2], facecolors='none',
            edgecolors=RED, s=120, lw=2, zorder=6)
axR.annotate('hole! limit missing',
             (len(trunc) - 1 + 0.6, sqrt2),
             xytext=(len(trunc) - 1 - 3.2, 1.415),
             color=RED, fontsize=10,
             arrowprops=dict(arrowstyle='->', color=RED, lw=1.2))
setup_axes(axR, xlim=(-0.5, len(trunc) + 0.5), ylim=(1.38, 1.425),
           title='incomplete space $\\mathbb{Q}$: limit falls into a hole',
           xlabel='n (decimal digits)', ylabel='$x_n$', grid=True)
axR.legend(loc='lower right', fontsize=9)

fig.suptitle('Cauchy sequences behave differently depending on completeness',
             fontsize=12.5, y=1.02)
save(fig, 'fig-7-19-1-metric-convergence.png')


# =====================================================================
# 图 2: 压缩映射迭代逼近 cos x = x 的不动点
# =====================================================================
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.6))

# 左图: y = x 与 y = cos x 的交点 = 不动点; 画"蛛网"迭代
xs = np.linspace(0, 1.2, 400)
plot_fn(axL, np.cos, 0, 1.2, color=BLUE, lw=2.2, label='$y=\\cos x$')
axL.plot(xs, xs, color=GREEN, lw=2.2, label='$y=x$')

# 蛛网迭代
x = 1.0
pts_x, pts_y = [x], [0.0]
for _ in range(8):
    y = np.cos(x)               # 垂直到 cos 曲线
    pts_x.append(x); pts_y.append(y)
    pts_x.append(y); pts_y.append(y)   # 水平到 y=x
    x = y
axL.plot(pts_x, pts_y, color=ORANGE, lw=1.3, alpha=0.9, label='iteration $x_{n+1}=\\cos(x_n)$')

fp = 0.7390851
marker(axL, fp, fp, color=RED, text='fixed point $\\approx 0.7391$', dx=0.02, dy=-0.08, fs=10)
setup_axes(axL, xlim=(0, 1.2), ylim=(0, 1.2), equal=True,
           title='contraction $T(x)=\\cos x$: cobweb converges',
           xlabel='x', ylabel='y', grid=True)
axL.legend(loc='upper left', fontsize=8.5)

# 右图: |x_n - x*| 的几何衰减(对数纵轴)
seq = []
x = 1.0
for _ in range(25):
    seq.append(x)
    x = np.cos(x)
seq = np.array(seq)
err = np.abs(seq - fp)
axR.semilogy(range(len(seq)), err, 'o-', color=RED, lw=1.4, ms=4, label='$|x_n - x^*|$')
# 参考线: 0.674^n (压缩常数 q = |sin x*|)
q = np.sin(fp)
axR.semilogy(range(len(seq)), 0.3 * q**np.arange(len(seq)),
             '--', color=GREEN, lw=1.6, label='$C\\cdot q^{\\,n},\\ q=|\\sin x^*|\\approx%.3f$' % q)
setup_axes(axR, xlim=(-0.5, len(seq) - 0.5),
           title='error decays geometrically (linear on log scale)',
           xlabel='n', ylabel='$|x_n - x^*|$', grid=True)
axR.legend(loc='upper right', fontsize=9)

fig.suptitle('Banach fixed point: $x=\\cos x$ solved by iteration',
             fontsize=12.5, y=1.02)
save(fig, 'fig-7-19-2-fixed-point.png')

print('done: fig-7-19-1, fig-7-19-2')
