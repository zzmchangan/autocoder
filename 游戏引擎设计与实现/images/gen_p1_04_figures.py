# -*- coding: utf-8 -*-
"""为《游戏引擎》P1-04 (组织游戏对象的困境: 面向对象为什么崩溃) 生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_p1_04_figures.py
"""
import os
import math
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch, Polygon

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
PURP  = (0.55, 0.36, 0.74)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=12, weight="normal", tc=INK):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=fc, edgecolor=ec, linewidth=1.4))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.6, style="-|>", rad=0.0, ms=14):
    cs = f"arc3,rad={rad}" if rad else "arc3,rad=0"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle=style, color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=ms))


# ============================================================ fig-p1_04_01
# Deep + diamond inheritance: Enemy tree grows as requirements pile up,
# showing the diamond (Enemy reached via two paths) and a failed deeper combo.
def fig_inheritance_tree():
    fig, ax = plt.subplots(figsize=(12.5, 8.6))

    # stage labels on the right
    def stage(y, txt, col=DGREY):
        ax.text(12.4, y, txt, ha="left", va="center", fontsize=10.5, color=col,
                style="italic")

    # ---- root Enemy
    box(ax, 5.4, 12.0, 2.4, 0.95, "Enemy\n(base)", fc=(1.0, 0.92, 0.92), weight="bold", fs=12)
    ax.text(6.6, 13.2, "v1: a simple enemy", ha="center", fontsize=11.5,
            color=GREEN, weight="bold")

    # ---- need flight => FlyingEnemy
    box(ax, 2.4, 10.0, 2.7, 0.9, "FlyingEnemy", fc=(0.92, 0.95, 1.0), fs=11.5)
    arrow(ax, (6.0, 12.0), (3.75, 10.9), color=INK, rad=-0.1)
    ax.text(3.75, 11.5, "need: can fly", ha="center", fontsize=10.5, color=BLUE)

    # ---- need swimming => SwimmingEnemy
    box(ax, 8.1, 10.0, 2.7, 0.9, "SwimmingEnemy", fc=(0.92, 0.95, 1.0), fs=11.5)
    arrow(ax, (7.2, 12.0), (9.45, 10.9), color=INK, rad=0.1)
    ax.text(9.45, 11.5, "need: can swim", ha="center", fontsize=10.5, color=BLUE)

    # ---- need fly + swim => FlyingSwimmingEnemy (diamond!)
    box(ax, 5.0, 7.6, 3.4, 0.95, "FlyingSwimmingEnemy", fc=(0.95, 0.88, 1.0), weight="bold", fs=11.5)
    # two parents => diamond
    arrow(ax, (3.75, 10.0), (5.7, 8.55), color=INK, rad=0.08)
    arrow(ax, (9.45, 10.0), (7.7, 8.55), color=INK, rad=-0.08)
    # mark the diamond with two red arrows from Enemy via both branches
    ax.annotate("", xy=(6.0, 12.0), xytext=(3.0, 10.9),
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.0, alpha=0.0))
    # diamond highlight: translucent red polygon connecting Enemy -> both -> FSE
    diamond = Polygon([(6.6, 12.0), (3.75, 10.0), (6.7, 7.6), (9.45, 10.0)],
                      closed=True, facecolor=RED, alpha=0.10, edgecolor=RED,
                      linewidth=1.8, linestyle="--")
    ax.add_patch(diamond)
    ax.text(6.7, 9.7, "DIAMOND\nEnemy data\nreached 2x",
            ha="center", va="center", fontsize=11, color=RED, weight="bold")

    # ---- need invisible + fly + swim => another level
    box(ax, 4.7, 5.2, 4.0, 0.9, "InvisibleFlyingSwimEnemy", fc=(0.90, 0.95, 0.92), fs=11)
    arrow(ax, (6.7, 7.6), (6.7, 6.1), color=INK)
    ax.text(8.9, 5.65, "need: invisible\n=> yet another level", ha="left",
            fontsize=10.5, color=AMBER)

    # ---- need spell-casting combo => explosion of leaves
    box(ax, 1.0, 2.8, 2.9, 0.8, "SpellFlyingSwim", fc=(0.94, 0.94, 1.0), fs=10)
    box(ax, 4.3, 2.8, 2.9, 0.8, "SpellInvisFlying", fc=(0.94, 0.94, 1.0), fs=10)
    box(ax, 7.6, 2.8, 3.2, 0.8, "InvisSpellSwim", fc=(0.94, 0.94, 1.0), fs=10)
    arrow(ax, (6.0, 5.2), (2.45, 3.6), color=INK, rad=-0.1, lw=1.2)
    arrow(ax, (6.7, 5.2), (5.75, 3.6), color=INK, lw=1.2)
    arrow(ax, (7.4, 5.2), (9.0, 3.6), color=INK, rad=0.1, lw=1.2)

    # ---- N behaviors => 2^N leaves (combinatorial explosion banner)
    box(ax, 2.2, 0.6, 8.2, 1.4,
        "N behaviors (fly / swim / spell / invis / drop / heal ...)\n"
        "=> up to 2^N subclasses, most empty / one-off",
        fc=(1.0, 0.93, 0.93), ec=RED, weight="bold", fs=11.5)
    ax.text(6.6, 0.05, "OOP by inheritance: tree explodes, every new combo forks the tree",
            ha="center", fontsize=11.5, color=INK, weight="bold")

    ax.set_xlim(0, 13.6); ax.set_ylim(-0.4, 13.7)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p1_04_01-inheritance-tree.png"))
    plt.close(fig)


