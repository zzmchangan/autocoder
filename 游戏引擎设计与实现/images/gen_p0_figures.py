# -*- coding: utf-8 -*-
"""为《游戏引擎》P0-01 生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_p0_figures.py
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch

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
GREY  = (0.78, 0.78, 0.80)
DGREY = (0.62, 0.62, 0.66)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=12, weight="normal", tc=INK):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=fc, edgecolor=ec, linewidth=1.4))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.6, style="-|>", rad=0.0):
    cs = f"arc3,rad={rad}" if rad else "arc3,rad=0"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle=style, color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=16))


# ---------------------------------------------------------------- fig-02
def fig_oop_inheritance():
    fig, ax = plt.subplots(figsize=(10, 6))
    box(ax, 4.0, 9.0, 2.4, 0.9, "Enemy\n(base)", fc=(1.0, 0.92, 0.92), weight="bold")
    box(ax, 1.2, 7.0, 2.6, 0.9, "FlyingEnemy", fc=(0.92, 0.95, 1.0))
    box(ax, 6.6, 7.0, 2.6, 0.9, "SwimmingEnemy", fc=(0.92, 0.95, 1.0))
    box(ax, 3.6, 4.8, 3.2, 0.9, "FlyingSwimmingEnemy", fc=(0.95, 0.90, 1.0), weight="bold")
    box(ax, 3.4, 2.6, 3.6, 0.9, "InvisibleFlyingSwimEnemy", fc=(0.90, 0.95, 0.92))
    # 继承箭头(空心三角近似)
    arrow(ax, (4.8, 9.0), (2.5, 7.9), color=INK)
    arrow(ax, (5.6, 9.0), (7.9, 7.9), color=INK)
    arrow(ax, (2.5, 7.0), (4.4, 5.7), color=INK)
    arrow(ax, (7.9, 7.0), (6.0, 5.7), color=INK)
    arrow(ax, (5.2, 4.8), (5.2, 3.5), color=INK)
    # 标注
    ax.text(5.2, 10.2, "inheritance tree grows deep", ha="center", fontsize=12.5,
            color=RED, weight="bold")
    ax.annotate("diamond inheritance:\nEnemy reached via 2 paths",
                xy=(4.6, 6.6), xytext=(0.2, 5.6), fontsize=11, color=RED,
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.3))
    ax.annotate("add a new behavior\n=> add another level",
                xy=(5.2, 3.0), xytext=(7.4, 1.4), fontsize=11, color=RED,
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.3))
    ax.text(5.2, 0.5, "OOP organizes game objects by inheritance => tree explodes",
            ha="center", fontsize=12.5, color=INK, weight="bold")
    ax.set_xlim(0, 10.4); ax.set_ylim(0, 11)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-02-oop-inheritance.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-03
def fig_ecs_trinity():
    fig, ax = plt.subplots(figsize=(11, 5.6))
    # Entity 列
    ax.text(1.5, 4.7, "Entity", ha="center", fontsize=14, weight="bold", color=INK)
    ax.text(1.5, 4.35, "= just an ID", ha="center", fontsize=11, color=DGREY)
    for i in range(4):
        box(ax, 0.8, 3.4 - i * 0.75, 1.4, 0.6, f"Entity {i}", fc=(1.0, 0.94, 0.94))
    # Component 列(按类型连续存)
    ax.text(5.4, 4.7, "Component (pure data, stored by type)", ha="center",
            fontsize=14, weight="bold", color=INK)
    box(ax, 3.2, 3.7, 4.4, 0.6, "Position[]   {x,y} of each entity", fc=(0.90, 0.94, 1.0))
    box(ax, 3.2, 3.0, 4.4, 0.6, "Velocity[]   {vx,vy} of each entity", fc=(0.90, 0.94, 1.0))
    box(ax, 3.2, 2.3, 4.4, 0.6, "Color[]      {r,g,b} of each entity", fc=(0.90, 0.94, 1.0))
    box(ax, 3.2, 1.6, 4.4, 0.6, "Radius[]     of each entity", fc=(0.90, 0.94, 1.0))
    # System 列
    ax.text(9.3, 4.7, "System (pure behavior)", ha="center", fontsize=14,
            weight="bold", color=INK)
    box(ax, 8.2, 3.5, 2.2, 0.7, "Movement\nSystem", fc=(0.92, 0.95, 0.90), weight="bold")
    box(ax, 8.2, 2.2, 2.2, 0.7, "Render\nSystem", fc=(0.92, 0.95, 0.90), weight="bold")
    # 箭头: System 查询 Component
    arrow(ax, (8.2, 3.85), (7.6, 3.6), color=GREEN, lw=1.8)
    arrow(ax, (8.2, 3.6), (7.6, 3.3), color=GREEN, lw=1.8)
    arrow(ax, (8.2, 2.4), (7.6, 2.6), color=GREEN, lw=1.8)
    ax.text(8.0, 4.15, "queries", fontsize=10.5, color=GREEN, ha="center")
    # 箭头: Component 属于 Entity (索引对齐)
    arrow(ax, (2.2, 3.55), (3.2, 3.95), color=BLUE, lw=1.4, rad=0.15)
    ax.text(2.4, 4.15, "indexed\nby ID", fontsize=10.5, color=BLUE, ha="center")
    ax.text(5.4, 0.6, "data + behavior are SPLIT; what an entity 'is' = which components it has",
            ha="center", fontsize=12, color=INK, weight="bold")
    ax.set_xlim(0, 11); ax.set_ylim(0, 5.2)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-03-ecs-trinity.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-04
def mem_cells(ax, ox, oy, cells, cw, ch):
    """cells: list of (text, color)"""
    x = ox
    for txt, col in cells:
        ax.add_patch(Rectangle((x, oy), cw, ch, facecolor=col, edgecolor="white", linewidth=1.2))
        ax.text(x + cw / 2, oy + ch / 2, txt, ha="center", va="center",
                fontsize=9.5, color="white" if col != GREY and col != DGREY else INK)
        x += cw


def fig_soa_vs_aos():
    fig, ax = plt.subplots(figsize=(11.5, 6.4))
    cw, ch = 0.95, 0.7
    P, V, C, R = BLUE, GREEN, GREY, DGREY  # pos / vel / color / radius
    # ---- AoS (上)
    ax.text(0.2, 8.6, "AoS  (Array of Structures)  -- OOP: each object is one block",
            fontsize=13, weight="bold", color=RED)
    for i in range(4):
        oy = 7.5 - i * 0.95
        ax.text(0.2, oy + 0.3, f"Ball{i}", fontsize=10, va="center")
        mem_cells(ax, 1.8, oy, [("pos", P), ("vel", V), ("color", C), ("r", R)], cw, ch)
    ax.annotate("scanning pos pulls in\nuseless color & radius",
                xy=(3.0, 7.4), xytext=(7.0, 7.7), fontsize=11, color=RED,
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.3))

    # ---- SoA (下)
    ax.text(0.2, 3.9, "SoA  (Structure of Arrays)  -- data-oriented: same field contiguous",
            fontsize=13, weight="bold", color=GREEN)
    rows = [("Position[]", P, "pos"),
            ("Velocity[]", V, "vel"),
            ("Color[]", C, "color"),
            ("Radius[]", R, "r")]
    for i, (label, col, txt) in enumerate(rows):
        oy = 3.0 - i * 0.8
        ax.text(0.2, oy + 0.35, label, fontsize=10.5, va="center", weight="bold")
        mem_cells(ax, 1.8, oy, [(txt, col)] * 4, cw, ch)
    ax.annotate("MovementSystem reads only\nPosition[] + Velocity[]: no waste",
                xy=(2.5, 2.9), xytext=(6.6, 3.2), fontsize=11, color=GREEN,
                arrowprops=dict(arrowstyle="->", color=GREEN, lw=1.3))

    ax.set_xlim(0, 12); ax.set_ylim(0, 9.2)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-04-soa-vs-aos.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-05
def fig_cache_traversal():
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.2))

    # 左: SoA 连续
    ax = axes[0]
    ax.set_title("SoA: contiguous memory", fontsize=13, weight="bold", color=GREEN)
    # 连续 pos 方块
    for i in range(10):
        ax.add_patch(Rectangle((0.3 + i * 0.95, 3.0), 0.9, 0.9, facecolor=BLUE,
                               edgecolor="white", linewidth=1.2))
        ax.text(0.75 + i * 0.95, 3.45, f"p{i}", ha="center", va="center",
                fontsize=9, color="white")
    # 缓存行框(圈 4 个)
    ax.add_patch(Rectangle((0.25, 2.95), 0.95 * 4 + 0.1, 1.0, fill=False,
                           edgecolor=AMBER, linewidth=2.2, linestyle="--"))
    ax.text(0.25 + (0.95 * 4) / 2, 4.2, "one cache line (64 B)", ha="center",
            fontsize=10.5, color=AMBER, weight="bold")
    ax.add_patch(Rectangle((0.25 + 0.95 * 4, 2.95), 0.95 * 4 + 0.1, 1.0, fill=False,
                           edgecolor=AMBER, linewidth=2.2, linestyle="--"))
    # 顺序扫描箭头
    arrow(ax, (0.5, 2.4), (9.2, 2.4), color=INK, lw=2.0)
    ax.text(4.8, 1.9, "sequential scan => cache hit + HW prefetch",
            ha="center", fontsize=11, color=GREEN, weight="bold")
    ax.text(4.8, 0.9, "fast: data the CPU needs is already in L1",
            ha="center", fontsize=11, color=INK)
    ax.set_xlim(0, 10); ax.set_ylim(0.4, 4.8)
    ax.set_aspect("equal"); clean_ax(ax)

    # 右: AoS 散落 (指针追逐)
    ax = axes[1]
    ax.set_title("AoS: scattered on heap (pointer chasing)", fontsize=13,
                 weight="bold", color=RED)
    # 散落的位置
    pts = [(0.6, 3.2), (3.2, 3.8), (1.8, 1.8), (5.0, 3.0), (4.0, 1.4),
           (7.0, 3.6), (6.2, 1.6), (8.6, 2.8), (2.6, 3.0), (8.0, 1.6)]
    for i, (px, py) in enumerate(pts):
        ax.add_patch(Rectangle((px, py), 0.7, 0.7, facecolor=BLUE,
                               edgecolor="white", linewidth=1.2))
        ax.text(px + 0.35, py + 0.35, f"p{i}", ha="center", va="center",
                fontsize=8.5, color="white")
    # 跳跃箭头
    for i in range(len(pts) - 1):
        a = (pts[i][0] + 0.6, pts[i][1] + 0.35)
        b = (pts[i + 1][0] + 0.1, pts[i + 1][1] + 0.35)
        arrow(ax, a, b, color=RED, lw=1.3, rad=0.12)
    ax.text(4.8, 0.7, "each jump may miss cache; prefetcher can't predict",
            ha="center", fontsize=11, color=RED, weight="bold")
    ax.text(4.8, 0.2, "slow: stall waiting for memory",
            ha="center", fontsize=11, color=INK)
    ax.set_xlim(0, 9.6); ax.set_ylim(0, 4.8)
    ax.set_aspect("equal"); clean_ax(ax)

    fig.savefig(os.path.join(OUT, "fig-05-cache-traversal.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_oop_inheritance()
    fig_ecs_trinity()
    fig_soa_vs_aos()
    fig_cache_traversal()
    print("done:", sorted(f for f in os.listdir(OUT) if f.endswith(".png")))
