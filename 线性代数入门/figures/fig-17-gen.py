"""Fig 17 · 最小二乘 (第 17 章) 独立生成脚本.

绝不修改 _plot_utils.py / make_figures.py; 只 import 复用.

两张图:
  fig-17-1-line-fit.png       : 直线拟合的经典画面 —— 4 个散点 + 最佳拟合直线
                                 y = 0.8 + 0.8 t, 带每点的垂直残差(误差)线.
  fig-17-2-column-space-view.png : 列空间视角 —— 在 4 维(用示意图表达)里, 目标 b,
                                 它在列空间上的投影 p = A xhat, 误差 e = b - p 垂直于列空间.

数值: 4 散点 (0,1),(1,1),(2,3),(3,3).
      A = [[1,0],[1,1],[1,2],[1,3]] , b = [1,1,3,3].
      xhat = (0.8, 0.8), p = A xhat = [0.8,1.6,2.4,3.2], e = [0.2,-0.6,0.6,-0.2].
      A^T e = 0 (误差垂直于列空间), 已 numpy 核对.
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


def _vline(ax, p, q, color, lw=1.8, ls='--', alpha=0.9, zorder=3):
    """画一条不带箭头的线段 p->q (用于残差线)."""
    p = np.asarray(p, float); q = np.asarray(q, float)
    ax.plot([p[0], q[0]], [p[1], q[1]], color=color, lw=lw, ls=ls, alpha=alpha, zorder=zorder)


# =============================================================
# 数据 & 最小二乘解 (numpy 算对, 供画图严格一致)
# =============================================================
t = np.array([0., 1., 2., 3.])
b = np.array([1., 1., 3., 3.])
A = np.vstack([np.ones_like(t), t]).T          # A 的第1列全是1, 第2列是 t_i
ATA = A.T @ A
ATb = A.T @ b
xhat = np.linalg.solve(ATA, ATb)               # (0.8, 0.8)
p = A @ xhat                                   # 投影 = 拟合直线上的值
e = b - p                                      # 残差
c, d = xhat                                    # 截距, 斜率


# =============================================================
# Fig 17.1  直线拟合: 散点 + 最佳拟合直线 + 每点残差线
# =============================================================
fig, ax = plt.subplots(figsize=(8.2, 6.6))
# 最佳拟合直线 (画长一点)
t_line = np.linspace(-0.4, 3.6, 50)
y_line = c + d * t_line
ax.plot(t_line, y_line, color=BLUE, lw=2.6, zorder=2,
        label=f"best-fit line  y = {c:.2f} + {d:.2f} t")
# 散点
ax.scatter(t, b, color=GREEN, s=80, zorder=5, label="data points  (t_i, y_i)")
# 每个点的残差线 (data -> fit) 和拟合点
for ti, yi, pi in zip(t, b, p):
    _vline(ax, (ti, yi), (ti, pi), RED, lw=1.7, ls='--', alpha=0.85)
    ax.scatter([ti], [pi], color=PURPLE, s=34, zorder=4)
# 残差标注 (只在第一根上标, 避免拥挤)
ax.annotate("residual  y_i - (c + d t_i)\n(minimized in sum of squares)",
            xy=(2, 3), xytext=(2.05, 3.55),
            color=RED, fontsize=10.5,
            arrowprops=dict(arrowstyle='-', color=RED, lw=1.0, alpha=0.7))
ax.text(0.02, 0.97, f"slope d = {d:.2f}\nintercept c = {c:.2f}\n||residual|| = {np.linalg.norm(e):.3f}",
        transform=ax.transAxes, va='top', ha='left', fontsize=10.5,
        bbox=dict(boxstyle='round,pad=0.35', fc='white', ec=GRAY, alpha=0.85))
ax.set_xlabel("t", fontsize=12); ax.set_ylabel("y", fontsize=12)
ax.set_xlim(-0.5, 3.7); ax.set_ylim(0.2, 3.9)
ax.axhline(0, color='gray', lw=0.6, alpha=0.35); ax.axvline(0, color='gray', lw=0.6, alpha=0.35)
ax.legend(loc='lower right', fontsize=10.5, framealpha=0.9)
ax.set_title("Least squares:  best-fit line minimizes the sum of squared residuals",
             fontsize=12)
save(fig, "fig-17-1-line-fit.png")


# =============================================================
# Fig 17.2  列空间视角: b 投到 Col(A), 投影 p = A xhat, 误差 e ⊥ Col(A)
# (4 维无法直画, 用一张示意图表达几何关系: 一条平直线 = 列空间,
#  b 在它之外, p 是垂足, e 垂直戳进列空间.)
# =============================================================
fig, ax = plt.subplots(figsize=(8.0, 6.8))
# 列空间画成一条水平直线 (示意), 在 R^4 里它其实是个 2 维平面
col_y = 1.5
ax.plot([-0.5, 5.3], [col_y, col_y], color=BLUE, lw=3.4, alpha=0.30, zorder=1)
ax.text(5.25, col_y - 0.42, "Col(A)  (a 2-dim subspace of R^4)", color=BLUE,
        fontsize=11, ha='right', style='italic')

# 列空间里的两个"生成方向"示意 (列1 = 全1, 列2 = t); 用两根斜箭头指代
arrow(ax, (0.2, col_y), (1.8, col_y), BLUE, lw=2.0)
ax.text(1.0, col_y + 0.16, "col 1 = (1,1,1,1)", color=BLUE, fontsize=10)
arrow(ax, (2.0, col_y), (4.2, col_y + 0.55), BLUE, lw=2.0)
ax.text(2.9, col_y + 0.62, "col 2 = (0,1,2,3)", color=BLUE, fontsize=10)

# b (目标, 在列空间之外)
bpt = np.array([3.4, 3.6])
arrow(ax, (0, 0), bpt, GREEN, lw=2.6)
ax.scatter(*bpt, color=GREEN, s=70, zorder=6)
ax.text(bpt[0] + 0.10, bpt[1] + 0.10, "b = (1,1,3,3)\n(off the subspace)",
        color=GREEN, fontsize=11.5, fontweight='bold')

# 投影点 p = A xhat (垂足, 在列空间直线上)
ppt = np.array([3.4, col_y])
arrow(ax, (0, 0), ppt, PURPLE, lw=2.6)
ax.scatter(*ppt, color=PURPLE, s=70, zorder=6)
ax.text(ppt[0] + 0.10, ppt[1] - 0.55,
        "p = A x_hat\n(the closest point\nin Col(A) to b)",
        color=PURPLE, fontsize=11, fontweight='bold')

# 误差线 e = b - p (垂直地戳进列空间)
_vline(ax, ppt, bpt, RED, lw=2.4, ls='--')
# 直角标记 (corner = p, dir1 = 沿列空间水平, dir2 = 沿误差竖直)
sz = 0.28
ax.plot([ppt[0], ppt[0] + sz, ppt[0] + sz, ppt[0]],
        [ppt[1], ppt[1], ppt[1] + sz, ppt[1] + sz], color=GRAY, lw=1.4, zorder=4)
ax.text(ppt[0] + 0.34, ppt[1] + 0.10, "90 deg", color=GRAY, fontsize=9.5)
ax.text((ppt[0] + bpt[0]) / 2 + 0.12, (ppt[1] + bpt[1]) / 2,
        "e = b - A x_hat\nperpendicular to Col(A)\n=>  A^T e = 0",
        color=RED, fontsize=10.5)

# 旁注: 这就是正规方程
ax.text(0.02, 0.97,
        "A^T (b - A x_hat) = 0\n=>  A^T A x_hat = A^T b   (normal equation)",
        transform=ax.transAxes, va='top', ha='left', fontsize=11,
        bbox=dict(boxstyle='round,pad=0.35', fc='white', ec=GRAY, alpha=0.9))

ax.set_xlim(-0.6, 5.6); ax.set_ylim(-0.2, 4.4)
ax.axhline(0, color='gray', lw=0.6, alpha=0.3); ax.axvline(0, color='gray', lw=0.6, alpha=0.3)
ax.set_xticks([]); ax.set_yticks([])
ax.set_title("Column-space view:  least squares = project b onto Col(A);\n"
             "the projection p = A x_hat,  error e is perpendicular to the subspace",
             fontsize=11.5)
save(fig, "fig-17-2-column-space-view.png")


print("OK fig-17 all saved to", HERE)
print(f"  x_hat = ({c}, {d}),  p = {p},  e = {e}")
print(f"  A^T e = {A.T @ e}   (should be ~0)")
