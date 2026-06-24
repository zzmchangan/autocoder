"""第 2 篇 第 8 章 · 特殊矩阵速写 配图生成脚本 (全局章号 8).

主图 (fig-8-1): 四子图对照四类揉捏.
  (a) Diagonal  D = [[3,0],[0,2]]  -> 沿坐标轴纯拉伸, 网格仍正交 (矩形).
  (b) Orthogonal Q = rotation 40 deg -> 纯旋转, 网格只转不变形状 (仍是正方形).
  (c) Symmetric S = [[2,1],[1,2]]  -> 正交方向上的拉/压, 网格变菱形但无旋转-剪切混合.
  (d) General   G = [[1,2],[3,4]]  -> 对比: 又转又剪又拉, 网格歪七扭八.

每张子图都画: 灰虚线 = 原始方格, 彩实线 = 揉捏后网格, 绿 i' / 红 j' = 两根新基向量.
所有坐标 / 矩阵的几何性质已用 numpy 核对. 图内标注用英文.
绝不修改 _plot_utils.py / make_figures.py.
"""
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE, GRAY  # noqa: E402
import numpy as np  # noqa: E402
import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def _panel(ax, M, after_color, title, subtitle, lim):
    """画一张子图: 灰虚原始方格 + 揉捏后网格(彩色) + 两根新基向量."""
    grid(ax, np.eye(2), color=GRAY, alpha=0.35, ls='--', lw=0.8, n=2)
    grid(ax, M, color=after_color, alpha=0.85, ls='-', lw=1.1, n=2)
    M = np.asarray(M, float)
    arrow(ax, [0, 0], M[:, 0], GREEN, lw=2.6)   # new i'
    arrow(ax, [0, 0], M[:, 1], RED, lw=2.6)     # new j'
    # label the new basis vectors
    ax.text(M[0, 0] * 1.12, M[1, 0] * 1.12, "i'", color=GREEN,
            fontsize=12, fontweight='bold')
    ax.text(M[0, 1] * 1.12, M[1, 1] * 1.12, "j'", color=RED,
            fontsize=12, fontweight='bold')
    frame(ax, lim, title)
    ax.text(0.5, -0.16, subtitle, transform=ax.transAxes,
            fontsize=10, ha='center', color=after_color)


# ---------------- 四类揉捏的矩阵 (numpy 已核对) ----------------
# (a) Diagonal: pure stretch along axes, grid stays rectangular
D = np.array([[3.0, 0.0],
              [0.0, 2.0]])

# (b) Orthogonal: rotation 40 deg, pure spin, lengths preserved
th = np.deg2rad(40)
Q = np.array([[np.cos(th), -np.sin(th)],
              [np.sin(th),  np.cos(th)]])

# (c) Symmetric: A = A^T, pulls along some perpendicular axes, no spin-shear mix
S = np.array([[2.0, 1.0],
              [1.0, 2.0]])

# (d) General: arbitrary, mix of rotate + shear + stretch
G = np.array([[1.0, 2.0],
              [3.0, 4.0]])

# sanity checks (printed so you can eyeball them against the prose)
print("D det =", round(np.linalg.det(D), 3))
print("Q.T@Q ="); print(np.round(Q.T @ Q, 6))
print("Q det =", round(np.linalg.det(Q), 3), " (orthogonal, =1 -> rotation)")
print("S == S.T ?", np.allclose(S, S.T))
print("G == G.T ?", np.allclose(G, G.T))

fig, axes = plt.subplots(2, 2, figsize=(11, 11))

# (a) Diagonal
ax = axes[0, 0]
_panel(ax, D, BLUE,
       "(a) Diagonal   D = [[3,0],[0,2]]",
       "pure stretch along axes\ngrid still rectangular, no rotation",
       lim=3.6)

# (b) Orthogonal (rotation)
ax = axes[0, 1]
_panel(ax, Q, PURPLE,
       "(b) Orthogonal   Q = rotate 40 deg",
       "pure rotation, lengths & angles kept\ngrid is just spun, shape unchanged",
       lim=1.8)

# (c) Symmetric
ax = axes[1, 0]
_panel(ax, S, GREEN,
       "(c) Symmetric   S = [[2,1],[1,2]]",
       "stretch/compress along PERPENDICULAR axes\nno rotate-shear mix (A = A^T)",
       lim=4.0)

# (d) General (contrast)
ax = axes[1, 1]
_panel(ax, G, RED,
       "(d) General   G = [[1,2],[3,4]]",
       "a mess: rotate + shear + stretch\n(special matrices above are the clean cases)",
       lim=6.2)

fig.suptitle("Special matrices = clean squashes:  gray dashed = before,  colored = after",
             fontsize=14, y=0.995)
save(fig, "fig-8-1-four-special-matrices.png")

print("done: fig-8-1-four-special-matrices.png")
