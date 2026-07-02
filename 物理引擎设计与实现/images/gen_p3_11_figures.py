# -*- coding: utf-8 -*-
"""Figures for chapter P3-11 (Dynamic AABB Tree) of the physics-engine book.

Run:  python gen_p3_11_figures.py
All text inside figures is English to avoid matplotlib CJK font issues.
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch, FancyArrowPatch, Circle, Polygon

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 12.0,
    "axes.linewidth": 1.1,
    "figure.dpi": 150,
    "savefig.dpi": 150,
    "savefig.bbox": "tight",
    "savefig.facecolor": "white",
    "savefig.pad_inches": 0.18,
})

OUT = os.path.dirname(os.path.abspath(__file__))

RED   = (0.86, 0.18, 0.18)
GREEN = (0.20, 0.70, 0.32)
BLUE  = (0.20, 0.45, 0.88)
AMBER = (0.95, 0.70, 0.18)
PURPLE = (0.55, 0.30, 0.78)
GREY  = (0.78, 0.78, 0.80)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.96, 0.96, 0.98)
LEAFC = (0.90, 0.96, 0.92)   # leaf light green
INTC  = (0.90, 0.94, 1.0)    # internal node light blue


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def aabb_rect(ax, lo, hi, ec=INK, fc="none", lw=1.6, alpha=1.0, ls="-"):
    ax.add_patch(Rectangle((lo[0], lo[1]), hi[0] - lo[0], hi[1] - lo[1],
                           facecolor=fc, edgecolor=ec, linewidth=lw,
                           alpha=alpha, linestyle=ls))


def union(loA, hiA, loB, hiB):
    lo = (min(loA[0], loB[0]), min(loA[1], loB[1]))
    hi = (max(hiA[0], hiB[0]), max(hiA[1], hiB[1]))
    return lo, hi


def arrow(ax, p1, p2, color=INK, lw=1.6, rad=0.0, mut=14):
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=f"arc3,rad={rad}",
                                  mutation_scale=mut))


def node_box(ax, cx, cy, w, h, text, fc, ec=INK, fs=10.5, weight="normal", tc=INK):
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=fc, edgecolor=ec, linewidth=1.5))
    ax.text(cx, cy, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def tree_edge(ax, p1, p2, color=GREY, lw=1.4):
    ax.plot([p1[0], p2[0]], [p1[1], p2[1]], color=color, lw=lw, zorder=1)


# ---------------------------------------------------------------- fig 1
def fig_tree_structure():
    """Scene of objects -> dynamic AABB tree: internal nodes = union of children,
    leaves = object fat AABBs, balanced height."""
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(13.5, 6.6),
                                   gridspec_kw={"width_ratios": [1, 1.15]})

    # ---- left: scene with 5 objects and their AABBs (plus internal unions) ----
    # define 5 leaves (lo, hi) in scene space
    leaves = {
        "A": ((0.6, 0.6), (1.8, 1.7)),
        "B": ((2.4, 0.7), (3.6, 1.9)),
        "C": ((4.6, 0.5), (5.7, 1.5)),
        "D": ((0.8, 3.0), (2.2, 4.3)),
        "E": ((4.2, 3.2), (5.6, 4.4)),
    }
    colors = {"A": RED, "B": AMBER, "C": GREEN, "D": BLUE, "E": PURPLE}
    for name, (lo, hi) in leaves.items():
        aabb_rect(axL, lo, hi, ec=colors[name], fc=(*colors[name], 0.10), lw=2.0)
        axL.add_patch(Circle(((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2),
                             0.18, facecolor=colors[name], edgecolor=INK))
        axL.text(hi[0] + 0.05, hi[1] + 0.05, name, fontsize=12,
                 weight="bold", color=colors[name])
    # internal N1 = union(A,B)
    loN1, hiN1 = union(*leaves["A"], *leaves["B"])
    aabb_rect(axL, loN1, hiN1, ec=INK, fc="none", lw=1.3, ls="--")
    axL.text((loN1[0] + hiN1[0]) / 2, hiN1[1] + 0.12, "N1=A|B",
             ha="center", fontsize=9.5, color=INK)
    # internal N2 = union(N1, C)
    loN2, hiN2 = union(loN1, hiN1, *leaves["C"])
    aabb_rect(axL, loN2, hiN2, ec=INK, fc="none", lw=1.3, ls=":")
    axL.text((loN2[0] + hiN2[0]) / 2, hiN2[1] + 0.12, "N2=N1|C",
             ha="center", fontsize=9.5, color=INK)
    # internal N3 = union(D,E) = root sibling
    loN3, hiN3 = union(*leaves["D"], *leaves["E"])
    aabb_rect(axL, loN3, hiN3, ec=INK, fc="none", lw=1.3, ls="--")
    axL.text((loN3[0] + hiN3[0]) / 2, hiN3[1] + 0.12, "N3=D|E",
             ha="center", fontsize=9.5, color=INK)
    axL.set_xlim(-0.2, 6.6); axL.set_ylim(-0.1, 5.2)
    axL.set_aspect("equal"); clean_ax(axL)
    axL.set_title("Scene: object AABBs (solid) +\ninternal union boxes (dashed)",
                  fontsize=12, weight="bold")

    # ---- right: the tree built from these leaves ----
    # layout (x, y) for each node; root at top
    pos = {
        "ROOT": (3.2, 5.0),
        "N2":   (1.7, 3.7),
        "N3":   (5.0, 3.7),
        "N1":   (0.9, 2.4),
        "C":    (3.0, 2.4),
        "D":    (4.3, 2.4),
        "E":    (5.9, 2.4),
        "A":    (0.3, 1.0),
        "B":    (1.7, 1.0),
    }
    edges = [("ROOT", "N2"), ("ROOT", "N3"),
             ("N2", "N1"), ("N2", "C"),
             ("N3", "D"), ("N3", "E"),
             ("N1", "A"), ("N1", "B")]
    for a, b in edges:
        tree_edge(axR, pos[a], pos[b])
    # internal nodes
    for n in ("ROOT", "N2", "N3", "N1"):
        node_box(axR, *pos[n], 1.1, 0.62, n, fc=INTC, ec=BLUE, weight="bold")
    # leaves
    leaf_style = {"A": RED, "B": AMBER, "C": GREEN, "D": BLUE, "E": PURPLE}
    for n, c in leaf_style.items():
        node_box(axR, *pos[n], 0.85, 0.6, n, fc=LEAFC, ec=c, weight="bold")
    axR.text(3.2, 5.7, "internal = AABB(children1 | children2)",
             ha="center", fontsize=10, color=BLUE)
    axR.text(3.2, 0.2, "leaves = object fat AABBs    height balanced (root h=3)",
             ha="center", fontsize=10, color=INK)
    axR.set_xlim(-0.6, 6.8); axR.set_ylim(-0.2, 6.1)
    axR.set_aspect("equal"); clean_ax(axR)
    axR.set_title("Dynamic AABB tree: leaves are objects,\n"
                  "internal nodes bound their two children",
                  fontsize=12, weight="bold")

    fig.suptitle("Dynamic AABB tree: a balanced binary tree over moving objects",
                 fontsize=13.5, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p3_11-01-tree-structure.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 2
def fig_move_proxy():
    """MoveProxy = remove leaf + re-insert (no rotation). Cheap incremental update.
    Object B moves right; old leaf removed, re-inserted near best sibling."""
    fig, axes = plt.subplots(1, 3, figsize=(14.5, 5.2))

    # common small tree (4 leaves)
    def draw_mini(ax, title, Bpos, highlight=None):
        # leaves
        L = {
            "A": ((0.3, 0.3), (1.3, 1.2)),
            "C": ((3.6, 0.3), (4.7, 1.3)),
            "D": ((0.4, 2.8), (1.6, 4.0)),
        }
        for n, (lo, hi) in L.items():
            aabb_rect(ax, lo, hi, ec=INK, fc=(0.9, 0.9, 0.92), lw=1.4)
            ax.text((lo[0] + hi[0]) / 2, hi[1] + 0.05, n,
                    ha="center", fontsize=10, color=INK)
        # B at given position
        bx, by = Bpos
        blo = (bx - 0.5, by - 0.45); bhi = (bx + 0.5, by + 0.45)
        bcolor = RED if highlight == "B" else AMBER
        aabb_rect(ax, blo, bhi, ec=bcolor, fc=(*bcolor, 0.18), lw=2.2)
        ax.text((blo[0] + bhi[0]) / 2, bhi[1] + 0.05, "B",
                ha="center", fontsize=11, weight="bold", color=bcolor)
        ax.set_xlim(-0.2, 5.4); ax.set_ylim(-0.2, 4.6)
        ax.set_aspect("equal"); clean_ax(ax)
        ax.set_title(title, fontsize=11.5, weight="bold")

    # panel 1: before move
    draw_mini(axes[0], "1. before: B sits under internal N1 (with A)", (1.0, 2.0))
    # annotate N1
    aabb_rect(axes[0], (0.3, 0.3), (1.5, 2.45), ec=BLUE, fc="none", lw=1.3, ls="--")
    axes[0].text(0.9, 2.6, "N1", color=BLUE, fontsize=10)

    # panel 2: B moves out of its box -> remove leaf
    draw_mini(axes[1], "2. B moves right -> RemoveLeaf(B) detaches N1", (2.6, 2.0), highlight="B")
    arrow(axes[1], (1.0, 2.0), (2.6, 2.0), color=AMBER, lw=1.8)
    aabb_rect(axes[1], (0.3, 0.3), (1.6, 1.2), ec=INK, fc=(0.9, 0.9, 0.92), lw=1.4)
    axes[1].text(0.9, 1.35, "A only", ha="center", fontsize=9, color=INK)
    axes[1].text(2.6, 3.0, "B's leaf node\nremoved from tree\n(shouldRotate=false)",
                 ha="center", fontsize=9.5, color=RED)

    # panel 3: re-insert B at best sibling (near C)
    draw_mini(axes[2], "3. InsertLeaf(B): best sibling = C -> new N4=B|C", (3.4, 2.0))
    aabb_rect(axes[2], (2.9, 1.55), (4.7, 2.45), ec=GREEN, fc="none", lw=1.5, ls="--")
    axes[2].text(3.8, 2.55, "N4=B|C", color=GREEN, fontsize=10, weight="bold")
    axes[2].text(3.4, 3.4, "re-insert finds a\ncheap sibling via SAH\ngreedy descent",
                 ha="center", fontsize=9.5, color=GREEN)

    fig.suptitle("MoveProxy = RemoveLeaf + InsertLeaf (incremental, no full rebuild)",
                 fontsize=13, weight="bold", y=1.04)
    fig.savefig(os.path.join(OUT, "fig-p3_11-02-move-proxy.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 3
def fig_rebuild_height():
    """After many moves, the tree degrades (tall, fat internal boxes).
    Rebuild() recomputes from scratch and lowers height -> faster queries."""
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(13.5, 6.2))

    # ---- left: degraded tree (tall, unbalanced) ----
    # a skewed chain of 7 leaves
    n = 7
    xs = np.linspace(0.5, 5.5, n)
    # build a degenerate right-leaning tree: each internal has one leaf + one internal
    leaf_y = 0.6
    intern_ys = [1.6, 2.2, 2.8, 3.4, 4.0, 4.6]
    # leaves
    for i, x in enumerate(xs):
        node_box(axL, x, leaf_y, 0.55, 0.5, f"L{i+1}", fc=LEAFC, ec=GREEN, fs=9)
    # internals stacked diagonally (degenerate)
    for i, y in enumerate(intern_ys):
        node_box(axL, 5.5 - i * 0.55, y, 0.8, 0.5, f"I{i+1}", fc=INTC, ec=BLUE, fs=9)
    # edges: chain
    chain = [("L1", (xs[0], leaf_y))]
    # we just draw a schematic chain: L1-I1-L2-I2-... not exact; use diagonal edges
    pts = [(xs[0], leaf_y)]
    for i in range(6):
        pts.append((5.5 - i * 0.55, intern_ys[i]))   # internal
        pts.append((xs[i + 1], leaf_y))               # next leaf
    for a, b in zip(pts[:-1], pts[1:]):
        tree_edge(axL, a, b, color=GREY)
    axL.text(3.0, 5.4, "degraded tree: height = 6, many fat boxes",
             ha="center", fontsize=11.5, color=RED, weight="bold")
    axL.text(3.0, 5.05, "(each move did remove+reinsert; quality slipped)",
             ha="center", fontsize=9.5, color=INK)
    axL.set_xlim(-0.2, 6.2); axL.set_ylim(0, 6.0)
    axL.set_aspect("equal"); clean_ax(axL)
    axL.set_title("Before Rebuild: tall, slow queries", fontsize=12, weight="bold")

    # ---- right: rebuilt balanced tree (height ~ log2(7) = 3) ----
    # balanced binary tree of 7 leaves
    pos = {
        "R":  (3.0, 5.0),
        "I1": (1.6, 3.9), "I2": (4.4, 3.9),
        "I3": (0.9, 2.7), "I4": (2.3, 2.7), "I5": (3.7, 2.7), "I6": (5.1, 2.7),
    }
    leaves_pos = {
        "L1": (0.5, 1.4), "L2": (1.3, 1.4),
        "L3": (2.0, 1.4), "L4": (2.6, 1.4),
        "L5": (3.4, 1.4), "L6": (4.0, 1.4),
        "L7": (5.4, 1.4),
    }
    edges = [("R", "I1"), ("R", "I2"),
             ("I1", "I3"), ("I1", "I4"),
             ("I2", "I5"), ("I2", "I6"),
             ("I3", "L1"), ("I3", "L2"),
             ("I4", "L3"), ("I4", "L4"),
             ("I5", "L5"), ("I5", "L6"),
             ("I6", "L7")]
    for a, b in edges:
        tree_edge(axR, pos[a] if a in pos else leaves_pos[a],
                  pos[b] if b in pos else leaves_pos[b])
    for n, p in pos.items():
        node_box(axR, *p, 0.75, 0.48, n, fc=INTC, ec=BLUE, fs=9, weight="bold")
    for n, p in leaves_pos.items():
        node_box(axR, *p, 0.6, 0.45, n, fc=LEAFC, ec=GREEN, fs=9)
    axR.text(3.0, 5.75, "rebuilt tree: height = 3 (= ceil(log2 7))",
             ha="center", fontsize=11.5, color=GREEN, weight="bold")
    axR.text(3.0, 5.4, "Rebuild() re-partitions all leaves by SAH/mid, O(n)",
             ha="center", fontsize=9.5, color=INK)
    axR.set_xlim(-0.2, 6.2); axR.set_ylim(0.8, 6.2)
    axR.set_aspect("equal"); clean_ax(axR)
    axR.set_title("After Rebuild: balanced, fast queries", fontsize=12, weight="bold")

    fig.suptitle("Rebuild periodically: cures height creep from incremental moves",
                 fontsize=13.5, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p3_11-03-rebuild-height.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 4
def fig_fat_aabb():
    """fat AABB = tight AABB + margin. Object can wiggle inside the fat box
    without touching the tree (no MoveProxy). Crossing the boundary triggers update."""
    fig, ax = plt.subplots(figsize=(10.5, 6.0))

    # tight AABB (object's current exact box)
    tight_lo = (1.5, 1.5); tight_hi = (3.5, 3.5)
    aabb_rect(ax, tight_lo, tight_hi, ec=RED, fc=(*RED, 0.10), lw=2.4)
    ax.text((tight_lo[0] + tight_hi[0]) / 2, (tight_lo[1] + tight_hi[1]) / 2,
            "tight AABB\n(shape's exact box)", ha="center", va="center",
            fontsize=10.5, color=RED, weight="bold")

    # fat AABB (margin around it)
    m = 0.7
    fat_lo = (tight_lo[0] - m, tight_lo[1] - m)
    fat_hi = (tight_hi[0] + m, tight_hi[1] + m)
    aabb_rect(ax, fat_lo, fat_hi, ec=BLUE, fc=(*BLUE, 0.06), lw=2.0, ls="--")
    ax.text(fat_hi[0] + 0.1, fat_hi[1], "fat AABB\n(stored in tree)",
            ha="left", va="top", fontsize=10.5, color=BLUE, weight="bold")

    # margin arrows
    arrow(ax, (tight_lo[0], tight_hi[1] + 0.05), (fat_lo[0], tight_hi[1] + 0.05),
          color=INK, lw=1.4)
    ax.text((tight_lo[0] + fat_lo[0]) / 2, tight_hi[1] + 0.25, "margin",
            ha="center", fontsize=9.5, color=INK)

    # small wiggle positions (object centers that stay inside fat box)
    for cx, cy in [(2.0, 2.0), (3.0, 2.8), (2.6, 2.2), (2.2, 3.0)]:
        ax.add_patch(Circle((cx, cy), 0.08, facecolor=GREEN, edgecolor=INK))
    ax.text(2.5, 0.9, "object wiggles inside fat box => NO tree update",
            ha="center", fontsize=10.5, color=GREEN, weight="bold")

    # crossed boundary -> trigger
    ax.add_patch(Circle((4.6, 3.4), 0.10, facecolor=RED, edgecolor=INK))
    arrow(ax, (3.3, 3.3), (4.5, 3.35), color=AMBER, lw=1.8)
    ax.text(5.0, 3.5, "crosses fat boundary\n=> MoveProxy (remove + reinsert)",
            ha="left", fontsize=10.5, color=AMBER, weight="bold")

    ax.text(3.0, 5.3,
            "fat AABB = tight AABB + margin  =>  small motion is free, tree stays still",
            ha="center", fontsize=12, weight="bold", color=INK)
    ax.text(3.0, 4.9,
            "margin = min(0.05 m, 0.125 * max_extent)   [Box2D B2_AABB_MARGIN]",
            ha="center", fontsize=10, color=INK)

    ax.set_xlim(0, 7.2); ax.set_ylim(0.4, 5.8)
    ax.set_aspect("equal"); clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p3_11-04-fat-aabb.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_tree_structure()
    fig_move_proxy()
    fig_rebuild_height()
    fig_fat_aabb()
    print("done:", sorted(f for f in os.listdir(OUT) if f.startswith("fig-p3_11")))
