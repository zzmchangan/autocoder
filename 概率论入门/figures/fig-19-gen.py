"""第 19 章 · 逻辑回归的概率视角 —— 配图脚本。

独立脚本, 只 import _plot_utils, 绝不修改它。运行:
    python figures/fig-19-gen.py
产出:
    figures/fig-19-1-sigmoid-decision-boundary.png  (招牌: sigmoid + 决策边界)
    figures/fig-19-2-softmax.png                    (可选: softmax 三类概率)
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         plot_pdf, vline, hline)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.special import expit, softmax

# ============================================================
# 图 19.1 · 招牌: sigmoid 曲线 + 决策边界
# 左子图: sigma(z) = 1/(1+e^-z), 把线性分数压到 (0,1)
# 右子图: 合成二分类数据 + 学到的线性决策边界
# ============================================================
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.4))

# ---------- 左:sigmoid ----------
z = np.linspace(-7, 7, 400)
p = expit(z)
axL.plot(z, p, color=BLUE, lw=2.6, label=r"$\sigma(z)=1/(1+e^{-z})$")
axL.axhline(0.5, color=ORANGE, ls='--', lw=1.3, label="threshold 0.5")
axL.axhline(0, color=GRAY, lw=0.6); axL.axhline(1, color=GRAY, lw=0.6)
# 标几个点
for zv in (-3, -2, -1, 0, 1, 2, 3):
    axL.scatter([zv], [expit(zv)], color=RED, s=22, zorder=5)
    axL.annotate(f"{expit(zv):.2f}", (zv, expit(zv)),
                 textcoords="offset points", xytext=(4, 7), fontsize=7.5, color=RED)
axL.scatter([0], [0.5], color='k', s=40, zorder=6)
axL.annotate("z=0 -> 0.5", (0, 0.5), xytext=(-50, -22),
             textcoords="offset points", fontsize=8.5,
             arrowprops=dict(arrowstyle="->", lw=0.8))
axL.set_xlabel("linear score  z = w . x + b")
axL.set_ylabel(r"probability  $\sigma(z)$")
axL.set_title("(a) Sigmoid: compress a score into (0,1)", fontsize=10.5)
axL.set_ylim(-0.05, 1.08); axL.legend(loc='lower right', fontsize=8.5)
axL.grid(alpha=0.25)

# ---------- 右:决策边界 ----------
rng = np.random.default_rng(42)
n = 200
X1 = rng.normal([2, 2], 0.8, (n // 2, 2))
X0 = rng.normal([-2, -2], 0.8, (n // 2, 2))
X = np.vstack([X1, X0])
y = np.array([1] * (n // 2) + [0] * (n // 2))

# 手写梯度下降(=伯努利 MLE, 与正文代码一致)
Xb = np.c_[np.ones(len(X)), X]
w = np.zeros(3); lr = 0.1
for _ in range(2000):
    q = expit(Xb @ w)
    w -= lr * Xb.T @ (q - y) / len(y)

axR.scatter(X0[:, 0], X0[:, 1], color=RED, s=18, alpha=0.65, edgecolor='white',
            linewidth=0.3, label="class 0")
axR.scatter(X1[:, 0], X1[:, 1], color=GREEN, s=18, alpha=0.65, edgecolor='white',
            linewidth=0.3, label="class 1")

# 决策边界 w0 + w1 x1 + w2 x2 = 0  ->  x2 = -(w0 + w1 x1)/w2
xs = np.linspace(-5, 5, 100)
ys = -(w[0] + w[1] * xs) / w[2]
axR.plot(xs, ys, color=BLUE, lw=2.4, label="decision boundary  w.x+b=0")
axR.set_xlim(-5, 5); axR.set_ylim(-5, 5)
axR.set_xlabel("feature  x1"); axR.set_ylabel("feature  x2")
axR.set_title(f"(b) Learned boundary  (acc=1.00, w={np.round(w,2)})", fontsize=10.5)
axR.legend(loc='upper left', fontsize=8.5)
axR.grid(alpha=0.25)

save(fig, "fig-19-1-sigmoid-decision-boundary.png")
print("saved fig-19-1")

# ============================================================
# 图 19.2 · softmax 把分数向量压成概率分布
# 固定另两类 z2=1.0, z3=0.0, 扫第一类分数 z1 从 -3 到 5
# 看三类概率如何此消彼长(总和恒为 1)
# ============================================================
fig2, ax = plt.subplots(figsize=(7.2, 4.4))
z1 = np.linspace(-3, 5, 300)
# 固定 z2=1.0, z3=0.0, 扫 z1
Z = np.stack([z1, np.full_like(z1, 1.0), np.full_like(z1, 0.0)], axis=1)
P = softmax(Z, axis=1)
ax.plot(z1, P[:, 0], color=BLUE, lw=2.4, label=r"$p_1$ (class 1, scanned $z_1$)")
ax.plot(z1, P[:, 1], color=GREEN, lw=2.0, label=r"$p_2$ (fixed $z_2=1.0$)")
ax.plot(z1, P[:, 2], color=RED, lw=2.0, label=r"$p_3$ (fixed $z_3=0.0$)")
ax.axhline(1.0, color=GRAY, lw=0.6)
ax.fill_between(z1, P.sum(axis=1), 1.0, color=GRAY, alpha=0.0)  # 占位
# 标一处 z1=2
i = np.argmin(np.abs(z1 - 2.0))
ax.scatter([z1[i]] * 3, [P[i, 0], P[i, 1], P[i, 2]],
           color='k', s=35, zorder=6)
ax.annotate(f"z=[2,1,0]\n-> {np.round(P[i],2)}", (z1[i], P[i, 0]),
            xytext=(2.6, 0.55), fontsize=8.5,
            arrowprops=dict(arrowstyle="->", lw=0.8))
ax.set_xlabel("score  z1  (z2=1.0, z3=0.0 fixed)")
ax.set_ylabel("softmax probability")
ax.set_title("Softmax: a score vector becomes a probability distribution (sum=1)", fontsize=10.5)
ax.set_ylim(-0.03, 1.05)
ax.legend(loc='center right', fontsize=8.5)
ax.grid(alpha=0.25)
save(fig2, "fig-19-2-softmax.png")
print("saved fig-19-2")

print("ALL DONE")
