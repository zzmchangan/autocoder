"""第 12 章 · 协方差与相关 —— 配图生成脚本。

严禁修改 _plot_utils.py, 本脚本独立产出:
  fig-12-1-corr-scatter.png    —— 招牌图:不同相关系数 rho 的散点图(五个子图)
  fig-12-2-anscombe.png        —— Anscombe 四组(同样相关系数, 不同长相)

图内标注一律英文(避免中文字体乱码), 正文用中文。
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# =========================================================================
# 图 12.1 —— 招牌图:不同相关系数 rho 的散点图
# =========================================================================
def fig_12_1_corr_scatter():
    rng = np.random.default_rng(42)
    n = 2000
    rhos = [-1.0, -0.5, 0.0, 0.5, 1.0]
    titles = [f"rho = {r:.1f}" for r in rhos]

    fig, axes = plt.subplots(1, 5, figsize=(17, 3.6))
    for ax, rho, title in zip(axes, rhos, titles):
        if abs(rho) == 1.0:
            # 完全线性: Y = rho * X (无噪声), 散点排成一条直线
            X = rng.normal(0, 1, n)
            Y = rho * X
        else:
            # 二维正态采样
            cov = [[1.0, rho], [rho, 1.0]]
            samples = rng.multivariate_normal([0, 0], cov, n)
            X, Y = samples[:, 0], samples[:, 1]

        ax.scatter(X, Y, s=6, alpha=0.35, color=BLUE, edgecolors='none')
        # 理论回归线 (Y on X 的 OLS 斜率 = rho, 因 sigma_x=sigma_y=1)
        xs = np.linspace(-4, 4, 50)
        ax.plot(xs, rho * xs, color=RED, lw=2.0, label=f"slope = {rho:.1f}")

        # 样本相关系数 (用十万次核对过的 numpy)
        r_hat = np.corrcoef(X, Y)[0, 1]
        ax.set_title(f"{title}   (sample r = {r_hat:.3f})", fontsize=11)
        ax.set_xlim(-4, 4); ax.set_ylim(-4, 4)
        ax.set_xlabel("x"); ax.set_ylabel("y")
        ax.axhline(0, color=GRAY, lw=0.5, alpha=0.5)
        ax.axvline(0, color=GRAY, lw=0.5, alpha=0.5)
        ax.set_aspect('equal')
        ax.legend(fontsize=8, loc='upper left')

    fig.suptitle("Scatter of bivariate normal at different correlation rho",
                 fontsize=12, y=1.02)
    save(fig, "fig-12-1-corr-scatter.png")


# =========================================================================
# 图 12.2 —— Anscombe 四组(同样的相关系数, 完全不同的数据长相)
# =========================================================================
def fig_12_2_anscombe():
    # Anscombe (1973) 的四组经典数据
    x1 = [10, 8, 13, 9, 11, 14, 6, 4, 12, 7, 5]
    y1 = [8.04, 6.95, 7.58, 8.81, 8.33, 9.96, 7.24, 4.26, 10.84, 4.82, 5.68]

    x2 = [10, 8, 13, 9, 11, 14, 6, 4, 12, 7, 5]
    y2 = [9.14, 8.14, 8.74, 8.77, 9.26, 8.10, 6.13, 3.10, 9.13, 7.26, 4.74]

    x3 = [10, 8, 13, 9, 11, 14, 6, 4, 12, 7, 5]
    y3 = [7.46, 6.77, 12.74, 7.11, 7.81, 8.84, 6.08, 5.39, 8.15, 6.42, 5.73]

    x4 = [8, 8, 8, 8, 8, 8, 8, 19, 8, 8, 8]
    y4 = [6.58, 5.76, 7.71, 8.84, 8.47, 7.04, 5.25, 12.50, 5.56, 7.91, 6.89]

    datasets = [(x1, y1, "I: linear"),
                (x2, y2, "II: curved"),
                (x3, y3, "III: outlier (high)"),
                (x4, y4, "IV: outlier (x)")]

    fig, axes = plt.subplots(1, 4, figsize=(16, 4))
    for ax, (x, y, name) in zip(axes, datasets):
        x_arr = np.asarray(x, float)
        y_arr = np.asarray(y, float)
        r = np.corrcoef(x_arr, y_arr)[0, 1]
        # 拟合回归线
        b, a = np.polyfit(x_arr, y_arr, 1)
        xs = np.linspace(x_arr.min() - 1, x_arr.max() + 1, 50)
        ax.scatter(x, y, s=55, color=BLUE, edgecolor='k', linewidth=0.5, zorder=3)
        ax.plot(xs, a + b * xs, color=RED, lw=2.0)
        ax.set_title(f"{name}\nr = {r:.3f}", fontsize=11)
        ax.set_xlabel("x"); ax.set_ylabel("y")
        ax.set_xlim(2, 20); ax.set_ylim(2, 14)

    fig.suptitle("Anscombe's quartet: same r = 0.816, four very different stories",
                 fontsize=12, y=1.02)
    save(fig, "fig-12-2-anscombe.png")


# =========================================================================
# 核对:十万次模拟, np.corrcoef 是否趋近设定 rho
# =========================================================================
def sanity_check():
    rng = np.random.default_rng(42)
    n = 100_000
    print("=== 核对: 十万次模拟, 样本相关系数 vs 设定 rho ===")
    for rho in [-0.9, -0.5, 0.0, 0.5, 0.9]:
        cov = [[1.0, rho], [rho, 1.0]]
        s = rng.multivariate_normal([0, 0], cov, n)
        r_hat = np.corrcoef(s[:, 0], s[:, 1])[0, 1]
        cov_hat = np.cov(s[:, 0], s[:, 1], bias=False)[0, 1]
        print(f"  rho={rho:+.2f}  ->  sample r={r_hat:+.4f}  "
              f"sample cov={cov_hat:+.4f}  (理论 cov={rho:+.2f})")

    print("\n=== Anscombe 四组的相关系数 ===")
    groups = [
        ([10, 8, 13, 9, 11, 14, 6, 4, 12, 7, 5],
         [8.04, 6.95, 7.58, 8.81, 8.33, 9.96, 7.24, 4.26, 10.84, 4.82, 5.68]),
        ([10, 8, 13, 9, 11, 14, 6, 4, 12, 7, 5],
         [9.14, 8.14, 8.74, 8.77, 9.26, 8.10, 6.13, 3.10, 9.13, 7.26, 4.74]),
        ([10, 8, 13, 9, 11, 14, 6, 4, 12, 7, 5],
         [7.46, 6.77, 12.74, 7.11, 7.81, 8.84, 6.08, 5.39, 8.15, 6.42, 5.73]),
        ([8, 8, 8, 8, 8, 8, 8, 19, 8, 8, 8],
         [6.58, 5.76, 7.71, 8.84, 8.47, 7.04, 5.25, 12.50, 5.56, 7.91, 6.89]),
    ]
    for i, (x, y) in enumerate(groups, 1):
        r = np.corrcoef(x, y)[0, 1]
        mx, my = np.mean(x), np.mean(y)
        cov = np.cov(x, y, bias=False)[0, 1]
        print(f"  组{i}: mean_x={mx:.2f} mean_y={my:.2f} "
              f"var_x={np.var(x, ddof=1):.3f} var_y={np.var(y, ddof=1):.3f} "
              f"cov={cov:.3f} r={r:.4f}")

    print("\n=== 协方差纸笔核对 (身高体重小例) ===")
    # X=身高偏离 (cm-170), Y=体重偏离 (kg-65), 5 个样本
    X = np.array([-3, -1, 0, 2, 6])    # 偏离均值 170
    Y = np.array([-5, -2, 0, 3, 8])    # 偏离均值 65
    print(f"  X 偏离: {X},  Y 偏离: {Y}")
    print(f"  E[X·Y] = mean(X*Y) = {np.mean(X*Y):.3f}")
    print(f"  即协方差 Cov(X,Y) = {np.mean(X*Y):.3f} (用总体口径)")
    print(f"  np.cov (样本口径, ddof=1) = {np.cov(X, Y, bias=False)[0,1]:.3f}")
    # 标准化为相关系数
    sx, sy = X.std(), Y.std()
    print(f"  相关 rho = Cov/(sx*sy) = {np.mean(X*Y)/(sx*sy):.4f}")


if __name__ == "__main__":
    sanity_check()
    fig_12_1_corr_scatter()
    fig_12_2_anscombe()
    print("\n生成完成: fig-12-1-corr-scatter.png, fig-12-2-anscombe.png")
