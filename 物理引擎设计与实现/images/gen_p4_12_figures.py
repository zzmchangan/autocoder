# -*- coding: utf-8 -*-
"""P4-12 SAT (Separating Axis Theorem) figures.
Run: python gen_p4_12_figures.py
All in-figure text is English to avoid CJK font issues.
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon, FancyArrowPatch, Circle, Rectangle

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
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def arrow(ax, p1, p2, color=INK, lw=1.6, rad=0.0, mut=16):
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=f"arc3,rad={rad}",
                                  mutation_scale=mut))


def edge_outward_normals(verts):
    """Return outward unit normals of a CCW convex polygon."""
    verts = np.asarray(verts, dtype=float)
    n = len(verts)
    normals = []
    for i in range(n):
        a = verts[i]
        b = verts[(i + 1) % n]
        e = b - a
        # outward normal of a CCW polygon: rotate edge by -90 deg -> (ey, -ex)
        nrm = np.array([e[1], -e[0]])
        nrm = nrm / np.linalg.norm(nrm)
        normals.append(nrm)
    return normals


def project_interval(verts, axis):
    verts = np.asarray(verts, dtype=float)
    d = verts @ axis
    return d.min(), d.max()


# ============================================================ fig 1
def fig_separating_axis_found():
    """Two disjoint convex polygons + a separating axis (a face normal).
    Projections on that axis do NOT overlap => disjoint.
    """
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 6.0))

    # ---- left: the two polygons and the candidate axes (face normals) ----
    ax = axes[0]
    # poly A (red), poly B (blue), disjoint with a clear gap on the right of A
    A = np.array([[0.0, 0.0], [2.0, 0.0], [2.0, 1.6], [0.0, 1.6]])
    B = np.array([[3.2, 0.8], [5.0, 0.8], [5.0, 2.4], [3.2, 2.4]])
    ax.add_patch(MplPolygon(A, closed=True, facecolor=(1.0, 0.85, 0.85),
                            edgecolor=RED, linewidth=2.0, alpha=0.85))
    ax.add_patch(MplPolygon(B, closed=True, facecolor=(0.85, 0.90, 1.0),
                            edgecolor=BLUE, linewidth=2.0, alpha=0.85))
    ax.text(1.0, 0.8, "A", ha="center", va="center", fontsize=16,
            weight="bold", color=RED)
    ax.text(4.1, 1.6, "B", ha="center", va="center", fontsize=16,
            weight="bold", color=BLUE)

    # draw ALL face normals of A and B as faint candidate axes
    for poly, col in [(A, RED), (B, BLUE)]:
        nrm = edge_outward_normals(poly)
        ctr = poly.mean(axis=0)
        for nv in nrm:
            arrow(ax, ctr, ctr + 1.2 * nv, color=col, lw=1.1, mut=11)
    ax.text(2.5, -0.7, "candidate axes = face normals of both polygons",
            ha="center", fontsize=11, color=INK)

    # highlight THE separating axis: A's right edge normal = (+1, 0)
    sep_axis = np.array([1.0, 0.0])
    arrow(ax, np.array([2.6, 1.2]), np.array([2.6, 1.2]) + 2.0 * sep_axis,
          color=GREEN, lw=2.8, mut=18)
    ax.text(4.8, 3.25, "separating axis found!", ha="center",
            fontsize=12, weight="bold", color=GREEN)

    ax.set_xlim(-1.5, 6.5); ax.set_ylim(-1.2, 4.0)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("(a) two convex polygons: project onto each face normal",
                 fontsize=12.5, weight="bold")

    # ---- right: the 1-D projection intervals on the separating axis ----
    ax = axes[1]
    amin, amax = project_interval(A, sep_axis)
    bmin, bmax = project_interval(B, sep_axis)
    y = 1.0
    ax.plot([amin, amax], [y, y], color=RED, lw=8, solid_capstyle="round")
    ax.plot([bmin, bmax], [y, y], color=BLUE, lw=8, solid_capstyle="round")
    ax.text((amin + amax) / 2, y + 0.35, "A's projection", ha="center",
            color=RED, fontsize=12, weight="bold")
    ax.text((bmin + bmax) / 2, y + 0.35, "B's projection", ha="center",
            color=BLUE, fontsize=12, weight="bold")
    # gap
    ax.annotate("", xy=(bmin, y), xytext=(amax, y),
                arrowprops=dict(arrowstyle="<->", color=GREEN, lw=2.2))
    ax.text((amax + bmin) / 2, y - 0.55, "GAP > 0\n=> separated!",
            ha="center", color=GREEN, fontsize=12.5, weight="bold")
    ax.axhline(y, color=GREY, lw=0.7)
    ax.text(0, y - 1.25, "1-D number line along the separating axis (normal of A's right edge)",
            ha="center", fontsize=10.5, color=INK)
    ax.set_xlim(-1.5, 6.5); ax.set_ylim(-1.7, 2.6)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("(b) on this axis the intervals are disjoint => NOT intersecting",
                 fontsize=12.5, weight="bold")

    fig.suptitle("SAT: if some axis separates the projections, the polygons are disjoint",
                 fontsize=14, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p4_12-1-separating-axis-found.png"))
    plt.close(fig)


# ============================================================ fig 2
def fig_all_overlap_intersecting():
    """Two OVERLAPPING convex polygons: every face-normal axis has overlapping
    projections => no separating axis exists => intersecting.
    """
    fig, axes = plt.subplots(1, 3, figsize=(15.5, 5.4))

    A = np.array([[0.0, 0.0], [3.0, 0.0], [3.0, 2.0], [0.0, 2.0]])
    B = np.array([[1.8, 0.8], [4.4, 0.6], [4.0, 3.0], [1.6, 2.6]])

    # three representative axes to show all overlap
    nA = edge_outward_normals(A)   # 4 normals
    nB = edge_outward_normals(B)
    axes_to_show = [
        ("axis = A's right-edge normal", nA[1]),
        ("axis = A's top-edge normal",   nA[2]),
        ("axis = B's slanted-edge normal", nB[3]),
    ]

    for k, (label, axis) in enumerate(axes_to_show):
        ax = axes[k]
        ax.add_patch(MplPolygon(A, closed=True, facecolor=(1.0, 0.85, 0.85),
                                edgecolor=RED, linewidth=2.0, alpha=0.55))
        ax.add_patch(MplPolygon(B, closed=True, facecolor=(0.85, 0.90, 1.0),
                                edgecolor=BLUE, linewidth=2.0, alpha=0.55))
        ctr = np.vstack([A, B]).mean(axis=0)
        arrow(ax, ctr, ctr + 2.0 * axis, color=AMBER, lw=2.4, mut=15)
        ax.set_xlim(-1, 6); ax.set_ylim(-1.5, 4.5)
        ax.set_aspect("equal"); clean_ax(ax)
        ax.set_title(label, fontsize=11.5, weight="bold")

        # inset-ish text: intervals overlap on this axis
        amin, amax = project_interval(A, axis)
        bmin, bmax = project_interval(B, axis)
        ax.text(2.5, -1.1, f"A: [{amin:.2f}, {amax:.2f}]   B: [{bmin:.2f}, {bmax:.2f}]   OVERLAP",
                ha="center", fontsize=10.5, color=AMBER, weight="bold")

    fig.suptitle("SAT: when polygons overlap, EVERY candidate axis has overlapping projections (no separating axis exists)",
                 fontsize=13, weight="bold", y=1.03)
    fig.savefig(os.path.join(OUT, "fig-p4_12-2-all-axes-overlap.png"))
    plt.close(fig)


# ============================================================ fig 3
def fig_flowchart():
    """SAT decision flow for two convex polygons."""
    fig, ax = plt.subplots(figsize=(8.4, 8.2))

    def box(y, text, fc=SOFT, ec=INK):
        ax.add_patch(MplPolygon(
            [[1.7, y], [6.7, y], [6.7, y + 1.0], [1.7, y + 1.0]],
            closed=True, facecolor=fc, edgecolor=ec, linewidth=1.6))
        ax.text(4.2, y + 0.5, text, ha="center", va="center",
                fontsize=11, color=INK)

    box(7.0, "two convex polygons A, B (broad-phase candidates)", fc=(0.90, 0.94, 1.0))
    box(5.6, "collect candidate axes:\nall face normals of A and B")
    # loop block
    ax.add_patch(MplPolygon(
        [[1.2, 3.6], [7.2, 3.6], [7.2, 4.8], [1.2, 4.8]],
        closed=True, facecolor=(1.0, 0.95, 0.80), edgecolor=AMBER, linewidth=1.8))
    ax.text(4.2, 4.2,
            "for each candidate axis n:\n  project A and B onto n\n  (dot every vertex with n)\n  get intervals [a-, a+] and [b-, b+]",
            ha="center", va="center", fontsize=10.5, color=INK)
    # decision
    ax.add_patch(MplPolygon(
        [[4.2, 1.4], [7.0, 2.6], [4.2, 3.8], [1.4, 2.6]],
        closed=True, facecolor=(0.90, 0.95, 0.90), edgecolor=GREEN, linewidth=1.8))
    ax.text(4.2, 2.6, "intervals disjoint?\n(a- > b+  or  b- > a+)",
            ha="center", va="center", fontsize=10.8, color=INK, weight="bold")

    box(0.0, "YES -> n is a separating axis\n=> A, B DO NOT intersect", fc=(1.0, 0.85, 0.85), ec=RED)
    box(-1.6, "NO  -> try next axis;\nif all axes overlap => INTERSECTING", fc=(0.85, 0.95, 0.90), ec=GREEN)

    arrow(ax, (4.2, 7.0), (4.2, 6.6), lw=1.6)
    arrow(ax, (4.2, 5.6), (4.2, 4.8), lw=1.6)
    arrow(ax, (4.2, 3.6), (4.2, 3.8), lw=1.6)
    arrow(ax, (2.6, 2.6), (2.6, 1.0), color=RED, lw=1.8)   # YES branch
    ax.text(2.75, 1.9, "YES", color=RED, weight="bold", fontsize=12)
    arrow(ax, (4.2, 1.4), (4.2, 1.0), lw=1.0)
    ax.text(4.35, 1.2, "NO", color=GREEN, weight="bold", fontsize=12)
    # loop back from NO to the loop block
    arrow(ax, (7.0, 2.6), (8.0, 2.6), color=GREEN, lw=1.4)
    arrow(ax, (8.0, 2.6), (8.0, 4.2), color=GREEN, lw=1.4, rad=0.0)
    arrow(ax, (8.0, 4.2), (7.2, 4.2), color=GREEN, lw=1.4)
    ax.text(8.1, 3.4, "next\naxis", color=GREEN, fontsize=9.5, ha="left")

    ax.set_xlim(0.5, 9.0); ax.set_ylim(-2.4, 8.4)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("SAT decision flow for two convex polygons",
                 fontsize=13.5, weight="bold")
    fig.savefig(os.path.join(OUT, "fig-p4_12-3-sat-flow.png"))
    plt.close(fig)


# ============================================================ fig 4
def fig_concave_counterexample():
    """Concave vs convex: SAT fails for concave shapes.
    Left: concave shape where a face normal shows a gap yet shapes interpenetrate.
    Right: convex decomposition (split concave into convex pieces).
    """
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 6.0))

    # ---- left: concave failure ----
    ax = axes[0]
    # a concave (L-shaped) polygon and a small triangle poking into the notch
    # such that all "outer" face normals give overlapping/ambiguous projections
    L = np.array([[0.0, 0.0], [3.0, 0.0], [3.0, 1.0], [1.0, 1.0],
                  [1.0, 3.0], [0.0, 3.0]])
    T = np.array([[1.2, 0.2], [2.8, 0.4], [1.6, 2.6]])
    ax.add_patch(MplPolygon(L, closed=True, facecolor=(1.0, 0.82, 0.82),
                            edgecolor=RED, linewidth=2.0, alpha=0.8))
    ax.add_patch(MplPolygon(T, closed=True, facecolor=(0.80, 0.88, 1.0),
                            edgecolor=BLUE, linewidth=2.0, alpha=0.85))
    # mark the notch (concave corner)
    ax.plot([1.0], [1.0], "o", color=AMBER, markersize=10, zorder=5)
    ax.text(0.55, 1.35, "concave\nreflex vertex", color=AMBER, fontsize=10,
            weight="bold", ha="center")
    # a red X
    ax.plot([1.3, 2.3], [0.7, 2.0], color=INK, lw=3)
    ax.plot([1.3, 2.0], [2.0, 0.7], color=INK, lw=3)
    ax.text(3.5, 2.6, "shapes actually\ninterpenetrate here", color=INK,
            fontsize=10.5, ha="center", weight="bold")
    ax.text(1.5, -0.9, "SAT (face-normal axes) can MISS the overlap at a concave notch\n"
                       "=> SAT only valid for CONVEX shapes",
            ha="center", fontsize=11, color=RED, weight="bold")
    ax.set_xlim(-1, 5); ax.set_ylim(-1.6, 4.0)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("(a) concave shape: SAT can be wrong", fontsize=12.5, weight="bold")

    # ---- right: convex decomposition ----
    ax = axes[1]
    # split the L into two convex rectangles
    R1 = np.array([[0.0, 0.0], [3.0, 0.0], [3.0, 1.0], [0.0, 1.0]])
    R2 = np.array([[0.0, 1.0], [1.0, 1.0], [1.0, 3.0], [0.0, 3.0]])
    ax.add_patch(MplPolygon(R1, closed=True, facecolor=(1.0, 0.88, 0.75),
                            edgecolor=AMBER, linewidth=2.0, alpha=0.85))
    ax.add_patch(MplPolygon(R2, closed=True, facecolor=(0.90, 0.95, 0.80),
                            edgecolor=GREEN, linewidth=2.0, alpha=0.85))
    ax.text(1.5, 0.5, "convex part 1", ha="center", fontsize=10, color=INK)
    ax.text(0.5, 2.0, "convex\npart 2", ha="center", fontsize=10, color=INK)
    arrow(ax, (1.0, 1.0), (1.0, 1.0), color=INK, lw=0)  # noop spacer
    ax.text(1.5, -0.9,
            "decompose concave shape into convex pieces,\nthen run SAT pairwise on each convex pair",
            ha="center", fontsize=11, color=GREEN, weight="bold")
    ax.set_xlim(-1, 5); ax.set_ylim(-1.6, 4.0)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("(b) fix: convex decomposition", fontsize=12.5, weight="bold")

    fig.suptitle("SAT requires convexity: concave shapes must be decomposed first",
                 fontsize=13.5, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p4_12-4-concave-vs-convex.png"))
    plt.close(fig)


# ============================================================ fig 5
def fig_max_separation_geometry():
    """Geometry of b2FindMaxSeparation: for each face normal n_i of poly1,
    find poly2's most negative projection onto n_i (deepest point), then take
    the maximum over all i. That maximum is the 'min separating edge'.
    """
    fig, ax = plt.subplots(figsize=(9.5, 7.0))
    A = np.array([[0.5, 0.0], [3.5, 0.0], [3.5, 1.8], [0.5, 1.8]])
    B = np.array([[2.6, 0.6], [5.2, 0.8], [4.8, 3.0], [2.4, 2.8]])
    ax.add_patch(MplPolygon(A, closed=True, facecolor=(1.0, 0.85, 0.85),
                            edgecolor=RED, linewidth=2.0, alpha=0.55))
    ax.add_patch(MplPolygon(B, closed=True, facecolor=(0.85, 0.90, 1.0),
                            edgecolor=BLUE, linewidth=2.0, alpha=0.55))
    ax.text(2.0, 0.9, "poly1 (A)", ha="center", fontsize=12, color=RED, weight="bold")
    ax.text(3.8, 1.8, "poly2 (B)", ha="center", fontsize=12, color=BLUE, weight="bold")

    nA = edge_outward_normals(A)
    # examine A's right-edge normal (the most separating one here)
    sep_idx = 1
    n = nA[sep_idx]
    edge_v = A[sep_idx]
    arrow(ax, edge_v, edge_v + 2.2 * n, color=AMBER, lw=2.6, mut=16)
    ax.text(edge_v[0] + 2.3, edge_v[1] + 1.6, "n_i  (A's face normal)",
            color=AMBER, fontsize=11.5, weight="bold")

    # the deepest point of B along n_i: project all B verts
    projs = B @ n
    deepest = B[np.argmin(projs)]
    ax.plot([deepest[0]], [deepest[1]], "o", color=GREEN, markersize=11, zorder=6)
    # dashed line from edge_v to deepest along n
    # draw the separation distance
    sep = np.dot(deepest - edge_v, n)
    # annotate
    mid = edge_v + 0.5 * (np.dot(deepest - edge_v, n)) * n
    ax.annotate("", xy=deepest, xytext=edge_v,
                arrowprops=dict(arrowstyle="<->", color=GREEN, lw=1.6,
                                linestyle="--"))
    ax.text(mid[0] - 0.2, mid[1] + 0.15,
            f"s_i = n_i . (v_B - v_i)\n= {sep:.2f}  (depth of B along n_i)",
            fontsize=10.5, color=GREEN, weight="bold", ha="right")

    # explain max over edges
    ax.text(2.9, -0.8,
            "b2FindMaxSeparation:  for each face i of poly1,\n"
            "  s_i = min over B's vertices of  n_i . (v_B - v_i)   (deepest point)\n"
            "  maxSeparation = max over all i of s_i   =>  the MIN separating edge",
            ha="center", fontsize=10.8, color=INK,
            bbox=dict(boxstyle="round,pad=0.4", fc=SOFT, ec=INK, lw=1.0))

    ax.set_xlim(-0.5, 6.5); ax.set_ylim(-1.6, 3.8)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("Box2D b2FindMaxSeparation: 'min separating edge' = SAT per face normal",
                 fontsize=12.5, weight="bold")
    fig.savefig(os.path.join(OUT, "fig-p4_12-5-max-separation.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_separating_axis_found()
    fig_all_overlap_intersecting()
    fig_flowchart()
    fig_concave_counterexample()
    fig_max_separation_geometry()
    print("done:", sorted(f for f in os.listdir(OUT)
                          if f.startswith("fig-p4_12") and f.endswith(".png")))
