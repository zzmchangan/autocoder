"""P1-04《实数系的完备性:极限为什么只在实数上成立》配图生成脚本.

两张图:
  fig-1-04-1-rational-holes.png   —— 数轴上有理数的"洞"(sqrt(2) 不在有理数中)
  fig-1-04-2-approx-sqrt2.png     —— 有理数列 1, 1.4, 1.41, 1.414, ... 逼近 sqrt(2)
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, plot_curve, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, sympy as sp, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---------- 先用 sympy / numpy 核对 ----------
print('[verify] sqrt(2) as sympy irrational =', sp.sqrt(2), '=', float(sp.sqrt(2)))
print('[verify] sqrt(2)^2 =', sp.simplify(sp.sqrt(2)**2))

# 有理逼近 sqrt(2) 的几个序列
print('[verify] decimal truncations of sqrt(2):')
s2 = float(sp.sqrt(2))
approx_vals = [1, 1.4, 1.41, 1.414, 1.4142, 1.41421]
for v in approx_vals:
    print('   %.5f -> squared = %.6f  (vs 2, diff = %.2e)' % (v, v*v, v*v - 2))

# 牛顿法 / 巴比伦法逼近 sqrt(2): x_{n+1} = (x_n + 2/x_n)/2
print('[verify] Babylonian method (x_{n+1} = (x_n + 2/x_n)/2):')
x = 1.0
for k in range(8):
    print('   iter %d: x = %.12f, |x^2 - 2| = %.2e' % (k, x, x*x - 2))
    x = (x + 2.0/x)/2.0


# ========== 图 1 · 数轴上有理数的"洞"(sqrt(2)) ==========
fig, ax = plt.subplots(figsize=(9.5, 3.4))

# 一根数轴
ax.axhline(0, color='gray', lw=2.0, zorder=1)

# 在数轴上标记一些有理点(密集的小竖线)
rationals = np.linspace(-1.5, 3.5, 60)
for r in rationals:
    ax.plot([r, r], [-0.04, 0.04], color=BLUE, lw=1.2, alpha=0.7)

# 标几个整数
for i in [-1, 0, 1, 2, 3]:
    ax.plot([i], [0], 'o', color=BLUE, ms=6)
    ax.text(i, -0.13, str(i), color=BLUE, fontsize=11, ha='center')

# sqrt(2) —— 一个"洞": 用空心圆 + 红色标注
ax.plot([s2], [0], marker='o', ms=12, mfc='white', mec=RED, mew=2.2, zorder=5)
ax.annotate(r'$\sqrt{2}\approx 1.4142135\ldots$' + '\n' + r'NOT in $\mathbb{Q}$  (a hole)',
            (s2, 0), xytext=(s2 + 0.25, 0.32),
            color=RED, fontsize=11,
            arrowprops=dict(arrowstyle='->', color=RED, lw=1.3))

# 在 sqrt(2) 附近画一段放大的"有理密集但有洞"示意
axins = ax.inset_axes([0.62, 0.55, 0.32, 0.7])
# 在 sqrt(2) 周围放更密的有理点, 但 sqrt(2) 本身留空
dense_r = np.linspace(s2 - 0.06, s2 + 0.06, 25)
for r in dense_r:
    if abs(r - s2) > 0.001:
        axins.plot([r], [0], '|', color=BLUE, ms=12, mew=1.5)
axins.axhline(0, color='gray', lw=1.0)
axins.plot([s2], [0], marker='o', ms=10, mfc='white', mec=RED, mew=2.0)
axins.set_xlim(s2 - 0.07, s2 + 0.07)
axins.set_ylim(-0.5, 0.5)
axins.set_yticks([])
axins.set_title(r'zoom in on $\sqrt{2}$: rationals dense, yet $\sqrt{2}$ missing',
                fontsize=8.5, color=RED)
axins.tick_params(labelsize=7)

setup_axes(ax, xlim=(-1.6, 3.6), ylim=(-0.3, 0.55),
           title=r'The number line $\mathbb{Q}$ is dense, but full of holes — $\sqrt{2}$ is one',
           xlabel='x', ylabel='', grid=False)
ax.set_yticks([])
save(fig, 'fig-1-04-1-rational-holes.png')
print('[done] fig-1-04-1-rational-holes.png')


# ========== 图 2 · 有理数列逼近 sqrt(2) ==========
fig, ax = plt.subplots(figsize=(8.2, 4.8))

# 巴比伦法序列
seq = [1.0]
x = 1.0
for _ in range(7):
    x = (x + 2.0/x)/2.0
    seq.append(x)
seq = np.array(seq)
ns = np.arange(len(seq))

# 真值 sqrt(2) 水平线
ax.axhline(s2, color=GREEN, lw=1.8, ls=':', label=r'$\sqrt{2}\approx 1.41421356$')

# 序列散点 + 折线
ax.plot(ns, seq, 'o-', color=BLUE, lw=1.8, ms=7.0, label=r'$x_{n+1} = (x_n + 2/x_n)/2$', zorder=4)

# 标几个早期点
for k in [0, 1, 2, 3]:
    ax.annotate('n=%d\n%.6f' % (k, seq[k]),
                (k, seq[k]),
                xytext=(k + 0.3, seq[k] - 0.13),
                color=BLUE, fontsize=8.5)

# 末点: 已经贴在 sqrt(2)
ax.scatter([ns[-1]], [seq[-1]], color=RED, s=46, zorder=6)
ax.annotate('n=%d\n%.10f' % (ns[-1], seq[-1]),
            (ns[-1], seq[-1]),
            xytext=(ns[-1] - 2.5, seq[-1] + 0.025),
            color=RED, fontsize=9,
            arrowprops=dict(arrowstyle='->', color=RED, lw=1.0))

setup_axes(ax, xlim=(-0.3, len(seq) - 0.5), ylim=(0.95, 1.55),
           title=r'A rational sequence converging to $\sqrt{2}$  (Babylonian method)',
           xlabel='n', ylabel=r'$x_n$', grid=True)
ax.legend(fontsize=10, loc='center right')
save(fig, 'fig-1-04-2-approx-sqrt2.png')
print('[done] fig-1-04-2-approx-sqrt2.png')

print('\n[all done] two figures generated.')
