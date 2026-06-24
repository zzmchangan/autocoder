"""第 2 章 · 概率空间 配图脚本(独立, 不修改 _plot_utils.py)。

产出:
  fig-2-1-dice-sum-pmf.png  -- 两颗骰子点数和的 PMF(理论柱 + 模拟频率叠加)
  fig-2-2-meeting-area.png  -- 约会问题:几何概率 = 面积比(|x-y|<=t 的带状区域)
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         plot_pmf, plot_pdf, sim_hist, convergence, heatmap2d,
                         vline, hline)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# ============================================================
# 图 2.1 · 两颗骰子点数和的 PMF(理论 + 模拟叠加)
# ============================================================
# 理论: 36 种等可能结果, 和=s 的结果数 = 6-|s-7|
rng = np.random.default_rng(42)

sums_theory = np.arange(2, 13)
counts = np.array([6 - abs(s - 7) for s in sums_theory])   # 1,2,3,4,5,6,5,4,3,2,1
ps_theory = counts / 36.0

# 模拟: 扔两颗骰子十万次
N = 100_000
rolls = rng.integers(1, 7, size=(N, 2))
sim_sums = rolls.sum(axis=1)
ps_sim = np.array([(sim_sums == s).mean() for s in sums_theory])

fig, ax = plt.subplots(figsize=(8.4, 4.8))
width = 0.38
# 理论柱(绿)
ax.bar(sums_theory - width/2, ps_theory, width=width, color=GREEN, alpha=0.85,
       edgecolor='k', linewidth=0.5, label='theory: count/36')
# 模拟柱(红, 半透明)
ax.bar(sums_theory + width/2, ps_sim, width=width, color=RED, alpha=0.55,
       edgecolor='k', linewidth=0.5, label=f'simulation: {N:,} rolls')

# 在理论柱顶标出概率值
for s, p in zip(sums_theory, ps_theory):
    ax.text(s - width/2, p + 0.004, f'{p:.3f}', ha='center', va='bottom',
            fontsize=7.5, color=GREEN)

ax.set_xlabel('sum of two dice')
ax.set_ylabel('P(sum = s)')
ax.set_xticks(sums_theory)
ax.set_ylim(0, max(ps_theory) * 1.18)
ax.set_title('Classical probability: sum of two fair dice', fontsize=11)
ax.legend(loc='upper left', fontsize=9)
ax.grid(axis='y', alpha=0.3)
save(fig, 'fig-2-1-dice-sum-pmf.png')
print('saved fig-2-1-dice-sum-pmf.png')

# ============================================================
# 图 2.2 · 约会问题:几何概率 = 面积比
# 两人各在 [0,60] 分钟内随机到达, 等待 t=15 分钟相遇。
# 相遇当 |x - y| <= 15, 即正方形内两条对角线之间的带状区域。
# P = 1 - (45/60)^2 = 7/16
# ============================================================
T = 60
wait = 15

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.4, 5.0))

# --- 左图: 几何区域 ---
# 整个 60x60 正方形(淡灰底)
ax1.add_patch(Rectangle((0, 0), T, T, facecolor=GRAY, alpha=0.12,
                         edgecolor='k', linewidth=1.2))
# 相遇带 |x-y|<=15 (填蓝): y in [x-15, x+15]
xs = np.linspace(0, T, 400)
band_low = np.clip(xs - wait, 0, T)
band_high = np.clip(xs + wait, 0, T)
ax1.fill_between(xs, band_low, band_high, color=BLUE, alpha=0.35,
                 label='meet: $|x-y| \\leq 15$')
# 两条对角线边界
ax1.plot(xs, xs - wait, color=BLUE, lw=1.4, ls='--')
ax1.plot(xs, xs + wait, color=BLUE, lw=1.4, ls='--')
# 主对角线 y=x(参考)
ax1.plot([0, T], [0, T], color=ORANGE, lw=1.2, ls=':', alpha=0.7)

ax1.set_xlim(0, T); ax1.set_ylim(0, T)
ax1.set_xlabel("A's arrival time  x  (min)")
ax1.set_ylabel("B's arrival time  y  (min)")
ax1.set_aspect('equal')
ax1.set_title('Geometric probability = area ratio', fontsize=11)
ax1.legend(loc='upper left', fontsize=9)
ax1.grid(alpha=0.25)
# 标注概率值
ax1.text(30, 5, 'P(meet) = blue area / 60$^2$ = 7/16 = 0.4375',
         ha='center', fontsize=9, color=BLUE,
         bbox=dict(facecolor='white', alpha=0.85, edgecolor=BLUE, boxstyle='round,pad=0.3'))

# --- 右图: 概率随等待时间 t 的变化 P(t) = 1 - ((T-t)/T)^2 ---
ts = np.linspace(0, T, 200)
ps = 1 - ((T - ts) / T) ** 2
ax2.plot(ts, ps, color=PURPLE, lw=2.4, label=r'$P(\mathrm{meet}) = 1 - \left(\frac{T-t}{T}\right)^2$')
# 标出 t=15 这一点
p15 = 1 - ((T - 15) / T) ** 2
ax2.scatter([15], [p15], color=RED, zorder=5, s=40)
ax2.annotate(f't=15: P = 7/16 = {p15:.4f}', xy=(15, p15),
             xytext=(22, p15 - 0.18), fontsize=9, color=RED,
             arrowprops=dict(arrowstyle='->', color=RED, lw=1.2))
# 标出 t=30(等一半时间): P=3/4
p30 = 1 - ((T - 30) / T) ** 2
ax2.scatter([30], [p30], color=GREEN, zorder=5, s=40)
ax2.annotate(f't=30: P = 3/4 = {p30:.4f}', xy=(30, p30),
             xytext=(33, p30 - 0.16), fontsize=9, color=GREEN,
             arrowprops=dict(arrowstyle='->', color=GREEN, lw=1.2))

ax2.set_xlabel('waiting time t (min)')
ax2.set_ylabel('P(meet)')
ax2.set_xlim(0, T); ax2.set_ylim(0, 1.02)
ax2.set_title('Meet probability vs waiting time', fontsize=11)
ax2.legend(loc='lower right', fontsize=9)
ax2.grid(alpha=0.3)

save(fig, 'fig-2-2-meeting-area.png')
print('saved fig-2-2-meeting-area.png')

print('\nALL FIGURES DONE.')
