"""P4-09《数值级数:无穷相加何时有意义》配图生成脚本。

三张图:
  fig-4-09-1-converge-vs-diverge.png  —— 收敛 vs 发散的部分和对比(几何收敛、调和发散、交错调和收敛)
  fig-4-09-2-harmonic-groups.png     —— 调和级数发散的分组证明(每组之和 >= 1/2)
  fig-4-09-3-ratio-comparison.png    —— 比值判别/比较判别的直观(a_n=n/2^n 被几何级数夹住)
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_curve, plot_fn, band, marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, sympy as sp, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt

# ========== 图 1 · 收敛 vs 发散:三种级数的部分和 ==========
fig, ax = plt.subplots(figsize=(7.6, 4.5))
N = 60
n = np.arange(1, N + 1)

geom  = np.cumsum(0.5 ** (n - 1))                       # 1+1/2+1/4+...  -> 2 (收敛)
harm  = np.cumsum(1.0 / n)                              # 1+1/2+1/3+...  -> oo (发散)
alt   = np.cumsum((-1.0) ** (n + 1) / n)                # 1-1/2+1/3-...  -> ln2 (条件收敛)

ax.plot(n, geom, color=GREEN, lw=2.3, label=r'geometric  $\sum(1/2)^{n-1}$  $\rightarrow$ 2  (converges)')
ax.plot(n, alt,  color=BLUE,  lw=2.0, label=r'alternating  $\sum(-1)^{n+1}/n$  $\rightarrow$ ln2  (converges)')
ax.plot(n, harm, color=RED,   lw=2.3, label=r'harmonic  $\sum 1/n$  $\rightarrow$ $\infty$  (diverges)')

ax.axhline(2.0, color=GREEN, ls=':', lw=1.2, alpha=0.8)
ax.text(2.5, 2.06, 'limit = 2', color=GREEN, fontsize=9.5)
ax.axhline(np.log(2), color=BLUE, ls=':', lw=1.2, alpha=0.8)
ax.text(2.5, np.log(2) - 0.16, 'limit = ln2  $\\approx$ 0.693', color=BLUE, fontsize=9.5)

setup_axes(ax, xlim=(1, N), ylim=(0, max(harm[-1], 2) + 0.6),
           title='Partial sums: two converge, one diverges',
           xlabel='number of terms N', ylabel='partial sum  $S_N$', grid=True)
ax.legend(fontsize=9, loc='upper left')
save(fig, 'fig-4-09-1-converge-vs-diverge.png')

# ========== 图 2 · 调和级数发散:分组证明(每组之和 >= 1/2) ==========
fig, ax = plt.subplots(figsize=(7.6, 4.4))
# 分组:1; 1/2; (1/3+1/4); (1/5+1/6+1/7+1/8); (1/9..1/16); ...
# 每组从 1/2^k+1 到 1/2^(k+1),项数 2^k,每项 >= 1/2^(k+1),故组和 >= 1/2
groups = []
terms  = []
# 第 0 组:1/2 (单独,实际 1/2); 然后第 k 组含 2^k 项
# 标准证明:1 + 1/2 + (1/3+1/4) + (1/5..1/8) + (1/9..1/16) + ...
cur = 2                       # 下一项的下标,跳过 1(单独画)和 1/2(单独)
group_bounds = [(1, 1), (2, 2)]   # 1; 1/2
k = 1
while cur <= 1024:
    nxt = 2 * cur              # 第 k 组:下标 cur+1 .. nxt
    group_bounds.append((cur + 1, nxt))
    cur = nxt
    k += 1

# 画每一项为一个细竖条,按组染色,并在每组顶部标出"组和 >= 1/2"
colors_grp = [ORANGE, PURPLE, BLUE, GREEN, RED]
idx = 1.0
group_centers = []
group_vals    = []
gcolor_map    = []
for gi, (lo, hi) in enumerate(group_bounds):
    c = colors_grp[gi % len(colors_grp)]
    gsum = 0.0
    gstart = idx
    for m in range(lo, hi + 1):
        ax.bar(idx, 1.0 / m, width=0.8, bottom=0.0, color=c, alpha=0.7,
               edgecolor=c, lw=0.3, zorder=3)
        gsum += 1.0 / m
        idx += 1.0
    group_centers.append(0.5 * (gstart + idx - 1))
    group_vals.append(gsum)
    gcolor_map.append(c)

# 在每组上方画一条横线表示"这组的和",并标 >= 1/2
# 用累积高度:把每组堆叠显示(展示"组和"作为发散的阶梯)
ax.set_axis_off()

# 重新做一张更清晰的图:左边画分组的项,右边画"组和"的阶梯累积
fig.clf()
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11.2, 4.6), gridspec_kw={'width_ratios': [3, 2]})

# 左:每组项的和,作为一块"砖",每块高度 = 该组和
running = 0.0
xpos = 0.0
for gi, (lo, hi) in enumerate(group_bounds):
    c = colors_grp[gi % len(colors_grp)]
    gsum = sum(1.0 / m for m in range(lo, hi + 1))
    axL.bar(xpos + 0.5, gsum, width=0.85, bottom=running, color=c, alpha=0.75,
            edgecolor='black', lw=0.4, zorder=3)
    label_txt = '1' if lo == 1 == hi else ('1/2' if lo == 2 == hi else
                ('1/3+1/4' if lo == 3 else '1/%d..1/%d' % (lo, hi)))
    axL.text(xpos + 0.5, running + gsum / 2, label_txt, ha='center', va='center',
             fontsize=8, color='black')
    running += gsum
    xpos += 1.0
axL.axhline(running, color=RED, ls='--', lw=1.0, alpha=0.6)
axL.text(xpos - 0.5, running + 0.1, 'group sums stacked', color=RED, fontsize=8.5, ha='right')
axL.set_xlim(-0.2, xpos + 0.2)
axL.set_ylim(0, running + 0.5)
axL.set_xlabel('group index', fontsize=10)
axL.set_ylabel('cumulative group sum', fontsize=10)
axL.set_title('Each group of terms adds up to >= 1/2', fontsize=11)
axL.grid(True, alpha=0.15)

# 右:组和 >= 1/2 的阶梯,无穷阶梯 -> 发散
gs = []
for gi, (lo, hi) in enumerate(group_bounds):
    gs.append(sum(1.0 / m for m in range(lo, hi + 1)))
xs = np.arange(len(gs))
cum = np.cumsum(gs)
axR.step(xs, cum, where='post', color=RED, lw=2.2, zorder=4)
axR.scatter(xs, cum, color=RED, s=22, zorder=5)
for i in range(len(gs)):
    axR.axhline(cum[i], xmin=0, xmax=1, color=GREEN, ls=':', lw=0.8, alpha=0.4)
axR.text(len(gs) - 1.5, cum[-1] * 0.5,
         'every group >= 1/2\ninfinite groups  ==>  $\\infty$',
         fontsize=9.5, color=RED, ha='right',
         bbox=dict(boxstyle='round', fc='white', ec=RED, alpha=0.85))
axR.set_xlim(-0.3, len(gs) - 0.5)
axR.set_ylim(0, cum[-1] * 1.12)
axR.set_xlabel('number of groups', fontsize=10)
axR.set_ylabel('partial sum of group sums', fontsize=10)
axR.set_title('Infinitely many 1/2 steps diverge', fontsize=11)
axR.grid(True, alpha=0.15)

save(fig, 'fig-4-09-2-harmonic-groups.png')

# ========== 图 3 · 比值判别 / 比较判别:n/2^n 被几何级数夹住 ==========
fig, ax = plt.subplots(figsize=(7.4, 4.4))
nn = np.arange(1, 31)
a_n = nn / 2.0**nn                       # 被判别的级数通项
bound = 1.0 / (2.0 ** (nn / 2.0))         # 一个收敛的几何上界(足够大 n 后 a_n <= 它)

ax.semilogy(nn, a_n,   'o-', color=BLUE,   lw=2.0, ms=4, label=r'$a_n = n/2^n$  (series to test)')
ax.semilogy(nn, bound, 's--', color=GREEN, lw=1.6, ms=3, label=r'geometric upper bound  $\propto (1/\sqrt{2})^n$')
ax.fill_between(nn, a_n, bound, where=(bound >= a_n), color=GREEN, alpha=0.12)

ax.text(20, 0.5, 'ratio  $a_{n+1}/a_n \\to 1/2 < 1$\n==>  converges by ratio test',
        fontsize=10, color=BLUE, ha='center',
        bbox=dict(boxstyle='round', fc='white', ec=BLUE, alpha=0.9))
setup_axes(ax, xlim=(1, 30), ylim=(1e-9, 2),
           title=r'Comparison/ratio test:  $n/2^n$  is squeezed by a geometric series',
           xlabel='n', ylabel='term value  (log scale)', grid=True)
ax.legend(fontsize=9, loc='upper right')
save(fig, 'fig-4-09-3-ratio-comparison.png')

# 数值自检
print('check: geom->2, alt->ln2=%.4f, harm@%d=%.4f'
      % (np.log(2), N, harm[-1]))
print('check: sum n/2^n (num) = %.5f  (exact = 2)' % np.sum(nn / 2.0**nn))
print('done · 3 figures generated')
