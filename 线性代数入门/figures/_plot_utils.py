"""线性代数系列 · 绘图辅助函数(供各章配图脚本 import 复用)。

设计目的: 多个写作 agent 并行写不同章节、各自生成配图时,
都从本模块 import 通用工具, **绝不修改本文件、也不修改 make_figures.py 主文件**,
各自写独立的 `fig-{篇}-{章}-gen.py` 脚本, 避免并行冲突。

用法(在某章的 gen 脚本里):
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE
"""
import os
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Polygon  # noqa: F401  (Polygon 供调用方按需用)

# ---------- 全书统一配色 ----------
GREEN  = '#2ca02c'   # 第一根基向量 i / 向量 v
RED    = '#d62728'   # 第二根基向量 j / 向量 w
BLUE   = '#1f77b4'   # 变换后 / 线性组合结果 / 张成区域
PURPLE = '#9467bd'   # 第三向量 / 冗余向量 / 特征方向
ORANGE = '#ff7f0e'   # 备用
GRAY   = 'gray'


def arrow(ax, start, end, color, lw=2.2, ms=18):
    """从 start 到 end 画一根带箭头的线段(向量)。"""
    ax.add_patch(FancyArrowPatch(tuple(start), tuple(end),
                 arrowstyle='-|>', mutation_scale=ms, color=color, lw=lw, zorder=5))


def grid(ax, M, color, alpha, ls='-', lw=0.9, n=2):
    """画线性变换 M 作用后的网格(每个方向画 -n..n 条线)。
    传 np.eye(2) 画原始方格; 传某个矩阵 M 画它"揉捏后"的网格。"""
    M = np.asarray(M, float)
    for k in range(-n, n + 1):
        v = np.array([[k, -n], [k, n]]) @ M.T
        h = np.array([[-n, k], [n, k]]) @ M.T
        ax.plot(v[:, 0], v[:, 1], color=color, alpha=alpha, ls=ls, lw=lw)
        ax.plot(h[:, 0], h[:, 1], color=color, alpha=alpha, ls=ls, lw=lw)


def frame(ax, lim, title=None):
    """统一坐标系外观: 等比例、十字坐标轴、对称范围。"""
    ax.set_aspect('equal'); ax.set_xlim(-lim, lim); ax.set_ylim(-lim, lim)
    ax.axhline(0, color='gray', lw=0.6, alpha=0.4)
    ax.axvline(0, color='gray', lw=0.6, alpha=0.4)
    if title:
        ax.set_title(title, fontsize=12)


def save(fig, name):
    """把图保存到本文件所在目录(figures/)。"""
    fig.tight_layout()
    fig.savefig(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), dpi=150)
    plt.close(fig)
