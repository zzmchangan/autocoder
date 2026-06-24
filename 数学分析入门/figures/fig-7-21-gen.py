"""第 7 篇 · 第 21 章 · 算子与谱 —— 配图脚本(全书最后一章).

两张图:
  fig-7-21-1-operator.png  —— 算子作用于函数:把一个函数映成另一个(导数/积分算子示意)
  fig-7-21-2-spectrum.png  —— 谱:分立谱 vs 连续谱,对比有限维特征值
注意: 图内标注一律英文, 正文用中文.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, plot_curve, marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# =====================================================================
# 图 1: 算子作用于函数 —— 把一个函数映成另一个
# =====================================================================
fig, ax = plt.subplots(1, 1, figsize=(8.6, 4.6))

x = np.linspace(-np.pi, np.pi, 800)
f = np.sin(x)                       # 输入函数
Df = np.cos(x)                      # D = d/dx 作用后
D2f = -np.sin(x)                    # D^2 作用后

ax.plot(x, f, color=BLUE, lw=2.4, label='input $f(x)=\\sin x$')
ax.plot(x, Df, color=ORANGE, lw=2.0, ls='--',
        label='$T=\\frac{d}{dx}:\\quad Tf=\\cos x$')
ax.plot(x, D2f, color=GREEN, lw=2.0, ls='-.',
        label='$T=\\frac{d^2}{dx^2}:\\quad Tf=-\\sin x$')

# 一个箭头从 f 指向 Tf, 强调"映射"
ax.annotate('', xy=(2.2, np.cos(2.2)), xytext=(2.2, np.sin(2.2)),
            arrowprops=dict(arrowstyle='->', color=RED, lw=1.8))
ax.text(2.35, 0.35, 'operator $T$\nmaps $f \\mapsto Tf$', color=RED, fontsize=10)

setup_axes(ax, xlim=(-np.pi, np.pi), ylim=(-1.3, 1.3),
           title='an operator maps functions to functions',
           xlabel='$x$', ylabel='$y$', grid=True)
ax.legend(loc='upper right', fontsize=9.5)
save(fig, 'fig-7-21-1-operator.png')


# =====================================================================
# 图 2: 谱 —— 分立谱(分立的点) vs 连续谱(一整段)
# =====================================================================
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.4))

# 左图: 分立谱 —— 量子势阱中 -d^2/dx^2 的特征值 n^2
ns = np.arange(1, 9)
eigs = ns.astype(float) ** 2
for n, e in zip(ns, eigs):
    axL.plot([0, 1], [e, e], color=PURPLE, lw=2.2)
    axL.scatter([1], [e], color=PURPLE, s=42, zorder=5)
    axL.text(1.06, e, '$E_{%d}=%d$' % (n, int(e)), color=PURPLE,
             fontsize=9.5, va='center')
axL.set_xlim(-0.1, 1.8)
axL.set_ylim(-2, 75)
setup_axes(axL, xlim=(-0.1, 1.8), ylim=(-2, 75),
           title='discrete spectrum: particle in a box  $E_n=n^2$',
           xlabel='', ylabel='energy / eigenvalue', grid=False)
axL.set_xticks([])
axL.axhline(0, color='gray', lw=0.6, alpha=0.4)

# 右图: 连续谱 —— 自由粒子(无势阱),能量可取任意正值(一整段)
axR.axvspan(0, 8, color=ORANGE, alpha=0.35, label='continuous spectrum $[0,\\infty)$')
axR.plot([0, 8], [0, 0], color=RED, lw=2.4)
axR.text(4, -0.6, 'free particle: $E\\in[0,\\infty)$, no gaps',
         color=RED, fontsize=10, ha='center')
# 对比: 在区间里画几个分立点, 提示"和左边不同"
for e in [1, 4, 9, 16, 25]:
    axR.scatter([e], [0.5], color=PURPLE, s=30, zorder=5)
axR.text(13, 1.4, '(discrete points\nfor comparison)', color=PURPLE,
         fontsize=8.5, ha='center')
setup_axes(axR, xlim=(-1, 18), ylim=(-1.5, 3),
           title='continuous spectrum: free particle',
           xlabel='energy', ylabel='', grid=False)
axR.set_yticks([])
axR.legend(loc='upper right', fontsize=9)

fig.suptitle('Spectrum: discrete (eigenvalues) vs continuous',
             fontsize=12.5, y=1.02)
save(fig, 'fig-7-21-2-spectrum.png')

print('done: fig-7-21-1, fig-7-21-2')
