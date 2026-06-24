"""第 2 篇第 6 章 · 矩阵乘法与复合 · 配图生成脚本 (独立, 只读共享工具).

主图: AB vs BA 作用于同一根向量 v.
  - A = 水平剪切 [[1,1],[0,1]]  (j 被推向右)
  - B = 非均匀缩放 [[2,0],[0,0.5]] (x 拉伸 2 倍, y 压缩到 0.5)
  - v = (1.5, 1)
  路线一 AB: 先 B 后 A  ->  v -> Bv -> ABv = (3.5, 0.5)
  路线二 BA: 先 A 后 B  ->  v -> Av -> BAv = (5.0, 0.5)
  两个终点不同 -> 直观证明 AB != BA.

严禁修改 _plot_utils.py 与 make_figures.py.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

# ---------- the two transforms and the vector ----------
A = np.array([[1., 1.],
              [0., 1.]])      # shear: j tips right
B = np.array([[2., 0.],
              [0., 0.5]])      # scale: x*2, y*0.5
v = np.array([1.5, 1.0])

# intermediate + final points (verified by numpy in the chapter)
Bv = B @ v          # after B first
ABv = A @ Bv        # = (AB) v
Av = A @ v          # after A first
BAv = B @ Av        # = (BA) v

fig, axes = plt.subplots(1, 2, figsize=(12, 6))

# ============ left panel: route AB = "first B, then A" ============
ax = axes[0]
# light original grid for reference
grid(ax, np.eye(2), 'gray', 0.20, '--', lw=0.7)
# step 1: v -> Bv  (B does its squeeze)
arrow(ax, (0, 0), v,   GREEN, lw=2.0, ms=14)
arrow(ax, (0, 0), Bv,  BLUE,  lw=2.2)
# step 2: Bv -> ABv  (A shears)
arrow(ax, Bv, ABv, PURPLE, lw=2.2)
# final result arrow
arrow(ax, (0, 0), ABv, RED, lw=3.0, ms=20)

ax.text(v[0]+0.10, v[1]+0.15, 'v', color=GREEN, fontsize=13, fontweight='bold')
ax.text(Bv[0]-0.55, Bv[1]+0.15, 'Bv', color=BLUE, fontsize=12, fontweight='bold')
ax.text(ABv[0]+0.12, ABv[1]-0.30, 'AB v', color=RED, fontsize=14, fontweight='bold')
ax.text((Bv[0]+ABv[0])/2, (Bv[1]+ABv[1])/2+0.20, 'shear A', color=PURPLE, fontsize=10)
ax.text(Bv[0]/2, Bv[1]/2-0.30, 'scale B', color=BLUE, fontsize=10)
frame(ax, 4.0)
ax.set_title('Route  AB :  first  B  (scale),  then  A  (shear)   ->  AB v', fontsize=11.5)

# ============ right panel: route BA = "first A, then B" ============
ax = axes[1]
grid(ax, np.eye(2), 'gray', 0.20, '--', lw=0.7)
# step 1: v -> Av  (A shears first)
arrow(ax, (0, 0), v,   GREEN, lw=2.0, ms=14)
arrow(ax, (0, 0), Av,  BLUE,  lw=2.2)
# step 2: Av -> BAv  (B does its squeeze)
arrow(ax, Av, BAv, PURPLE, lw=2.2)
# final result arrow
arrow(ax, (0, 0), BAv, RED, lw=3.0, ms=20)

ax.text(v[0]+0.10, v[1]+0.15, 'v', color=GREEN, fontsize=13, fontweight='bold')
ax.text(Av[0]+0.12, Av[1]-0.32, 'Av', color=BLUE, fontsize=12, fontweight='bold')
ax.text(BAv[0]-0.75, BAv[1]+0.15, 'BA v', color=RED, fontsize=14, fontweight='bold')
ax.text((Av[0]+BAv[0])/2, (Av[1]+BAv[1])/2+0.20, 'scale B', color=PURPLE, fontsize=10)
ax.text(Av[0]/2, Av[1]/2-0.30, 'shear A', color=BLUE, fontsize=10)
frame(ax, 4.0)
ax.set_title('Route  BA :  first  A  (shear),  then  B  (scale)   ->  BA v', fontsize=11.5)

fig.suptitle('Non-commutativity:  same two transforms,  reversed order  ->  different destination',
             fontsize=13)
fig.tight_layout(rect=[0, 0, 1, 0.95])
fig.savefig(os.path.join(HERE, "fig-6-1-ab-vs-ba.png"), dpi=150)
plt.close(fig)

print("OK saved fig-6-1-ab-vs-ba.png")
print("AB v =", ABv, "  BA v =", BAv, "  (different endpoints)")
