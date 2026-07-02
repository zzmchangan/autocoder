# -*- coding: utf-8 -*-
"""Generate PNG figures for chapter P5-16 (Sequential Impulse constraint solving).

Run:  python gen_p5_16_figures.py
All text inside figures is English to avoid missing-glyph boxes.
Figures:
  fig-p5_16-01-stages.png           b2Solve 9-stage pipeline (with iteration loop)
  fig-p5_16-02-si-convergence.png   SI / PGS iteration: violation vs iteration
  fig-p5_16-03-coloring.png         constraint graph coloring for parallel SI
  fig-p5_16-04-single-vs-iter.png   1 impulse vs 10 iterations on a stack
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Circle, Patch

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
PURPLE= (0.55, 0.30, 0.75)
GREY  = (0.78, 0.78, 0.80)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.96, 0.96, 0.97)
BSOFT = (0.90, 0.94, 1.0)
GSOFT = (0.90, 0.95, 0.90)
OSOFT = (1.00, 0.93, 0.85)   # warm start / soft


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=11, weight="normal", tc=INK, lw=1.4):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=fc, edgecolor=ec, linewidth=lw))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.7, rad=0.0, ls="-"):
    cs = f"arc3,rad={rad}"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=15,
                                  linestyle=ls))


# ---------------------------------------------------------------- fig 1
def fig_stages():
    """The 9-stage b2Solve pipeline, highlighting the per-substep iteration loop."""
    fig, ax = plt.subplots(figsize=(14.5, 7.4))
    ax.set_xlim(0, 14.5); ax.set_ylim(0, 7.4)
    clean_ax(ax)

    ax.text(7.25, 7.05, "b2Solve: 9-stage pipeline (Box2D v3.2)",
            ha="center", fontsize=16, weight="bold", color=INK)
    ax.text(7.25, 6.65,
            "each b2World_Step(dt, subStepCount) runs N sub-steps; "
            "the dotted loop is one sub-step (h = dt / N)",
            ha="center", fontsize=11, color=INK)

    # prepare stages (run once)
    box(ax, 0.4, 5.25, 2.0, 0.8, "PrepareJoints", fc=BSOFT, fs=10.5)
    box(ax, 2.55, 5.25, 2.0, 0.8, "PrepareContacts\n(eff.mass, bias, soft)", fc=BSOFT, fs=10.0)
    ax.text(2.5, 6.20, "run once per step", ha="center", fontsize=9.5,
            color=BLUE, style="italic")

    # sub-step iteration block
    ax.add_patch(FancyBboxPatch((0.3, 1.1), 13.8, 3.85,
                 boxstyle="round,pad=0.03,rounding_size=0.10",
                 facecolor=(1, 0.97, 0.92), edgecolor=AMBER, linewidth=1.8,
                 linestyle=(0, (4, 3))))
    ax.text(0.55, 4.75, "sub-step loop  (subStepCount = N)",
            fontsize=11, weight="bold", color=AMBER)

    # iteration row (per color, per iteration)
    box(ax, 0.5, 3.35, 2.05, 0.75, "IntegrateVelocities\nv += h*a", fc="white", fs=9.5)
    box(ax, 2.7, 3.35, 1.85, 0.75, "WarmStart\n(prev impulses)", fc=OSOFT, fs=9.5)

    # the iteration loop over colors for solve
    ax.add_patch(FancyBboxPatch((4.75, 3.10), 4.55, 1.25,
                 boxstyle="round,pad=0.02,rounding_size=0.08",
                 facecolor=(0.94, 0.88, 1.0), edgecolor=PURPLE, linewidth=1.8))
    ax.text(7.0, 4.18, "Solve  x  ITERATIONS  x  colors", ha="center",
            fontsize=10.5, weight="bold", color=PURPLE)
    ax.text(7.0, 3.85, "Sequential Impulse (PGS)", ha="center", fontsize=9.5, color=PURPLE)
    ax.text(7.0, 3.55, "per color: parallel across constraints", ha="center",
            fontsize=8.8, color=INK, style="italic")
    # small iteration self-loop arrow
    ax.add_artist(FancyArrowPatch((9.0, 4.10), (5.1, 4.10),
                 arrowstyle="-|>", color=PURPLE, lw=1.5,
                 connectionstyle="arc3,rad=0.55", mutation_scale=14,
                 linestyle=(0, (2, 2))))
    ax.text(7.0, 4.95, "iteration", ha="center", fontsize=8.5,
            color=PURPLE, style="italic")

    box(ax, 9.5, 3.35, 2.0, 0.75, "IntegratePositions\nx += h*v", fc="white", fs=9.5)
    box(ax, 11.65, 3.35, 2.3, 0.75, "Relax (no bias)\nTGS tail", fc=OSOFT, fs=9.5)

    # arrows in sub-step row
    arrow(ax, (2.55, 3.72), (2.70, 3.72), color=AMBER, lw=1.4)
    arrow(ax, (4.55, 3.72), (4.75, 3.72), color=AMBER, lw=1.4)
    arrow(ax, (9.30, 3.72), (9.50, 3.72), color=AMBER, lw=1.4)
    arrow(ax, (11.50, 3.72), (11.65, 3.72), color=AMBER, lw=1.4)

    # loop-back arrow from Relax back to IntegrateVelocities (sub-step)
    arrow(ax, (12.8, 3.30), (1.55, 1.55), color=AMBER, lw=1.7, rad=-0.25,
          ls=(0, (4, 3)))
    ax.text(7.0, 1.85, "next sub-step (h = dt / N)",
            ha="center", fontsize=9.5, color=AMBER, style="italic")

    # post-loop stages
    box(ax, 0.5, 0.35, 2.0, 0.7, "Restitution\n(bounce post)", fc="white", fs=9.5)
    box(ax, 2.7, 0.35, 2.2, 0.7, "StoreImpulses\n(for warm start)", fc=GSOFT, fs=9.5)

    # arrows from prepare into loop and from loop to post
    arrow(ax, (3.55, 5.25), (1.5, 4.15), color=INK, lw=1.5, rad=0.15)
    arrow(ax, (12.8, 3.30), (1.5, 1.05), color=AMBER, lw=1.5, rad=-0.2, ls=(0, (4, 3)))
    # store impulses arrow from end of loop area
    arrow(ax, (12.8, 0.70), (12.8, 0.70), color=INK)
    # link last sub-step relax -> restitution
    arrow(ax, (3.8, 3.30), (1.5, 1.05), color=INK, lw=1.4, rad=0.2)

    # legend
    leg = [Patch(facecolor=BSOFT, edgecolor=INK, label="prepare (once)"),
           Patch(facecolor=OSOFT, edgecolor=INK, label="warm start / relax"),
           Patch(facecolor=(0.94, 0.88, 1.0), edgecolor=PURPLE, label="SI / PGS iteration"),
           Patch(facecolor=GSOFT, edgecolor=INK, label="store for next frame")]
    ax.legend(handles=leg, loc="lower right", fontsize=9.0, framealpha=0.95,
              bbox_to_anchor=(0.995, 0.005))

    out = os.path.join(OUT, "fig-p5_16-01-stages.png")
    plt.savefig(out); plt.close(fig)
    print("wrote", out)


# ---------------------------------------------------------------- fig 2
def fig_si_convergence():
    """SI / PGS iteration on a vertical stack of 4 boxes: how total constraint
    violation drops as iterations proceed. Modeled as projected Gauss-Seidel."""
    fig, ax = plt.subplots(figsize=(11.5, 6.4))
    np.random.seed(7)

    # We model a chain of 4 boxes resting on a static floor. Each contact is a
    # 1-D normal constraint. Sequential impulse = projected Gauss-Seidel on the
    # block-tridiagonal effective-mass system. We just simulate the iteration
    # numerically and track the residual (total penetration energy / violation).
    n = 4  # dynamic boxes
    # contact j connects box j (below) and box j+1 (above); plus floor contact.
    # Each contact has effective mass ~ 1 / (1/mA + 1/mB).
    m = np.ones(n)              # inverse masses
    # Build Jacobian rows for n contacts: row j affects body j-1 and j (j=0 is floor)
    # We treat velocity corrections; residual = sum |penetration remaining|.
    penetrations0 = np.array([0.30, 0.20, 0.12, 0.07, 0.04])  # 5 contacts

    def run_si(iters, warm=0.0):
        """Returns residual-vs-iteration curve."""
        pen = penetrations0.copy()
        # impulse accumulator (warm start seeds it)
        lam = np.full_like(pen, warm)
        residual = [pen.sum()]
        for _ in range(iters):
            # forward Gauss-Seidel sweep over contacts
            for j in range(len(pen)):
                # effective mass ~ 0.5 (two unit masses); solve for impulse to kill pen
                meff = 0.5
                dlam = -meff * pen[j]
                lam[j] = max(0.0, lam[j] + dlam)   # non-penetration complementarity
                dlam = lam[j] - (lam[j] - dlam) if lam[j] > 0 else dlam
                # apply: this correction reduces pen[j] and propagates to neighbor
                pen[j] += dlam
                if j + 1 < len(pen):
                    pen[j + 1] += 0.5 * dlam
                if j - 1 >= 0:
                    pen[j - 1] += 0.5 * dlam
            residual.append(max(0.0, pen.sum()))
        return np.array(residual)

    iters = 25
    cold = run_si(iters, warm=0.0)
    warm = run_si(iters, warm=0.15)  # warm-started: seed accumulator

    xs = np.arange(len(cold))
    ax.plot(xs, cold, "-o", color=BLUE, lw=2.2, ms=6,
            label="cold start (accumulator = 0)")
    ax.plot(xs, warm, "-s", color=RED, lw=2.2, ms=6,
            label="warm start (seeded from last frame)")
    ax.axhline(0, color=INK, lw=1.0, alpha=0.5)
    ax.set_xlabel("iteration count  (each = one Gauss-Seidel sweep)")
    ax.set_ylabel("total residual violation  (sum of penetrations)")
    ax.set_title("Sequential Impulse / PGS: monotone convergence on a 4-box stack",
                 fontsize=13.5, weight="bold")
    ax.set_ylim(-0.02, penetrations0.sum() * 1.05)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=11, loc="upper right")

    # annotate
    ax.annotate("each iteration reduces violation;\nnever increases (monotone)",
                xy=(3, cold[3]), xytext=(7, cold[3] + 0.18),
                fontsize=10, color=INK,
                arrowprops=dict(arrowstyle="->", color=INK, lw=1.2))
    ax.annotate("warm start begins\ncloser to the solution",
                xy=(1, warm[1]), xytext=(5, warm[1] + 0.05),
                fontsize=10, color=RED,
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.2))

    out = os.path.join(OUT, "fig-p5_16-02-si-convergence.png")
    plt.savefig(out); plt.close(fig)
    print("wrote", out)


# ---------------------------------------------------------------- fig 3
def fig_coloring():
    """Constraint graph coloring: constraints sharing a body get different colors,
    same-color constraints can be solved in parallel."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6.6))

    # ---- left: bodies + constraints graph
    ax1.set_xlim(-0.5, 6.5); ax1.set_ylim(-0.5, 5.0)
    clean_ax(ax1)
    ax1.set_title("(a) bodies and contact constraints", fontsize=12.5, weight="bold")

    # bodies (vertices)
    bodies = {
        "A": (1.0, 4.0), "B": (3.0, 4.0), "C": (5.0, 4.0),
        "D": (2.0, 2.0), "E": (4.0, 2.0),
        "floor": (3.0, 0.3),
    }
    for name, (x, y) in bodies.items():
        fc = GREY if name == "floor" else SOFT
        ax1.add_patch(Circle((x, y), 0.32, facecolor=fc, edgecolor=INK, linewidth=1.6))
        ax1.text(x, y, name, ha="center", va="center", fontsize=11, weight="bold")

    # constraints (edges): each is a contact between two bodies
    edges = [
        ("A", "B", 0), ("B", "C", 1),     # different colors (share B)
        ("A", "D", 2), ("B", "D", 0),     # B-D shares B-D ok with A-B? A-B col0, B-D col0 -> conflict
        ("B", "E", 1), ("C", "E", 2),
        ("D", "E", 0),
        ("D", "floor", 1), ("E", "floor", 2),
    ]
    # We will recompute a valid coloring below to avoid the conflict above.
    # Build adjacency, greedy-color edges so that edges sharing a vertex differ.
    from collections import defaultdict
    incid = defaultdict(list)
    for i, (u, v, _) in enumerate(edges):
        incid[u].append(i); incid[v].append(i)
    color_of = {}
    for i, (u, v, _) in enumerate(edges):
        used = set()
        for j in incid[u] + incid[v]:
            if j in color_of:
                used.add(color_of[j])
        c = 0
        while c in used:
            c += 1
        color_of[i] = c
    palette = [BLUE, RED, GREEN, AMBER, PURPLE]
    for i, (u, v, _) in enumerate(edges):
        x1, y1 = bodies[u]; x2, y2 = bodies[v]
        c = palette[color_of[i] % len(palette)]
        ax1.plot([x1, x2], [y1, y2], color=c, lw=4.0, alpha=0.85, solid_capstyle="round")

    # legend of colors
    n_colors = max(color_of.values()) + 1
    handles = [Patch(facecolor=palette[k % len(palette)], edgecolor=INK,
                     label=f"color {k}") for k in range(n_colors)]
    ax1.legend(handles=handles, fontsize=9.5, loc="upper left",
               bbox_to_anchor=(0.0, 1.0), framealpha=0.95)
    ax1.text(3.0, -0.35, "edges sharing a vertex get different colors",
             ha="center", fontsize=10, style="italic", color=INK)

    # ---- right: time-line of parallel execution per color
    ax2.set_xlim(0, 10); ax2.set_ylim(-0.5, n_colors + 0.5)
    clean_ax(ax2)
    ax2.set_title("(b) solving by color = parallel within a color", fontsize=12.5, weight="bold")
    ax2.set_xlabel("time")

    # how many constraints per color
    by_color = defaultdict(int)
    for i in range(len(edges)):
        by_color[color_of[i]] += 1

    y = n_colors
    for k in range(n_colors):
        cnt = by_color.get(k, 0)
        # draw the color band
        c = palette[k % len(palette)]
        ax2.add_patch(Rectangle((1.0, y - 0.30), 2.5, 0.55,
                                facecolor=c, alpha=0.85, edgecolor=INK))
        ax2.text(2.25, y, f"color {k}:  {cnt} constraint(s)",
                 ha="center", va="center", fontsize=10.5, weight="bold", color="white")
        # "all run in parallel" annotation
        ax2.text(3.75, y, "<-- solved in parallel", fontsize=9.5,
                 color=c, va="center")
        y -= 1.0

    # sequential arrows between colors
    yy = n_colors - 0.4
    for k in range(n_colors - 1):
        arrow(ax2, (2.25, yy), (2.25, yy - 0.6), color=INK, lw=1.6)
        yy -= 1.0

    ax2.text(5.0, 0.2,
             "colors are solved sequentially (each color's result feeds the next),\n"
             "but inside a color all constraints are independent and run in parallel.",
             fontsize=10, color=INK, style="italic")

    plt.suptitle("Constraint graph coloring: parallelizing Sequential Impulse",
                 fontsize=14, weight="bold", y=1.02)
    plt.tight_layout()
    out = os.path.join(OUT, "fig-p5_16-03-coloring.png")
    plt.savefig(out); plt.close(fig)
    print("wrote", out)


