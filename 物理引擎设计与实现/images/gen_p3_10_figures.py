# -*- coding: utf-8 -*-
"""Figures for Chapter P3-10 Broad Phase: Spatial Partitioning.

Run: python gen_p3_10_figures.py
All in-canvas text is English (avoids CJK font issues).
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, FancyBboxPatch, FancyArrowPatch, Polygon

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 12.0,
    "axes.linewidth": 1.1,
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
PURP  = (0.55, 0.30, 0.78)
GREY  = (0.78, 0.78, 0.80)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def arrow(ax, p1, p2, color=INK, lw=1.6, rad=0.0, alpha=1.0):
    cs = f"arc3,rad={rad}"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=14, alpha=alpha))


def gen_objects(seed=7, n=24, span=10.0, r=0.28):
    rng = np.random.default_rng(seed)
    xs = rng.uniform(0.5, span - 0.5, n)
    ys = rng.uniform(0.5, span - 0.5, n)
    return xs, ys, r


# ---------------------------------------------------------------- fig 1
# Brute force O(n^2) all-pairs (dense tangle) vs spatial-partition candidates (sparse).
def fig_brute_vs_partition():
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(13.0, 6.4))

    xs, ys, r = gen_objects()

    # ---- left: brute force, connect EVERY pair
    axL.set_title("Brute force: test every pair  =  O(n^2)\n"
                  "n=24  ->  276 pairs (all drawn)",
                  fontsize=12.5, weight="bold", color=RED)
    for i in range(len(xs)):
        for j in range(i + 1, len(xs)):
            axL.plot([xs[i], xs[j]], [ys[i], ys[j]], color=RED,
                     lw=0.5, alpha=0.30, zorder=1)
    axL.scatter(xs, ys, s=90, color=INK, zorder=3)
    axL.set_xlim(0, 10); axL.set_ylim(0, 10)
    axL.set_aspect("equal"); clean_ax(axL)
    axL.text(5.0, -0.7, "expensive: even far-apart objects get tested",
            ha="center", fontsize=10.5, color=RED, style="italic")

    # ---- right: uniform grid, only connect pairs that share / neighbor a cell
    axR.set_title("After spatial partition (uniform grid)\n"
                  "only near neighbors become candidates",
                  fontsize=12.5, weight="bold", color=GREEN)
    cell = 2.0
    # draw grid
    for g in np.arange(0, 10 + cell, cell):
        axR.axvline(g, color=GREY, lw=0.6, zorder=0)
        axR.axhline(g, color=GREY, lw=0.6, zorder=0)
    # bucket
    buckets = {}
    for i, (x, y) in enumerate(zip(xs, ys)):
        key = (int(x // cell), int(y // cell))
        buckets.setdefault(key, []).append(i)
    # only test within cell + 8 neighbors
    cand_pairs = set()
    for (cx, cy), members in buckets.items():
        nbrs = [(cx + dx, cy + dy)
                for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
        for nk in nbrs:
            for other in buckets.get(nk, []):
                if other not in members:
                    continue
                pass
        # within cell
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                cand_pairs.add((min(members[a], members[b]),
                                max(members[a], members[b])))
        # with neighbor cells (only forward to avoid dup): right, down, diag
        forward = [(cx + 1, cy), (cx, cy - 1),
                   (cx + 1, cy - 1), (cx + 1, cy + 1)]
        for nk in forward:
            for ia in members:
                for ib in buckets.get(nk, []):
                    cand_pairs.add((min(ia, ib), max(ia, ib)))

    n_pairs = 0
    for (i, j) in sorted(cand_pairs):
        d = np.hypot(xs[i] - xs[j], ys[i] - ys[j])
        if d < 1.4:  # only the genuinely close ones survive
            axR.plot([xs[i], xs[j]], [ys[i], ys[j]], color=GREEN,
                     lw=1.4, alpha=0.85, zorder=2)
            n_pairs += 1
    axR.scatter(xs, ys, s=90, color=INK, zorder=3)
    axR.set_xlim(0, 10); axR.set_ylim(0, 10)
    axR.set_aspect("equal"); clean_ax(axR)
    axR.text(5.0, -0.7,
             f"far objects pruned by grid lookup  ->  only ~{n_pairs} candidate pairs",
             ha="center", fontsize=10.5, color=GREEN, style="italic")

    fig.suptitle("Why spatial partitioning beats O(n^2)",
                 fontsize=14, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p3_10-brute-vs-partition.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 2
# Three spatial partitioning strategies side by side.
def fig_three_strategies():
    fig, axes = plt.subplots(1, 3, figsize=(15.5, 5.6))

    xs, ys, r = gen_objects(seed=3, n=18, span=10.0)

    # (a) uniform grid
    ax = axes[0]
    ax.set_title("(a) Uniform grid", fontsize=13, weight="bold", color=BLUE)
    cell = 2.0
    for g in np.arange(0, 10 + cell, cell):
        ax.axvline(g, color=BLUE, lw=0.7, alpha=0.55)
        ax.axhline(g, color=BLUE, lw=0.7, alpha=0.55)
    ax.scatter(xs, ys, s=110, color=INK, zorder=4)
    # highlight one cell
    ax.add_patch(Rectangle((4.0, 4.0), cell, cell, fill=True,
                           facecolor=(0.8, 0.9, 1.0), alpha=0.55,
                           edgecolor=BLUE, lw=1.6, zorder=1))
    ax.text(5.0, 5.0, "cell", ha="center", va="center", color=BLUE,
            fontsize=11, weight="bold")
    ax.text(5.0, -1.0,
            "fixed cells; check own + 8 neighbors\nbest: uniform density, O(1) lookup",
            ha="center", fontsize=10.2, color=INK)
    ax.set_xlim(-0.2, 10.2); ax.set_ylim(-1.8, 10.2)
    ax.set_aspect("equal"); clean_ax(ax)

    # (b) sweep and prune (sort endpoints on one axis)
    ax = axes[1]
    ax.set_title("(b) Sweep and prune (SAP)", fontsize=13, weight="bold",
                 color=AMBER)
    # axis line
    ax.axhline(5.0, color=GREY, lw=0.8)
    ax.text(0.0, 5.25, "x-axis", fontsize=10, color=INK)
    # show a few objects as projected intervals on the axis (rectangles),
    # plus their sweep order
    intervals = sorted([(x - 0.7, x + 0.7, x, y) for x, y in zip(xs, ys)],
                       key=lambda t: t[0])
    # draw bodies
    for (lo, hi, x, y) in intervals:
        ax.add_patch(Rectangle((x - 0.28, y - 0.28), 0.56, 0.56,
                               facecolor=(1.0, 0.92, 0.75),
                               edgecolor=AMBER, lw=1.3, zorder=3))
    # interval endpoints on the axis
    for k, (lo, hi, x, y) in enumerate(intervals):
        ax.plot([lo, hi], [5.0, 5.0], color=AMBER, lw=2.6, alpha=0.85,
                solid_capstyle="butt", zorder=2)
        ax.plot([lo, lo], [4.85, 5.15], color=INK, lw=1.0)
        ax.plot([hi, hi], [4.85, 5.15], color=INK, lw=1.0)
    # sweep arrow
    arrow(ax, (0.3, 5.8), (9.7, 5.8), color=INK, lw=1.8)
    ax.text(5.0, 6.15, "sweep endpoints, only overlapping intervals pair",
            ha="center", fontsize=10.2, color=INK)
    ax.text(5.0, -1.0,
            "sort interval endpoints; active set\ngreat: low object velocity (stable sort)",
            ha="center", fontsize=10.2, color=INK)
    ax.set_xlim(-0.2, 10.2); ax.set_ylim(-1.8, 7.2)
    ax.set_aspect("equal"); clean_ax(ax)

    # (c) bounding volume hierarchy (dynamic AABB tree)
    ax = axes[2]
    ax.set_title("(c) Bounding Volume Hierarchy (AABB tree)",
                 fontsize=13, weight="bold", color=PURP)
    # hand-drawn toy tree: root box, two children, four leaves
    root = (0.6, 4.6, 8.8, 4.0)
    c1   = (0.9, 5.0, 4.0, 3.2)
    c2   = (5.1, 4.8, 4.0, 3.4)
    leaves = [(1.1, 5.3, 1.6, 1.3), (3.0, 6.2, 1.6, 1.3),
              (5.4, 5.2, 1.6, 1.3), (7.2, 6.1, 1.6, 1.3)]
    for (x, y, w, h) in [root]:
        ax.add_patch(Rectangle((x, y), w, h, fill=False,
                               edgecolor=PURP, lw=2.2))
    for (x, y, w, h) in [c1, c2]:
        ax.add_patch(Rectangle((x, y), w, h, fill=False,
                               edgecolor=PURP, lw=1.5, alpha=0.8))
    for (x, y, w, h) in leaves:
        ax.add_patch(Rectangle((x, y), w, h, fill=True,
                               facecolor=(0.92, 0.86, 0.97),
                               edgecolor=PURP, lw=1.3))
    # bodies inside leaves
    body_pos = [(1.9, 5.95), (3.8, 6.85), (6.2, 5.85), (8.0, 6.75)]
    for (bx, by) in body_pos:
        ax.add_patch(Circle((bx, by), 0.22, facecolor=INK))
    # query AABB (dashed) overlapping only 1 leaf branch -> prunes the other
    ax.add_patch(Rectangle((0.8, 5.2), 2.2, 1.7, fill=True,
                           facecolor=(1.0, 0.85, 0.85), alpha=0.35,
                           edgecolor=RED, lw=1.8, linestyle="--"))
    ax.text(1.9, 4.2, "query", ha="center", color=RED, fontsize=10,
            weight="bold")
    ax.text(5.0, -1.0,
            "descend only overlapping branches\nbest: dynamic, big scenes  (Box2D's choice)",
            ha="center", fontsize=10.2, color=INK)
    ax.set_xlim(-0.2, 10.2); ax.set_ylim(-1.8, 9.2)
    ax.set_aspect("equal"); clean_ax(ax)

    fig.suptitle("Three ways to partition space for broad phase",
                 fontsize=14, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p3_10-three-strategies.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 3
# Box2D broad phase: three trees (per body type) feeding candidate pairs.
def fig_box2d_three_trees():
    fig, ax = plt.subplots(figsize=(12.5, 6.0))

    def tree_box(x, y, w, h, title, sub, color):
        ax.add_patch(FancyBboxPatch((x, y), w, h,
                                    boxstyle="round,pad=0.04,rounding_size=0.10",
                                    facecolor="white", edgecolor=color, lw=2.0))
        ax.text(x + w / 2, y + h - 0.35, title, ha="center", va="center",
                fontsize=12.5, weight="bold", color=color)
        ax.text(x + w / 2, y + h - 0.85, sub, ha="center", va="center",
                fontsize=9.8, color=INK)

    tree_box(0.3, 1.2, 3.2, 3.6, "static tree",
             "walls, ground\n(never moves)", (0.45, 0.45, 0.48))
    tree_box(4.3, 1.2, 3.2, 3.6, "kinematic tree",
             "platforms, motors\n(scripted motion)", AMBER)
    tree_box(8.3, 1.2, 3.2, 3.6, "dynamic tree",
             "boxes, balls\n(physics-driven)", BLUE)

    # a moving dynamic proxy queries the other two trees
    arrow(ax, (9.9, 3.0), (7.5, 3.0), color=BLUE, lw=2.0, rad=0.0)
    ax.text(8.7, 3.35, "query", ha="center", color=BLUE, fontsize=10,
            weight="bold")
    arrow(ax, (9.9, 2.2), (3.5, 2.2), color=BLUE, lw=2.0, rad=0.0)
    ax.text(6.7, 2.55, "query", ha="center", color=BLUE, fontsize=10,
            weight="bold")

    ax.text(5.9, 5.2,
            "b2BroadPhase holds 3 dynamic trees (one per body type)",
            ha="center", fontsize=13, weight="bold", color=INK)
    ax.text(5.9, 4.75,
            "only MOVING proxies (buffered in moveArray) trigger pair queries",
            ha="center", fontsize=10.5, color=INK, style="italic")

    # output
    ax.add_patch(FancyBboxPatch((4.0, -0.2), 3.8, 0.85,
                                boxstyle="round,pad=0.03,rounding_size=0.08",
                                facecolor=(0.90, 0.95, 0.90),
                                edgecolor=GREEN, lw=1.6))
    ax.text(5.9, 0.22,
            "candidate pairs  ->  narrow phase (SAT/GJK)",
            ha="center", va="center", fontsize=10.8, weight="bold",
            color=GREEN)
    arrow(ax, (5.9, 1.2), (5.9, 0.7), color=GREEN, lw=1.8)

    ax.set_xlim(-0.2, 12.0); ax.set_ylim(-0.6, 5.6)
    ax.set_aspect("equal"); clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p3_10-box2d-three-trees.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 4
# Tree query path: stack-driven descent prunes non-overlapping branches.
def fig_tree_query_descent():
    fig, ax = plt.subplots(figsize=(10.0, 6.8))

    # root AABB
    ax.add_patch(Rectangle((0.6, 0.6), 8.8, 6.0, fill=False,
                           edgecolor=PURP, lw=2.4))
    ax.text(5.0, 6.25, "root", ha="center", color=PURP, fontsize=11,
            weight="bold")
    # left child (overlaps query -> descend)
    ax.add_patch(Rectangle((0.9, 0.9), 4.0, 5.2, fill=False,
                           edgecolor=PURP, lw=1.6, alpha=0.8))
    ax.text(2.9, 5.75, "child A", ha="center", color=PURP, fontsize=10)
    # right child (does NOT overlap -> pruned)
    ax.add_patch(Rectangle((5.2, 0.9), 3.9, 5.2, fill=False,
                           edgecolor=GREY, lw=1.6, linestyle=(0, (5, 3))))
    ax.text(7.15, 5.75, "child B  (no overlap)", ha="center",
            color=(0.55, 0.55, 0.55), fontsize=10)

    # leaves under A
    for (lx, ly, name) in [(1.1, 1.2, "L1"), (3.3, 1.2, "L2"),
                            (1.1, 3.8, "L3"), (3.3, 3.8, "L4")]:
        ax.add_patch(Rectangle((lx, ly), 1.5, 1.6, fill=True,
                               facecolor=(0.92, 0.86, 0.97),
                               edgecolor=PURP, lw=1.2))
        ax.text(lx + 0.75, ly + 0.8, name, ha="center", va="center",
                fontsize=10, color=INK)
        ax.add_patch(Circle((lx + 0.75, ly + 0.8), 0.20, facecolor=INK))

    # query AABB
    ax.add_patch(Rectangle((2.7, 1.05), 2.4, 2.1, fill=True,
                           facecolor=(1.0, 0.85, 0.85), alpha=0.35,
                           edgecolor=RED, lw=2.0, linestyle="--"))
    ax.text(3.9, 0.65, "query AABB", ha="center", color=RED, fontsize=11,
            weight="bold")

    # descent order
    steps = [
        (0.2, -0.7, "1. push root  ->  overlaps? yes  -> push A, B"),
        (3.5, -0.7, "2. pop B       -> overlaps? NO  -> skip (pruned)"),
        (7.0, -0.7, "3. pop A       -> overlaps? yes  -> push L3, L4"),
    ]
    for (x, y, t) in steps:
        ax.text(x, y, t, fontsize=9.8, color=INK)

    ax.text(5.0, -1.4,
            "cost = O(log n + k):  only branches intersecting the query AABB are visited",
            ha="center", fontsize=11.2, color=GREEN, weight="bold")

    ax.set_xlim(0, 9.6); ax.set_ylim(-2.0, 6.9)
    ax.set_aspect("equal"); clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p3_10-tree-query-descent.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_brute_vs_partition()
    fig_three_strategies()
    fig_box2d_three_trees()
    fig_tree_query_descent()
    print("done:", sorted(f for f in os.listdir(OUT)
                          if f.startswith("fig-p3_10")))
