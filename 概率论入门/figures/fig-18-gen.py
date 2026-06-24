"""第 18 章 · 信息熵与交叉熵 —— 配图生成脚本。

图清单:
  fig-18-1-bernoulli-entropy.png   伯努利熵 H(p) 随 p 变化曲线(招牌图:p=0.5 最大, p=0/1 为 0)
  fig-18-2-entropy-decomposition.png  交叉熵 = H(P) + KL(P||Q) 分解示意

严禁修改 _plot_utils.py;本脚本只 import 其工具。
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
from scipy.stats import entropy


# ============================================================
# 图 18.1 · 伯努利熵 H(p) 随 p 变化(招牌图)
#   H(p) = -p log2 p - (1-p) log2(1-p)
#   p=0 或 1 -> H=0 (完全确定)
#   p=0.5    -> H=1 bit (最不确定, 等概率最难猜)
# ============================================================
ps = np.linspace(1e-6, 1 - 1e-6, 1000)
H = -(ps * np.log2(ps) + (1 - ps) * np.log2(1 - ps))

fig, ax = plt.subplots(figsize=(7.4, 4.4))
ax.plot(ps, H, color=BLUE, lw=2.6, label=r"$H(p) = -p\log_2 p - (1-p)\log_2(1-p)$", zorder=4)
ax.fill_between(ps, H, color=BLUE, alpha=0.12, zorder=2)

# 标关键点
ax.scatter([0.5], [1.0], color=RED, s=70, zorder=6)
ax.annotate("p = 0.5  ->  H = 1 bit\n(most uncertain: hardest to guess)",
            xy=(0.5, 1.0), xytext=(0.53, 0.96), fontsize=9.5, color=RED,
            arrowprops=dict(arrowstyle="->", color=RED, lw=1.3))
ax.scatter([0.0, 1.0], [0.0, 0.0], color=ORANGE, s=70, zorder=6)
ax.annotate("p = 0  or  1  ->  H = 0\n(completely certain)",
            xy=(1.0, 0.0), xytext=(0.58, 0.18), fontsize=9.5, color=ORANGE,
            arrowprops=dict(arrowstyle="->", color=ORANGE, lw=1.3))

ax.set_xlabel("probability p  (Bernoulli: P(X=1) = p)", fontsize=11)
ax.set_ylabel("entropy  H(p)   [bits]", fontsize=11)
ax.set_title("Entropy of a Bernoulli distribution vs success probability",
             fontsize=12)
ax.set_xlim(0, 1)
ax.set_ylim(0, 1.12)
ax.legend(loc="upper center", fontsize=10)
ax.grid(alpha=0.25)
save(fig, "fig-18-1-bernoulli-entropy.png")


# ============================================================
# 图 18.2 · 交叉熵 = H(P) + KL(P||Q) 分解
#   二分类: 真实 p=0.3, 模型对正类预测 q
#   画 H(p||q) 随 q 变化, 在 q=p 处取最小=H(p), 用虚线标出
#   标注"超过 H(p) 的部分 = KL(P||Q)"
# ============================================================
p_true = 0.3
qs = np.linspace(1e-4, 1 - 1e-4, 1000)
H_P = entropy([p_true, 1 - p_true], base=2)   # 真实分布的熵, 常数
CE = -(p_true * np.log2(qs) + (1 - p_true) * np.log2(1 - qs))
KL = CE - H_P

fig, ax = plt.subplots(figsize=(7.6, 4.6))
# 总交叉熵
ax.plot(qs, CE, color=RED, lw=2.6, label=r"cross-entropy  $H(p,q) = -\sum p\log_2 q$", zorder=5)
# 底部真实熵(常数, 交叉熵能达到的下限)
ax.axhline(H_P, color=GREEN, ls="--", lw=2.0,
           label=f"$H(p)$ = {H_P:.3f} bits  (minimum, reached at q = p = {p_true})")
# KL 填充(交叉熵高出 H(p) 的部分)
ax.fill_between(qs, H_P, CE, where=(CE >= H_P), color=ORANGE, alpha=0.25,
                label=r"$D_{KL}(p\,\|\,q)$  =  extra cost of using $q$ instead of $p$")

# 标 q=p 处的峰谷
ax.scatter([p_true], [H_P], color=RED, s=70, zorder=7)
ax.annotate(f"q = p = {p_true}\nH(p,q) = H(p),  KL = 0",
            xy=(p_true, H_P), xytext=(0.36, 0.72), fontsize=9.5, color=RED,
            arrowprops=dict(arrowstyle="->", color=RED, lw=1.3))

ax.set_xlabel(r"model prediction  q = P_model(X=1)", fontsize=11)
ax.set_ylabel("bits", fontsize=11)
ax.set_title(r"Cross-entropy = entropy of truth  $H(p)$  +  KL divergence $D_{KL}(p\|q)$",
             fontsize=11.5)
ax.set_xlim(0, 1)
ax.set_ylim(0.5, 3.0)
ax.legend(loc="upper center", fontsize=9.5)
ax.grid(alpha=0.25)
save(fig, "fig-18-2-entropy-decomposition.png")


# ============================================================
# 图 18.3 · 蒙特卡洛: 用样本均值逼近熵(经验熵 -> 理论熵)
#   熵 = E_P[-log2 p(X)], 对 P 采样后求样本均值
# ============================================================
rng = np.random.default_rng(42)
P = np.array([0.5, 0.25, 0.125, 0.125])   # 偏置 4 面骰
theory_H = entropy(P, base=2)             # = 1.75 bits

ns = np.unique(np.round(np.logspace(1, 5, 60)).astype(int))
emp = []
for n in ns:
    s = rng.choice(4, size=n, p=P)
    emp.append((-np.log2(P[s])).mean())
emp = np.array(emp)

fig, ax = plt.subplots(figsize=(7.4, 4.3))
ax.plot(ns, emp, color=BLUE, lw=1.5, alpha=0.85, label="empirical entropy  $-\\log_2 p$ averaged over samples")
ax.axhline(theory_H, color=ORANGE, ls="--", lw=2.0, label=f"theory  H(P) = {theory_H:.3f} bits")
ax.set_xscale("log")
ax.set_xlabel("number of samples  n", fontsize=11)
ax.set_ylabel("entropy  [bits]", fontsize=11)
ax.set_title("Monte Carlo: sample mean of $-\\log_2 p$ converges to entropy H(P)",
             fontsize=11.5)
ax.legend(loc="lower right", fontsize=9.5)
ax.grid(alpha=0.25)
save(fig, "fig-18-3-mc-entropy.png")

print("done: 3 figures generated")
