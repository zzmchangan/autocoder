"""第 2 篇 · 第 5 章 · 线性变换的全貌 · 配图脚本 (独立, 只读 import _plot_utils)。

产出两张图:
  fig-5-1-projection.png   —— 2x3 矩阵的 "投影式揉捏": 3D 空间被压扁到 2D。
  fig-5-2-3d-transform.png —— 三维空间里的变换 (绕 z 轴旋转 + 3D 剪切)。

注意: 绝不修改 _plot_utils.py 与 make_figures.py。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401  (注册 3D 投影)


# ============================================================
# Fig 5.1 —— 投影式揉捏: 2x3 矩阵 P = [[1,0,0],[0,1,0]] 把 3D 压扁成 2D
# 左: 3D 里几根向量 (i, j, k, v=(2,1,3), w=(0,0,5))
# 右: 投影到 2D 后的结果 (k 和 w 都被压到原点)
# ============================================================
P = np.array([[1., 0, 0],
              [0., 1, 0]])          # 2x3: 3D -> 2D, 丢弃 z 坐标

vec3 = {'i': (1., 0, 0), 'j': (0., 1, 0), 'k': (0., 0, 1),
        'v': (2., 1, 3), 'w': (0., 0, 5)}
color3 = {'i': GREEN, 'j': RED, 'k': PURPLE, 'v': BLUE, 'w': '#ff7f0e'}

fig = plt.figure(figsize=(13, 6))

# --- 左: 3D 原始 ---
ax3 = fig.add_subplot(1, 2, 1, projection='3d')
lim = 3.2
# 画 3D 方格的几条参考线 (i-j 底面网格)
for k in range(-1, 3):
    ax3.plot([k, k], [-1, 3], [0, 0], color='gray', alpha=0.3, lw=0.7)
    ax3.plot([-1, 3], [k, k], [0, 0], color='gray', alpha=0.3, lw=0.7)
# 三个轴方向的细参考线
ax3.plot([-1, 3.2], [0, 0], [0, 0], color='gray', lw=0.6, alpha=0.4)
ax3.plot([0, 0], [-1, 3.2], [0, 0], color='gray', lw=0.6, alpha=0.4)
ax3.plot([0, 0], [0, 0], [-0.5, 3.2], color='gray', lw=0.6, alpha=0.4)

for name, v in vec3.items():
    ax3.quiver(0, 0, 0, v[0], v[1], v[2], color=color3[name],
               arrow_length_ratio=0.12, linewidth=2.4)
    ax3.text(v[0]*1.12, v[1]*1.12, v[2]*1.12, name,
             color=color3[name], fontsize=13, fontweight='bold')
ax3.set_xlim(-1, lim); ax3.set_ylim(-1, lim); ax3.set_zlim(-0.5, lim)
ax3.set_xlabel('x'); ax3.set_ylabel('y'); ax3.set_zlabel('z')
ax3.set_title('3D space (before projection)', fontsize=12)
ax3.view_init(elev=20, azim=35)

# --- 右: 2D 投影后 ---
ax2 = fig.add_subplot(1, 2, 2)
grid(ax2, np.eye(2), 'gray', 0.3, '--')
# 三个基向量被映成的 2D 位置 (就是 P 的三列)
for name, v in vec3.items():
    pv = P @ np.array(v)
    if np.linalg.norm(pv) > 1e-9:
        arrow(ax2, (0, 0), pv, color3[name], lw=2.6)
        ax2.text(pv[0]+0.12, pv[1]+0.12, name, color=color3[name],
                 fontsize=13, fontweight='bold')
    else:
        # 被压到原点的: 画个空心圈标注, 文字放在右下, 两根错开
        ax2.plot(0, 0, 'o', mfc='none', mec=color3[name], ms=14, mew=2)
        offset = {'k': (0.22, -0.55), 'w': (0.22, -1.05)}[name]
        ax2.text(offset[0], offset[1],
                 name + " -> 0  (crushed)", color=color3[name],
                 fontsize=11, fontweight='bold')
frame(ax2, 3.2)
ax2.set_title('After P (2x3):  k and w crushed to origin', fontsize=12)
# 在右图加一个文字说明 z 被丢
ax2.text(-2.9, 2.7,
         "z-coordinate thrown away\nwhole 3D space squashed to xy-plane",
         fontsize=10, color='#555',
         bbox=dict(boxstyle='round', fc='#f0f0f0', ec='none'))

fig.suptitle("Non-square matrix (2x3) = projection:  3D crushed into 2D",
             fontsize=14)
fig.tight_layout(rect=[0, 0, 1, 0.95])
fig.savefig(os.path.join(HERE, "fig-5-1-projection.png"), dpi=150)
plt.close(fig)


# ============================================================
# Fig 5.2 —— 三维空间里的变换: 绕 z 轴旋转 45° (左) + 3D 剪切 (右)
# 每张子图都是 3D, 画 i, j, k 三根基向量被揉去的新位置 i', j', k'
# ============================================================
th = np.pi / 4
Rz = np.array([[np.cos(th), -np.sin(th), 0],
               [np.sin(th),  np.cos(th), 0],
               [0,           0,          1]])
Shear = np.array([[1., 0, 1],   # x <- x + z  (k 倾斜)
                  [0., 1, 0],
                  [0., 0, 1]])

def draw_basis(ax, M, title):
    lim = 2.2
    # 原始 i,j,k 用细虚线
    for c, col in zip([GREEN, RED, PURPLE], np.eye(3)):
        ax.plot([0, col[0]], [0, col[1]], [0, col[2]],
                color=c, lw=1.0, ls='--', alpha=0.45)
    # 新基 i',j',k' 实线粗箭头
    labels = ["i'", "j'", "k'"]
    for c, lab, col in zip([GREEN, RED, PURPLE], labels, M.T):
        ax.quiver(0, 0, 0, col[0], col[1], col[2], color=c,
                  arrow_length_ratio=0.14, linewidth=2.6)
        ax.text(col[0]*1.18, col[1]*1.18, col[2]*1.18, lab,
                color=c, fontsize=13, fontweight='bold')
    # 底面参考网格 (变换后的 i-j 平面)
    for s in np.linspace(-1.5, 1.5, 4):
        a = s * M[:, 0]; b = 1.5 * M[:, 1]
        ax.plot([a[0]-b[0], a[0]+b[0]],
                [a[1]-b[1], a[1]+b[1]],
                [a[2]-b[2], a[2]+b[2]], color=BLUE, alpha=0.15, lw=0.7)
    ax.set_xlim(-lim, lim); ax.set_ylim(-lim, lim); ax.set_zlim(-lim, lim)
    ax.set_xlabel('x'); ax.set_ylabel('y'); ax.set_zlabel('z')
    ax.set_title(title, fontsize=12)
    ax.view_init(elev=18, azim=32)

fig = plt.figure(figsize=(13, 6))
axL = fig.add_subplot(1, 2, 1, projection='3d')
draw_basis(axL, Rz, "Rotation about z-axis (45 deg):  k unchanged")
axR = fig.add_subplot(1, 2, 2, projection='3d')
draw_basis(axR, Shear, "3D shear (x <- x + z):  k tilts, i,j stay")

fig.suptitle("Linear transforms in 3D:  3 columns = where i, j, k are sent",
             fontsize=14)
fig.tight_layout(rect=[0, 0, 1, 0.95])
fig.savefig(os.path.join(HERE, "fig-5-2-3d-transform.png"), dpi=150)
plt.close(fig)

print("OK figures saved to", HERE)
print("  fig-5-1-projection.png")
print("  fig-5-2-3d-transform.png")
