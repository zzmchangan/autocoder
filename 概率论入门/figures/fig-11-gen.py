"""第 11 章 · 联合分布与边缘分布 —— 配图生成脚本。

图 11.1 · 三种联合分布, 边缘却完全相同(独立 / 正相关 / 反相关):
        揭示"边缘分布丢失了关系信息"。这是本章最深、也最招牌的一张图。
图 11.2 · 二维正态的联合密度(热力图)+ 边缘分布(把另一个积分掉):
        连续情形的"联合 -> 边缘 = 积分"。

只 import _plot_utils.py 的工具, 绝不修改它。模拟固定种子 np.random.default_rng(42)。
图内标注一律英文, 正文中文。
"""
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                         plot_pmf, plot_pdf, sim_hist, heatmap2d, vline)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats


# ============================================================
# 图 11.1 · 三个联合分布, 边缘完全相同
#   行 = X(身高档 0矮/1中/2高), 列 = Y(体重档 0轻/1中/2重)
#   独立、正相关、反相关三种联合, 行和列和都一模一样。
# ============================================================
def fig_11_1_same_marginal_different_joint():
    # 边缘
    px = np.array([0.25, 0.50, 0.25])   # X: 矮/中/高
    py = np.array([0.20, 0.60, 0.20])   # Y: 轻/中/重

    # 1) 独立联合 = 外积
    P_indep = np.outer(px, py)
    # 2) 正相关(对角线集中, 身高高->体重大)
    P_pos = np.array([
        [0.18, 0.06, 0.01],
        [0.02, 0.46, 0.02],
        [0.00, 0.08, 0.17]])
    # 3) 反相关(反对角线集中, 身高高->体重小)
    P_neg = np.array([
        [0.00, 0.08, 0.17],
        [0.02, 0.46, 0.02],
        [0.18, 0.06, 0.01]])

    titles = ["independent  P(X,Y)=P(X)P(Y)",
              "positively correlated",
              "negatively correlated"]
    mats = [P_indep, P_pos, P_neg]

    fig, axes = plt.subplots(1, 3, figsize=(14, 4.4))
    Xs = [0, 1, 2]
    Ys = [0, 1, 2]

    for ax, M, t in zip(axes, mats, titles):
        # 热力图
        im = ax.imshow(M, cmap='Blues', vmin=0, vmax=0.50, origin='lower')
        # 在格子里写概率值
        for i in range(3):
            for j in range(3):
                val = M[i, j]
                color = 'white' if val > 0.25 else 'black'
                ax.text(j, i, f"{val:.2f}", ha='center', va='center',
                        color=color, fontsize=10, fontweight='bold')
        ax.set_xticks(Ys)
        ax.set_yticks(Xs)
        ax.set_xticklabels(['light', 'medium', 'heavy'])
        ax.set_yticklabels(['short', 'medium', 'tall'])
        ax.set_xlabel("Y  (weight)")
        ax.set_ylabel("X  (height)")
        ax.set_title(t, fontsize=10.5)

    # 统一 colorbar
    cbar = fig.colorbar(im, ax=axes, shrink=0.85, pad=0.02)
    cbar.set_label("P(X=i, Y=j)")

    # 顶部大标题: 强调三者边缘相同
    fig.suptitle("Three joint distributions, identical marginals  "
                 "(marginal X = [0.25, 0.50, 0.25],  marginal Y = [0.20, 0.60, 0.20])",
                 fontsize=11.5, y=1.02)

    save(fig, "fig-11-1-same-marginal-different-joint.png")
    print("saved: fig-11-1-same-marginal-different-joint.png")


# ============================================================
# 图 11.2 · 二维正态: 联合密度热力图 + 两条边缘(积分掉另一个)
#   上方/右侧的曲线 = 把另一个变量积分掉得到的边缘, 都是正态。
# ============================================================
def fig_11_2_bivariate_normal_joint_and_marginals():
    mux, muy = 170.0, 65.0
    sdx, sdy = 7.0, 10.0
    rho = 0.7
    cov = [[sdx**2, rho*sdx*sdy],
           [rho*sdx*sdy, sdy**2]]
    mvn = stats.multivariate_normal([mux, muy], cov)

    # 网格
    xs = np.linspace(140, 200, 220)
    ys = np.linspace(35, 95, 220)
    X, Y = np.meshgrid(xs, ys)
    pos = np.dstack([X, Y])
    Z = mvn.pdf(pos)   # 联合密度

    fig = plt.figure(figsize=(11, 9))
    # 用 GridSpec: 主热力图 + 上方边缘X + 右侧边缘Y
    gs = fig.add_gridspec(2, 2, width_ratios=[4, 1], height_ratios=[1, 4],
                          hspace=0.05, wspace=0.05)
    ax_top = fig.add_subplot(gs[0, 0])     # 边缘 f_X(x)
    ax_main = fig.add_subplot(gs[1, 0])    # 联合热力图
    ax_right = fig.add_subplot(gs[1, 1])   # 边缘 f_Y(y)
    ax_top.sharex(ax_main)
    ax_right.sharey(ax_main)

    # 主热力图: 联合密度
    im = ax_main.pcolormesh(X, Y, Z, cmap='viridis', shading='auto')
    ax_main.set_xlabel("X  (height, cm)")
    ax_main.set_ylabel("Y  (weight, kg)")
    ax_main.set_title("joint density  f(x, y)   "
                      "(bivariate normal,  rho = 0.7)", pad=28)
    # 等高线叠一层, 更立体
    ax_main.contour(X, Y, Z, levels=8, colors='white', linewidths=0.5, alpha=0.6)

    # 边缘 f_X(x) = 对 y 积分 = N(mux, sdx)  (二维正态铁律)
    fx = stats.norm(mux, sdx).pdf(xs)
    ax_top.plot(xs, fx, color=ORANGE, lw=2.4, label=r"$f_X(x)=\int f(x,y)dy$")
    ax_top.fill_between(xs, fx, color=ORANGE, alpha=0.25)
    ax_top.set_ylabel(r"$f_X(x)$")
    ax_top.legend(loc='upper right', fontsize=9)
    ax_top.tick_params(labelbottom=False)

    # 边缘 f_Y(y) = 对 x 积分 = N(muy, sdy)
    fy = stats.norm(muy, sdy).pdf(ys)
    ax_right.plot(fy, ys, color=RED, lw=2.4, label=r"$f_Y(y)=\int f(x,y)dx$")
    ax_right.fill_betweenx(ys, fy, color=RED, alpha=0.25)
    ax_right.set_xlabel(r"$f_Y(y)$")
    ax_right.legend(loc='upper right', fontsize=9)
    ax_right.tick_params(labelleft=False)

    # colorbar
    cbar = fig.colorbar(im, ax=ax_main, pad=0.02, shrink=0.85)
    cbar.set_label("joint density  f(x, y)")

    save(fig, "fig-11-2-joint-and-marginals.png")
    print("saved: fig-11-2-joint-and-marginals.png")


if __name__ == "__main__":
    fig_11_1_same_marginal_different_joint()
    fig_11_2_bivariate_normal_joint_and_marginals()
    print("done.")
