"""第 4 篇第 12 章 · 特征值与特征向量 —— 配图生成脚本.

独立脚本, 只 import 自 _plot_utils, 不碰 make_figures.py / _plot_utils.py.
生成:
  fig-12-1-eigenvectors.png   主图: A=[[3,1],[1,3]] 揉捏前后网格 + 两根特征轴
  fig-12-2-rotation-no-eigen.png  反例: 旋转 90° 没有实特征向量
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ============================================================
# Fig 12.1  主图: A=[[3,1],[1,3]] 的特征向量方向不变
#   eigvals: 4 沿 (1,1)方向,  2 沿 (1,-1)方向  (numpy 已核对)
# ============================================================
A = np.array([[3., 1.],
              [1., 3.]])
# 特征向量(取未归一化的整数方向, 便于读者对照)
e1 = np.array([1., 1.])     # lambda = 4
e2 = np.array([1., -1.])    # lambda = 2

fig, ax = plt.subplots(figsize=(8, 8))

# 原始网格(灰虚) 与 揉捏后网格(蓝实)
grid(ax, np.eye(2), 'gray', 0.35, '--')
grid(ax, A, BLUE, 0.55, '-', 0.9, n=2)

# 特征方向: 原方向(虚) + 变换后(实, 沿同一根轴拉长)
# 变换前: e1, e2 (单位长度, 画成虚线影子)
len1 = 1.2; len2 = 1.2
v1 = e1 / np.linalg.norm(e1) * len1
v2 = e2 / np.linalg.norm(e2) * len2
ax.plot([0, v1[0]], [0, v1[1]], color=PURPLE, lw=1.3, ls='--', alpha=0.7)
ax.plot([0, v2[0]], [0, v2[1]], color=GREEN,  lw=1.3, ls='--', alpha=0.7)

# 变换后: A @ v1 = 4*v1 (沿同方向拉长4倍), A @ v2 = 2*v2
Av1 = A @ v1
Av2 = A @ v2
arrow(ax, (0, 0), Av1, PURPLE, lw=3.0)
arrow(ax, (0, 0), Av2, GREEN,  lw=3.0)

# 标注: 特征向量 + 特征值
ax.text(Av1[0]*1.02 + 0.15, Av1[1]*1.02 - 0.05,
        "eigenvector (1,1)\n  A v = 4 v", color=PURPLE,
        fontsize=12, fontweight='bold')
ax.text(Av2[0]*1.02 + 0.15, Av2[1]*1.02 - 0.45,
        "eigenvector (1,-1)\n   A v = 2 v", color=GREEN,
        fontsize=12, fontweight='bold')

# 一根"普通"向量(会被转方向)做对比: (1, 0.4) -> A@(1,0.4)
v = np.array([1., 0.4])
Av = A @ v
arrow(ax, (0, 0), v,  'gray', lw=1.5, ms=12)
arrow(ax, (0, 0), Av, BLUE, lw=2.2)
ax.text(v[0] + 0.05,  v[1] - 0.25, "ordinary v", color='gray', fontsize=11)
ax.text(Av[0] + 0.05, Av[1] + 0.10, "A v (turned!)", color=BLUE, fontsize=11)

frame(ax, 5.6)
ax.set_title("Eigenvectors: axes that don't turn, only stretch   "
             "(A=[[3,1],[1,3]])", fontsize=12)
save(fig, "fig-12-1-eigenvectors.png")
print("saved fig-12-1-eigenvectors.png")

# ============================================================
# Fig 12.2  反例: 旋转 90° R=[[0,-1],[1,0]] 没有实特征向量
#   每一根箭头都被转 90°, 没有谁"方向不变" -> 实数域无特征向量
# ============================================================
R = np.array([[0., -1.],
              [1.,  0.]])
fig, axes = plt.subplots(1, 2, figsize=(11, 5.5))

# 左: 旋转前后的网格(灰虚 vs 蓝实), 叠上几根普通向量看它们都被转了
ax = axes[0]
grid(ax, np.eye(2), 'gray', 0.35, '--')
grid(ax, R, BLUE, 0.55, '-', 0.9, n=2)
for v in [np.array([1.5, 0.3]), np.array([0.4, 1.3]), np.array([-1.2, 0.5])]:
    arrow(ax, (0, 0), v, 'gray', lw=1.4, ms=12)
    arrow(ax, (0, 0), R @ v, BLUE, lw=2.0)
frame(ax, 3.2)
ax.set_title("Rotation 90 deg: every axis turns\n"
             "no real eigenvector", fontsize=11)

# 右: 对比 A=[[3,1],[1,3]] 有两根不转的轴
ax = axes[1]
AA = np.array([[3., 1.], [1., 3.]])
grid(ax, np.eye(2), 'gray', 0.35, '--')
grid(ax, AA, BLUE, 0.55, '-', 0.9, n=2)
# 特征轴
ue1 = np.array([1., 1.]) / np.sqrt(2) * 1.6
ue2 = np.array([1., -1.]) / np.sqrt(2) * 1.6
arrow(ax, (0, 0), AA @ ue1, PURPLE, lw=2.8)
arrow(ax, (0, 0), AA @ ue2, GREEN,  lw=2.8)
ax.plot([0, ue1[0]], [0, ue1[1]], color=PURPLE, lw=1.2, ls='--', alpha=0.6)
ax.plot([0, ue2[0]], [0, ue2[1]], color=GREEN,  lw=1.2, ls='--', alpha=0.6)
frame(ax, 4.8)
ax.set_title("A=[[3,1],[1,3]]: two axes DON'T turn\n"
             "two real eigenvectors", fontsize=11)

fig.suptitle("Not every matrix has real eigenvectors   "
             "(rotation is the counterexample)", fontsize=13)
save(fig, "fig-12-2-rotation-no-eigen.png")
print("saved fig-12-2-rotation-no-eigen.png")
