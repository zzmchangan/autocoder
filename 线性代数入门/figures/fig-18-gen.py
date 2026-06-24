"""P6-18 基变换·换个视角 · 配图生成。

核心要表达的: 同一个线性变换(空间揉捏动作), 在标准基下是矩阵 A,
换一组基 B 后, 它的数字长相变成了 A' = P^{-1} A P.
动作没变, 变的是"用什么坐标去记录它" —— 相似矩阵 = 同一个变换换了一副眼镜.

约定: 不碰 make_figures.py / _plot_utils.py, 只读 import.
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
A = np.array([[1., 1.],            # 标准基下的剪切: i 不动, j 歪到右上
              [0., 1.]])
P = np.array([[1., -1.],           # 列向量 = 新基 B: e1=(1,1), e2=(-1,1)
              [1.,  1.]])
Pinv = np.linalg.inv(P)
Ap = Pinv @ A @ P                  # 相似矩阵 A' = P^{-1} A P

# 验证(脚本自检, 算对才画)
assert np.allclose(Ap, [[1.5, 0.5], [-0.5, 0.5]]), Ap
wA, _  = np.linalg.eig(A)
wAp, _ = np.linalg.eig(Ap)
assert np.allclose(np.sort(wA), np.sort(wAp)), (wA, wAp)   # 特征值应一致

e1, e2 = P[:, 0], P[:, 1]
Ae1, Ae2 = A @ e1, A @ e2          # 新基的两根, 被 A 揉去了哪(标准坐标下)

# ============ 图 18.1: 同一变换, 两组基, 两套数字 ============
fig, axes = plt.subplots(2, 2, figsize=(11.6, 11.2))

def panel_before(ax, basis_mat, basis_name, basis_cols, title):
    """变换前: 画原始(标准)方格 + 某组基的两根箭头."""
    grid(ax, np.eye(2), 'gray', 0.26, '--')
    grid(ax, basis_mat, BLUE, 0.42, '-', 0.9)
    arrow(ax, (0, 0), basis_cols[0], GREEN, 2.0)
    arrow(ax, (0, 0), basis_cols[1], RED, 2.0)
    ax.text(basis_cols[0][0] + 0.12, basis_cols[0][1] - 0.32,
            basis_name[0], color=GREEN, fontsize=13, fontweight='bold')
    ax.text(basis_cols[1][0] - 0.62, basis_cols[1][1] + 0.08,
            basis_name[1], color=RED, fontsize=13, fontweight='bold')
    frame(ax, 3.4)
    ax.set_title(title, fontsize=11.5)

def panel_after(ax, M, basis_mat, basis_name, basis_cols_after, title):
    """变换后: 画 A 作用后的方格 + 某组基被揉去的新位置."""
    grid(ax, np.eye(2), 'gray', 0.18, '--')
    grid(ax, M, BLUE, 0.42)                      # 揉捏后的歪方格
    arrow(ax, (0, 0), basis_cols_after[0], GREEN, 2.0)
    arrow(ax, (0, 0), basis_cols_after[1], RED, 2.0)
    ax.text(basis_cols_after[0][0] + 0.12, basis_cols_after[0][1] - 0.34,
            basis_name[0] + "'", color=GREEN, fontsize=13, fontweight='bold')
    ax.text(basis_cols_after[1][0] - 0.95, basis_cols_after[1][1] + 0.10,
            basis_name[1] + "'", color=RED, fontsize=13, fontweight='bold')
    frame(ax, 3.4)
    ax.set_title(title, fontsize=11.5)

# 顶行: 标准基 {i, j}
std = np.eye(2)
i_, j_ = std[:, 0], std[:, 1]
Ai, Aj = A @ i_, A @ j_           # 标准基被 A 揉去: (1,0) 和 (1,1)
panel_before(axes[0, 0], std, ("i", "j"), (i_, j_),
             "Before:  standard basis {i, j}    (identity grid)")
panel_after (axes[0, 1], A, std, ("i", "j"), (Ai, Aj),
             "After shear A:  i->(1,0)  j->(1,1)    [matrix A]")

# 底行: 新基 B = {e1, e2}
panel_before(axes[1, 0], P, ("e1", "e2"), (e1, e2),
             "Before:  basis B = {e1=(1,1), e2=(-1,1)}    (tilted grid)")
panel_after (axes[1, 1], A, P, ("e1", "e2"), (Ae1, Ae2),
             "After the SAME shear:  e1->(2,1)  e2->(0,1)    "
             "[but recorded in B as A']")

fig.suptitle("Same shear, two pairs of glasses:  "
             "A and A' = P^{-1} A P describe the identical transformation",
             fontsize=13)
fig.tight_layout(rect=[0, 0, 1, 0.955])
save(fig, "fig-18-1-same-transform-two-bases.png")
print("A  =\n", A)
print("A' = P^{-1} A P =\n", Ap)
print("eig(A)  =", np.sort(wA))
print("eig(A') =", np.sort(wAp))
print("OK saved fig-18-1-same-transform-two-bases.png")
