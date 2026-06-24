"""第 4 篇第 14 章 · 对称矩阵的优美 —— 配图生成脚本.

独立脚本, 只 import 自 _plot_utils, 不碰 make_figures.py / _plot_utils.py.
生成:
  fig-14-1-orthogonal-eigenbasis.png  主图: 对称矩阵 A=[[2,1],[1,2]] 的正交特征基
                                       (lambda=3 沿 (1,1), lambda=1 沿 (1,-1), 两轴垂直)
  fig-14-2-symmetric-vs-not.png       对比: 左 对称(正交特征轴纯拉伸),
                                       右 非对称 [[2,1],[0,3]] (特征轴不正交, 有剪切)

所有特征值/向量/正交性均已用 numpy 核对 (见章节正文计算佐证节).
图内标注一律用英文.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ============================================================
# Fig 14.1  主图: 对称矩阵 A=[[2,1],[1,2]] 的正交特征基
#   eigvals: 3 沿 (1,1)方向,  1 沿 (1,-1)方向  (numpy 已核对)
#   两根特征向量互相垂直 (dot=0), 这是对称矩阵的特权
# ============================================================
A = np.array([[2., 1.],
              [1., 2.]])
# 未归一化的整数方向, 便于读者对照
e1 = np.array([1., 1.])     # lambda = 3
e2 = np.array([1., -1.])    # lambda = 1

fig, ax = plt.subplots(figsize=(8, 8))

# 原始网格(灰虚) 与 揉捏后网格(蓝实)
grid(ax, np.eye(2), 'gray', 0.35, '--')
grid(ax, A, BLUE, 0.55, '-', 0.9, n=2)

# 特征方向 (单位长度影子, 虚线)
L = 1.2
u1 = e1 / np.linalg.norm(e1) * L
u2 = e2 / np.linalg.norm(e2) * L
ax.plot([0, u1[0]], [0, u1[1]], color=PURPLE, lw=1.3, ls='--', alpha=0.7)
ax.plot([0, u2[0]], [0, u2[1]], color=GREEN,  lw=1.3, ls='--', alpha=0.7)

# 变换后: A@v1 = 3*v1, A@v2 = 1*v2  (沿同一根轴拉长/不动)
Av1 = A @ u1
Av2 = A @ u2
arrow(ax, (0, 0), Av1, PURPLE, lw=3.0)
arrow(ax, (0, 0), Av2, GREEN,  lw=3.0)

# 标注: 特征向量 + 特征值 + "perpendicular"
ax.text(Av1[0] + 0.15, Av1[1] - 0.05,
        "eigenvector (1,1)\n   A v = 3 v", color=PURPLE,
        fontsize=12, fontweight='bold')
ax.text(Av2[0] + 0.15, Av2[1] - 0.55,
        "eigenvector (1,-1)\n   A v = 1 v", color=GREEN,
        fontsize=12, fontweight='bold')

# 在原点附近画一个直角符号, 强调两根特征向量互相垂直
corner = 0.18
ax.plot([corner*e1[0]/np.linalg.norm(e1), corner*e1[0]/np.linalg.norm(e1) + corner*e2[0]/np.linalg.norm(e2),
         corner*e2[0]/np.linalg.norm(e2)],
        [corner*e1[1]/np.linalg.norm(e1), corner*e1[1]/np.linalg.norm(e1) + corner*e2[1]/np.linalg.norm(e2),
         corner*e2[1]/np.linalg.norm(e2)],
        color='black', lw=1.6)
ax.text(0.30, 0.22, "perpendicular\n(orthogonal)", color='black',
        fontsize=10, fontstyle='italic')

frame(ax, 5.0)
ax.set_title("Symmetric matrix A=[[2,1],[1,2]]: orthogonal eigenbasis\n"
             "stretch along two perpendicular axes, no twist", fontsize=12)
save(fig, "fig-14-1-orthogonal-eigenbasis.png")
print("saved fig-14-1-orthogonal-eigenbasis.png")

# ============================================================
# Fig 14.2  对比: 对称 (正交特征轴纯拉伸) vs 非对称 (特征轴斜交, 有剪切)
# ============================================================
fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.8))

# ---- 左: 对称矩阵 A=[[2,1],[1,2]] ----
ax = axes[0]
Asym = np.array([[2., 1.], [1., 2.]])
grid(ax, np.eye(2), 'gray', 0.35, '--')
grid(ax, Asym, BLUE, 0.55, '-', 0.9, n=2)
ue1 = np.array([1., 1.]) / np.sqrt(2) * 1.5
ue2 = np.array([1., -1.]) / np.sqrt(2) * 1.5
arrow(ax, (0, 0), Asym @ ue1, PURPLE, lw=2.8)
arrow(ax, (0, 0), Asym @ ue2, GREEN,  lw=2.8)
ax.plot([0, ue1[0]], [0, ue1[1]], color=PURPLE, lw=1.2, ls='--', alpha=0.6)
ax.plot([0, ue2[0]], [0, ue2[1]], color=GREEN,  lw=1.2, ls='--', alpha=0.6)
ax.text((Asym @ ue1)[0] + 0.10, (Asym @ ue1)[1] + 0.05, "v1, 3x", color=PURPLE, fontsize=11)
ax.text((Asym @ ue2)[0] + 0.10, (Asym @ ue2)[1] - 0.35, "v2, 1x", color=GREEN,  fontsize=11)
frame(ax, 4.6)
ax.set_title("Symmetric A=[[2,1],[1,2]]\n"
             "real eigenvalues, ORTHOGONAL eigenvectors\n"
             "(pure stretch, no shear)", fontsize=10.5)

# ---- 右: 非对称矩阵 [[2,1],[0,3]] (上三角, 有剪切成分) ----
ax = axes[1]
Anon = np.array([[2., 1.], [0., 3.]])
grid(ax, np.eye(2), 'gray', 0.35, '--')
grid(ax, Anon, BLUE, 0.55, '-', 0.9, n=2)
# eigvals: 2 沿 (1,0); 3 沿 (1,1) -> 两根斜交, 不正交
we1 = np.array([1., 0.]) * 1.5
we2 = np.array([1., 1.]) / np.sqrt(2) * 1.5
arrow(ax, (0, 0), Anon @ we1, PURPLE, lw=2.8)
arrow(ax, (0, 0), Anon @ we2, GREEN,  lw=2.8)
ax.plot([0, we1[0]], [0, we1[1]], color=PURPLE, lw=1.2, ls='--', alpha=0.6)
ax.plot([0, we2[0]], [0, we2[1]], color=GREEN,  lw=1.2, ls='--', alpha=0.6)
ax.text((Anon @ we1)[0] + 0.10, (Anon @ we1)[1] - 0.30, "v1, 2x", color=PURPLE, fontsize=11)
ax.text((Anon @ we2)[0] + 0.10, (Anon @ we2)[1] + 0.10, "v2, 3x", color=GREEN, fontsize=11)
frame(ax, 4.6)
ax.set_title("Non-symmetric A=[[2,1],[0,3]]\n"
             "real eigenvalues, NON-orthogonal eigenvectors\n"
             "(stretch + shear, the grid is skewed)", fontsize=10.5)

fig.suptitle("Symmetric vs non-symmetric: only symmetric matrices\n"
             "have an ORTHOGONAL eigenbasis (spectral theorem)", fontsize=12.5)
save(fig, "fig-14-2-symmetric-vs-not.png")
print("saved fig-14-2-symmetric-vs-not.png")
