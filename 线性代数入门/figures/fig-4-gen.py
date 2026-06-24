"""P1-04 基与维数 · 配图生成。

核心要表达的:同一根物理箭头,换一套基(换一把量地的尺子),
它的"坐标"(门牌号)就变了 —— 箭头是本质,数字是投影。

约定: 不碰 make_figures.py / _plot_utils.py, 只读 import。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---- 数据(先用 numpy 算对, 再画) ----
v = np.array([1., 3.])                      # 物理箭头(两种基下都不变的真身)
B = np.array([[1., -1.],                    # 列向量 = 非标准基的两根: e1=(1,1)
              [1.,  1.]])                   #                            e2=(-1,1)
coords_B = np.linalg.solve(B, v)            # B 基下的坐标, 应得 (2, 1)
assert np.allclose(coords_B, [2., 1.]), coords_B

e1, e2 = B[:, 0], B[:, 1]

fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.6))

# ---------- 左: 标准基 i, j ----------
ax = axes[0]
grid(ax, np.eye(2), 'gray', 0.28, '--')     # 标准方格
arrow(ax, (0, 0), (1, 0), GREEN, 1.6)       # i
arrow(ax, (0, 0), (0, 1), RED, 1.6)         # j
ax.text(1.02, -0.40, "i", color=GREEN, fontsize=12, fontweight='bold')
ax.text(-0.42, 1.02, "j", color=RED, fontsize=12, fontweight='bold')
arrow(ax, (0, 0), v, BLUE, 3.0)             # 物理箭头 v
ax.text(v[0] + 0.15, v[1] + 0.08, "(1, 3)", color=BLUE, fontsize=15, fontweight='bold')
frame(ax, 4.3)
ax.set_title("Standard basis  {i, j}   ->   coords (1, 3)", fontsize=12)

# ---------- 右: 非标准基 B = {e1, e2} ----------
ax = axes[1]
grid(ax, np.eye(2), 'gray', 0.16, '--')     # 背景标准方格(淡, 仅作参照)
grid(ax, B, BLUE, 0.40)                     # B 基的斜网格(关键: 换了尺子, 网格歪了)
arrow(ax, (0, 0), e1, GREEN, 1.9)           # e1
arrow(ax, (0, 0), e2, RED, 1.9)             # e2
ax.text(e1[0] + 0.12, e1[1] - 0.32, "e1", color=GREEN, fontsize=12, fontweight='bold')
ax.text(e2[0] - 0.62, e2[1] + 0.08, "e2", color=RED, fontsize=12, fontweight='bold')
arrow(ax, (0, 0), v, BLUE, 3.0)             # 同一根物理箭头 v(位置不变!)
ax.text(v[0] + 0.15, v[1] + 0.08, "(2, 1)", color=BLUE, fontsize=15, fontweight='bold')
ax.text(v[0] + 0.15, v[1] - 0.50, "in basis B", color=BLUE, fontsize=10)
frame(ax, 4.3)
ax.set_title("Basis  B = {e1=(1,1), e2=(-1,1)}   ->   coords (2, 1)", fontsize=11)

fig.suptitle("Same arrow, different basis:  the vector is unchanged, its numbers change",
             fontsize=13)
fig.tight_layout(rect=[0, 0, 1, 0.94])
save(fig, "fig-4-1-basis-coords.png")
print("coords in basis B:", coords_B)
print("OK saved fig-4-1-basis-coords.png")
