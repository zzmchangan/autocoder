"""第 20 章 · 生成模型与贝叶斯网 —— 配图脚本(全书收官章)。

独立脚本, 只 import _plot_utils, 绝不修改它。运行:
    python figures/fig-20-gen.py
产出:
    figures/fig-20-1-gmm-clustering.png     (招牌: 二维三类混合 + GMM 拟合 + 椭圆轮廓)
    figures/fig-20-2-mixture-of-gaussians.png (两个高斯叠加成双峰, 生成模型的"叠加"直觉)
    figures/fig-20-3-bayes-net.png          (贝叶斯网络 DAG: 医疗诊断示例)

所有图内标注一律英文(避免中文字体乱码), 正文用中文解释。
模拟一律固定随机种子(np.random.default_rng(42)), 保证可复现。
GMM 拟合用纯 numpy 手写 EM(不依赖 sklearn), 与正文代码一致, 读者可直接跑。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         plot_pdf)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import norm
from matplotlib.patches import Ellipse

# ============================================================
# 公共:手写 GMM 的 EM(diag 协方差), 与正文模拟代码一致
# 输入 X (N,2), 返回 weights, means, sds(N,2), resp(N,K)
# ============================================================
def fit_gmm_diag(X, K, n_iter=120, seed=0):
    rng = np.random.default_rng(seed)
    N = X.shape[0]
    idx = rng.choice(N, K, replace=False)
    mu = X[idx].copy()
    var = np.full((K, X.shape[1]), X.var(0))
    w = np.full(K, 1.0 / K)
    resp = None
    for _ in range(n_iter):
        resp = np.zeros((N, K))
        for k in range(K):
            resp[:, k] = w[k] * np.prod(
                norm.pdf(X, mu[k], np.sqrt(var[k])), axis=1)
        resp /= resp.sum(1, keepdims=True)
        Nk = resp.sum(0)
        w = Nk / N
        for k in range(K):
            mu[k] = (resp[:, k:k + 1] * X).sum(0) / Nk[k]
            var[k] = (resp[:, k:k + 1] * (X - mu[k]) ** 2).sum(0) / Nk[k]
    sds = np.sqrt(var)
    return w, mu, sds, resp


# ============================================================
# 图 20.1 · 招牌: GMM 聚类(三类二维混合)
# 左:生成的混合数据(按真实类着色) + 三个高斯分量的 2-sigma 椭圆
# 右:EM 拟合恢复的参数(weights/means/sds)与真值对照
# ============================================================
rng_master = np.random.default_rng(42)

# 真实三类: 权重 0.25/0.50/0.25, 均值 [-4,0],[0,0],[4,0], sd 0.7
true_w = np.array([0.25, 0.50, 0.25])
true_mu = np.array([[-4.0, 0.0], [0.0, 0.0], [4.0, 0.0]])
true_sd = 0.7
N = 4000
comp = rng_master.choice(3, size=N, p=true_w)
X = true_mu[comp] + rng_master.normal(0, true_sd, (N, 2))

# 注意: EM 对初始化敏感, 有局部最优。seed=42 恰好给出干净的恢复;
# 换成 seed=0 / 1 会落到差的局部解(正文会点出这一点)。
w, mu, sds, resp = fit_gmm_diag(X, K=3, n_iter=120, seed=42)
order = np.argsort(mu[:, 0])  # 按 x 排序, 与真值对齐
w_o = w[order]; mu_o = mu[order]; sds_o = sds[order]
pred = order[resp.argmax(1)]
acc = (pred == comp).mean()

fig, (axL, axR) = plt.subplots(1, 2, figsize=(12, 4.8))

# 左: 散点 + 椭圆(真实分量)
cols = [RED, GREEN, BLUE]
for c in range(3):
    axL.scatter(X[comp == c, 0], X[comp == c, 1], color=cols[c], s=8,
                alpha=0.35, edgecolor='none', label=f"true comp {c+1}")
for c in range(3):
    # 2-sigma 椭圆(真实均值/标准差)
    ell = Ellipse(xy=true_mu[c], width=2 * 2 * true_sd, height=2 * 2 * true_sd,
                  angle=0, edgecolor='k', facecolor='none', lw=1.6, ls='--')
    axL.add_patch(ell)
    axL.annotate(f"N({true_mu[c,0]:.0f},0; {true_sd}s)",
                 (true_mu[c, 0], true_mu[c, 1] + 2 * true_sd + 0.25),
                 ha='center', fontsize=8)
axL.set_xlim(-7, 7); axL.set_ylim(-6, 6)
axL.set_xlabel("feature  x1"); axL.set_ylabel("feature  x2")
axL.set_title("(a) Mixture data (3 Gaussians, 2-sigma ellipses)", fontsize=10.5)
axL.legend(loc='upper right', fontsize=8.5, markerscale=2)
axL.grid(alpha=0.25)
axL.set_aspect('equal', adjustable='box')

# 右: 拟合恢复的参数 vs 真值, 文字 + 简图
axR.axis('off')
txt = "EM fit recovered parameters vs truth\n" + "=" * 46 + "\n\n"
txt += "         weights              means (x, y)         sd\n"
txt += "-" * 50 + "\n"
for c in range(3):
    txt += (f"fit  :  [{w_o[c]:.3f}]    "
            f"({mu_o[c,0]:+.3f}, {mu_o[c,1]:+.3f})    "
            f"{sds_o[c,0]:.3f}\n")
    txt += (f"true :  [{true_w[c]:.3f}]    "
            f"({true_mu[c,0]:+.3f}, {true_mu[c,1]:+.3f})    "
            f"{true_sd:.3f}\n\n")
txt += "-" * 50 + "\n"
txt += f"clustering accuracy (after label alignment): {acc*100:.1f}%\n"
txt += f"sample size N = {N}"
axR.text(0.02, 0.98, txt, transform=axR.transAxes, fontsize=9.2,
         verticalalignment='top', family='monospace',
         bbox=dict(boxstyle='round', facecolor='#f7f7f7', edgecolor='gray'))
axR.set_title("(b) Generative model recovers how the world made the data",
              fontsize=10.5)

save(fig, "fig-20-1-gmm-clustering.png")
print("saved fig-20-1-gmm-clustering.png  (acc=%.3f)" % acc)

# ============================================================
# 图 20.2 · 两个高斯叠加成双峰(生成模型的"叠加"直觉)
# p(x) = 0.6 * N(-2, 1) + 0.4 * N(2, 0.8)
# 画出两个分量(半透明)+ 叠加后的混合 PDF(实线), 叠加模拟直方图
# ============================================================
fig2, ax = plt.subplots(figsize=(8.2, 4.6))
xs = np.linspace(-6, 6, 600)
pi1, pi2 = 0.6, 0.4
m1, s1, m2, s2 = -2.0, 1.0, 2.0, 0.8
p1 = norm.pdf(xs, m1, s1)
p2 = norm.pdf(xs, m2, s2)
mix = pi1 * p1 + pi2 * p2

ax.plot(xs, p1, color=BLUE, lw=1.8, ls='--', alpha=0.8,
        label=f"comp1: N({m1}, {s1})")
ax.plot(xs, p2, color=GREEN, lw=1.8, ls='--', alpha=0.8,
        label=f"comp2: N({m2}, {s2})")
ax.fill_between(xs, 0, pi1 * p1, color=BLUE, alpha=0.15)
ax.fill_between(xs, 0, pi2 * p2, color=GREEN, alpha=0.15)
ax.plot(xs, mix, color=RED, lw=2.8,
        label=r"mixture: $0.6\,N(-2,1)+0.4\,N(2,0.8)$")

# 叠加模拟直方图验证
rng2 = np.random.default_rng(7)
nsamp = 200000
which = rng2.choice(2, size=nsamp, p=[pi1, pi2])
samples = np.where(which == 0,
                   rng2.normal(m1, s1, nsamp),
                   rng2.normal(m2, s2, nsamp))
ax.hist(samples, bins=80, density=True, color=ORANGE, alpha=0.25,
        edgecolor='white', linewidth=0.2, label="samples from mixture")

ax.set_xlabel("x"); ax.set_ylabel("density  f(x)")
ax.set_title("A mixture of Gaussians = stacking weighted components",
             fontsize=10.8)
ax.set_ylim(0, 0.46)
ax.legend(loc='upper right', fontsize=8.8)
ax.grid(alpha=0.25)
save(fig2, "fig-20-2-mixture-of-gaussians.png")
print("saved fig-20-2-mixture-of-gaussians.png")

# ============================================================
# 图 20.3 · 贝叶斯网络 DAG(医疗诊断示例)
# 节点: Smoking -> LungCancer, Smoking -> Bronchitis,
#       LungCancer -> Xray, LungCancer -> Fatigue,
#       Bronchitis -> Cough, Bronchitis -> Fatigue
# 用 matplotlib 手画节点 + 有向箭头
# ============================================================
fig3, ax3 = plt.subplots(figsize=(8.6, 5.4))
ax3.set_xlim(0, 10); ax3.set_ylim(0, 7)
ax3.axis('off')

nodes = {
    "Smoking":     (5.0, 6.2),
    "LungCancer":  (2.4, 4.2),
    "Bronchitis":  (7.6, 4.2),
    "Xray":        (1.0, 1.6),
    "Fatigue":     (5.0, 1.6),
    "Cough":       (9.0, 1.6),
}
edges = [("Smoking", "LungCancer"), ("Smoking", "Bronchitis"),
         ("LungCancer", "Xray"), ("LungCancer", "Fatigue"),
         ("Bronchitis", "Cough"), ("Bronchitis", "Fatigue")]

# 画边(有向箭头), 留出节点边缘
def draw_edge(ax, a, b, shrink=0.32):
    x1, y1 = nodes[a]; x2, y2 = nodes[b]
    dx, dy = x2 - x1, y2 - y1
    L = np.hypot(dx, dy)
    ux, uy = dx / L, dy / L
    ax.annotate("", xy=(x2 - ux * shrink, y2 - uy * shrink),
                xytext=(x1 + ux * shrink, y1 + uy * shrink),
                arrowprops=dict(arrowstyle="->", lw=1.6, color='k'))

for a, b in edges:
    draw_edge(ax3, a, b)

# 画节点(矩形, 根节点 vs 症状节点 颜色区分)
root_color = "#cfe8ff"; cause_color = "#ffe0b3"; symp_color = "#d7f5d7"
node_color = {"Smoking": root_color, "LungCancer": cause_color,
              "Bronchitis": cause_color, "Xray": symp_color,
              "Fatigue": symp_color, "Cough": symp_color}
for name, (x, y) in nodes.items():
    ax3.add_patch(plt.Rectangle((x - 0.85, y - 0.32), 1.7, 0.64,
                                facecolor=node_color[name],
                                edgecolor='k', lw=1.3))
    ax3.text(x, y, name, ha='center', va='center', fontsize=10)

# 标注: 隐藏 vs 可观测
ax3.text(5.0, 6.95, "Bayes Net for medical diagnosis", ha='center',
         fontsize=11.5, fontweight='bold')
ax3.text(0.2, 0.5, "observed\n(symptoms / test)", fontsize=8.5,
         color='#2c7a2c')
ax3.text(6.7, 5.0, "hidden causes\n(diseases)", fontsize=8.5,
         color='#b5651d')
ax3.text(4.3, 6.7, "root (prior)", fontsize=8.5, color='#1f6fb2')

ax3.set_title("Directed Acyclic Graph (DAG): encode conditional dependencies",
              fontsize=10.5)
save(fig3, "fig-20-3-bayes-net.png")
print("saved fig-20-3-bayes-net.png")

print("ALL DONE")
