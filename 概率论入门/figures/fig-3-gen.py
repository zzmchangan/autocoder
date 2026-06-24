"""第 3 章 · 条件概率与独立性 配图脚本(独立, 不修改 _plot_utils.py)。

产出:
  fig-3-1-conditional-venn.png   -- 条件概率的文氏直觉: P(A|B) = A∩B 占 B 的份额
  fig-3-2-two-girls-and-tree.png -- 两孩问题: 已知至少一女, 求两个都是女(模拟验证 1/3)
                                    + 条件概率树
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
from matplotlib.patches import Circle, FancyArrowPatch

# ============================================================
# 图 3.1 · 条件概率 = A∩B 占 B 的份额(文氏/区域示意)
# 用摸球例子: 袋中 3 红 5 白, 摸两次不放回。
# 左: 朴素样本空间(8 个球); 右: 已知"第一次红"后, 样本空间缩成 7 个球(剩 2 红 5 白)
# ============================================================
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.2, 5.0))

# --- 左图: 朴素文氏图, 圆 B(第一次红) 与 圆 A(第二次红) 相交 ---
# 用两个圆示意, 相交部分 = A∩B
# 大圆代表全空间(淡灰底)
omega = Circle((0.5, 0.5), 0.46, facecolor=GRAY, alpha=0.10,
               edgecolor='k', linewidth=1.2)
ax1.add_patch(omega)
ax1.text(0.86, 0.86, r'$\Omega$', fontsize=14, ha='center', va='center')

# 圆 B: 第一次红 (蓝), 圆 A: 第二次红 (绿)
B_center, A_center, r = (0.38, 0.50), (0.62, 0.50), 0.26
cB = Circle(B_center, r, facecolor=BLUE, alpha=0.25, edgecolor=BLUE, linewidth=1.8)
cA = Circle(A_center, r, facecolor=GREEN, alpha=0.25, edgecolor=GREEN, linewidth=1.8)
ax1.add_patch(cB)
ax1.add_patch(cA)
ax1.text(B_center[0] - 0.10, B_center[1] + 0.16, r'$B$: 1st red',
         fontsize=10, color=BLUE, ha='center')
ax1.text(A_center[0] + 0.10, A_center[1] + 0.16, r'$A$: 2nd red',
         fontsize=10, color=GREEN, ha='center')

# 标注交集 A∩B: P=6/56, 占 B 的 (6/56)/(3/8) = 2/7
ax1.text(0.5, 0.46, r'$A \cap B$', fontsize=11, ha='center', va='center',
         color='black')
ax1.annotate(r'$P(A \cap B)=\frac{6}{56}$' + '\n(both red)',
             xy=(0.5, 0.42), xytext=(0.5, 0.10), fontsize=9, ha='center',
             color='black',
             arrowprops=dict(arrowstyle='->', color='black', lw=1.0))

# 文氏图下方公式
ax1.text(0.5, 0.02,
         r'$P(A|B)=\frac{P(A \cap B)}{P(B)}=\frac{6/56}{3/8}=\frac{2}{7}$',
         fontsize=12, ha='center', color=RED,
         bbox=dict(facecolor='white', alpha=0.9, edgecolor=RED, boxstyle='round,pad=0.3'))

ax1.set_xlim(0, 1); ax1.set_ylim(0, 1)
ax1.set_aspect('equal')
ax1.axis('off')
ax1.set_title('Conditional probability = A$\\cap$B as a share of B', fontsize=11)

# --- 右图: 已知 B(第一次红) 后, 样本空间缩成 B, 在 B 里重新标份额 ---
# 把"已知第一次摸走了一个红球后, 袋里剩 7 个球"画成 7 个小圆
# (2 红 5 白), A∩B = 第二次红的那 2 个
ax2.set_xlim(0, 1); ax2.set_ylim(0, 1)
ax2.set_aspect('equal')
ax2.axis('off')

# 新样本空间 B 的框
from matplotlib.patches import FancyBboxPatch
boxB = FancyBboxPatch((0.10, 0.42), 0.80, 0.40,
                      boxstyle="round,pad=0.02",
                      facecolor=BLUE, alpha=0.10, edgecolor=BLUE, linewidth=1.8)
ax2.add_patch(boxB)
ax2.text(0.50, 0.86, r'new universe: $B$ (after 1st red removed, 7 balls left)',
         fontsize=9.5, ha='center', color=BLUE)

# 7 个球: 2 红(在 A∩B 里) + 5 白
balls = [('R', True), ('R', True)] + [('W', False)] * 5
np.random.seed(0)
# 手动摆位, 7 个球排两行
positions = [(0.22, 0.62), (0.36, 0.62), (0.50, 0.62), (0.64, 0.62),
             (0.29, 0.50), (0.43, 0.50), (0.57, 0.50)]
for (lab, inA), (x, y) in zip(balls, positions):
    color = RED if lab == 'R' else 'white'
    ec = RED if lab == 'R' else GRAY
    ax2.add_patch(Circle((x, y), 0.05, facecolor=color, edgecolor=ec, linewidth=1.5))

# 框出 A∩B 区域(那 2 个红球)
from matplotlib.patches import Rectangle
rectA = Rectangle((0.16, 0.55), 0.26, 0.16, fill=False,
                  edgecolor=GREEN, linewidth=2.0, linestyle='--')
ax2.add_patch(rectA)
ax2.text(0.29, 0.73, r'$A \cap B$' + '\n(2nd red)', fontsize=9, ha='center',
         color=GREEN)

# 右下角公式: 在新样本空间里, A 的份额 = 2/7
ax2.text(0.50, 0.28,
         r'In the shrunken universe $B$:' + '\n'
         r'$P(A|B)=\frac{|A \cap B|}{|B|}=\frac{2}{7}$',
         fontsize=11, ha='center', color=RED,
         bbox=dict(facecolor='white', alpha=0.9, edgecolor=RED, boxstyle='round,pad=0.3'))
ax2.text(0.50, 0.10,
         'evidence shrinks the map,\nthen we re-measure the share',
         fontsize=9, ha='center', color=GRAY, style='italic')

ax2.set_title('Shrink the universe to $B$, re-measure A', fontsize=11)

plt.tight_layout()
save(fig, 'fig-3-1-conditional-venn.png')
print('saved fig-3-1-conditional-venn.png')


# ============================================================
# 图 3.2 · 两孩问题: 已知至少一女, 求两个都是女
# 左: 条件概率树(第一层=老大, 第二层=老二), 高亮符合条件的分支
# 右: 模拟收敛曲线(条件频率趋近 1/3)
# ============================================================
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.4, 5.2),
                                gridspec_kw={'width_ratios': [1.0, 1.15]})

# --- 左图: 条件概率树 ---
ax1.set_xlim(0, 10); ax1.set_ylim(0, 10)
ax1.axis('off')

# 节点坐标
root = (1.0, 5.0)
# 第一层: 老大 B / G
n_B1 = (4.0, 7.5)
n_G1 = (4.0, 2.5)
# 第二层: 老二 B / G
n_B1B2 = (8.0, 8.8); n_B1G2 = (8.0, 6.2)
n_G1B2 = (8.0, 3.8); n_G1G2 = (8.0, 1.2)

def arrow(ax, p, q, color=GRAY, lw=1.6, alpha=0.85):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle='-|>',
                                  mutation_scale=14, color=color, lw=lw, alpha=alpha))

# 根 -> 第一层
arrow(ax1, root, n_B1, color=BLUE)
arrow(ax1, root, n_G1, color=BLUE)
# 第一层 -> 第二层
arrow(ax1, n_B1, n_B1B2, color=GRAY)
arrow(ax1, n_B1, n_B1G2, color=GRAY)
arrow(ax1, n_G1, n_G1B2, color=GRAY)
arrow(ax1, n_G1, n_G1G2, color=GREEN, lw=2.4)   # 高亮 GG

# 节点标签
def node(ax, p, lab, color='black', fs=11):
    ax.scatter([p[0]], [p[1]], s=420, color='white',
               edgecolor=color, linewidth=1.8, zorder=5)
    ax.text(p[0], p[1], lab, fontsize=fs, ha='center', va='center', zorder=6)

node(ax1, root, 'start', color='black')
node(ax1, n_B1, 'B', color=BLUE); node(ax1, n_G1, 'G', color=BLUE)
node(ax1, n_B1B2, 'BB', color=GRAY); node(ax1, n_B1G2, 'BG', color=GRAY)
node(ax1, n_G1B2, 'GB', color=GRAY); node(ax1, n_G1G2, 'GG', color=GREEN)

# 边上的概率标签(每条 1/2)
ax1.text(2.5, 6.6, '1/2', fontsize=9, color=BLUE, ha='center')
ax1.text(2.5, 3.4, '1/2', fontsize=9, color=BLUE, ha='center')
ax1.text(6.0, 8.4, '1/2', fontsize=8.5, color=GRAY, ha='center')
ax1.text(6.0, 6.9, '1/2', fontsize=8.5, color=GRAY, ha='center')
ax1.text(6.0, 4.5, '1/2', fontsize=8.5, color=GRAY, ha='center')
ax1.text(6.0, 2.0, '1/2', fontsize=8.5, color=GREEN, ha='center')

# 四个叶子概率 1/4
for p, lab, col in [(n_B1B2, '1/4', GRAY), (n_B1G2, '1/4', GRAY),
                    (n_G1B2, '1/4', GRAY), (n_G1G2, '1/4', GREEN)]:
    ax1.text(p[0] + 0.55, p[1], lab, fontsize=9, color=col, ha='left', va='center')

# 条件: 已知"至少一个 G" -> 排除 BB, 剩 BG, GB, GG (各 1/4)
ax1.add_patch(Rectangle((7.2, 1.0), 2.0, 8.0, fill=True,
                         facecolor=GREEN, alpha=0.10, edgecolor=GREEN,
                         linewidth=1.5, linestyle='--'))
ax1.text(8.2, 9.6, 'given: at least one G\n(BB ruled out)',
         fontsize=9, ha='center', color=GREEN,
         bbox=dict(facecolor='white', alpha=0.85, edgecolor=GREEN, boxstyle='round,pad=0.3'))

# 结论
ax1.text(5.0, 0.2,
         r'$P(\mathrm{GG} \mid \geq 1\,G)=\frac{1/4}{3/4}=\frac{1}{3}$',
         fontsize=13, ha='center', color=RED,
         bbox=dict(facecolor='white', alpha=0.9, edgecolor=RED, boxstyle='round,pad=0.3'))
ax1.set_title('Probability tree: two-child problem', fontsize=11)

# --- 右图: 模拟收敛曲线 ---
rng = np.random.default_rng(42)
N = 100_000
kids = rng.integers(0, 2, size=(N, 2))           # 0=男, 1=女
at_least_one_girl = (kids.sum(axis=1) >= 1)
both_girls = (kids.sum(axis=1) == 2)

# 累计条件频率: 每次有"至少一女"才计入分母
ns = np.unique(np.geomspace(10, N, 200).astype(int))
freqs = []
for n in ns:
    a = at_least_one_girl[:n]
    b = both_girls[:n]
    if a.sum() > 0:
        freqs.append(b[a].mean())
    else:
        freqs.append(np.nan)
freqs = np.array(freqs)

ax2.plot(ns, freqs, color=BLUE, lw=1.6, label='simulation')
ax2.axhline(1/3, color=ORANGE, ls='--', lw=2.0, label='theory = 1/3')
ax2.set_xscale('log')
ax2.set_xlabel('number of families sampled  n')
ax2.set_ylabel(r'$P(\mathrm{both\ girls} \mid \geq 1\, \mathrm{girl})$')
ax2.set_ylim(0.15, 0.55)
ax2.set_title('Monte-Carlo: conditional frequency -> 1/3', fontsize=11)
ax2.legend(loc='upper right', fontsize=9)
ax2.grid(alpha=0.3, which='both')

# 最终模拟值标注
final = both_girls[at_least_one_girl].mean()
ax2.scatter([N], [final], color=RED, zorder=5, s=45)
ax2.annotate(f'n={N:,}: {final:.4f}', xy=(N, final),
             xytext=(N * 0.012, final + 0.05), fontsize=9, color=RED,
             arrowprops=dict(arrowstyle='->', color=RED, lw=1.0))

plt.tight_layout()
save(fig, 'fig-3-2-two-girls-and-tree.png')
print('saved fig-3-2-two-girls-and-tree.png')

print('\nALL FIGURES DONE.')
