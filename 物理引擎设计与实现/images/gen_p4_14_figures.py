# -*- coding: utf-8 -*-
"""Generate PNG figures for the physics-engine book, chapter P4-14
(Contact Manifold: normal, penetration, contact points).

This script is the SOLE owner of fig-p4_14-* images. Do not touch other
gen_p*.py files.

Run:  python gen_p4_14_figures.py

Outputs (into this directory):
  fig-p4_14-01-manifold-anatomy.png    A two-polygon contact showing normal,
                                       penetration depth, and 1..2 contact
                                       points (the geometry a manifold stores)
  fig-p4_14-02-clipping.png            Reference edge + incident edge clipping:
                                       how two contact points are produced
                                       by cutting the incident edge against
                                       the side planes of the reference edge
  fig-p4_14-03-one-vs-two-points.png   Why 2 contact points beat 1 for a
                                       resting box: single point tips over,
                                       two points make a stable support line
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import (FancyArrowPatch, Circle, Rectangle,
                                Polygon as MplPolygon, FancyBboxPatch)
from matplotlib.lines import Line2D

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

RED    = (0.86, 0.18, 0.18)
GREEN  = (0.20, 0.70, 0.32)
BLUE   = (0.20, 0.45, 0.88)
AMBER  = (0.95, 0.70, 0.18)
PURPLE = (0.55, 0.30, 0.80)
TEAL   = (0.13, 0.62, 0.62)
GREY   = (0.78, 0.78, 0.80)
INK    = (0.12, 0.12, 0.12)
SOFTA  = (0.90, 0.94, 1.00)   # light blue fill for polygon A
SOFTB  = (1.00, 0.92, 0.90)   # light orange fill for polygon B


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_aspect("equal")


def arrow(ax, p1, p2, color=INK, lw=1.8, rad=0.0, mut=18):
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=f"arc3,rad={rad}",
                                  mutation_scale=mut))


def label(ax, p, text, color=INK, fs=12.5, dx=0.0, dy=0.0, ha="center", va="center",
          bbox=False):
    ax.text(p[0] + dx, p[1] + dy, text, color=color, fontsize=fs,
            ha=ha, va=va, zorder=6,
            bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="none", alpha=0.85) if bbox else None)


# ========================================================================
#  Figure 1: Manifold anatomy (normal + penetration + contact points)
# ========================================================================
def fig_p4_14_01_manifold_anatomy():
    fig, ax = plt.subplots(figsize=(11.6, 7.2))

    # Polygon A: a flat box resting on polygon B, slightly rotated
    # A sits above-left, B sits below-right. They overlap at the bottom-right
    # corner of A meeting the top edge of B.
    th = np.deg2rad(-9.0)
    ca, sa = np.cos(th), np.sin(th)
    Rm = np.array([[ca, -sa], [sa, ca]])

    def box(cx, cy, w, h):
        return np.array([[cx - w/2, cy - h/2], [cx + w/2, cy - h/2],
                         [cx + w/2, cy + h/2], [cx - w/2, cy + h/2]])

    A = (box(4.6, 4.55, 4.2, 1.5) @ Rm.T)
    B = box(5.6, 2.55, 6.0, 1.6)

    # box() returns [BL, BR, TR, TL] in CCW order.
    # B's top edge is TR -> TL = B[2] -> B[3]; outward normal points up (+y).
    top_y = B[2][1]
    bx_lo, bx_hi = B[3][0], B[2][0]   # left..right along the top edge
    # Reference edge endpoints (left to right along B top)
    bL = np.array([bx_lo, top_y])
    bR = np.array([bx_hi, top_y])

    # A's bottom edge (lowest edge after rotation) is A[0] -> A[1] (CCW lower edge).
    # Clip it against the horizontal line y = top_y -> gives the overlap window in x.
    P, Q = A[0], A[1]
    # Parametric A-bottom: point(s) below top_y stay inside the overlap.
    def ycross(p, q, y):
        t = (y - p[1]) / (q[1] - p[1])
        return p + t * (q - p)
    # A[0]=(3.06,3.36) just above top, A[1]=(7.21,2.705) below top.
    crossings = []
    if (P[1] - top_y) * (Q[1] - top_y) < 0:
        crossings.append(ycross(P, Q, top_y))
    # Endpoints of A-bottom that are AT or BELOW top_y are inside the overlap window.
    for V in (P, Q):
        if V[1] <= top_y + 1e-6:
            crossings.append(V)
    xs = sorted([c[0] for c in crossings])
    cL = np.array([np.clip(xs[0], bx_lo, bx_hi), top_y])
    cR = np.array([np.clip(xs[-1], bx_lo, bx_hi), top_y])
    b1, b2 = bL, bR   # alias for the reference-edge highlight below

    # Draw polygons
    ax.add_patch(MplPolygon(A, closed=True, facecolor=SOFTA, edgecolor=BLUE,
                            linewidth=2.2, zorder=3))
    ax.add_patch(MplPolygon(B, closed=True, facecolor=SOFTB, edgecolor=AMBER,
                            linewidth=2.2, zorder=3))
    label(ax, A.mean(axis=0), "polygon A", color=BLUE, fs=15)
    label(ax, B.mean(axis=0), "polygon B", color=(0.74, 0.45, 0.05), fs=15)

    # Contact normal: from A to B, perpendicular to reference edge -> downward
    n = np.array([0.0, -1.0])
    # Midpoint of the two contact points
    mid = 0.5 * (cL + cR)
    # Normal arrow
    n_start = mid + np.array([0.0, 0.55])
    n_end   = mid + np.array([0.0, -0.55])
    arrow(ax, n_start, n_end, color=RED, lw=2.6, mut=22)
    label(ax, n_start + np.array([0.18, 0.30]), "contact normal n", color=RED,
          fs=13, ha="left", bbox=True)

    # Penetration depth: how far A's lowest corner sinks below B's top edge.
    # Draw it on the right side of the contact window, as a double-headed
    # arrow between the A corner and the B top.
    a_lowest = A[np.argmin(A[:, 1])]
    depth_x = cR[0] + 0.75
    depth_top = np.array([depth_x, top_y])               # B top
    depth_bot = np.array([depth_x, a_lowest[1]])          # A's lowest corner
    ax.add_artist(FancyArrowPatch(depth_top, depth_bot,
                                  arrowstyle="<|-|>", color=PURPLE, lw=2.6,
                                  mutation_scale=18))
    # faint guide lines from the actual geometry to the arrow
    ax.add_line(Line2D([top_y and cR[0], depth_x], [top_y, top_y],
                       color=PURPLE, lw=0.9, ls=(0,(2,3)), alpha=0.55))
    ax.add_line(Line2D([a_lowest[0], depth_x],
                       [a_lowest[1], a_lowest[1]],
                       color=PURPLE, lw=0.9, ls=(0,(2,3)), alpha=0.55))
    label(ax, np.array([depth_x + 0.18, (top_y + a_lowest[1]) / 2]),
          "penetration\ndepth d", color=PURPLE, fs=12.5, ha="left", va="center",
          bbox=True)

    # Contact points
    for P, name in [(cL, "contact point 1"), (cR, "contact point 2")]:
        ax.add_patch(Circle(P, 0.10, facecolor=RED, edgecolor="white", lw=1.6, zorder=7))
    label(ax, cL + np.array([-0.05, 0.30]), "contact point 1", color=RED, fs=12.5,
          ha="right", bbox=True)
    label(ax, cR + np.array([ 0.05, -0.32]), "contact point 2", color=RED, fs=12.5,
          ha="left", bbox=True)

    # Reference edge highlight (top edge of B)
    ax.add_line(Line2D([b1[0], b2[0]], [b1[1], b2[1]], color=(0.74,0.45,0.05),
                       lw=4.0, alpha=0.45, zorder=4))
    label(ax, (b1 + b2)/2 + np.array([0.0, -0.55]), "reference edge (B top)",
          color=(0.74, 0.45, 0.05), fs=12, bbox=True)

    # A small legend box describing what the manifold stores
    legend = (
        "b2LocalManifold stores:\n"
        "  normal          = n  (A -> B)\n"
        "  points[0].point = contact 1\n"
        "  points[0].separation = -d\n"
        "  points[1].point = contact 2\n"
        "  points[1].separation = -d\n"
        "  pointCount      = 2"
    )
    ax.text(0.4, 7.2, legend, fontsize=11.5, family="monospace",
            va="top", ha="left", color=INK,
            bbox=dict(boxstyle="round,pad=0.5", fc="#f6f6fa", ec=GREY, lw=1.2))

    ax.set_xlim(0, 11.6); ax.set_ylim(0.8, 7.8)
    clean_ax(ax)
    ax.set_title("Contact manifold anatomy: normal + penetration + contact points",
                 fontsize=14, pad=12)
    fig.savefig(os.path.join(OUT, "fig-p4_14-01-manifold-anatomy.png"))
    plt.close(fig)


# ========================================================================
#  Figure 2: Reference edge + incident edge clipping -> 2 contact points
# ========================================================================
def fig_p4_14_02_clipping():
    fig, axes = plt.subplots(1, 3, figsize=(15.6, 6.2))

    # ---- Common geometry ----
    # Polygon A (reference): wide flat box at the bottom
    A = np.array([[1.0, 1.0], [7.0, 1.0], [7.0, 2.4], [1.0, 2.4]])
    # Polygon B (incident): a tilted box above, overlapping the top of A
    th = np.deg2rad(18)
    ca, sa = np.cos(th), np.sin(th)
    Rm = np.array([[ca, -sa], [sa, ca]])
    def box(cx, cy, w, h):
        return np.array([[cx - w/2, cy - h/2], [cx + w/2, cy - h/2],
                         [cx + w/2, cy + h/2], [cx - w/2, cy + h/2]])
    Bcenter = np.array([4.6, 3.4])
    B = (box(0, 0, 3.4, 1.4) @ Rm.T) + Bcenter

    # Reference edge: top edge of A = A[3] -> A[2]  (CCW so outward normal = +y)
    ref1, ref2 = A[3], A[2]
    ref_normal = np.array([0.0, 1.0])
    # Incident edge: edge of B whose normal is most anti-parallel to ref_normal
    # i.e. smallest dot(normal, +y). Edges of B (CCW): i -> i+1
    edges = [(i, (i + 1) % 4) for i in range(4)]
    edge_normals = []
    for (i, j) in edges:
        e = B[j] - B[i]
        n = np.array([e[1], -e[0]]); n = n / np.linalg.norm(n)  # left normal = outward for CCW
        edge_normals.append(n)
    dots = [np.dot(n, ref_normal) for n in edge_normals]
    inc_idx = int(np.argmin(dots))
    inc1, inc2 = B[inc_idx], B[(inc_idx + 1) % 4]

    # ---- Panel 1: reference + incident edges identified ----
    ax = axes[0]
    ax.add_patch(MplPolygon(A, closed=True, facecolor=SOFTA, edgecolor=BLUE, lw=2.0, zorder=2))
    ax.add_patch(MplPolygon(B, closed=True, facecolor=SOFTB, edgecolor=AMBER, lw=2.0, zorder=2))
    # highlight reference edge
    ax.add_line(Line2D([ref1[0], ref2[0]], [ref1[1], ref2[1]], color=BLUE, lw=5.0, alpha=0.5, zorder=3))
    # highlight incident edge
    ax.add_line(Line2D([inc1[0], inc2[0]], [inc1[1], inc2[1]], color=AMBER, lw=5.0, alpha=0.6, zorder=3))
    label(ax, ref1 + np.array([0.0, -0.45]), "reference edge\n(min separating axis edge)",
          color=BLUE, fs=11.5, bbox=True)
    label(ax, 0.5*(inc1 + inc2) + np.array([0.45, 0.30]), "incident edge\n(most anti-parallel normal)",
          color=(0.74,0.45,0.05), fs=11.5, ha="left", bbox=True)
    label(ax, A.mean(axis=0) + np.array([0, -0.55]), "A (reference)", color=BLUE, fs=12)
    label(ax, B.mean(axis=0) + np.array([-0.1, 0.65]), "B (incident)", color=(0.74,0.45,0.05), fs=12)
    ax.set_title("Step 1: pick reference & incident edge", fontsize=12.5)
    ax.set_xlim(0, 8.5); ax.set_ylim(0, 5.6); clean_ax(ax)

    # ---- Panel 2: clip incident edge to reference edge's side planes ----
    ax = axes[1]
    ax.add_patch(MplPolygon(A, closed=True, facecolor=SOFTA, edgecolor=BLUE, lw=2.0, zorder=2))
    ax.add_patch(MplPolygon(B, closed=True, facecolor=SOFTB, edgecolor=AMBER, lw=2.0, zorder=2))
    ax.add_line(Line2D([ref1[0], ref2[0]], [ref1[1], ref2[1]], color=BLUE, lw=4.0, alpha=0.4, zorder=3))
    ax.add_line(Line2D([inc1[0], inc2[0]], [inc1[1], inc2[1]], color=AMBER, lw=4.0, alpha=0.55, zorder=3))

    # side planes at ref1 and ref2 (vertical lines along tangent)
    tangent = ref2 - ref1; tangent = tangent / np.linalg.norm(tangent)
    for endp, side_label in [(ref1, "side plane 1"), (ref2, "side plane 2")]:
        ax.add_line(Line2D([endp[0], endp[0]], [0.4, 4.8], color=PURPLE, lw=1.4,
                           ls=(0, (5, 4)), alpha=0.7, zorder=2))
        label(ax, endp + np.array([0.0, 1.7]), side_label, color=PURPLE, fs=11, bbox=True)

    # Mark the portions of the incident edge BEFORE clipping (faint full segment)
    # and the kept portion AFTER clipping.
    # Compute clip: keep points of incident edge whose tangent-coordinate is in [0, len(ref)]
    L0 = 0.0
    L1 = np.dot(ref2 - ref1, tangent)
    t1 = np.dot(inc1 - ref1, tangent)
    t2 = np.dot(inc2 - ref1, tangent)
    # Parametric form along incident: P(s) = inc2 + s*(inc1 - inc2), s in [0,1]
    # (we order so that s=0 -> inc2, s=1 -> inc1 just for nicer numbering)
    def along_inc(s):
        return inc2 + s * (inc1 - inc2)
    def tcoord(P):
        return np.dot(P - ref1, tangent)
    ts1, ts2 = tcoord(inc2), tcoord(inc1)
    # Clip to [L0, L1]
    s_lo = max(0.0, (L0 - ts2) / (ts1 - ts2)) if ts1 != ts2 else 0.0
    s_hi = min(1.0, (L1 - ts2) / (ts1 - ts2)) if ts1 != ts2 else 1.0
    clip_lo = along_inc(s_lo)
    clip_hi = along_inc(s_hi)

    # discarded tails (drawn faded)
    if s_lo > 0:
        ax.add_line(Line2D([inc2[0], clip_lo[0]], [inc2[1], clip_lo[1]],
                           color=AMBER, lw=2.0, alpha=0.30, zorder=3))
    if s_hi < 1:
        ax.add_line(Line2D([clip_hi[0], inc1[0]], [clip_hi[1], inc1[1]],
                           color=AMBER, lw=2.0, alpha=0.30, zorder=3))
    # kept segment
    ax.add_line(Line2D([clip_lo[0], clip_hi[0]], [clip_lo[1], clip_hi[1]],
                       color=AMBER, lw=5.0, alpha=0.9, zorder=4))
    for P, nm in [(inc2, "v21"), (inc1, "v22")]:
        ax.add_patch(Circle(P, 0.085, facecolor="white", edgecolor=AMBER, lw=2.0, zorder=6))
        label(ax, P + np.array([0.18, 0.0]), nm, color=(0.74,0.45,0.05), fs=11, ha="left")

    ax.set_title("Step 2: clip incident edge to side planes", fontsize=12.5)
    ax.set_xlim(0, 8.5); ax.set_ylim(0, 5.6); clean_ax(ax)

    # ---- Panel 3: project kept endpoints onto reference edge -> 2 contact pts ----
    ax = axes[2]
    ax.add_patch(MplPolygon(A, closed=True, facecolor=SOFTA, edgecolor=BLUE, lw=2.0, zorder=2))
    ax.add_patch(MplPolygon(B, closed=True, facecolor=SOFTB, edgecolor=AMBER, lw=2.0, zorder=2))
    ax.add_line(Line2D([ref1[0], ref2[0]], [ref1[1], ref2[1]], color=BLUE, lw=4.0, alpha=0.4, zorder=3))
    # contact points = the two clipped endpoints (projected to mid-skin)
    for P, nm in [(clip_lo, "contact 1"), (clip_hi, "contact 2")]:
        proj = np.array([P[0], ref1[1]])   # project onto reference edge line
        ax.add_patch(Circle(P, 0.10, facecolor=RED, edgecolor="white", lw=1.6, zorder=8))
        ax.add_patch(Circle(proj, 0.075, facecolor=GREEN, edgecolor="white", lw=1.4, zorder=7))
        ax.add_line(Line2D([P[0], proj[0]], [P[1], proj[1]], color=GREEN, lw=1.4,
                           ls=(0, (3, 3)), alpha=0.8, zorder=5))
        label(ax, P + np.array([0.15, 0.18]), nm, color=RED, fs=11.5, ha="left", bbox=True)
    # legend dots
    ax.add_patch(Circle([7.4, 5.0], 0.08, facecolor=RED, edgecolor="white", lw=1.4))
    label(ax, np.array([7.55, 5.0]), "clipped point", color=RED, fs=10.5, ha="left")
    ax.add_patch(Circle([7.4, 4.6], 0.065, facecolor=GREEN, edgecolor="white", lw=1.4))
    label(ax, np.array([7.55, 4.6]), "on reference edge", color=GREEN, fs=10.5, ha="left")

    ax.set_title("Step 3: kept endpoints -> up to 2 contact points", fontsize=12.5)
    ax.set_xlim(0, 8.5); ax.set_ylim(0, 5.6); clean_ax(ax)

    fig.suptitle("Reference edge + incident edge clipping: how a 2-point contact manifold is built",
                 fontsize=14, y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p4_14-02-clipping.png"))
    plt.close(fig)


# ========================================================================
#  Figure 3: Why 2 contact points beat 1 (resting box stability)
# ========================================================================
def fig_p4_14_03_one_vs_two_points():
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 6.4))

    # ---- Left: single contact point -> tips over ----
    ax = axes[0]
    ground = np.array([[0.6, 1.0], [9.4, 1.0], [9.4, 0.4], [0.6, 0.4]])
    ax.add_patch(MplPolygon(ground, closed=True, facecolor=SOFTB, edgecolor=AMBER, lw=2.0))
    label(ax, ground.mean(axis=0) + np.array([0, -0.05]), "ground", color=(0.74,0.45,0.05), fs=12)

    # box, tilted about the single contact point
    th = np.deg2rad(22)
    ca, sa = np.cos(th), np.sin(th)
    Rm = np.array([[ca, -sa], [sa, ca]])
    pivot = np.array([3.2, 1.0])
    box_local = np.array([[-1.6, -1.0], [1.6, -1.0], [1.6, 1.0], [-1.6, 1.0]])
    # shift so bottom-left corner sits at pivot
    box_local = box_local - np.array([-1.6, -1.0])
    box_world = (box_local @ Rm.T) + pivot
    ax.add_patch(MplPolygon(box_world, closed=True, facecolor=SOFTA, edgecolor=BLUE, lw=2.0))
    # single contact point at pivot
    ax.add_patch(Circle(pivot, 0.12, facecolor=RED, edgecolor="white", lw=1.6, zorder=8))
    label(ax, pivot + np.array([0.0, -0.30]), "1 contact point", color=RED, fs=12, bbox=True)
    # rotation arrow around pivot
    arc = FancyArrowPatch(pivot + np.array([1.6, 0.2]), pivot + np.array([0.5, 1.7]),
                          arrowstyle="-|>", color=PURPLE, lw=2.0,
                          connectionstyle="arc3,rad=-0.55", mutation_scale=18)
    ax.add_artist(arc)
    label(ax, pivot + np.array([2.6, 1.5]), "tips over!\n(no torque balance)",
          color=PURPLE, fs=12, ha="left", bbox=True)
    label(ax, box_world.mean(axis=0) + np.array([0.6, 0.2]), "box", color=BLUE, fs=12)
    ax.set_title("Single contact point: unstable, box tips", fontsize=12.5)
    ax.set_xlim(0, 10); ax.set_ylim(0, 5); clean_ax(ax)

    # ---- Right: two contact points -> stable support line ----
    ax = axes[1]
    ax.add_patch(MplPolygon(ground, closed=True, facecolor=SOFTB, edgecolor=AMBER, lw=2.0))
    def box_r(cx, cy, w, h):
        return np.array([[cx - w/2, cy - h/2], [cx + w/2, cy - h/2],
                         [cx + w/2, cy + h/2], [cx - w/2, cy + h/2]])
    box_w = box_r(5.0, 2.0, 3.2, 2.0)
    ax.add_patch(MplPolygon(box_w, closed=True, facecolor=SOFTA, edgecolor=BLUE, lw=2.0))
    label(ax, box_w.mean(axis=0), "box", color=BLUE, fs=12)
    c1 = box_w[0]; c2 = box_w[1]
    for P, nm in [(c1, "contact 1"), (c2, "contact 2")]:
        ax.add_patch(Circle(P, 0.12, facecolor=RED, edgecolor="white", lw=1.6, zorder=8))
        label(ax, P + np.array([0, -0.32]), nm, color=RED, fs=12, bbox=True)
    # support line
    ax.add_line(Line2D([c1[0], c2[0]], [c1[1], c2[1]], color=GREEN, lw=3.5, alpha=0.6, zorder=4))
    label(ax, 0.5*(c1 + c2) + np.array([0, 0.45]),
          "support line: 2 points\n-> torque balanced", color=GREEN, fs=12, bbox=True)
    # weight arrow from CoM
    com = box_w.mean(axis=0)
    arrow(ax, com, com + np.array([0, -1.0]), color=INK, lw=2.0, mut=16)
    label(ax, com + np.array([0.15, -0.6]), "weight", color=INK, fs=11, ha="left")

    ax.set_title("Two contact points: stable, supports a stack", fontsize=12.5)
    ax.set_xlim(0, 10); ax.set_ylim(0, 5); clean_ax(ax)

    fig.suptitle("Why 2 contact points are the lifeblood of stable stacking",
                 fontsize=14, y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p4_14-03-one-vs-two-points.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_p4_14_01_manifold_anatomy()
    fig_p4_14_02_clipping()
    fig_p4_14_03_one_vs_two_points()
    print("Wrote fig-p4_14-01..03 PNGs to", OUT)
