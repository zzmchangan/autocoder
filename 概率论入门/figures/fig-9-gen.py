"""第 9 章配图脚本 · 连续主力:均匀、指数、正态。

生成两张图:
  fig-9-1-three-pdfs.png   —— 三种连续 PDF 对比(均匀 / 指数 / 正态), 各 fill 一段面积显示"概率"
  fig-9-2-memoryless.png   —— 指数分布的无记忆性(招牌): 已等 s 后剩余分布 == 原始分布

只 import _plot_utils, 绝不修改它。固定种子, 可复现。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         plot_pmf, plot_pdf, sim_hist, convergence, heatmap2d, vline, hline)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import uniform, expon, norm


# ============================================================
# 图 9.1 · 三种连续 PDF 对比: 均匀 U(0,1) / 指数 Exp(1) / 正态 N(0,1)
# 三个子图并排, 每个 PDF fill 一段区间, 标注"这段面积 = 概率"
# ============================================================
fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.2))

# (a) 均匀 U(0,1): fill [0.2, 0.6]
ax = axes[0]
xs = np.linspace(-0.3, 1.3, 600)
ys = uniform(loc=0, scale=1).pdf(xs)
plot_pdf(ax, xs, ys, color=BLUE, label="Uniform(0,1)")
# fill 一段区间 [0.2, 0.6]
xf = np.linspace(0.2, 0.6, 200)
yf = uniform(loc=0, scale=1).pdf(xf)
ax.fill_between(xf, yf, color=BLUE, alpha=0.28, zorder=2)
ax.text(0.4, 0.55, "area = 0.40\n= P(0.2<X<0.6)", ha="center", va="center",
        fontsize=9, color=BLUE)
ax.set_xlim(-0.3, 1.3); ax.set_ylim(0, 1.25)
ax.set_title("(a) Uniform: flat, no preference", fontsize=11)
ax.legend(loc="upper center", fontsize=9)

# (b) 指数 Exp(1): fill [0, 1]  (= 1 - e^-1 ≈ 0.632)
ax = axes[1]
xs = np.linspace(0, 6, 600)
ys = expon(scale=1).pdf(xs)
plot_pdf(ax, xs, ys, color=ORANGE, label="Exponential(1)")
xf = np.linspace(0, 1, 200)
yf = expon(scale=1).pdf(xf)
ax.fill_between(xf, yf, color=ORANGE, alpha=0.28, zorder=2)
ax.text(1.4, 0.30, "area = 0.632\n= P(X<1)", ha="left", va="center",
        fontsize=9, color=ORANGE)
vline(ax, 1, color=GRAY, ls=":", lw=1.2)
ax.set_xlim(0, 6); ax.set_ylim(0, 1.05)
ax.set_title("(b) Exponential: decay, no memory", fontsize=11)
ax.legend(loc="upper right", fontsize=9)

# (c) 正态 N(0,1): fill [-1, 1]  (= 0.6827)
ax = axes[2]
xs = np.linspace(-4, 4, 800)
ys = norm(0, 1).pdf(xs)
plot_pdf(ax, xs, ys, color=GREEN, label="Normal(0,1)")
xf = np.linspace(-1, 1, 300)
yf = norm(0, 1).pdf(xf)
ax.fill_between(xf, yf, color=GREEN, alpha=0.28, zorder=2)
ax.text(0, 0.13, "area = 0.683\n= P(-1<X<1)", ha="center", va="center",
        fontsize=9, color=GREEN)
vline(ax, 0, color=GRAY, ls=":", lw=1.2)
ax.set_xlim(-4, 4); ax.set_ylim(0, 0.45)
ax.set_title("(c) Normal: bell, symmetric", fontsize=11)
ax.legend(loc="upper right", fontsize=9)

fig.suptitle("Fig 9.1  Three faces of continuous randomness", fontsize=12, y=1.02)
save(fig, "fig-9-1-three-pdfs.png")
print("saved: fig-9-1-three-pdfs.png")


# ============================================================
# 图 9.2 · 指数分布的无记忆性(招牌图)
# 左: 原始 Exp(1) 的 PDF; 右: 已等 s=2 后, 剩余 (X-2 | X>2) 的 PDF —— 二者完全重合
# 并叠加十万次模拟的直方图, 验证"剩余等待分布 == 原始分布"
# ============================================================
rng = np.random.default_rng(42)
s_thr = 2.0  # 已等待的时间

# 模拟原始 Exp(1) 和 条件剩余
N = 200_000
raw = rng.exponential(scale=1.0, size=N)
cond = raw[raw > s_thr] - s_thr   # 已等 s_thr 后, 还要等多久

fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.4))

# 左: 原始分布
ax = axes[0]
xs = np.linspace(0, 6, 600)
ys = expon(scale=1).pdf(xs)
plot_pdf(ax, xs, ys, color=ORANGE, label="Exp(1) PDF")
sim_hist(ax, raw, bins=60, color=RED, label="100k draws of X")
vline(ax, s_thr, color=BLUE, ls="--", lw=1.6, label=f"already waited s={s_thr}")
ax.set_xlim(0, 6); ax.set_ylim(0, 1.05)
ax.set_title("(a) Original waiting time  X ~ Exp(1)", fontsize=11)
ax.legend(loc="upper right", fontsize=9)

# 右: 条件剩余 (X - s | X > s) —— 与原始分布完全重合
ax = axes[1]
xs = np.linspace(0, 6, 600)
ys = expon(scale=1).pdf(xs)
plot_pdf(ax, xs, ys, color=ORANGE, label="Exp(1) PDF (original)")
sim_hist(ax, cond, bins=60, color=BLUE, label=f"residual (X-{s_thr:.0f} | X>{s_thr:.0f})")
ax.set_xlim(0, 6); ax.set_ylim(0, 1.05)
ax.set_title("(b) After waiting s: residual has SAME distribution", fontsize=11)
ax.legend(loc="upper right", fontsize=9)

# 在右图角上加一行注解: 直觉翻译
ax.text(0.5, 4.8, "waited s already?  future looks identical",
        transform=ax.transData, fontsize=9, color=GRAY, style="italic")

fig.suptitle("Fig 9.2  Memorylessness of the exponential distribution", fontsize=12, y=1.02)
save(fig, "fig-9-2-memoryless.png")
print("saved: fig-9-2-memoryless.png")

# 顺便把验证数字打到 stdout, 供正文核对
print("--- memorylessness check ---")
print(f"E[X]           = {raw.mean():.4f}   (theory 1.0)")
print(f"E[X-s | X>s]   = {cond.mean():.4f}   (should also be ~1.0)")
print(f"P(X>3)         = {np.mean(raw>3):.5f}   (theory {np.exp(-3):.5f})")
print(f"P(X-s>3 | X>s) = {np.mean(cond>3):.5f}   (should match P(X>3))")
