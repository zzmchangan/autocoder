"""Fig 16 · 正交与投影 (第 16 章) 独立生成脚本.

绝不修改 _plot_utils.py / make_figures.py; 只 import 复用.

三张图:
  fig-16-1-projection-2d.png  : 向量 v 投到 u 方向(一条线)上的影子,
                                 带垂足、直角标记、误差线 v - proj.
  fig-16-2-orthogonal.png     : 点积 = 0 的几何 —— 两根垂直的箭头.
  fig-16-3-projection-3d.png  : 向量 v 投到一个二维平面(子空间)上, 误差垂直于平面.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import arrow, frame, save, GREEN, RED, BLUE, PURPLE, GRAY
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401


def _vline(ax, p, q, color, lw=1.8, ls='--', alpha=0.9, zorder=3):
    """画一条不带箭头的线段 p->q (用于垂线/误差线)."""
    p = np.asarray(p, float); q = np.asarray(q, float)
    ax.plot([p[0], q[0]], [p[1], q[1]], color=color, lw=lw, ls=ls, alpha=alpha, zorder=zorder)


def _right_angle(ax, corner, dir1, dir2, size=0.28, color=GRAY):
    """在 corner 处画一个直角标记, 沿 dir1, dir2 方向各伸出 size."""
    corner = np.asarray(corner, float)
    d1 = np.asarray(dir1, float); d1 = d1 / (np.linalg.norm(d1) + 1e-12) * size
    d2 = np.asarray(dir2, float); d2 = d2 / (np.linalg.norm(d2) + 1e-12) * size
    p1 = corner + d1; p2 = corner + d1 + d2; p3 = corner + d2
    ax.plot([p1[0], p2[0], p3[0]], [p1[1], p2[1], p3[1]], color=color, lw=1.4, zorder=4)


# =============================================================
# Fig 16.1  投影: 向量 v 投到 u 方向(一条直线)的影子.
# v = (3, 2),  u = (2, 0)  (x 轴方向)
# proj = (v.u / u.u) u = (6/4)*(2,0) = (3, 0)
# err = v - proj = (0, 2),  err . u = 0  (垂直!)
# =============================================================
v = np.array([3., 2.]); u = np.array([2., 0.])
proj = (v @ u) / (u @ u) * u
err = v - proj

fig, ax = plt.subplots(figsize=(7.4, 7.4))
# the subspace = the x-axis line (span of u), drawn long
ax.plot([-0.4, 4.6], [0, 0], color=BLUE, lw=3.2, alpha=0.30, zorder=1)
ax.text(4.55, -0.28, "span(u)  (a line)", color=BLUE, fontsize=11, ha='right')
# u
arrow(ax, (0, 0), u, BLUE, lw=2.0)
ax.text(u[0] + 0.08, u[1] + 0.18, "u", color=BLUE, fontsize=13)
# v
arrow(ax, (0, 0), v, GREEN, lw=2.6)
ax.text(v[0] + 0.10, v[1] + 0.12, "v", color=GREEN, fontsize=14, fontweight='bold')
# projection point
ax.plot(*proj, 'o', color=PURPLE, ms=9, zorder=6)
ax.text(proj[0] + 0.10, proj[1] - 0.42, "proj = (v·u/u·u)u", color=PURPLE, fontsize=12, fontweight='bold')
# the projection arrow (from origin to proj)
arrow(ax, (0, 0), proj, PURPLE, lw=2.6)
# error line v - proj  (the perpendicular)
_vline(ax, proj, v, RED, lw=2.4, ls='--')
ax.text(v[0] + 0.10, (proj[1] + v[1]) / 2 + 0.02, "error  v - proj  (perpendicular)", color=RED, fontsize=11)
# right-angle mark at the foot (corner = proj; one dir = +u, other dir = +err)
_right_angle(ax, proj, u, err, size=0.32)
ax.text(proj[0] - 0.05, 0.34, "90°", color=GRAY, fontsize=10)
frame(ax, 4.0)
ax.set_title("Projection:  v's shadow on the line span(u)\nerror is perpendicular to the subspace", fontsize=12)
save(fig, "fig-16-1-projection-2d.png")


# =============================================================
# Fig 16.2  正交: 点积 = 0  <=>  垂直.  a = (2,1), b = (-1,2), a.b = 0.
# =============================================================
a = np.array([2., 1.]); b = np.array([-1., 2.])
fig, ax = plt.subplots(figsize=(6.6, 6.6))
arrow(ax, (0, 0), a, GREEN, lw=2.6)
arrow(ax, (0, 0), b, RED, lw=2.6)
ax.text(a[0] + 0.10, a[1] + 0.08, "a = (2,1)", color=GREEN, fontsize=13, fontweight='bold')
ax.text(b[0] - 1.35, b[1] + 0.10, "b = (-1,2)", color=RED, fontsize=13, fontweight='bold')
_right_angle(ax, (0, 0), a, b, size=0.30)
ax.text(0.08, 0.10, "90°", color=GRAY, fontsize=10)
ax.text(-2.0, -1.55, "a·b = 2·(-1) + 1·2 = 0", fontsize=12, color=PURPLE)
ax.text(-2.0, -2.05, "=> orthogonal", fontsize=12, color=PURPLE, fontweight='bold')
frame(ax, 2.6)
ax.set_title("Orthogonal:  dot product = 0  <=>  perpendicular", fontsize=12)
save(fig, "fig-16-2-orthogonal.png")


# =============================================================
# Fig 16.3  向子空间投影(深度): 把 v 投到一个二维平面上.
# 3D 中, 平面 = xy-平面 (z=0), v = (1.6, 1.0, 2.2).
# proj = (1.6, 1.0, 0);  err = (0, 0, 2.2), 垂直于平面.
# =============================================================
v3 = np.array([1.6, 1.0, 2.2])
proj3 = np.array([1.6, 1.0, 0.0])
err3 = v3 - proj3

fig = plt.figure(figsize=(7.8, 7.2))
ax3 = fig.add_subplot(111, projection='3d')
# the plane (subspace) as a translucent patch
xs = np.array([-2.4, 2.6, 2.6, -2.4]); ys = np.array([-2.2, -2.2, 2.6, 2.6]); zs = np.zeros(4)
ax3.plot_trisurf(np.array([xs[0], xs[1], xs[2], xs[3]]),
                 np.array([ys[0], ys[1], ys[2], ys[3]]),
                 np.array([0, 0, 0, 0]), color=BLUE, alpha=0.16)
# v
ax3.quiver(0, 0, 0, v3[0], v3[1], v3[2], color=GREEN, linewidth=2.4, arrow_length_ratio=0.12)
ax3.text(v3[0] + 0.1, v3[1] + 0.1, v3[2] + 0.15, "v", color=GREEN, fontsize=14, fontweight='bold')
# projection
ax3.quiver(0, 0, 0, proj3[0], proj3[1], proj3[2], color=PURPLE, linewidth=2.4, arrow_length_ratio=0.14)
ax3.scatter(*proj3, color=PURPLE, s=46, depthshade=False)
ax3.text(proj3[0] + 0.12, proj3[1] - 0.55, proj3[2] - 0.05, "proj (closest point in plane)",
         color=PURPLE, fontsize=10.5, fontweight='bold')
# error (perpendicular to plane)
ax3.plot([proj3[0], v3[0]], [proj3[1], v3[1]], [proj3[2], v3[2]], color=RED, lw=2.2, ls='--')
ax3.text(proj3[0] + 0.1, proj3[1] + 0.15, (proj3[2] + v3[2]) / 2,
         "error  ⊥  plane", color=RED, fontsize=10.5)
ax3.set_xlim(-2.4, 2.8); ax3.set_ylim(-2.2, 2.6); ax3.set_zlim(0, 2.8)
ax3.set_xlabel("x"); ax3.set_ylabel("y"); ax3.set_zlabel("z")
ax3.set_title("Project v onto a 2D subspace (plane)\nproj = closest point to v inside the subspace",
              fontsize=11.5)
ax3.view_init(elev=20, azim=-58)
save(fig, "fig-16-3-projection-3d.png")

print("OK fig-16 all saved to", HERE)
