"""数学分析系列 · 绘图辅助函数(供各章配图脚本 import 复用)。

设计目的: 多个写作 agent 并行写不同章节、各自生成配图时,
都从本模块 import 通用工具, **绝不修改本文件**, 各自写独立的
`fig-{篇}-{章}-gen.py` 脚本, 避免并行冲突。

提供的工具面向分析数学的典型画面:
    - plot_fn / plot_curve : 画函数曲线(导数、泰勒、波形)
    - riemann_rects        : 黎曼和矩形(积分)
    - tangent              : 切线(导数)
    - spectrum             : 频谱柱(傅里叶)
    - band                 : ε-δ 区间带 / 收敛区间(极限)
    - marker               : 标记关键点
    - setup_axes / save    : 统一外观与保存

用法(在某章的 gen 脚本里):
    import sys, os
    HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, HERE)
    from _plot_utils import plot_fn, riemann_rects, tangent, spectrum, band, marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
    import numpy as np, matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

注意: 图内标注一律用英文(避免中文字体乱码), 正文用中文。
"""
import os
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# ---------- 全书统一配色(与系列其他书一致) ----------
GREEN  = '#2ca02c'   # 主曲线 / 理论值 / 收敛目标
RED    = '#d62728'   # 切线 / 逼近误差 / 重点标记
BLUE   = '#1f77b4'   # 被逼近的原函数 / 时域信号
PURPLE = '#9467bd'   # 频谱 / 高维 / 复域
ORANGE = '#ff7f0e'   # 矩形和 / 逼近序列 / 辅助
GRAY   = 'gray'


# ---------- 基础绘图 ----------

def plot_curve(ax, x, y, color=BLUE, lw=2.2, label=None, ls='-'):
    """给定 x, y 数组画一条曲线。"""
    ax.plot(x, y, color=color, lw=lw, label=label, ls=ls, zorder=4)


def plot_fn(ax, f, a, b, n=400, color=BLUE, lw=2.2, label=None, ls='-'):
    """在区间 [a, b] 上画 y = f(x)。f 是 callable(会自动 vectorize, 兼容 sympy.lambdify)。"""
    x = np.linspace(a, b, n)
    fv = np.vectorize(f)
    y = fv(x)
    ax.plot(x, y, color=color, lw=lw, label=label, ls=ls, zorder=4)
    return x, y


def riemann_rects(ax, f, a, b, n, pos='mid', color=ORANGE, alpha=0.45, edge=ORANGE):
    """画 n 个等宽黎曼矩形。pos 决定取样点: 'left' / 'right' / 'mid'。返回矩形面积之和(数值积分的近似)。"""
    xs = np.linspace(a, b, n + 1)
    w = xs[1] - xs[0]
    fv = np.vectorize(f)
    total = 0.0
    for i in range(n):
        if pos == 'left':
            sx = xs[i]
        elif pos == 'right':
            sx = xs[i + 1]
        else:
            sx = 0.5 * (xs[i] + xs[i + 1])
        h = float(fv(sx))
        total += h * w
        ax.add_patch(Rectangle((xs[i], 0.0), w, h, facecolor=color, alpha=alpha,
                               edgecolor=edge, lw=0.5, zorder=2))
    return total


def tangent(ax, x0, slope, y0, span=1.0, color=RED, lw=1.8, ls='--', label=None):
    """过点 (x0, y0)、斜率为 slope 画一条切线(点向左右各延展 span)。"""
    x = np.array([x0 - span, x0 + span])
    y = y0 + slope * (x - x0)
    ax.plot(x, y, color=color, lw=lw, ls=ls, label=label, zorder=4)


def spectrum(ax, freqs, mags, color=PURPLE):
    """画频谱柱状图(freqs: 频率数组, mags: 幅值数组)。傅里叶用。"""
    ax.stem(freqs, mags, linefmt=color, markerfmt=' ', basefmt=' ', zorder=3)
    ax.set_xlabel('frequency', fontsize=11)
    ax.set_ylabel('magnitude', fontsize=11)


def band(ax, axis, c, delta, color=ORANGE, alpha=0.15):
    """在坐标轴上画一个宽度 2*delta、中心 c 的区间带(ε-δ / 收敛区间用)。
    axis='x' 画竖直带(axvspan); axis='y' 画水平带(axhspan)。"""
    if axis == 'x':
        ax.axvspan(c - delta, c + delta, color=color, alpha=alpha, zorder=1)
    else:
        ax.axhspan(c - delta, c + delta, color=color, alpha=alpha, zorder=1)


def marker(ax, x, y, color=RED, text=None, dx=0.06, dy=0.06, fs=11):
    """标记一个点, 并可选地在旁边加文字标注。"""
    ax.scatter([x], [y], color=color, s=32, zorder=6)
    if text:
        ax.annotate(text, (x, y), xytext=(x + dx, y + dy), color=color, fontsize=fs)


# ---------- 外观与保存 ----------

def setup_axes(ax, xlim=None, ylim=None, title=None, equal=False,
               xlabel='x', ylabel='y', grid=True):
    """统一坐标系外观: 十字坐标轴、可选范围/标题/等比例/网格。"""
    ax.axhline(0, color='gray', lw=0.6, alpha=0.4, zorder=0)
    ax.axvline(0, color='gray', lw=0.6, alpha=0.4, zorder=0)
    if xlim is not None:
        ax.set_xlim(*xlim)
    if ylim is not None:
        ax.set_ylim(*ylim)
    if equal:
        ax.set_aspect('equal')
    if xlabel is not None:
        ax.set_xlabel(xlabel, fontsize=11)
    if ylabel is not None:
        ax.set_ylabel(ylabel, fontsize=11)
    if title:
        ax.set_title(title, fontsize=12)
    if grid:
        ax.grid(True, alpha=0.15)


def save(fig, name):
    """把图保存到本文件所在目录(figures/)。"""
    fig.tight_layout()
    fig.savefig(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), dpi=150)
    plt.close(fig)
