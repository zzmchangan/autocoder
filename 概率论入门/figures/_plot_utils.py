"""概率论系列 · 绘图辅助函数(供各章配图脚本 import 复用)。

设计目的: 多个写作 agent 并行写不同章节、各自生成配图时,
都从本模块 import 通用工具, **绝不修改本文件**, 各自写独立的
`fig-{章}-gen.py` 脚本, 避免并行冲突。

用法(在某章的 gen 脚本里):
    import sys, os
    HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, HERE)
    from _plot_utils import (save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY,
                             plot_pmf, plot_pdf, sim_hist, convergence, heatmap2d, vline)
    import numpy as np, matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
"""
import os
import numpy as np
import matplotlib.pyplot as plt

# ---------- 全书统一配色(与线代系列一致) ----------
GREEN  = '#2ca02c'   # 理论曲线 / 主分布
RED    = '#d62728'   # 模拟 / 强调 / 真值线
BLUE   = '#1f77b4'   # 收敛曲线 / 第二分布 / 填充
PURPLE = '#9467bd'   # 第三分布 / 后验
ORANGE = '#ff7f0e'   # 备用 / 收敛真值线
GRAY   = 'gray'


def save(fig, name):
    """把图保存到本文件所在目录(figures/)。"""
    fig.tight_layout()
    fig.savefig(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), dpi=150)
    plt.close(fig)


def plot_pmf(ax, xs, ps, color=GREEN, label=None, width=0.7):
    """画离散分布的 PMF(柱状图)。xs=取值, ps=概率。"""
    xs = np.asarray(xs)
    ps = np.asarray(ps, float)
    ax.bar(xs, ps, width=width, color=color, alpha=0.85,
           edgecolor='k', linewidth=0.4, label=label)
    ax.set_xlabel("x")
    ax.set_ylabel("P(X = x)")


def plot_pdf(ax, xs, ys, color=GREEN, label=None, fill=False, fill_alpha=0.15, lw=2.2):
    """画连续分布的 PDF 曲线。xs=坐标, ys=密度; fill=True 在曲线下填色(表示概率/面积)。"""
    xs = np.asarray(xs, float)
    ys = np.asarray(ys, float)
    ax.plot(xs, ys, color=color, lw=lw, label=label, zorder=4)
    if fill:
        ax.fill_between(xs, ys, color=color, alpha=fill_alpha, zorder=2)
    ax.set_xlabel("x")
    ax.set_ylabel("density  f(x)")


def sim_hist(ax, samples, bins=40, color=RED, density=True, label=None):
    """画模拟样本的直方图(常与 plot_pdf 叠加, 验证'模拟 ≈ 理论')。"""
    ax.hist(samples, bins=bins, density=density, color=color, alpha=0.45,
            edgecolor='white', linewidth=0.3, label=label)


def convergence(ax, ns, values, truth=None, color=BLUE, truth_color=ORANGE,
                ylabel="value"):
    """画'扔 n 次, 某统计量趋近真值'的收敛曲线(大数定律 / 蒙特卡洛的招牌图)。
    ns=抽样次数序列, values=对应统计量序列, truth=理论真值(画水平虚线)。
    x 轴用对数, 便于看清从剧烈抖动到稳定收敛的过程。"""
    ax.plot(ns, values, color=color, lw=1.6, label="simulation")
    if truth is not None:
        ax.axhline(truth, color=truth_color, ls='--', lw=1.8, label=f"truth = {truth}")
    ax.set_xlabel("number of draws  n")
    ax.set_ylabel(ylabel)
    ax.set_xscale('log')


def heatmap2d(ax, X, Y, Z, cmap='Blues'):
    """画二维联合分布 / 后验的热力图。X, Y=坐标网格, Z=概率/密度。"""
    im = ax.pcolormesh(X, Y, Z, cmap=cmap, shading='auto')
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    return im


def vline(ax, x, color=RED, ls='--', lw=1.6, label=None):
    """画一条竖直参考线(如均值、中位数、拒绝域边界)。"""
    ax.axvline(x, color=color, ls=ls, lw=lw, label=label)


def hline(ax, y, color=ORANGE, ls='--', lw=1.6, label=None):
    """画一条水平参考线(如理论真值、显著水平)。"""
    ax.axhline(y, color=color, ls=ls, lw=lw, label=label)