# ---------------------------------------------------------------- fig 4
def fig_single_vs_iter():
    """A stack of boxes: one impulse pass leaves residual overlap & jitter;
    many iterations (PGS) converge to a stable, penetration-free stack."""
    fig, axes = plt.subplots(1, 3, figsize=(14.5, 5.6))

    np.random.seed(3)
    n = 5
    box_w, box_h = 1.0, 0.7
    floor_y = 0.0

    def draw_stack(ax, title, y_offsets, color):
        ax.set_xlim(-1.0, 4.0); ax.set_ylim(-0.5, 5.5)
        clean_ax(ax)
        # floor
        ax.add_patch(Rectangle((-1.0, -0.5), 5.0, 0.5, facecolor=GREY, edgecolor=INK))
        ax.text(1.5, -0.35, "static floor", ha="center", va="center",
                fontsize=9, color=INK)
        for i in range(n):
            y = floor_y + i * box_h + y_offsets[i]
            fc = (1.0, 0.85, 0.85) if y_offsets[i] < -0.01 else color
            ax.add_patch(Rectangle((1.0, y), box_w, box_h,
                                   facecolor=fc, edgecolor=INK, linewidth=1.4))
            ax.text(1.5, y + box_h / 2, f"box {i}", ha="center", va="center",
                    fontsize=9.5, weight="bold")
        # arrows showing overlap
        for i in range(n):
            if y_offsets[i] < -0.01:
                ax.annotate("", xy=(2.6, floor_y + i * box_h + y_offsets[i]),
                            xytext=(2.6, floor_y + i * box_h),
                            arrowprops=dict(arrowstyle="<->", color=RED, lw=1.4))
                ax.text(2.9, floor_y + i * box_h + y_offsets[i] / 2,
                        f"{abs(y_offsets[i]):.2f}", fontsize=8.5, color=RED, va="center")
        ax.set_title(title, fontsize=11.5, weight="bold")

    # (a) initial overlap (after integrate, before solve)
    initial_off = np.array([-0.05, -0.08, -0.06, -0.04, -0.03])
    draw_stack(axes[0], "(a) after integrate: overlapping",
               initial_off, (0.85, 0.90, 1.0))

    # (b) 1 impulse pass: mostly fixed but residual + jitter
    one_off = np.array([-0.02, -0.04, 0.02, -0.02, 0.015])
    draw_stack(axes[1], "(b) 1 impulse pass: jitter remains",
               one_off, (1.0, 0.92, 0.80))

    # (c) many PGS iterations: settled
    settled = np.array([0.0, 0.0, 0.0, 0.0, 0.0])
    draw_stack(axes[2], "(c) PGS iterations: stable stack",
               settled, (0.80, 0.95, 0.80))

    plt.suptitle("Single impulse vs many Sequential Impulse iterations on a stack",
                 fontsize=14, weight="bold", y=1.03)
    plt.tight_layout()
    out = os.path.join(OUT, "fig-p5_16-04-single-vs-iter.png")
    plt.savefig(out); plt.close(fig)
    print("wrote", out)


if __name__ == "__main__":
    fig_stages()
    fig_si_convergence()
    fig_coloring()
    fig_single_vs_iter()
    print("done.")