# ============================================================ fig-p1_04_02
# Memory layout: OOP objects scattered on the heap (pointer chasing) vs
# data-oriented contiguous arrays (SoA). Highlight the access pattern of
# MovementSystem (only pos + vel).
def fig_memory_layout():
    fig, ax = plt.subplots(figsize=(13.2, 8.0))
    cw, ch = 1.25, 0.78   # wider cells so 'color' fits without touching neighbours

    def cells(ax, ox, oy, lst):
        x = ox
        for txt, col in lst:
            ax.add_patch(Rectangle((x, oy), cw, ch, facecolor=col,
                                   edgecolor="white", linewidth=2.2))
            tc = "white" if col not in (GREY, DGREY) else INK
            ax.text(x + cw / 2, oy + ch / 2, txt, ha="center", va="center",
                    fontsize=11, color=tc, weight="bold")
            x += cw

    # use single-letter glyphs so no adjacent label can be misread as merged
    P, V, C, R = BLUE, GREEN, GREY, DGREY
    SCATTERED = (0.97, 0.88, 0.88)  # pale red bg

    # ---- top: OOP objects scattered on heap
    ax.text(0.2, 7.6, "OOP: each object is one block, allocated separately on the heap",
            fontsize=13.5, weight="bold", color=RED)
    # 5 objects at uneven addresses with clearly visible gaps (varying spacing)
    # bigger gaps between later objects to emphasise scatter
    xs = [0.5, 3.4, 5.6, 8.7, 10.8]
    labels = ["0x1008", "0x43A0", "0x72C0", "0x9110", "0xB740"]
    for i, (ox, lab) in enumerate(zip(xs, labels)):
        oy = 6.0
        # strong pale-red rounded background so each object reads as ONE block
        ax.add_patch(FancyBboxPatch((ox - 0.18, oy - 0.18), 4 * cw + 0.36, ch + 0.36,
                                    boxstyle="round,pad=0.02,rounding_size=0.08",
                                    facecolor=SCATTERED, edgecolor=RED,
                                    linewidth=1.6))
        cells(ax, ox, oy, [("P", P), ("V", V), ("C", C), ("R", R)])
        ax.text(ox + 2 * cw, oy + ch + 0.45, f"Enemy{i}", fontsize=10,
                color=INK, weight="bold", ha="center")
        ax.text(ox + 2 * cw, oy - 0.42, f"addr {lab}", fontsize=8.8, color=DGREY,
                ha="center")
    # pointer arrows between objects (curve up over the gaps) => pointer chasing
    for i in range(len(xs) - 1):
        a = (xs[i] + 4 * cw + 0.16, 6.0 + ch / 2)
        b = (xs[i + 1] - 0.16, 6.0 + ch / 2)
        arrow(ax, a, b, color=RED, lw=1.6, style="-|>", rad=-0.28, ms=12)
    ax.text(6.9, 4.7, "iterating = following pointers; each hop may stall on a cache miss",
            ha="center", fontsize=11.5, color=RED, weight="bold")

    # access mask: MovementSystem wants pos+vel, color/r are dead weight
    ax.annotate("", xy=(xs[0], 6.0 + ch + 0.0), xytext=(xs[0] + 2 * cw, 6.0 + ch + 0.0),
                arrowprops=dict(arrowstyle="<->", color=GREEN, lw=2.2))
    ax.text(xs[0] + cw, 7.45, "MovementSystem\nneeds only pos+vel",
            ha="center", fontsize=10, color=GREEN, weight="bold")
    ax.annotate("color & radius dragged\ninto the cache line for nothing",
                xy=(xs[0] + 3 * cw + 0.2, 6.0 + ch / 2), xytext=(2.4, 8.6),
                fontsize=10.2, color=AMBER,
                arrowprops=dict(arrowstyle="->", color=AMBER, lw=1.3))

    # ---- bottom: data-oriented contiguous arrays (SoA)
    ax.text(0.2, 4.0, "Data-oriented: same field of all entities stored contiguously (SoA)",
            fontsize=13.5, weight="bold", color=GREEN)
    rows = [("Position[]", P, "P"),
            ("Velocity[]", V, "V"),
            ("Color[]",    C, "C"),
            ("Radius[]",   R, "R")]
    for i, (label, col, txt) in enumerate(rows):
        oy = 3.1 - i * 0.78
        ax.text(0.2, oy + ch / 2, label, fontsize=10.5, va="center",
                weight="bold", color=INK)
        cells(ax, 1.8, oy, [(txt, col)] * 5)
    # cache line highlight on Position
    ax.add_patch(Rectangle((1.75, 3.05), 5 * cw + 0.1, ch + 0.1, fill=False,
                           edgecolor=AMBER, linewidth=2.0, linestyle="--"))
    ax.text(1.75 + (5 * cw) / 2, 3.1 + ch + 0.3,
            "one cache line brings many positions at once",
            ha="center", fontsize=10.5, color=AMBER, weight="bold")

    ax.text(7.0, 0.05,
            "MovementSystem streams Position[] + Velocity[] only => no waste, sequential reads",
            ha="center", fontsize=11, color=GREEN, weight="bold")

    ax.set_xlim(0, 14.8); ax.set_ylim(-0.6, 9.4)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p1_04_02-memory-layout.png"))
    plt.close(fig)


