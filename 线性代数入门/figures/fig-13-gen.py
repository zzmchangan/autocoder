"""第 13 章 · 对角化 配图生成脚本(独立, 不碰 _plot_utils.py / make_figures.py)。

生成 fig-13-1-diagonalization.png:
  左:标准基 {i, j} 下, A=[[3,1],[1,3]] 把方格揉歪(又转又拉).
  右:特征基 {(1,1),(1,-1)} 下, 同一个变换 = 沿两根特征轴的纯拉伸(对角阵 Lambda).
强调"换个眼镜, 歪的变直的".
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE, GRAY
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

A = np.array([[3., 1.],
              [1., 3.]])
# 特征基: e1=(1,1) -> lambda=4, e2=(1,-1) -> lambda=2
P = np.array([[1., 1.],
              [1., -1.]])           # 列 = 特征向量(未归一化)
Lambda = np.diag([4., 2.])          # 列对齐: 列1 对应 lam=4, 列2 对应 lam=2

fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.8))

# ============ 左:标准基下的 A ============
ax = axes[0]
grid(ax, np.eye(2), GRAY, 0.30, '--', n=2)   # 揉之前的标准方格
grid(ax, A, BLUE, 0.55, '-', n=2)            # 揉之后, 网格歪了
arrow(ax, (0, 0), A[:, 0], GREEN, lw=2.6)
arrow(ax, (0, 0), A[:, 1], RED, lw=2.6)
# 标注 i', j'
ax.text(A[0, 0] * 1.12, A[1, 0] * 1.12 + 0.12, "i'", color=GREEN, fontsize=12, fontweight='bold')
ax.text(A[0, 1] + 0.12, A[1, 1] * 1.12, "j'", color=RED, fontsize=12, fontweight='bold')
frame(ax, 5.6)
ax.set_title("Standard basis:  A twists the grid\n"
             "(grid skews,  i and j get rotated)", fontsize=11.5)

# ============ 右:特征基下的同一个变换 = 对角阵 Lambda = 纯拉伸 ============
ax = axes[1]
# 这里画的是"用特征基这副眼镜看同一个变换":
#   画特征基本身的网格作底(灰虚), 再画它被 Lambda 作用后的网格(蓝实).
#   因为 Lambda 是对角阵, 蓝实网格与灰虚网格方向完全一致(仍平行于特征轴),
#   只是 e1 方向被拉长 4 倍, e2 方向被拉长 2 倍.
grid(ax, P, GRAY, 0.30, '--', n=2)                 # 特征基方格(灰虚): 沿 (1,1),(1,-1)
grid(ax, P @ Lambda, BLUE, 0.55, '-', n=2)         # 被 Lambda 作用后(蓝实): 方向不变, 只拉长
# 标两根特征轴方向(画长一点, 让"轴"概念清晰)
arrow(ax, (0, 0), P[:, 0] * 1.0, PURPLE, lw=2.0)
arrow(ax, (0, 0), P[:, 1] * 1.0, GREEN, lw=2.0)
ax.text(P[0, 0] * 1.05 + 0.15, P[1, 0] * 1.05, "e1 (1,1)", color=PURPLE, fontsize=11)
ax.text(P[0, 1] * 1.05 + 0.15, P[1, 1] * 1.05 - 0.05, "e2 (1,-1)", color=GREEN, fontsize=11)
# 标 lambda 倍数
arrow(ax, (0, 0), P[:, 0] * 4.0, PURPLE, lw=2.6)
arrow(ax, (0, 0), P[:, 1] * 2.0, GREEN, lw=2.6)
ax.text(P[0, 0] * 4.0 + 0.15, P[1, 0] * 4.0 - 0.05, "x4", color=PURPLE, fontsize=13, fontweight='bold')
ax.text(P[0, 1] * 2.0 + 0.2, P[1, 1] * 2.0, "x2", color=GREEN, fontsize=13, fontweight='bold')
frame(ax, 5.6)
ax.set_title("Eigenbasis:  same transform = Lambda\n"
             "grid keeps direction,  just stretches x4 and x2", fontsize=11.5)

fig.suptitle("Diagonalization = put on the eigenbasis glasses:  "
             "twisted becomes pure stretch", fontsize=13)
fig.tight_layout(rect=[0, 0, 1, 0.95])
out = os.path.join(HERE, "fig-13-1-diagonalization.png")
fig.savefig(out, dpi=150)
plt.close(fig)
print("saved:", out)

# ============ 健全性核对(打印, 供作者核对数字与图一致) ============
Pinv = np.linalg.inv(P)
print("A =\n", A)
print("P =\n", P)
print("Lambda =\n", Lambda)
print("P @ Lambda @ inv(P) =\n", P @ Lambda @ Pinv, " (应等于 A)")
print("A^3 direct      =\n", A @ A @ A)
print("P @ Lambda^3 @ inv(P) =\n", P @ np.linalg.matrix_power(Lambda, 3) @ Pinv)
