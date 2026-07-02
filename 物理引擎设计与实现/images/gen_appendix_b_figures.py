# -*- coding: utf-8 -*-
"""为《物理引擎》附录 B 生成一组 PNG 示意图。运行: python gen_appendix_b_figures.py
图内严禁中文(避 matplotlib 字体方块), 全部英文标注。
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch, FancyArrowPatch, Circle, Polygon as MplPolygon

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 12.5,
    "axes.linewidth": 1.2,
    "figure.dpi": 150,
    "savefig.dpi": 150,
    "savefig.bbox": "tight",
    "savefig.facecolor": "white",
    "savefig.pad_inches": 0.15,
})

OUT = os.path.dirname(os.path.abspath(__file__))

RED   = (0.86, 0.18, 0.18)
GREEN = (0.20, 0.70, 0.32)
BLUE  = (0.20, 0.45, 0.88)
AMBER = (0.95, 0.70, 0.18)
GREY  = (0.62, 0.62, 0.66)
DARK  = (0.38, 0.40, 0.46)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)
GROUND_FC = (0.50, 0.42, 0.36)   # 地面棕色
GROUND_EC = (0.30, 0.24, 0.20)
BOX_FC    = (0.30, 0.58, 0.86)   # 箱子蓝
BOX_EC    = (0.16, 0.34, 0.58)


def clean_ax(ax, xmin, xmax, ymin, ymax):
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect("equal")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def draw_ground(ax, x0, half_w, y_top, thick=0.9, label="static ground (y=0)"):
    # 地面: 顶面在 y_top
    ax.add_patch(Rectangle((x0 - half_w, y_top - thick), 2 * half_w, thick,
                           facecolor=GROUND_FC, edgecolor=GROUND_EC, linewidth=1.4))
    # 阴影线表示静态
    n = int(2 * half_w / 0.7)
    for i in range(n):
        xs = x0 - half_w + i * 0.7 + 0.1
        ax.plot([xs, xs - 0.35], [y_top, y_top - 0.35], color=GROUND_EC, lw=0.8)
    ax.text(x0, y_top - thick - 0.55, label, ha="center", va="top",
            fontsize=9.5, color=DARK)


def draw_box(ax, cx, cy, half, ec=BOX_EC, fc=BOX_FC, label=None, fs=8):
    ax.add_patch(Rectangle((cx - half, cy - half), 2 * half, 2 * half,
                           facecolor=fc, edgecolor=ec, linewidth=1.3))
    if label:
        ax.text(cx, cy, label, ha="center", va="center", fontsize=fs, color="white", weight="bold")


# ---------------------------------------------------------------- fig 01
def fig_demo_snapshot():
    """demo 两个时刻: 左 t=0 八个箱子在高处, 右 跑稳后堆叠."""
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 6.4))

    # ---- 左: t=0 初始 ----
    # y 范围开到 19, 容下全部 8 个箱子 (代码 y = 4 + 2*i, i=0..7 => 最高 18)
    ax = axes[0]
    clean_ax(ax, -9, 11, -2.5, 19.5)
    draw_ground(ax, 0, 8.0, 0.0, thick=1.0, label="static ground")
    # 八个箱子在高处, 错开 x 和 y (和代码里的位置一致: x=-2+0.7i, y=4+2i)
    init_pos = [(-2.0 + 0.7 * i, 4.0 + 2.0 * i) for i in range(8)]
    for i, (x, y) in enumerate(init_pos):
        draw_box(ax, x, y, 0.5, label=f"#{i}")
    # 重力箭头 (放在右侧空白区)
    ax.add_artist(FancyArrowPatch((9.0, 16.0), (9.0, 12.0), arrowstyle="-|>",
                                  color=RED, lw=2.0, mutation_scale=18))
    ax.text(9.5, 14.0, "gravity\n(0,-10) m/s^2", ha="left", va="center",
            fontsize=10, color=RED)
    ax.text(-8.5, 18.7, "t = 0  (initial)", ha="left", fontsize=13,
            weight="bold", color=INK)
    ax.text(-8.5, 17.8, "8 dynamic boxes released from height",
            ha="left", fontsize=10.5, color=DARK)

    # ---- 右: 跑稳后堆叠 ----
    ax = axes[1]
    clean_ax(ax, -9, 11, -2.5, 19.5)
    draw_ground(ax, 0, 8.0, 0.0, thick=1.0, label="static ground")
    # 简化的稳态: 三摞堆叠 (模拟落定后的样子, 不是精确物理结果)
    stacks = [
        (-3.0, [0.5, 1.5, 2.5]),     # 三层
        (-1.0, [0.5, 1.5]),          # 两层
        (1.0,  [0.5, 1.5, 2.5, 3.5]),# 四层
        (3.5,  [0.5]),               # 一个
    ]
    idx = 0
    for cx, ys in stacks:
        for y in ys:
            draw_box(ax, cx, y, 0.5, label=f"#{idx % 8}")
            idx += 1
    # 标注: 不穿透
    ax.annotate("no tunneling:\nboxes rest on ground\nand on each other",
                xy=(1.0, 0.5), xytext=(5.5, 10.0), fontsize=9.5, color=GREEN,
                ha="left",
                arrowprops=dict(arrowstyle="->", color=GREEN, lw=1.4))
    ax.text(-8.5, 18.7, "t = steady  (after ~600 steps)", ha="left",
            fontsize=13, weight="bold", color=INK)
    ax.text(-8.5, 17.8, "boxes stacked, no overlap, no tunneling",
            ha="left", fontsize=10.5, color=DARK)

    fig.suptitle("Box2D v3 stacking demo: integrate -> detect -> solve",
                 fontsize=14, weight="bold", y=0.99)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(os.path.join(OUT, "fig-appendix_b-01-demo-snapshot.png"))
    plt.close(fig)
    print("wrote fig-appendix_b-01-demo-snapshot.png")


# ---------------------------------------------------------------- fig 02
def fig_three_knobs():
    """三个旋钮效果对比: subStepCount / restitution / enableContinuous."""
    fig, axes = plt.subplots(1, 3, figsize=(15.5, 6.2))

    # ---- 左: subStepCount 1 vs 8 ----
    ax = axes[0]
    clean_ax(ax, -4, 8, -1.5, 7)
    draw_ground(ax, -1.5, 2.5, 0.0, thick=0.6, label="")

    # 左半: subStepCount=1, 抖动 (箱子画成略倾斜 + 抖动轨迹)
    ax.text(-2.8, 6.4, "subStepCount = 1", ha="left", fontsize=11.5,
            weight="bold", color=AMBER)
    ax.text(-2.8, 5.8, "(jitter, unstable stack)", ha="left", fontsize=9.5, color=DARK)
    # 三个箱子, 略歪斜
    draw_box(ax, -2.0, 0.5, 0.5, fc=(0.95, 0.75, 0.30), ec=AMBER)
    draw_box(ax, -1.9, 1.55, 0.5, fc=(0.95, 0.75, 0.30), ec=AMBER)
    draw_box(ax, -2.1, 2.6, 0.5, fc=(0.95, 0.75, 0.30), ec=AMBER)
    # 抖动小箭头
    for yy in [0.5, 1.55, 2.6]:
        ax.add_artist(FancyArrowPatch((-1.2, yy), (-0.9, yy + 0.08), arrowstyle="<->",
                                      color=AMBER, lw=1.2, mutation_scale=10))
    ax.text(-0.7, 1.55, "jitter", ha="left", va="center", fontsize=9, color=AMBER)

    # 右半: subStepCount=8, 稳
    ax.text(1.5, 6.4, "subStepCount = 8", ha="left", fontsize=11.5,
            weight="bold", color=GREEN)
    ax.text(1.5, 5.8, "(stable, well-converged)", ha="left", fontsize=9.5, color=DARK)
    for i, yy in enumerate([0.5, 1.5, 2.5, 3.5]):
        draw_box(ax, 3.0, yy, 0.5, fc=(0.30, 0.70, 0.42), ec=(0.18, 0.45, 0.28))
    ax.text(4.0, 2.0, "solid\nstack", ha="left", va="center", fontsize=9.5, color=GREEN)

    ax.set_title("knob 1: subStepCount\n(more sub-steps => better PGS convergence)",
                 fontsize=10.5, color=INK)

    # ---- 中: restitution 0 vs 0.7 ----
    ax = axes[1]
    clean_ax(ax, -4, 8, -1.5, 7)
    draw_ground(ax, -1.5, 2.5, 0.0, thick=0.6, label="")

    # 左: e=0 贴住
    ax.text(-2.8, 6.4, "restitution = 0.0", ha="left", fontsize=11.5,
            weight="bold", color=BLUE)
    ax.text(-2.8, 5.8, "(no bounce, sticks)", ha="left", fontsize=9.5, color=DARK)
    draw_box(ax, -1.5, 0.5, 0.5, fc=(0.55, 0.72, 0.95), ec=BLUE)
    ax.text(-1.5, -0.9, "v_down -> 0", ha="center", fontsize=8.5, color=BLUE)

    # 右: e=0.7 弹起
    ax.text(1.5, 6.4, "restitution = 0.7", ha="left", fontsize=11.5,
            weight="bold", color=RED)
    ax.text(1.5, 5.8, "(bounces back up)", ha="left", fontsize=9.5, color=DARK)
    # 箱子在空中 (弹起)
    draw_box(ax, 3.0, 3.0, 0.5, fc=(0.92, 0.55, 0.55), ec=RED)
    # 下落 + 弹起 轨迹
    ax.add_artist(FancyArrowPatch((3.0, 5.5), (3.0, 3.6), arrowstyle="-|>",
                                  color=GREY, lw=1.3, mutation_scale=12,
                                  connectionstyle="arc3,rad=0"))
    ax.text(3.3, 4.8, "fall", fontsize=8.5, color=GREY)
    ax.add_artist(FancyArrowPatch((3.0, 0.5), (3.0, 3.0), arrowstyle="-|>",
                                  color=RED, lw=1.6, mutation_scale=14,
                                  connectionstyle="arc3,rad=0"))
    ax.text(3.3, 1.5, "bounce\nv_up = 0.7 * v_down", fontsize=8.5, color=RED)
    # 地面接触点
    ax.plot(3.0, 0.0, "o", color=RED, markersize=5)

    ax.set_title("knob 2: restitution\n(impulse keeps fraction of normal speed)",
                 fontsize=10.5, color=INK)

    # ---- 右: enableContinuous on vs off ----
    ax = axes[2]
    clean_ax(ax, -4, 8, -5, 7)
    draw_ground(ax, -1.5, 2.5, 0.0, thick=0.6, label="")

    # 左: CCD on, 停住
    ax.text(-2.8, 6.4, "CCD on (default)", ha="left", fontsize=11.5,
            weight="bold", color=GREEN)
    ax.text(-2.8, 5.8, "(fast box stops)", ha="left", fontsize=9.5, color=DARK)
    # 高速箱子停在地面
    draw_box(ax, -1.5, 0.5, 0.5, fc=(0.30, 0.70, 0.42), ec=(0.18, 0.45, 0.28))
    # 下落虚线
    ax.plot([-1.5, -1.5], [5.5, 1.0], "--", color=GREY, lw=1.0)
    ax.add_artist(FancyArrowPatch((-1.5, 5.5), (-1.5, 3.0), arrowstyle="-|>",
                                  color=GREY, lw=1.3, mutation_scale=12))
    ax.text(-1.1, 4.2, "fast", fontsize=8.5, color=GREY)
    ax.plot(-1.5, 0.0, "o", color=GREEN, markersize=6)
    ax.text(-1.0, -0.8, "caught", fontsize=8.5, color=GREEN)

    # 右: CCD off, 穿透
    ax.text(1.5, 6.4, "CCD off", ha="left", fontsize=11.5,
            weight="bold", color=RED)
    ax.text(1.5, 5.8, "(tunneling through ground!)", ha="left", fontsize=9.5, color=DARK)
    # 箱子穿透到地下
    draw_box(ax, 3.0, -3.0, 0.5, fc=(0.92, 0.45, 0.45), ec=RED)
    # 穿透轨迹 (穿过地面)
    ax.plot([3.0, 3.0], [5.5, -3.5], "--", color=RED, lw=1.2)
    ax.add_artist(FancyArrowPatch((3.0, 5.5), (3.0, -3.0), arrowstyle="-|>",
                                  color=RED, lw=1.6, mutation_scale=14))
    # 地面被穿透的标注
    ax.annotate("tunnel!", xy=(3.0, 0.0), xytext=(4.5, 2.5), fontsize=10,
                color=RED, weight="bold",
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.4))
    ax.text(3.0, -4.2, "fell through", ha="center", fontsize=8.5, color=RED)

    ax.set_title("knob 3: enableContinuous\n(discrete step + fast body = tunnel)",
                 fontsize=10.5, color=INK)

    fig.suptitle("Three knobs to turn, three chapters to revisit",
                 fontsize=14, weight="bold", y=0.99)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(os.path.join(OUT, "fig-appendix_b-02-three-knobs.png"))
    plt.close(fig)
    print("wrote fig-appendix_b-02-three-knobs.png")


if __name__ == "__main__":
    fig_demo_snapshot()
    fig_three_knobs()
    print("done.")
