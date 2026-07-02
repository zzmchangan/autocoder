# -*- coding: utf-8 -*-
"""Generate PNG figures for chapter P5-18 (Sleeping / CCD / Stability).
Run: python gen_p5_18_figures.py
All in-figure text is English to avoid CJK font fallback issues.
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import (Rectangle, FancyBboxPatch, FancyArrowPatch,
                                Circle, Polygon as MplPolygon, Ellipse, PathPatch)
from matplotlib.path import Path

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
DGREY = (0.45, 0.45, 0.48)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.96, 0.96, 0.97)
WALL  = (0.50, 0.42, 0.34)
CSOFT = (0.92, 0.80, 0.99)   # sleeping
ASOFT = (0.90, 0.95, 0.90)   # awake


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def arrow(ax, p1, p2, color=INK, lw=1.6, rad=0.0, style="-|>"):
    cs = f"arc3,rad={rad}"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle=style, color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=15))


# ---------------------------------------------------------------- fig 1
def fig_tunneling_vs_ccd():
    """Left: discrete sampling misses the wall (tunneling).
       Right: swept shape / TOI catches the collision."""
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(13.5, 6.0))

    # ---------- left: tunneling ----------
    # thin wall
    axL.add_patch(Rectangle((4.5, 0.5), 0.25, 5.5, facecolor=WALL, edgecolor=INK))
    axL.text(4.62, 6.0, "thin wall", ha="center", fontsize=11, color=WALL, weight="bold")
    # sampled positions: t0 (left of wall), t1 (right of wall) - skipped over
    axL.add_patch(Circle((2.0, 3.0), 0.45, facecolor=BLUE, edgecolor=INK))
    axL.text(2.0, 3.7, "t0", ha="center", fontsize=11, color=BLUE, weight="bold")
    axL.add_patch(Circle((7.5, 3.0), 0.45, facecolor=BLUE, edgecolor=INK))
    axL.text(7.5, 3.7, "t1", ha="center", fontsize=11, color=BLUE, weight="bold")
    # the straight path goes through the wall
    arrow(axL, (2.45, 3.0), (7.05, 3.0), color=RED, lw=2.2)
    axL.text(4.62, 4.4, "one step\nleaps across\nthe wall", ha="center",
             fontsize=11, color=RED, weight="bold")
    # big red X over the wall
    axL.plot([4.3, 4.95], [2.4, 3.6], color=RED, lw=3.0)
    axL.plot([4.3, 4.95], [3.6, 2.4], color=RED, lw=3.0)
    axL.text(4.62, 1.4, "discrete samples at t0, t1\n=> no overlap detected\n=> tunneling!",
             ha="center", fontsize=10.5, color=RED)
    axL.set_xlim(0, 9.5); axL.set_ylim(0, 6.8)
    axL.set_aspect("equal"); clean_ax(axL)
    axL.set_title("Without CCD: discrete sampling misses the wall",
                  fontsize=12.5, weight="bold")

    # ---------- right: CCD (swept + TOI) ----------
    axR.add_patch(Rectangle((4.5, 0.5), 0.25, 5.5, facecolor=WALL, edgecolor=INK))
    axR.text(4.62, 6.0, "thin wall", ha="center", fontsize=11, color=WALL, weight="bold")
    # swept volume (capsule) along the path - a faint band
    band = MplPolygon([(2.0, 2.55), (7.5, 2.55), (7.5, 3.45), (2.0, 3.45)],
                      closed=True, facecolor=AMBER, edgecolor=AMBER, alpha=0.30)
    axR.add_patch(band)
    # endpoints of motion
    axR.add_patch(Circle((2.0, 3.0), 0.45, facecolor=BLUE, edgecolor=INK))
    axR.text(1.35, 3.0, "c1", ha="center", fontsize=11, color=BLUE, weight="bold")
    axR.add_patch(Circle((7.5, 3.0), 0.45, facecolor=BLUE, edgecolor=INK, alpha=0.4))
    axR.text(8.15, 3.0, "c2", ha="center", fontsize=11, color=BLUE, weight="bold")
    # TOI: stopped at the wall
    axR.add_patch(Circle((3.95, 3.0), 0.45, facecolor=GREEN, edgecolor=INK, lw=2))
    axR.text(3.95, 3.8, "TOI = 0.39\n(stopped at wall)", ha="center",
             fontsize=10.5, color=GREEN, weight="bold")
    # arrows: c1 -> TOI (green, actual), TOI -> c2 (grey dashed, the would-be remainder)
    arrow(axR, (2.45, 3.0), (3.5, 3.0), color=GREEN, lw=2.2)
    arrow(axR, (4.4, 3.0), (7.05, 3.0), color=GREY, lw=1.4, style="-|>")
    axR.plot([4.45, 7.3], [2.75, 2.75], color=GREY, lw=0.8, linestyle=":")
    axR.text(5.9, 2.35, "remainder discarded\n(or resolved next step)", ha="center",
             fontsize=9.5, color=DGREY)
    axR.text(2.0, 4.6, "swept shape + TOI sweep\nscans the WHOLE path", ha="center",
             fontsize=10.5, color=AMBER, weight="bold")
    axR.text(4.62, 1.3, "collision found at fraction = 0.39\n=> no tunneling",
             ha="center", fontsize=10.5, color=GREEN)
    axR.set_xlim(0, 9.5); axR.set_ylim(0, 6.8)
    axR.set_aspect("equal"); clean_ax(axR)
    axR.set_title("With CCD: swept shape + TOI catches it",
                  fontsize=12.5, weight="bold")

    fig.suptitle("Tunneling vs CCD: a fast ball through a thin wall",
                 fontsize=13.5, weight="bold", y=0.99)
    fig.savefig(os.path.join(OUT, "fig-p5_18-01-tunneling-vs-ccd.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 2
def fig_toi_conservative_advancement():
    """Conservative advancement: iteratively advance t1 while shapes stay
       separated, narrowing down to the time of impact."""
    fig, ax = plt.subplots(figsize=(11.5, 5.6))

    # wall at x = 7
    ax.add_patch(Rectangle((7.0, 0.5), 0.25, 5.0, facecolor=WALL, edgecolor=INK))
    ax.text(7.12, 5.8, "static wall", ha="center", fontsize=11, color=WALL, weight="bold")

    # moving circle radius
    r = 0.5
    # iteration snapshots of t (fraction): t0=0, t1~0.4, t2~0.72, toi~0.86 (illustrative)
    xs = [1.0, 3.0, 4.7, 5.7, 6.2]
    ts = ["t = 0", "iter 1", "iter 2", "iter 3", "TOI"]
    cols = [BLUE, AMBER, AMBER, AMBER, GREEN]
    alphas = [0.95, 0.30, 0.45, 0.7, 1.0]
    for x, t, c, a in zip(xs, ts, cols, alphas):
        ax.add_patch(Circle((x, 3.0), r, facecolor=c, edgecolor=INK, alpha=a, lw=1.4))
        ax.text(x, 3.95, t, ha="center", fontsize=9.5, color=c, weight="bold")

    # distance lines at each snapshot (to wall left face x=7)
    for x, c in zip(xs[:-1], cols[:-1]):
        ax.plot([x + r, 7.0], [3.0, 3.0], color=c, lw=1.0, linestyle="--", alpha=0.7)

    # velocity arrow (overall motion)
    arrow(ax, (1.6, 1.7), (6.4, 1.7), color=INK, lw=2.0)
    ax.text(4.0, 1.3, "velocity v (sweep c1 -> c2)", ha="center", fontsize=11, color=INK)

    # legend / explanation
    ax.text(4.0, 5.0,
            "conservative advancement:\nat each t, measure distance d to the wall,\n"
            "advance by  dt <= d / |v_rel|  (never overshoot),\n"
            "repeat until d <= target  =>  that t is the TOI",
            ha="center", fontsize=10, color=INK,
            bbox=dict(boxstyle="round,pad=0.4", fc=SOFT, ec=GREY))

    ax.set_xlim(0, 8.5); ax.set_ylim(0, 6.3)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("b2TimeOfImpact: conservative advancement narrows down the impact time",
                 fontsize=12, weight="bold")
    fig.savefig(os.path.join(OUT, "fig-p5_18-02-toi-conservative-advancement.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 3
def fig_speculative_contact():
    """Speculative contact: the AABB is inflated by speculativeDistance; a contact
       point with separation in (0, dist] still generates a bias that decelerates
       the body BEFORE it overlaps."""
    fig, ax = plt.subplots(figsize=(11.0, 5.8))

    # ground box
    ax.add_patch(Rectangle((0.5, 0.5), 10.0, 0.9, facecolor=WALL, edgecolor=INK))
    ax.text(5.5, 0.95, "static ground", ha="center", fontsize=11, color="white", weight="bold")

    # falling ball, two scenarios side by side
    # left: NO speculative (only real overlap handled)
    bx_l = 3.2
    ax.add_patch(Circle((bx_l, 3.4), 0.55, facecolor=RED, edgecolor=INK))
    arrow(ax, (bx_l + 0.9, 4.6), (bx_l + 0.9, 3.0), color=RED, lw=2.0)
    ax.text(bx_l, 4.5, "no speculative:\nonly handled once\nit actually overlaps",
            ha="center", fontsize=9.5, color=RED)
    # show a gap with a dashed line = the "danger zone" not anticipated
    ax.plot([bx_l - 0.55, bx_l + 0.55], [2.85, 2.85], color=RED, lw=1.2, linestyle=":")
    ax.text(bx_l, 2.2, "gap = invisible\nuntil overlap", ha="center", fontsize=9, color=RED)

    # right: WITH speculative -> inflated AABB (faint) + bias decelerates early
    bx_r = 7.8
    # inflated AABB around the ball
    ax.add_patch(Rectangle((bx_r - 0.8, 2.5), 1.6, 1.8,
                           facecolor=AMBER, edgecolor=AMBER, alpha=0.22))
    ax.add_patch(Rectangle((bx_r - 0.8, 2.5), 1.6, 1.8,
                           facecolor="none", edgecolor=AMBER, lw=1.4, linestyle="--"))
    ax.text(bx_r + 1.5, 3.4, "inflated AABB\n(+ speculativeDistance)",
            fontsize=9.5, color=AMBER, va="center")
    ax.add_patch(Circle((bx_r, 3.4), 0.55, facecolor=GREEN, edgecolor=INK))
    arrow(ax, (bx_r + 0.9, 4.6), (bx_r + 0.9, 3.6), color=GREEN, lw=2.0)
    arrow(ax, (bx_r + 0.9, 3.55), (bx_r + 0.9, 3.05), color=AMBER, lw=1.8)
    ax.text(bx_r + 1.4, 3.95, "specBias = s * inv_h\nbrakes early", fontsize=9.5,
            color=AMBER, va="center")
    ax.text(bx_r, 4.7, "speculative:\nbias decelerates BEFORE\nthe real overlap",
            ha="center", fontsize=9.5, color=GREEN, weight="bold")
    # speculative margin arrow on the ground side
    ax.annotate("", xy=(bx_r - 1.3, 1.4), xytext=(bx_r - 1.3, 2.85),
                arrowprops=dict(arrowstyle="<->", color=AMBER, lw=1.4))
    ax.text(bx_r - 1.7, 2.1, "speculative\nmargin", fontsize=9, color=AMBER, ha="center")

    ax.set_xlim(0, 12); ax.set_ylim(0, 6.2)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("Speculative contact: anticipate the collision, brake before overlap",
                 fontsize=12, weight="bold")
    fig.savefig(os.path.join(OUT, "fig-p5_18-03-speculative-contact.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 4
def fig_sleep_state_machine():
    """The sleep / wake cycle: awake -> (low motion, accumulates sleepTime)
       -> island sleeps -> (disturbance) -> wake. Solver-set migration arrows."""
    fig, ax = plt.subplots(figsize=(11.5, 6.0))

    def rbox(cx, cy, w, h, text, fc, ec, fs=12, tc=INK, weight="bold"):
        ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                                    boxstyle="round,pad=0.04,rounding_size=0.12",
                                    facecolor=fc, edgecolor=ec, linewidth=2.0))
        ax.text(cx, cy, text, ha="center", va="center",
                fontsize=fs, color=tc, weight=weight)

    # AWAKE state
    rbox(2.5, 4.2, 3.4, 1.7, "AWAKE\n(in b2_awakeSet)\n- solved every step\n- sleepTime accumulates",
         ASOFT, GREEN, fs=10.5, tc=GREEN)
    # SLEEPING state
    rbox(9.0, 4.2, 3.4, 1.7, "SLEEPING\n(sleeping solver set)\n- NOT solved\n- frozen velocities",
         CSOFT, (0.62, 0.24, 0.72), fs=10.5, tc=(0.62, 0.24, 0.72))

    # transition: awake -> sleeping (island goes to sleep)
    arrow(ax, (4.4, 4.5), (7.1, 4.5), color=(0.62, 0.24, 0.72), lw=2.2)
    ax.text(5.75, 5.0, "island low-motion\nfor B2_TIME_TO_SLEEP (0.5s)\n=> b2TrySleepIsland()\nmoves whole island",
            ha="center", fontsize=9.5, color=(0.62, 0.24, 0.72))

    # transition: sleeping -> awake (wake)
    arrow(ax, (7.1, 3.9), (4.4, 3.9), color=GREEN, lw=2.2, rad=-0.15)
    ax.text(5.75, 3.15, "disturbance\n(new contact / force /\nadjacent body wakes)\n=> b2WakeSolverSet()",
            ha="center", fontsize=9.5, color=GREEN)

    # reset branch: motion detected while awake
    rbox(2.5, 1.4, 3.4, 1.2, "motion > sleepThreshold\n=> sleepTime = 0\n(stays AWAKE)",
         "white", RED, fs=9.5, tc=RED, weight="normal")
    arrow(ax, (2.5, 3.3), (2.5, 2.05), color=RED, lw=1.8)
    ax.text(1.2, 2.7, "still moving", fontsize=9, color=RED, ha="center")

    # performance note
    ax.text(9.0, 1.3,
            "Why it saves CPU:\nsleeping bodies + contacts + joints\nare skipped by the solver entirely.",
            ha="center", fontsize=9.5, color=INK,
            bbox=dict(boxstyle="round,pad=0.4", fc=SOFT, ec=GREY))

    ax.set_xlim(0, 11.5); ax.set_ylim(0.2, 6.0)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("Sleep / wake cycle: whole-island migration between solver sets",
                 fontsize=12, weight="bold")
    fig.savefig(os.path.join(OUT, "fig-p5_18-04-sleep-state-machine.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_tunneling_vs_ccd()
    fig_toi_conservative_advancement()
    fig_speculative_contact()
    fig_sleep_state_machine()
    print("done:", sorted(f for f in os.listdir(OUT) if f.startswith("fig-p5_18") and f.endswith(".png")))
