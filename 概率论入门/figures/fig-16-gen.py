"""第 16 章 · 假设检验与 p 值 · 配图脚本。

产出三张图:
  fig-16-1-pvalue-rejection-region.png  — 招牌图:零假设下的正态分布,
                                          观测统计量 z=2.0 的右尾面积 = 单尾 p 值,
                                          以及 alpha=0.05 的拒绝域临界线 z=1.645。
  fig-16-2-pvalue-distribution.png      — 左:H0 为真时 p 值近似 Uniform(0,1);
                                          右:有效应时 p 值堆积向 0(功效)。
  fig-16-3-ci-coverage.png              — 重复抽样下 95% 置信区间的覆盖频率
                                          (多数区间套住真值,约 5% 漏掉)。

严禁修改 _plot_utils.py;只 import。模拟固定种子 np.random.default_rng(42)。
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
from scipy import stats


# ============================================================
# 图 16.1 · p 值与拒绝域(招牌图)
# ============================================================
def fig1_pvalue_rejection_region():
    fig, ax = plt.subplots(figsize=(9, 4.8))

    # 零假设下的标准正态分布 (例如样本均值标准化后的分布)
    xs = np.linspace(-3.6, 3.6, 800)
    ys = stats.norm.pdf(xs)
    plot_pdf(ax, xs, ys, color=BLUE, label="distribution under H0:  N(0,1)")

    # 观测到的统计量 z_obs = 2.0 (例如 100 次抛硬币 60 次正面的 z 统计量)
    z_obs = 2.0
    # 单尾 p 值 = P(Z >= z_obs | H0) = 右尾面积
    p_one = 1 - stats.norm.cdf(z_obs)
    # 双尾 p 值
    p_two = 2 * p_one

    # 填充右尾 (= 单尾 p 值)
    xtail = np.linspace(z_obs, 3.6, 200)
    ytail = stats.norm.pdf(xtail)
    ax.fill_between(xtail, ytail, color=RED, alpha=0.35,
                    label=f"one-sided p  =  P(Z>=2.0)  =  {p_one:.3f}")
    # 填充对称的左尾,凑出双尾
    xtail_l = np.linspace(-3.6, -z_obs, 200)
    ytail_l = stats.norm.pdf(xtail_l)
    ax.fill_between(xtail_l, ytail_l, color=RED, alpha=0.18,
                    label=f"two-sided p  =  {p_two:.3f}")

    # 拒绝域临界线: 单尾 alpha=0.05 => z = 1.645
    z_crit = stats.norm.ppf(0.95)
    vline(ax, z_crit, color=ORANGE, ls='--', lw=1.8,
          label=f"critical value  z_0.05  =  {z_crit:.3f}  (rejection region)")
    vline(ax, z_obs, color=RED, ls='-', lw=1.6,
          label=f"observed  z  =  {z_obs:.2f}")

    # 观测统计量标注
    ax.annotate(f"observed z = {z_obs}",
                xy=(z_obs, stats.norm.pdf(z_obs)),
                xytext=(z_obs + 0.35, 0.22),
                fontsize=10, color=RED,
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.2))
    ax.annotate(f"p = {p_one:.3f}\n(tail area)",
                xy=(z_obs + 0.6, 0.02),
                xytext=(z_obs + 0.6, 0.08),
                fontsize=9.5, color=RED, ha="center",
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.0))

    ax.set_title("p-value and rejection region  (testing H0 under N(0,1))",
                 fontsize=12)
    ax.set_xlim(-3.6, 3.6)
    ax.set_ylim(0, 0.45)
    ax.legend(loc="upper left", fontsize=8.6, framealpha=0.92)
    save(fig, "fig-16-1-pvalue-rejection-region.png")


# ============================================================
# 图 16.2 · p 值分布:H0 下均匀,有效应时堆积向 0
# ============================================================
def fig2_pvalue_distribution():
    rng = np.random.default_rng(42)
    N = 20000
    n = 30

    # 左:H0 为真 (mu=0),反复做单样本 t 检验
    pvals_H0 = np.empty(N)
    for i in range(N):
        d = rng.normal(0, 1, n)
        _, p = stats.ttest_1samp(d, 0)
        pvals_H0[i] = p

    # 右:有效应 (mu=0.5)
    pvals_H1 = np.empty(N)
    for i in range(N):
        d = rng.normal(0.5, 1, n)
        _, p = stats.ttest_1samp(d, 0)
        pvals_H1[i] = p

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.3))

    # 左图: H0 下 p 值应近似均匀
    ax = axes[0]
    ax.hist(pvals_H0, bins=20, range=(0, 1), density=True,
            color=BLUE, alpha=0.55, edgecolor='white',
            label=f"simulated p-values  (n={N})")
    hline(ax, 1.0, color=ORANGE, ls='--', lw=1.8,
          label="Uniform(0,1)  (theory under H0)")
    ax.axvline(0.05, color=RED, ls=':', lw=1.6)
    ax.text(0.055, 1.6, f"P(p<0.05) = {(pvals_H0<0.05).mean():.3f}  ~=  0.05",
            color=RED, fontsize=9.5)
    ax.set_xlabel("p-value")
    ax.set_ylabel("density")
    ax.set_xlim(0, 1); ax.set_ylim(0, 2.0)
    ax.set_title("H0 true  ->  p-value ~ Uniform(0,1)", fontsize=11)
    ax.legend(loc="upper right", fontsize=8.6, framealpha=0.92)

    # 右图: 有效应时 p 值堆积向 0
    ax = axes[1]
    ax.hist(pvals_H1, bins=20, range=(0, 1), density=True,
            color=RED, alpha=0.5, edgecolor='white',
            label=f"simulated p-values  (true effect=0.5)")
    ax.axvline(0.05, color=RED, ls=':', lw=1.6)
    power = (pvals_H1 < 0.05).mean()
    ax.text(0.055, 4.0, f"power = P(p<0.05 | effect) = {power:.3f}",
            color=RED, fontsize=9.5)
    ax.set_xlabel("p-value")
    ax.set_ylabel("density")
    ax.set_xlim(0, 1); ax.set_ylim(0, 6.0)
    ax.set_title("Effect exists  ->  p-value piles up at 0", fontsize=11)
    ax.legend(loc="upper right", fontsize=8.6, framealpha=0.92)

    save(fig, "fig-16-2-pvalue-distribution.png")


# ============================================================
# 图 16.3 · 置信区间覆盖率
# ============================================================
def fig3_ci_coverage():
    rng = np.random.default_rng(42)
    mu_true, sigma, n = 5.0, 2.0, 20
    N_SHOW = 50              # 画 50 条区间
    N_TOTAL = 20000          # 算总覆盖率用 2 万

    # 先画 N_SHOW 条
    cis = []
    for i in range(N_SHOW):
        d = rng.normal(mu_true, sigma, n)
        m, s = d.mean(), d.std(ddof=1)
        half = stats.t.ppf(0.975, n - 1) * s / np.sqrt(n)
        cis.append((m - half, m + half, m))

    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.6),
                             gridspec_kw={'width_ratios': [3, 2]})

    # 左: 50 条 95% CI, 红色 = 没套住真值
    ax = axes[0]
    miss = 0
    for i, (lo, hi, m) in enumerate(cis):
        hit = (lo <= mu_true <= hi)
        if not hit:
            miss += 1
        color = GREEN if hit else RED
        ax.plot([lo, hi], [i, i], color=color, lw=1.4,
                alpha=0.85 if not hit else 0.55)
        ax.plot(m, i, 'o', color=color, markersize=3.2)
    ax.axvline(mu_true, color=ORANGE, ls='--', lw=1.8,
               label=f"true mean  mu = {mu_true}")
    ax.set_xlabel("confidence interval for mu")
    ax.set_ylabel("sample index")
    ax.set_title(f"50 of the 95% CIs  (red = misses true mu)", fontsize=11)
    ax.legend(loc="lower right", fontsize=8.8, framealpha=0.92)
    ax.set_ylim(-1, N_SHOW)

    # 右: 用 2 万次算覆盖率 + 一个条形
    # 复用 rng 继续
    cov = 0
    for i in range(N_TOTAL):
        d = rng.normal(mu_true, sigma, n)
        m, s = d.mean(), d.std(ddof=1)
        half = stats.t.ppf(0.975, n - 1) * s / np.sqrt(n)
        if (m - half) <= mu_true <= (m + half):
            cov += 1
    rate = cov / N_TOTAL

    ax = axes[1]
    bars = ax.bar(["covers\ntrue mu", "misses\ntrue mu"],
                  [rate, 1 - rate],
                  color=[GREEN, RED], alpha=0.78, edgecolor='k', linewidth=0.4)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("frequency over 20,000 samples")
    ax.set_title(f"95% CI coverage = {rate:.4f}", fontsize=11)
    for b, v in zip(bars, [rate, 1 - rate]):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.02, f"{v:.3f}",
                ha="center", fontsize=10)

    save(fig, "fig-16-3-ci-coverage.png")


if __name__ == "__main__":
    fig1_pvalue_rejection_region()
    fig2_pvalue_distribution()
    fig3_ci_coverage()
    # 列出生成的 PNG
    out = [f for f in os.listdir(HERE)
           if f.startswith("fig-16-") and f.endswith(".png")]
    print("Generated:", sorted(out))
