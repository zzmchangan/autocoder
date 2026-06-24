"""第 5 篇 第 15 章 配图脚本 · 线性方程组 Ax=b 的几何。

独立脚本, import 自共享 _plot_utils, 绝不修改 _plot_utils.py / make_figures.py。
生成两张图:
  fig-15-1-column-space-and-b.png
      两个子图对照解的存在性。
      左: 满秩 A, 列空间 = 整个平面, 任意 b 都在列空间 -> 有解(画出唯一解 x)。
      右: 降秩 A, 列空间 = 一条线; b1 不在线 -> 无解; b2 在线 -> 有解(且不唯一)。
  fig-15-2-three-cases.png
      行视角 vs 列视角对照, 三种情况(唯一/无/多)的行交点与列组合。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY  # noqa: F401
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon


# ============================================================
# 图 15.1  b 在不在列空间, 决定有没有解
# ============================================================
fig, axes = plt.subplots(1, 2, figsize=(13, 6.2))

# ---------- 左图: 满秩, 列空间 = 整个平面, 一定有解 ----------
ax = axes[0]
A = np.array([[2., 1.],
              [1., 3.]])
col1 = A[:, 0]   # (2,1)  绿
col2 = A[:, 1]   # (1,3)  红
b = np.array([3., 5.])
x = np.linalg.solve(A, b)   # (0.8, 1.4) -> 唯一解

# 用一片淡色点阵铺满整个平面, 表示"列空间 = 整个平面"
for a in np.linspace(-1.4, 1.4, 11):
    for bb in np.linspace(-1.4, 1.4, 11):
        p = a * col1 + bb * col2
        ax.plot(*p, '.', color=BLUE, alpha=0.18, ms=2)

# 两列
arrow(ax, (0, 0), col1, GREEN)
arrow(ax, (0, 0), col2, RED)
ax.text(col1[0] + .12, col1[1] - .28, "col1", color=GREEN, fontsize=12, fontweight='bold')
ax.text(col2[0] - .55, col2[1] + .12, "col2", color=RED, fontsize=12, fontweight='bold')

# b
arrow(ax, (0, 0), b, BLUE, lw=3.0)
ax.text(b[0] + .12, b[1] + .12, "b", color=BLUE, fontsize=14, fontweight='bold')

# 标注: b 落在(被铺满的)列空间里 -> 有解
ax.text(0.02, -0.62,
        "b in Col(A)  ->  solution exists\n"
        "x = (0.8, 1.4):  0.8*col1 + 1.4*col2 = b",
        color='#333333', fontsize=10.5,
        bbox=dict(boxstyle='round', fc='white', ec=BLUE, alpha=0.9))

frame(ax, 4.6)
ax.set_title("Full rank:  Col(A) = whole plane\n(every b is reachable, unique x)", fontsize=12)

# ---------- 右图: 降秩, 列空间 = 一条线 ----------
ax = axes[1]
A2 = np.array([[1., 2.],
               [2., 4.]])
col1b = A2[:, 0]   # (1,2)
# col2 = 2*col1, 共线 -> 列空间只是这条线 y=2x
line_dir = col1b

# 画列空间这条线(粗蓝半透明)
t = np.linspace(-2.6, 2.6, 2)
line = np.outer(t, line_dir)
ax.plot(line[:, 0], line[:, 1], color=BLUE, lw=7, alpha=0.25, zorder=1)
ax.text(-2.0, -3.7, "Col(A) = line y=2x", color=BLUE, fontsize=11, fontweight='bold')

# 两列(共线, 第二列画细一点表示冗余)
arrow(ax, (0, 0), col1b, GREEN)
arrow(ax, (0, 0), 2 * col1b, RED, lw=1.7)
ax.text(col1b[0] - .15, col1b[1] + .15, "col1", color=GREEN, fontsize=12, fontweight='bold')
ax.text((2 * col1b)[0] + .12, (2 * col1b)[1] - .05, "col2=2col1", color=RED, fontsize=10.5)

# 两个 b: b_off 不在线(无解), b_on 在线(多解)
b_off = np.array([3., 5.])   # 不在 y=2x
b_on = np.array([3., 6.])    # 在 y=2x

arrow(ax, (0, 0), b_off, ORANGE, lw=2.6)
ax.text(b_off[0] + .15, b_off[1] + .05, "b_off=(3,5)\nNOT on line -> no x", color=ORANGE,
        fontsize=10.5, fontweight='bold')

arrow(ax, (0, 0), b_on, PURPLE, lw=2.6)
ax.text(b_on[0] + .15, b_on[1] - .55, "b_on=(3,6)\non line -> many x", color=PURPLE,
        fontsize=10.5, fontweight='bold')

# 给 b_on 标一个特解组合: 3*col1 + 0*col2 = (3,6)
ax.text(0.02, -3.95,
        "x=(3,0):  3*col1 + 0*col2 = b_on\n"
        "but (2,-1) in Null(A) too  ->  infinitely many x",
        color='#333333', fontsize=10.5,
        bbox=dict(boxstyle='round', fc='white', ec=PURPLE, alpha=0.9))

frame(ax, 4.9)
ax.set_title("Rank 1:  Col(A) = a single line\n(b on line -> many;  b off line -> none)", fontsize=12)

fig.suptitle("Does x exist with  Ax = b ?   <=>   Is  b  in the column space of A ?",
             fontsize=14)
fig.tight_layout(rect=[0, 0, 1, 0.94])
save(fig, "fig-15-1-column-space-and-b.png")


# ============================================================
# 图 15.2  行视角 vs 列视角: 三种情况的几何
# ============================================================
fig, axes = plt.subplots(2, 3, figsize=(14.5, 9))

titles = [
    "Unique solution\n(rank 2, lines cross at one point)",
    "No solution\n(lines parallel, never meet)",
    "Infinitely many solutions\n(two lines coincide)",
]

# ----- 行视角(上排): 三种情况的直线交点 -----
# 例1: 2x+y=3, x+3y=5
# 例2: x+2y=3, 2x+4y=10  (矛盾)
# 例3: x+2y=3, 2x+4y=6   (重合)
row_specs = [
    ("2x + y = 3", lambda x: 3 - 2 * x, GREEN,
     "x + 3y = 5", lambda x: (5 - x) / 3, RED, axes[0, 0], titles[0]),
    ("x + 2y = 3", lambda x: (3 - x) / 2, GREEN,
     "2x + 4y = 10", lambda x: (10 - 2 * x) / 4, RED, axes[0, 1], titles[1]),
    ("x + 2y = 3", lambda x: (3 - x) / 2, GREEN,
     "2x + 4y = 6", lambda x: (6 - 2 * x) / 4, RED, axes[0, 2], titles[2]),
]
for (la, fa, ca, lb, fb, cb, ax, ti) in row_specs:
    xs = np.linspace(-3.5, 3.5, 100)
    ax.plot(xs, fa(xs), color=ca, lw=2.4, label=la)
    ax.plot(xs, fb(xs), color=cb, lw=2.4, label=lb)
    ax.legend(fontsize=9, loc='upper right')
    frame(ax, 4.0)
    ax.set_title(ti, fontsize=11)
# 例1 标出交点
axes[0, 0].plot(0.8, 1.4, 'o', color=BLUE, ms=10, zorder=6)
axes[0, 0].text(0.9, 1.55, "(0.8, 1.4)", color=BLUE, fontsize=11, fontweight='bold')

# ----- 列视角(下排): 同三种情况, 用列向量凑 b -----
col_specs = [
    # 例1: A=[[2,1],[1,3]], b=(3,5), 解 (0.8,1.4)
    (np.array([[2., 1.], [1., 3.]]), np.array([3., 5.]),
     np.array([0.8, 1.4]), None, axes[1, 0], titles[0]),
    # 例2: A=[[1,2],[2,4]], b=(3,5) 不在列空间
    (np.array([[1., 2.], [2., 4.]]), np.array([3., 5.]),
     None, None, axes[1, 1], titles[1]),
    # 例3: A=[[1,2],[2,4]], b=(3,6), 特解 (3,0), 零空间向量 (2,-1)
    (np.array([[1., 2.], [2., 4.]]), np.array([3., 6.]),
     np.array([3., 0.]), np.array([2., -1.]), axes[1, 2], titles[2]),
]
for (A, b, sol, nullvec, ax, ti) in col_specs:
    c1 = A[:, 0]
    c2 = A[:, 1]
    rank = np.linalg.matrix_rank(A)

    # 画列空间: 满秩铺点; 降秩画线
    if rank == 2:
        for a in np.linspace(-1.3, 1.3, 9):
            for bb in np.linspace(-1.3, 1.3, 9):
                p = a * c1 + bb * c2
                ax.plot(*p, '.', color=BLUE, alpha=0.16, ms=2)
    else:
        tt = np.linspace(-2.4, 2.4, 2)
        ln = np.outer(tt, c1)
        ax.plot(ln[:, 0], ln[:, 1], color=BLUE, lw=7, alpha=0.22, zorder=1)

    # 两列
    arrow(ax, (0, 0), c1, GREEN)
    arrow(ax, (0, 0), c2, RED, lw=1.7 if rank == 1 else 2.2)
    ax.text(c1[0] + .12, c1[1] + .12, "col1", color=GREEN, fontsize=11, fontweight='bold')
    ax.text(c2[0] + .12, c2[1] - .3, "col2" + ("=2col1" if rank == 1 else ""),
            color=RED, fontsize=10)

    # b
    arrow(ax, (0, 0), b, BLUE, lw=2.8)
    ax.text(b[0] + .15, b[1] + .12, "b", color=BLUE, fontsize=13, fontweight='bold')

    # 解的情况标注
    if sol is not None and nullvec is None:
        ax.text(-3.7, -3.6,
                "x=(0.8,1.4)\n0.8*col1 + 1.4*col2 = b",
                color='#333', fontsize=9.5,
                bbox=dict(boxstyle='round', fc='white', ec=BLUE, alpha=0.9))
    elif sol is None:
        ax.text(-3.7, -3.6,
                "b NOT in Col(A)\n-> no x can hit b",
                color='#333', fontsize=9.5,
                bbox=dict(boxstyle='round', fc='white', ec=ORANGE, alpha=0.9))
    else:
        # 多解: 画一个特解组合 + 零空间方向
        ax.text(-3.7, -3.6,
                "x=(3,0) + t*(2,-1)\nwhole line of x works",
                color='#333', fontsize=9.5,
                bbox=dict(boxstyle='round', fc='white', ec=PURPLE, alpha=0.9))

    frame(ax, 4.3)
    ax.set_title(ti, fontsize=11)

axes[0, 0].set_ylabel("Row view\n(lines crossing)", fontsize=11)
axes[1, 0].set_ylabel("Column view\n(vectors combining into b)", fontsize=11)

fig.suptitle("Three faces of Ax=b:   row view (top)   vs   column view (bottom)   --- one truth",
             fontsize=14)
fig.tight_layout(rect=[0, 0, 1, 0.95])
save(fig, "fig-15-2-three-cases.png")

print("OK figures saved to", HERE)
