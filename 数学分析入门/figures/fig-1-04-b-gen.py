"""P1-04 补充配图脚本(追加, 不修改原 fig-1-04-gen.py).

一张图:
  fig-1-04-3-nested-intervals.png  —— 区间套定理: 二分法夹出 sqrt(2)
                                     一串越缩越窄的闭区间, 长度 -> 0, 交集唯一
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---------- 先用数值核对二分法 ----------
s2 = np.sqrt(2)
print('[verify] sqrt(2) = %.10f' % s2)
a, b = 1.0, 2.0
print('[verify] nested intervals (bisection) bracketing sqrt(2):')
iters = []
for k in range(8):
    mid = (a + b) / 2.0
    if mid * mid < 2:
        a = mid
    else:
        b = mid
    iters.append((k + 1, a, b, b - a, a <= s2 <= b))
    print('   n=%d: [%.10f, %.10f]  len=%.2e  sqrt2 in? %s' %
          (k + 1, a, b, b - a, a <= s2 <= b))

# ========== 图 · 区间套定理: 二分法夹出 sqrt(2) ==========
fig, ax = plt.subplots(figsize=(9.5, 5.2))

N = 8
a, b = 1.0, 2.0
# 每个区间画成一条水平线段, 堆叠在不同 y 高度, 形成"套娃"画面
y_levels = np.linspace(0.15, 0.95, N)[::-1]   # 从下往上画(最早的最大区间在最下面)
colors_seq = [BLUE, '#2a9d8f', GREEN, ORANGE, '#e76f51', PURPLE, RED, '#6a4c93']

# 真值 sqrt(2) 竖线(贯穿)
ax.axvline(s2, color=RED, lw=1.8, ls=':', alpha=0.6, zorder=1)
ax.text(s2 + 0.005, 0.02, r'$\sqrt{2}\approx 1.41421356$',
        color=RED, fontsize=9.5, rotation=90, va='bottom')

a, b = 1.0, 2.0
for k in range(N):
    mid = (a + b) / 2.0
    # 画当前区间 [a, b] 为水平粗线段
    y0 = y_levels[k]
    c = colors_seq[k]
    ax.plot([a, b], [y0, y0], color=c, lw=4.5, alpha=0.85, zorder=4,
            solid_capstyle='round')
    # 两端打点
    ax.scatter([a, b], [y0, y0], color=c, s=22, zorder=6)
    # 标注区间长度
    ax.annotate(r'$I_{%d}$: $[%.4f,\ %.4f]$,  len=%.2e' % (k + 1, a, b, b - a),
                ((a + b) / 2, y0), xytext=((a + b) / 2 + 0.02, y0 + 0.015),
                color=c, fontsize=8.5)
    # 更新区间(把 sqrt(2) 套住的那一半)
    if mid * mid < 2:
        a = mid
    else:
        b = mid

# 顶部标注"唯一交点"
ax.annotate(r'intersection shrinks to one point: $\sqrt{2}$',
            (s2, y_levels[-1]), xytext=(s2 + 0.08, y_levels[-1] + 0.06),
            color=RED, fontsize=10,
            arrowprops=dict(arrowstyle='->', color=RED, lw=1.1))

setup_axes(ax, xlim=(0.98, 1.52), ylim=(0.0, 1.1),
           title=r'Nested intervals theorem:  bisection squeezing out $\sqrt{2}$  ($|I_n| \to 0$,  unique intersection)',
           xlabel='x', ylabel='', grid=True)
ax.set_yticks([])   # y 轴只是堆叠用, 不标数值
save(fig, 'fig-1-04-3-nested-intervals.png')
print('[done] fig-1-04-3-nested-intervals.png')
print('\n[supplement done] one extra figure generated.')
