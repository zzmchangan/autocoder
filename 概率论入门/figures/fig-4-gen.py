"""第 4 章 · 贝叶斯思想 —— 配图生成脚本。

产出两张图:
  fig-4-1-prior-posterior-medical.png  —— 招牌图:体检假阳性 先验 -> 后验 更新柱状
  fig-4-2-convergence-of-priors.png    —— 不同先验的人, 拿到同样证据后信念逐步趋近

图内标注一律英文; 正文用中文。绝不修改 _plot_utils.py。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         plot_pmf, plot_pdf, sim_hist, convergence, heatmap2d,
                         vline, hline)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats

# ============================================================
# 图 4.1 · 体检假阳性: 先验(患病 1%) -> 拿到阳性证据 -> 后验(~15%)
# ============================================================
fig, ax = plt.subplots(figsize=(8.2, 4.8))

prev, sens, fp = 0.01, 0.90, 0.05
post = sens * prev / (sens * prev + fp * (1 - prev))

# 两根柱: 患病概率的"先验"与"后验"
labels = ["prior\nP(D)", "posterior\nP(D | +)"]
values = [prev, post]
colors = [BLUE, PURPLE]
bars = ax.bar(labels, values, width=0.55, color=colors, alpha=0.85,
              edgecolor='k', linewidth=0.6)

# 标注数值
for b, v in zip(bars, values):
    ax.text(b.get_x() + b.get_width()/2, v + 0.012, f"{v:.3f}",
            ha='center', va='bottom', fontsize=12, fontweight='bold')

# 标注似然 + 全概率分母的拆解
ax.annotate("", xy=(1, post), xytext=(1, prev),
            arrowprops=dict(arrowstyle="<->", color=RED, lw=1.8))
ax.text(1.18, (prev + post)/2,
        f"evidence (+)\nlifts belief\n{prev:.2f} -> {post:.3f}",
        color=RED, fontsize=10, va='center')

ax.set_ylabel("probability of disease", fontsize=11)
ax.set_ylim(0, max(values) * 1.35)
ax.set_title("Bayes update: rare disease, positive test", fontsize=12)
ax.grid(axis='y', alpha=0.3)

# 图例信息框
info = (f"prior P(D) = {prev}\n"
        f"likelihood P(+|D) = {sens}\n"
        f"false positive P(+|notD) = {fp}\n"
        f"posterior P(D|+) = {post:.3f}")
ax.text(0.98, 0.97, info, transform=ax.transAxes, ha='right', va='top',
        fontsize=9, family='monospace',
        bbox=dict(boxstyle='round,pad=0.4', fc='white', ec=GRAY, alpha=0.9))

plt.tight_layout()
save(fig, "fig-4-1-prior-posterior-medical.png")
print("saved fig-4-1-prior-posterior-medical.png")


# ============================================================
# 图 4.2 · 不同先验的人, 拿到同样证据后信念逐步趋近(学习曲线)
#         场景: 扔一枚未知硬币, 真实 p=0.6; 观测 150 次;
#         三个人先验不同, 每来一个数据点用贝叶斯更新一次后验均值。
# ============================================================
rng = np.random.default_rng(42)
true_p = 0.6
n_obs = 150
flips = rng.random(n_obs) < true_p   # 150 次观测, 1=正面

# 三个先验(Beta 分布):
priors = {
    "uniform Beta(1,1)":        (1, 1, BLUE),
    "skeptical Beta(2,8)":      (2, 8, ORANGE),
    "strong prior Beta(50,50)": (50, 50, PURPLE),
}

fig, ax = plt.subplots(figsize=(8.6, 4.8))

for name, (a, b, col) in priors.items():
    means = []
    aa, bb = a, b
    for obs in flips:
        if obs:        # 正面 -> a += 1
            aa += 1
        else:          # 反面 -> b += 1
            bb += 1
        means.append(aa / (aa + bb))
    ax.plot(range(1, n_obs + 1), means, color=col, lw=1.8, label=name,
            alpha=0.9)

ax.axhline(true_p, color=RED, ls='--', lw=1.8, label=f"truth p = {true_p}")
ax.set_xlabel("number of observations", fontsize=11)
ax.set_ylabel("posterior mean of p", fontsize=11)
ax.set_title("Different priors, same data -> beliefs converge (Bayes = learning)",
             fontsize=11)
ax.set_xlim(0, n_obs)
ax.set_ylim(0.2, 0.95)
ax.legend(fontsize=9, loc='right')
ax.grid(alpha=0.3)

plt.tight_layout()
save(fig, "fig-4-2-convergence-of-priors.png")
print("saved fig-4-2-convergence-of-priors.png")

print("done.")
