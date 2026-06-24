"""P0-01《分析就是驯服无穷》配图生成脚本。

两张图:
  fig-0-01-1-infinite-sums.png  —— 无穷相加的两种命运(调和级数发散 vs 等比级数收敛)
  fig-0-01-2-epsilon-delta.png  —— ε-δ 是一份契约:你定 ε=0.1,我给 x>10
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_curve, setup_axes, save, GREEN, RED, BLUE, ORANGE
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ========== 图 1 · 无穷相加的两种命运 ==========
fig, ax = plt.subplots(figsize=(7.4, 4.3))
N = 50
n = np.arange(1, N + 1)
harm = np.cumsum(1.0 / n)            # 调和级数 1+1/2+1/3+... 部分和(发散)
geom = np.cumsum(0.5 ** (n - 1))     # 等比级数 1+1/2+1/4+... 部分和(收敛到 2)

ax.plot(n, harm, color=RED, lw=2.2, label='harmonic  1+1/2+1/3+...  (diverges)')
ax.plot(n, geom, color=GREEN, lw=2.2, label='geometric  1+1/2+1/4+...  (sum = 2)')
ax.axhline(2.0, color=GREEN, ls=':', lw=1.3, alpha=0.8)
ax.text(2, 2.07, 'limit = 2', color=GREEN, fontsize=10)
ax.axhline(harm[-1], color=RED, ls=':', lw=1.0, alpha=0.5)
ax.text(2, harm[-1] + 0.08, 'partial sum @50 = %.2f  (still growing)' % harm[-1],
        color=RED, fontsize=9)
setup_axes(ax, xlim=(1, N), ylim=(0, harm[-1] + 0.8),
           title='Two fates of "adding infinitely many terms"',
           xlabel='number of terms N', ylabel='partial sum', grid=True)
ax.legend(fontsize=9, loc='center right')
save(fig, 'fig-0-01-1-infinite-sums.png')

# ========== 图 2 · ε-δ 是一份契约(以 y=1/x 为例) ==========
fig, ax = plt.subplots(figsize=(6.8, 4.6))
eps = 0.1
x0 = 1.0 / eps                      # = 10,此处 1/x 恰好等于 eps
xs = np.linspace(0.6, 17, 400)
plot_curve(ax, xs, 1.0 / xs, color=BLUE, lw=2.4, label='y = 1/x')

ax.axhspan(0, eps, color=ORANGE, alpha=0.18)          # "你定的精度" ε 带
ax.axhline(eps, color=ORANGE, ls='--', lw=1.4)
ax.axvline(x0, color=RED, ls='--', lw=1.4)            # "我给的范围" δ 分界
ax.scatter([x0], [eps], color=RED, s=36, zorder=6)
ax.annotate('(10, 0.1)', (x0, eps), xytext=(x0 - 3.2, eps + 0.12),
            color=RED, fontsize=10)
ax.text(0.8, eps + 0.015, 'epsilon = 0.1  (you set the precision)',
        color=ORANGE, fontsize=9.5)
ax.text(x0 + 0.3, 0.92, 'x > 10  ==>  1/x < 0.1', color=RED, fontsize=10)
setup_axes(ax, xlim=(0, 17.5), ylim=(0, 1.25),
           title='epsilon-delta as a contract:  you set eps=0.1, I give x>10',
           xlabel='x', ylabel='1/x', grid=True)
ax.legend(fontsize=9, loc='upper right')
save(fig, 'fig-0-01-2-epsilon-delta.png')

print('done · harm@50 = %.4f · geom@50 = %.4f · 1/10 = %.2f'
      % (harm[-1], geom[-1], 1.0 / x0))
