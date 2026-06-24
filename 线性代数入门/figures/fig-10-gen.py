"""第 10 章 · 秩:还剩几维 —— 配图生成脚本。

本脚本独立运行, 只 import 自共享的 _plot_utils.py, 绝不修改 _plot_utils.py 或 make_figures.py。

产出:
  fig-10-1-full-vs-rank-deficient.png   主图: 满秩(rank 2, 平面揉成平面) vs 降秩(rank 1, 平面压成线)
  fig-10-2-rank-nullity.png             (深度) 秩-零度定理: 输入空间被拆成"被揉到输出的部分(秩)"
                                        和"被揉到原点的部分(零空间)", 两者维数相加 = 输入总维数

所有图内标注一律用英文, 正文用中文。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE, GRAY
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon


# ============================================================
# Fig 10.1 —— 满秩 vs 降秩: 揉捏后空间还剩几维
#   左: 满秩矩阵 A = [[1,2],[3,4]], rank 2.
#       两列不共线, 整个 2D 平面被揉成整个 2D 平面(只是被拉伸+翻转)。
#   右: 降秩矩阵 B = [[1,2],[2,4]], rank 1.
#       两列共线(第二列 = 2 * 第一列), 整个 2D 平面被压扁成一条 1D 直线。
# ============================================================
A = np.array([[1., 2.],
              [3., 4.]])          # rank 2, det = -2
B = np.array([[1., 2.],
              [2., 4.]])          # rank 1, det = 0; 两列均沿 (1,2)

fig, axes = plt.subplots(1, 2, figsize=(11.5, 6.0))

# ---------- 左: 满秩 ----------
ax = axes[0]
grid(ax, np.eye(2), GRAY, 0.30, ls='--')   # 揉之前(原始方格)
grid(ax, A, BLUE, 0.55, ls='-', lw=0.9)    # 揉之后(仍是一片 2D 网格)
arrow(ax, (0, 0), A[:, 0], GREEN)          # 新 i'
arrow(ax, (0, 0), A[:, 1], RED)            # 新 j'
ax.text(A[0, 0] + 0.1, A[1, 0] + 0.1, "col1 (new i')", color=GREEN, fontsize=11, fontweight='bold')
ax.text(A[0, 1] + 0.1, A[1, 1] - 0.35, "col2 (new j')", color=RED, fontsize=11, fontweight='bold')
frame(ax, 5.6)
ax.set_title("Full rank  A=[[1,2],[3,4]]   rank 2\n"
             "2D plane  ->  2D plane  (NOT flattened)", fontsize=11.5)

# ---------- 右: 降秩 rank 1 ----------
ax = axes[1]
grid(ax, np.eye(2), GRAY, 0.30, ls='--')   # 揉之前
# 揉之后: B 的网格会塌成一族平行线(全沿 col1 方向)
grid(ax, B, BLUE, 0.60, ls='-', lw=1.0)
# 标出两列: 共线(都沿 (1,2)), 故压成一条线
arrow(ax, (0, 0), B[:, 0], GREEN)
arrow(ax, (0, 0), B[:, 1], RED, lw=1.6)    # col2 = 2*col1, 与 col1 共线
ax.text(B[0, 0] - 1.3, B[1, 0] - 0.2, "col1 (1,2)", color=GREEN, fontsize=11, fontweight='bold')
ax.text(B[0, 1] + 0.15, B[1, 1] + 0.1, "col2=2*col1", color=RED, fontsize=11, fontweight='bold')
# 沿着压扁后的方向(列空间)画一根粗线, 强调"整个平面被塞进这条线"
t = np.linspace(-2.2, 2.2, 2)
col_dir = B[:, 0]
line = np.outer(t, col_dir)
ax.plot(line[:, 0], line[:, 1], color=PURPLE, lw=4.5, alpha=0.30, zorder=2)
ax.text(col_dir[0] * 1.6 + 0.1, col_dir[1] * 1.6 + 0.1,
        "column space = a LINE", color=PURPLE, fontsize=10.5, fontweight='bold')
frame(ax, 5.6)
ax.set_title("Rank deficient  B=[[1,2],[2,4]]   rank 1\n"
             "2D plane  ->  1D line  (flattened!)", fontsize=11.5)

fig.suptitle("Rank = how many dimensions survive the squash   "
             "(gray dashed = before, blue solid = after)",
             fontsize=13.5)
fig.tight_layout(rect=[0, 0, 1, 0.94])
save(fig, "fig-10-1-full-vs-rank-deficient.png")


# ============================================================
# Fig 10.2 —— 秩-零度定理 (rank + nullity = 输入列数)
#   用 B = [[1,2],[2,4]] (rank 1) 演示。
#   输入是整个 2D 平面。揉捏把它拆成两半:
#     - 一条"存活"的方向(沿 col1=(1,2)): 被揉到输出(列空间), 维数 = 秩 = 1。
#     - 一条"阵亡"的方向(沿 null=(2,-1)): 被揉到原点, 维数 = 零空间 = 1。
#   1 + 1 = 2 = 输入列数。
# ============================================================
fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.8))

# 输入空间: 标出"存活轴"(绿, 列空间的原像) 与 "阵亡轴"(紫, 零空间)
ax = axes[0]
grid(ax, np.eye(2), GRAY, 0.28, ls='--')
survive = np.array([1., 2.]) / np.linalg.norm([1., 2.]) * 2.0     # 列空间方向的代表(单位化后拉长)
nullv   = np.array([2., -1.]) / np.linalg.norm([2., -1.]) * 2.0   # 零空间方向(与列空间垂直)
arrow(ax, (0, 0), survive, GREEN, lw=2.6)
arrow(ax, (0, 0), nullv,   PURPLE, lw=2.6)
ax.text(survive[0] + 0.1, survive[1] + 0.1, "survives\n(-> column space)", color=GREEN, fontsize=10.5, fontweight='bold')
ax.text(nullv[0] - 2.1, nullv[1] + 0.15, "crushed to origin\n(null space)", color=PURPLE, fontsize=10.5, fontweight='bold')
frame(ax, 3.4)
ax.set_title("Input space (2D):  split into two directions", fontsize=11.5)

# 输出空间: 揉捏后, "存活"方向被沿到一条线上(rank 1), "阵亡"方向全堆到原点
ax = axes[1]
grid(ax, B, BLUE, 0.55, ls='-', lw=0.9)
# 输出只剩一条线(列空间): 沿 (1,2) 方向
t = np.linspace(-1.8, 1.8, 2)
line = np.outer(t, np.array([1., 2.]))
ax.plot(line[:, 0], line[:, 1], color=GREEN, lw=4.5, alpha=0.35, zorder=2)
# 原点高亮: 所有"阵亡"方向都被揉到这里
ax.plot(0, 0, 'o', color=PURPLE, markersize=11, zorder=6)
ax.text(0.15, -0.45, "null-space dirs\nall land HERE", color=PURPLE, fontsize=10, fontweight='bold')
ax.text(1.2 * 1 + 0.1, 1.2 * 2 + 0.1, "output = a line\n(rank 1)", color=GREEN, fontsize=10.5, fontweight='bold')
frame(ax, 5.4)
ax.set_title("Output:  rank 1 line  +  everything else -> origin\n"
             "rank(1) + nullity(1) = input cols(2)", fontsize=11.5)

fig.suptitle("Rank-Nullity:  input split into  "
             "'survives -> output (rank)'  +  'crushed to origin (null space)'",
             fontsize=13)
fig.tight_layout(rect=[0, 0, 1, 0.94])
save(fig, "fig-10-2-rank-nullity.png")

print("OK fig-10 figures saved to", HERE)
