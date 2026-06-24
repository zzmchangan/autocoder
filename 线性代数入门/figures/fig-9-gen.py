"""第 9 章 · 行列式 配图生成脚本(独立,绝不修改 _plot_utils.py / make_figures.py)。

图 9.1: 单位正方形被矩阵揉成平行四边形,标注面积 = |det|。
        四子图:Shear(det=1,面积不变只变形)、Scale x2(det=4)、
        Singular(det=0,退化成线段)、Flip(det=-1,翻转)。
图 9.2: det=0 的压扁(单独放大看),平行四边形退化成一条线段,面积=0。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon


def draw_parallelogram(ax, M, color, alpha=0.22):
    """画以原点 + 矩阵两列为边的平行四边形(填充 + 边线)。"""
    c1 = M[:, 0]; c2 = M[:, 1]
    pts = np.array([0 * c1, c1, c1 + c2, c2])
    ax.add_patch(Polygon(pts, closed=True, facecolor=color, alpha=alpha,
                         edgecolor=color, lw=2.0, zorder=2))


def draw_unit_square(ax, color=GREEN, alpha=0.15):
    """画原始单位正方形(虚线边)。"""
    pts = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], float)
    ax.add_patch(Polygon(pts, closed=True, facecolor=color, alpha=alpha,
                         edgecolor=color, lw=1.4, ls='--', zorder=1))


# =========================================================================
# Fig 9.1: 四种变换的平行四边形 + 面积标注(2x2 组图)
# =========================================================================
cases = [
    ("Shear  det = 1\n(area unchanged, shape bent)", np.array([[1., 1], [0, 1]])),
    ("Scale x2  det = 4\n(area x4)",                  np.array([[2., 0], [0, 2]])),
    ("Singular  det = 0\n(area -> 0, collapsed)",     np.array([[1., 2], [2, 4]])),
    ("Flip x-axis  det = -1\n(flipped, area x1)",     np.array([[1., 0], [0, -1]])),
]

fig, axes = plt.subplots(2, 2, figsize=(10.5, 10.5))
for ax, (name, M) in zip(axes.ravel(), cases):
    # 原始方格(灰虚)+ 单位正方形(绿虚)
    grid(ax, np.eye(2), 'gray', 0.30, '--', lw=0.8)
    draw_unit_square(ax, color=GREEN, alpha=0.12)
    # 揉捏后方格(蓝实)+ 平行四边形(蓝填充)
    grid(ax, M, BLUE, 0.42, '-', lw=0.8)
    draw_parallelogram(ax, M, BLUE, alpha=0.22)
    # 两根新基向量
    arrow(ax, (0, 0), M[:, 0], GREEN, lw=2.4)
    arrow(ax, (0, 0), M[:, 1], RED, lw=2.4)
    ax.text(M[0, 0] * 1.18, M[1, 0] * 1.18, "i'", color=GREEN,
            fontsize=12, fontweight='bold')
    ax.text(M[0, 1] * 1.18, M[1, 1] * 1.18, "j'", color=RED,
            fontsize=12, fontweight='bold')
    # 面积标注(放在平行四边形重心附近)
    center = (M[:, 0] + M[:, 1]) / 3.0
    det = np.linalg.det(M)
    ax.text(center[0], center[1] - 0.28,
            f"area = |det| = {abs(det):.0f}",
            color=BLUE, fontsize=11, ha='center', fontweight='bold')
    frame(ax, 4.0, name)

fig.suptitle("det = how much area scales after the squish   "
             "(gray dashed grid = before, blue = after)",
             fontsize=14)
fig.tight_layout(rect=[0, 0, 1, 0.965])
save(fig, "fig-9-1-area-scale.png")
print("saved fig-9-1-area-scale.png")


# =========================================================================
# Fig 9.2: det=0 压扁 —— 单独放大,讲清"退化成线段"
# =========================================================================
fig, ax = plt.subplots(figsize=(8.5, 7.5))
M = np.array([[1., 2], [2, 4]])   # 两列共线 (2,4)=2*(1,2),det=0

# 原始方格 + 单位正方形
grid(ax, np.eye(2), 'gray', 0.30, '--', lw=0.8)
draw_unit_square(ax, color=GREEN, alpha=0.15)
# 揉捏后方格(全部共线,只能画出一条方向的线)
grid(ax, M, BLUE, 0.55, '-', lw=1.0)
# 退化"平行四边形":四个顶点 (0, c1=(1,2), c1+c2=(3,6), c2=(2,4)) 全在一条线上
c1 = M[:, 0]; c2 = M[:, 1]
# 把退化线段画粗一点(从 -0.5*c1 到 2.5*c1)
t = np.array([-0.6, 2.8])
line = np.outer(t, c1 / np.linalg.norm(c1) * np.linalg.norm(c1))
ax.plot(line[:, 0], line[:, 1], color=BLUE, lw=5.0, alpha=0.35, zorder=2)
# 两根共线的基向量
arrow(ax, (0, 0), c1, GREEN, lw=2.6)
arrow(ax, (0, 0), c2, RED, lw=2.0)
ax.text(c1[0] + 0.15, c1[1] + 0.15, "col 1 = (1,2)", color=GREEN,
        fontsize=12, fontweight='bold')
ax.text(c2[0] + 0.15, c2[1] - 0.35, "col 2 = (2,4) = 2 x col 1", color=RED,
        fontsize=12, fontweight='bold')
# 标注
ax.text(1.6, 3.6, "two columns on the same line\n"
                  "-> parallelogram has 0 area\n"
                  "-> det = 0  (space collapsed to 1D)",
        color=BLUE, fontsize=12, ha='left', va='center',
        bbox=dict(boxstyle='round,pad=0.4', facecolor='white',
                  edgecolor=BLUE, alpha=0.9))
frame(ax, 5.0, "det = 0 :  2D plane squished into a single line")
save(fig, "fig-9-2-singular-collapse.png")
print("saved fig-9-2-singular-collapse.png")

print("OK chapter 9 figures done.")