# ============================================================ fig-p1_04_03
# Performance simulation: model cache-miss cost as object count grows,
# for "AoS pointer-chase" vs "SoA contiguous". Numbers are illustrative
# (order-of-magnitude) cache-miss model, not measured.
def fig_perf_simulation():
    import numpy as np
    fig, axes = plt.subplots(1, 2, figsize=(13.0, 5.4))

    # ---- model: each object ~ 40 bytes OOP block, fields used by movement = 16B
    # cache line = 64B.
    # AoS: stride = object size = 48B (pos16+vel8+color16+r4+misc8 ~ approx).
    #   -> each step crosses a cache line almost every obj => ~1 miss/obj after warmup
    #   -> cost ~ miss_penalty (say 8 ns) per obj movement
    # SoA: stride within Position[] = 16B => 4 entities per 64B line => ~1 miss per 4 obj
    #   -> ~0.25 miss/obj
    # We plot scaled "per-frame update time (ms)" vs object count.
    ns = np.array([500, 1000, 2000, 5000, 10000, 20000, 50000, 100000])
    miss_pen = 8.0e-9  # 8 ns per miss
    aos_miss_per = 1.0      # ~ one miss per object (scattered)
    soa_miss_per = 0.22     # ~ one miss per ~4-5 entities
    aos_ms = ns * aos_miss_per * miss_pen * 1000.0
    soa_ms = ns * soa_miss_per * miss_pen * 1000.0
    budget = np.full_like(ns, 16.0, dtype=float)  # 16ms frame budget

    ax = axes[0]
    ax.plot(ns, aos_ms, "-o", color=RED, lw=2.2, ms=6,
            label="OOP / AoS (pointer chasing)")
    ax.plot(ns, soa_ms, "-s", color=GREEN, lw=2.2, ms=6,
            label="Data-oriented / SoA (contiguous)")
    ax.plot(ns, budget, "--", color=AMBER, lw=1.8,
            label="16 ms frame budget (60 FPS)")
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("number of objects updated per frame", fontsize=11.5)
    ax.set_ylabel("update time per frame (ms)", fontsize=11.5)
    ax.set_title("movement update cost vs object count\n(order-of-magnitude cache model)",
                 fontsize=12.5, weight="bold", color=INK)
    ax.grid(True, which="both", linestyle=":", alpha=0.45)
    ax.legend(fontsize=10.5, loc="upper left", framealpha=0.92)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=10)

    # annotate the crossover where AoS blows the budget
    # find where aos_ms first > 16
    idx = np.argmax(aos_ms > 16.0) if np.any(aos_ms > 16.0) else None
    if idx is not None and aos_ms[idx] > 16.0:
        ax.annotate("OOP blows the 16 ms\nbudget here",
                    xy=(ns[idx], aos_ms[idx]), xytext=(ns[idx] * 0.25, aos_ms[idx] * 6),
                    fontsize=10.5, color=RED, weight="bold",
                    arrowprops=dict(arrowstyle="->", color=RED, lw=1.4))

    # ---- right: cache miss rate model (bar chart of effective misses per 1000 obj)
    ax2 = axes[1]
    scenarios = ["OOP / AoS\n(scattered,\npointer chasing)",
                 "Composition\n(objects still\nown fields)",
                 "Data-oriented\n/ SoA\n(contiguous)"]
    misses_per_1000 = [1000, 720, 210]      # illustrative
    colors = [RED, AMBER, GREEN]
    bars = ax2.bar(scenarios, misses_per_1000, color=colors, edgecolor="white", width=0.62)
    for b, v in zip(bars, misses_per_1000):
        ax2.text(b.get_x() + b.get_width() / 2, v + 25, f"~{v}",
                 ha="center", fontsize=11.5, weight="bold", color=INK)
    ax2.set_ylabel("approx. cache misses per 1000 objects (lower = better)",
                   fontsize=11)
    ax2.set_title("effective cache misses during a movement scan\n(illustrative cache-line model)",
                  fontsize=12.5, weight="bold", color=INK)
    ax2.set_ylim(0, 1180)
    ax2.grid(True, axis="y", linestyle=":", alpha=0.4)
    for s in ("top", "right"):
        ax2.spines[s].set_visible(False)
    ax2.tick_params(labelsize=10)

    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig-p1_04_03-perf-simulation.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_inheritance_tree()
    fig_memory_layout()
    fig_perf_simulation()
    print("done:", sorted(f for f in os.listdir(OUT) if f.startswith("fig-p1_04_") and f.endswith(".png")))
