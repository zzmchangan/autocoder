# -*- coding: utf-8 -*-
"""P3-09 AABB chapter figures. Run: python gen_p3_09_figures.py

All text in figures is English (avoids matplotlib CJK glyph boxes).
Geometry is drawn precisely with patches.
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Polygon, FancyArrowPatch, RegularPolygon
from matplotlib.patches import Circle as MplCircle

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
PURPLE = (0.55, 0.30, 0.72)
GREY  = (0.78, 0.78, 0.80)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)
AABB_FACE = (0.30, 0.55, 0.90, 0.18)   # translucent blue fill for AABB
AABB_EDGE = (0.18, 0.36, 0.75)
SHAPE_FACE = (0.95, 0.55, 0.55, 0.55)  # translucent red fill for the shape


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def arrow(ax, p1, p2, color=INK, lw=1.6, rad=0.0, mut=14):
    cs = f"arc3,rad={rad}"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=mut))


def aabb_rect(ax, lower, upper, fc=AABB_FACE, ec=AABB_EDGE, lw=2.0, zorder=2):
    """Draw an AABB given lower/upper (2-tuples)."""
    w = upper[0] - lower[0]
    h = upper[1] - lower[1]
    r = Rectangle(lower, w, h, facecolor=fc, edgecolor=ec, linewidth=lw,
                  zorder=zorder, linestyle="-")
    ax.add_patch(r)
    return r


# ---------------------------------------------------------------- fig 1
def fig_shapes_to_aabb():
    """Three shapes (circle / polygon / capsule) each wrapped by an AABB."""
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 5.0))

    titles = ["Circle", "Convex polygon", "Capsule"]
    # ---- 1) Circle at (1,1) radius 0.9  -> AABB (0.1,0.1)-(1.9,1.9)
    ax = axes[0]
    aabb_rect(ax, (0.1, 0.1), (1.9, 1.9))
    ax.add_patch(MplCircle((1.0, 1.0), 0.9, facecolor=SHAPE_FACE, edgecolor=RED,
                           linewidth=1.8, zorder=3))
    ax.plot([0.1, 0.1], [1.0, 1.0], "o", color=INK, ms=0)  # noop keep bounds
    ax.annotate("", xy=(0.1, 0.05), xytext=(1.9, 0.05),
                arrowprops=dict(arrowstyle="<->", color=AMBER, lw=1.4))
    ax.text(1.0, -0.12, "lowerBound.x ... upperBound.x", ha="center",
            fontsize=10.5, color=AMBER)
    ax.set_title(titles[0] + "  ->  AABB", fontsize=13, weight="bold")

    # ---- 2) Convex polygon (a rotated pentagon-ish blob)
    ax = axes[1]
    pts = np.array([[2.4, 0.6], [3.8, 0.4], [4.6, 1.4], [4.1, 2.5],
                    [2.7, 2.3], [2.0, 1.4]])
    aabb_rect(ax, (pts[:, 0].min(), pts[:, 1].min()),
                  (pts[:, 0].max(), pts[:, 1].max()))
    ax.add_patch(Polygon(pts, closed=True, facecolor=SHAPE_FACE, edgecolor=RED,
                         linewidth=1.8, zorder=3))
    ax.set_title(titles[1] + "  ->  AABB", fontsize=13, weight="bold")

    # ---- 3) Capsule (thick segment) at an angle
    ax = axes[2]
    p1 = np.array([5.4, 0.7]); p2 = np.array([6.6, 2.3])
    r = 0.45
    # capsule outline = two semicircles + rectangle strip; approximate with thick line
    # precise AABB: for each endpoint add +/- r in both axes (circle AABB),
    #   then union.
    def circle_aabb(c):
        return (c[0]-r, c[1]-r), (c[0]+r, c[1]+r)
    lo1, up1 = circle_aabb(p1); lo2, up2 = circle_aabb(p2)
    lo = (min(lo1[0], lo2[0]), min(lo1[1], lo2[1]))
    up = (max(up1[0], up2[0]), max(up1[1], up2[1]))
    aabb_rect(ax, lo, up)
    # draw the capsule body: two end-circles + a rectangle strip along the segment
    ang = np.degrees(np.arctan2(p2[1]-p1[1], p2[0]-p1[0]))
    ax.add_patch(MplCircle(p1, r, facecolor=SHAPE_FACE, edgecolor=RED,
                           linewidth=1.6, zorder=4))
    ax.add_patch(MplCircle(p2, r, facecolor=SHAPE_FACE, edgecolor=RED,
                           linewidth=1.6, zorder=4))
    # rectangle strip: from p1 to p2, width 2r, rotated
    from matplotlib.patches import Polygon as Poly
    d = p2 - p1
    nrm = np.array([-d[1], d[0]]) / np.linalg.norm(d) * r
    strip = np.array([p1+nrm, p2+nrm, p2-nrm, p1-nrm])
    ax.add_patch(Poly(strip, closed=True, facecolor=SHAPE_FACE, edgecolor=RED,
                      linewidth=1.6, zorder=3))
    ax.set_title(titles[2] + "  ->  AABB", fontsize=13, weight="bold")

    for ax in axes:
        ax.set_xlim(-0.3, 7.4); ax.set_ylim(-0.5, 3.2)
        ax.set_aspect("equal"); clean_ax(ax)

    fig.suptitle("Any shape fits inside one axis-aligned box (AABB)",
                 fontsize=13.5, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p3_09-01-shapes-to-aabb.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 2
def fig_aabb_overlap_axis():
    """Three cases of two AABBs: x-separated, y-separated, both overlap."""
    fig, axes = plt.subplots(1, 3, figsize=(14.0, 5.2))

    cases = [
        ("x-axis separates -> NO overlap", "separated on x"),
        ("y-axis separates -> NO overlap", "separated on y"),
        ("both axes overlap -> INTERSECT", "both overlap"),
    ]

    # case 1: A=(0.3,0.6)-(2.0,2.2)  B=(2.6,0.8)-(4.3,2.4)   (x gap)
    # case 2: A=(0.3,1.6)-(2.4,2.8)  B=(0.6,0.2)-(2.8,1.0)   (y gap)
    # case 3: A=(0.6,0.7)-(2.6,2.5)  B=(1.8,1.4)-(3.8,3.0)   (both overlap)
    AABBs = [
        (((0.3, 0.6), (2.0, 2.2)), ((2.6, 0.8), (4.3, 2.4))),
        (((0.3, 1.6), (2.4, 2.8)), ((0.6, 0.2), (2.8, 1.0))),
        (((0.6, 0.7), (2.6, 2.5)), ((1.8, 1.4), (3.8, 3.0))),
    ]

    for ax, (A, B), (title, _) in zip(axes, AABBs, cases):
        aabb_rect(ax, A[0], A[1], fc=(0.30, 0.55, 0.90, 0.20), ec=AABB_EDGE)
        aabb_rect(ax, B[0], B[1], fc=(0.95, 0.55, 0.55, 0.22), ec=RED)
        # labels
        ax.text((A[0][0]+A[1][0])/2, A[1][1]+0.12, "A", ha="center",
                fontsize=12, weight="bold", color=AABB_EDGE)
        ax.text((B[0][0]+B[1][0])/2, B[1][1]+0.12, "B", ha="center",
                fontsize=12, weight="bold", color=RED)
        ax.set_title(title, fontsize=11.5, weight="bold")
        ax.set_xlim(-0.2, 4.6); ax.set_ylim(-0.2, 3.4)
        ax.set_aspect("equal"); clean_ax(ax)

    # Add axis-comparison annotations under each subplot
    # case 1: B.lowerX > A.upperX  (the x test alone says NO)
    axes[0].annotate("", xy=(2.0, 0.35), xytext=(2.6, 0.35),
                     arrowprops=dict(arrowstyle="<->", color=AMBER, lw=1.6))
    axes[0].text(2.3, 0.18, "gap", ha="center", fontsize=10, color=AMBER)
    axes[0].text(2.3, -0.05,
                 "B.lowerX > A.upperX  =>  separated on x\n(only need 1 comparison!)",
                 ha="center", fontsize=10, color=INK)

    # case 2: B.upperY < A.lowerY
    axes[1].annotate("", xy=(1.5, 1.6), xytext=(1.5, 1.0),
                     arrowprops=dict(arrowstyle="<->", color=AMBER, lw=1.6))
    axes[1].text(1.7, 1.3, "gap", fontsize=10, color=AMBER, va="center")
    axes[1].text(1.6, -0.05,
                 "A.lowerY > B.upperY  =>  separated on y\n(only need 1 comparison!)",
                 ha="center", fontsize=10, color=INK)

    # case 3: both overlap -> intersect
    # highlight overlap region
    ox0 = max(AABBs[2][0][0][0], AABBs[2][1][0][0])
    oy0 = max(AABBs[2][0][0][1], AABBs[2][1][0][1])
    ox1 = min(AABBs[2][0][1][0], AABBs[2][1][1][0])
    oy1 = min(AABBs[2][0][1][1], AABBs[2][1][1][1])
    axes[2].add_patch(Rectangle((ox0, oy0), ox1-ox0, oy1-oy0,
                                facecolor=(0.20, 0.70, 0.32, 0.30),
                                edgecolor=GREEN, linewidth=1.6,
                                hatch="////", zorder=5))
    axes[2].text((ox0+ox1)/2, (oy0+oy1)/2, "overlap",
                 ha="center", va="center", fontsize=10, weight="bold",
                 color=GREEN, zorder=6)
    axes[2].text(2.2, -0.05,
                 "no axis separates  =>  INTERSECT\n(all 4 comparisons fail to separate)",
                 ha="center", fontsize=10, color=INK)

    fig.suptitle("AABB intersection: per-axis lower/upper test (2D = 4 comparisons)",
                 fontsize=13.5, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p3_09-02-aabb-overlap-axes.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 3
def fig_fat_aabb():
    """Fat AABB: a moving shape's tight AABB is buffered outward.
    Left: shape + tight AABB. Middle: buffered fat AABB (margin m).
    Right: shape drifts; as long as it stays inside fat AABB, tree NOT updated."""
    fig, axes = plt.subplots(1, 3, figsize=(14.0, 5.4))

    # ---- panel 1: tight AABB only, shape moves each frame -> update every frame
    ax = axes[0]
    ax.text(2.6, 4.5, "Naive: store TIGHT AABB", ha="center", fontsize=12.5,
            weight="bold", color=RED)
    centers = [(1.2, 1.5), (1.7, 1.6), (2.2, 1.4), (2.7, 1.7), (3.2, 1.5)]
    r = 0.55
    cols = [(0.5,0.5,0.5,0.25)]*5
    for (cx, cy), c in zip(centers, cols):
        aabb_rect(ax, (cx-r, cy-r), (cx+r, cy+r),
                  fc=(0.86,0.40,0.40,0.18), ec=RED, lw=1.6)
        ax.add_patch(MplCircle((cx, cy), r*0.8, facecolor=c,
                               edgecolor=INK, linewidth=1.0))
    arrow(ax, (1.2, 0.7), (3.2, 0.7), color=INK, lw=1.4)
    ax.text(2.2, 0.4, "shape drifts each frame", ha="center", fontsize=10.5)
    ax.text(2.6, 0.05, "=> tree updated EVERY frame (expensive!)",
            ha="center", fontsize=11, color=RED, weight="bold")
    ax.set_xlim(0, 5.2); ax.set_ylim(-0.4, 5.0); ax.set_aspect("equal"); clean_ax(ax)

    # ---- panel 2: fat AABB = tight AABB + margin m
    ax = axes[1]
    ax.text(2.6, 4.5, "Fat AABB = tight + margin m", ha="center",
            fontsize=12.5, weight="bold", color=GREEN)
    cx, cy = 2.6, 2.0
    r = 0.55
    m = 0.9
    # fat AABB (outer, dashed)
    aabb_rect(ax, (cx-r-m, cy-r-m), (cx+r+m, cy+r+m),
              fc=(0.20,0.70,0.32,0.10), ec=GREEN, lw=2.0)
    # tight AABB (inner, solid)
    aabb_rect(ax, (cx-r, cy-r), (cx+r, cy+r),
              fc=(0.30,0.55,0.90,0.18), ec=AABB_EDGE, lw=1.8)
    ax.add_patch(MplCircle((cx, cy), r*0.8, facecolor=SHAPE_FACE,
                           edgecolor=INK, linewidth=1.2))
    # margin annotation
    ax.annotate("", xy=(cx-r-m, cy-r-0.55), xytext=(cx-r, cy-r-0.55),
                arrowprops=dict(arrowstyle="<->", color=AMBER, lw=1.6))
    ax.text(cx-r-m/2, cy-r-0.85, "margin m", ha="center", fontsize=10.5,
            color=AMBER)
    ax.text(cx, cy-r-m-1.15, "fat AABB (stored in tree)\ntight AABB (recomputed)",
            ha="center", fontsize=10, color=INK)
    ax.set_xlim(0, 5.2); ax.set_ylim(-0.4, 5.0); ax.set_aspect("equal"); clean_ax(ax)

    # ---- panel 3: shape drifts INSIDE fat AABB -> no tree update
    ax = axes[2]
    ax.text(2.6, 4.5, "Drift inside fat box -> NO tree update",
            ha="center", fontsize=12.5, weight="bold", color=GREEN)
    # fat AABB fixed (same as panel 2)
    aabb_rect(ax, (cx-r-m, cy-r-m), (cx+r+m, cy+r+m),
              fc=(0.20,0.70,0.32,0.10), ec=GREEN, lw=2.0)
    # several shape positions all INSIDE
    drifts = [(2.1, 1.7), (2.4, 2.1), (2.8, 1.85), (3.1, 2.15), (2.0, 2.25)]
    for (dx, dy) in drifts:
        ax.add_patch(MplCircle((dx, dy), r*0.7, facecolor=(0.5,0.5,0.5,0.30),
                               edgecolor=INK, linewidth=0.9))
    ax.text(2.6, 0.9, "tight AABB still INSIDE fat AABB\n=> tree NOT touched",
            ha="center", fontsize=10.5, color=GREEN)
    ax.text(2.6, 0.25, "only when tight box EXITS fat box\n=> recompute fat, move one proxy",
            ha="center", fontsize=10, color=INK)
    ax.set_xlim(0, 5.2); ax.set_ylim(-0.4, 5.0); ax.set_aspect("equal"); clean_ax(ax)

    fig.suptitle("Fat AABB: a buffer that absorbs small motion, sparing tree updates",
                 fontsize=13.5, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p3_09-03-fat-aabb.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 4
def fig_3d_analogy_projection():
    """2D per-axis test as orthogonal projections; hint at 3D (3 axes)."""
    fig, axes = plt.subplots(1, 2, figsize=(12.6, 5.6))

    # Left: 2D two AABBs, project onto x-axis and y-axis bars
    ax = axes[0]
    A = ((1.0, 1.4), (3.2, 3.0)); B = ((2.6, 2.2), (4.6, 3.8))
    aabb_rect(ax, A[0], A[1], fc=(0.30,0.55,0.90,0.22), ec=AABB_EDGE)
    aabb_rect(ax, B[0], B[1], fc=(0.95,0.55,0.55,0.22), ec=RED)
    ox0 = max(A[0][0], B[0][0]); ox1 = min(A[1][0], B[1][0])
    oy0 = max(A[0][1], B[0][1]); oy1 = min(A[1][1], B[1][1])
    ax.add_patch(Rectangle((ox0, oy0), ox1-ox0, oy1-oy0,
                           facecolor=(0.20,0.70,0.32,0.30), edgecolor=GREEN,
                           linewidth=1.6, hatch="////"))
    # x-axis projection bar at bottom
    ax.add_patch(Rectangle((A[0][0], 0.2), A[1][0]-A[0][0], 0.3,
                           facecolor=(0.30,0.55,0.90,0.5), edgecolor=AABB_EDGE))
    ax.add_patch(Rectangle((B[0][0], 0.2), B[1][0]-B[0][0], 0.3,
                           facecolor=(0.95,0.55,0.55,0.5), edgecolor=RED))
    ax.text(2.8, 0.05, "project on x-axis: overlap", ha="center",
            fontsize=10.5, color=GREEN, weight="bold")
    # y-axis projection bar on left
    ax.add_patch(Rectangle((0.15, A[0][1]), 0.3, A[1][1]-A[0][1],
                           facecolor=(0.30,0.55,0.90,0.5), edgecolor=AABB_EDGE))
    ax.add_patch(Rectangle((0.15, B[0][1]), 0.3, B[1][1]-B[0][1],
                           facecolor=(0.95,0.55,0.55,0.5), edgecolor=RED))
    ax.text(0.0, 2.6, "project\non y-axis:\noverlap", ha="center",
            fontsize=10.5, color=GREEN, weight="bold", rotation=90, va="center")
    ax.text(2.8, 4.1, "both projections overlap -> INTERSECT",
            ha="center", fontsize=11.5, weight="bold", color=GREEN)
    ax.set_xlim(-0.6, 5.2); ax.set_ylim(-0.2, 4.4); ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("2D: project onto x and y axes (2 intervals)", fontsize=12.5,
                 weight="bold")

    # Right: 3D analogy -- 3 perpendicular axes
    ax = axes[1]
    ax.set_axis_off()
    ax.text(0.5, 0.93, "3D extension: 3 axes (x, y, z) => 6 comparisons",
            ha="center", fontsize=12.5, weight="bold", transform=ax.transAxes)
    # three interval bars stacked
    axes_labels = ["x-axis", "y-axis", "z-axis"]
    y_positions = [0.66, 0.42, 0.18]
    # box A intervals and box B intervals per axis (illustrative, overlapping)
    A_iv = [(0.10, 0.50), (0.08, 0.46), (0.14, 0.44)]
    B_iv = [(0.38, 0.82), (0.34, 0.70), (0.30, 0.62)]
    for i, (yl, alab) in enumerate(zip(y_positions, axes_labels)):
        a0, a1 = A_iv[i]; b0, b1 = B_iv[i]
        ax.add_patch(Rectangle((a0, yl), a1-a0, 0.06, transform=ax.transAxes,
                               facecolor=(0.30,0.55,0.90,0.6), edgecolor=AABB_EDGE))
        ax.add_patch(Rectangle((b0, yl), b1-b0, 0.06, transform=ax.transAxes,
                               facecolor=(0.95,0.55,0.55,0.6), edgecolor=RED))
        # overlap hatch
        o0, o1 = max(a0, b0), min(a1, b1)
        ax.add_patch(Rectangle((o0, yl), o1-o0, 0.06, transform=ax.transAxes,
                               facecolor="none", edgecolor=GREEN, lw=1.8,
                               hatch="////"))
        ax.text(0.02, yl+0.03, alab, transform=ax.transAxes, fontsize=11,
                weight="bold", va="center")
        ax.text(0.90, yl+0.03, "overlap", transform=ax.transAxes, fontsize=10,
                color=GREEN, va="center")
    ax.text(0.5, 0.05, "all 3 axes overlap -> boxes intersect\n(any one axis separates -> miss)",
            ha="center", fontsize=10.5, transform=ax.transAxes, color=INK)
    ax.set_title("3D: 3 intervals, same per-axis rule", fontsize=12.5,
                 weight="bold")

    fig.savefig(os.path.join(OUT, "fig-p3_09-04-projection-3d.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_shapes_to_aabb()
    fig_aabb_overlap_axis()
    fig_fat_aabb()
    fig_3d_analogy_projection()
    print("done:", sorted(f for f in os.listdir(OUT) if f.startswith("fig-p3_09")))
