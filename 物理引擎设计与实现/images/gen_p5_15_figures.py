# -*- coding: utf-8 -*-
"""Figures for chapter P5-15 (Impulse-based collision response).
Run:  python gen_p5_15_figures.py
All in-figure text is English to avoid CJK font issues.
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch, FancyArrowPatch, Circle

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
PURP  = (0.55, 0.30, 0.75)
GREY  = (0.78, 0.78, 0.80)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def arrow(ax, p1, p2, color=INK, lw=1.8, rad=0.0):
    cs = f"arc3,rad={rad}"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=16))


# --------------------------------------------------------------- fig-01
# Before/after velocity: two spheres approach, normal impulse flips the
# relative normal velocity (scaled by restitution e=0.8 here).
def fig_velocity_flip():
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.0))

    # ---- left: BEFORE ----
    ax = axes[0]
    ax.add_patch(Circle((2.0, 3.0), 0.7, facecolor=BLUE, edgecolor=INK, alpha=0.85))
    ax.text(2.0, 3.0, "A", ha="center", va="center", color="white", weight="bold", fontsize=14)
    ax.add_patch(Circle((7.0, 3.0), 0.7, facecolor=RED, edgecolor=INK, alpha=0.85))
    ax.text(7.0, 3.0, "B", ha="center", va="center", color="white", weight="bold", fontsize=14)
    # contact normal n points from A to B (rightward)
    arrow(ax, (2.9, 4.3), (6.1, 4.3), color=AMBER, lw=2.2)
    ax.text(4.5, 4.55, r"contact normal  $\hat{n}$", ha="center", color=AMBER, fontsize=12)
    # velocities: A moves right (+), B moves left (-): approaching
    arrow(ax, (1.0, 3.0), (2.0, 3.0), color=BLUE, lw=2.4)
    ax.text(0.2, 3.25, r"$v_A$", color=BLUE, fontsize=14, weight="bold")
    arrow(ax, (8.0, 3.0), (7.0, 3.0), color=RED, lw=2.4)
    ax.text(8.1, 3.25, r"$v_B$", color=RED, fontsize=14, weight="bold")
    ax.text(4.5, 1.4, "BEFORE:  approaching\n" + r"$v_{rel}\cdot\hat{n} = (v_B-v_A)\cdot\hat{n} < 0$",
            ha="center", fontsize=12, color=INK,
            bbox=dict(boxstyle="round,pad=0.3", facecolor=SOFT, edgecolor=GREY))
    ax.set_xlim(-0.5, 9.5); ax.set_ylim(0.5, 5.2)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("before impact (approaching)", fontsize=13, weight="bold")

    # ---- right: AFTER ----
    ax = axes[1]
    ax.add_patch(Circle((2.0, 3.0), 0.7, facecolor=BLUE, edgecolor=INK, alpha=0.85))
    ax.text(2.0, 3.0, "A", ha="center", va="center", color="white", weight="bold", fontsize=14)
    ax.add_patch(Circle((7.0, 3.0), 0.7, facecolor=RED, edgecolor=INK, alpha=0.85))
    ax.text(7.0, 3.0, "B", ha="center", va="center", color="white", weight="bold", fontsize=14)
    # impulse J on A is -n direction (leftward), on B is +n (rightward)
    arrow(ax, (2.0, 2.0), (1.1, 2.0), color=AMBER, lw=2.6)
    ax.text(0.3, 1.75, r"$-J\hat{n}$ on A", color=AMBER, fontsize=12)
    arrow(ax, (7.0, 2.0), (7.9, 2.0), color=AMBER, lw=2.6)
    ax.text(7.95, 1.75, r"$+J\hat{n}$ on B", color=AMBER, fontsize=12)
    # after velocities: A pushed left, B pushed right -> separating
    arrow(ax, (2.0, 3.0), (1.2, 3.0), color=BLUE, lw=2.4)
    ax.text(0.3, 3.25, r"$v_A'$", color=BLUE, fontsize=14, weight="bold")
    arrow(ax, (7.0, 3.0), (7.8, 3.0), color=RED, lw=2.4)
    ax.text(7.9, 3.25, r"$v_B'$", color=RED, fontsize=14, weight="bold")
    ax.text(4.5, 1.4, r"AFTER:  separating,  $v_{rel}'\cdot\hat{n} = -e\,(v_{rel}\cdot\hat{n})$",
            ha="center", fontsize=12, color=INK,
            bbox=dict(boxstyle="round,pad=0.3", facecolor=SOFT, edgecolor=GREY))
    ax.set_xlim(-0.5, 9.5); ax.set_ylim(0.5, 5.2)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("after impulse (separating, scaled by e)", fontsize=13, weight="bold")

    fig.suptitle("Normal impulse flips relative normal velocity (e=0.8)",
                 fontsize=14, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p5_15-velocity-flip.png"))
    plt.close(fig)


# --------------------------------------------------------------- fig-02
# Coefficient of restitution comparison.
def fig_restitution_compare():
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 5.2), sharey=True)
    es = [1.0, 0.5, 0.0]
    titles = ["e = 1.0  perfectly elastic\n(original speed rebound)",
              "e = 0.5  partial bounce",
              "e = 0.0  perfectly inelastic\n(stick together)"]
    g = 9.8
    # simulate a ball dropped from height H, then bouncing with restitution e.
    # Each bounce starts from the ground (y=0); peak height = v0^2/(2g).
    H = 3.0
    for k, (e, title) in enumerate(zip(es, titles)):
        ax = axes[k]
        ax.add_patch(Rectangle((0, 0), 4.0, 0.35, facecolor=(0.55, 0.45, 0.35), edgecolor=INK))
        col = [GREEN, AMBER, RED][k]
        x = 0.4
        # first segment: free fall from H down to ground
        nfall = 40
        tfall = np.sqrt(2 * H / g)
        ts = np.linspace(0, tfall, nfall)
        fall = H - 0.5 * g * ts ** 2
        xfall = x + np.linspace(0, 0.5, nfall)
        ax.plot(xfall, fall, color=col, lw=2.0, alpha=0.9)
        x = xfall[-1]
        # successive bounces: each starts at ground, peaks at v0^2/(2g) = e^2 * prev_peak
        peak = H  # first impact gives rebound peak = e^2 * H (energy e^2 of normal KE)
        for bounce in range(4):
            v0 = e * np.sqrt(2 * g * peak)  # rebound speed right after impact
            if v0 < 0.4:
                break
            nb = 50
            tb = 2 * v0 / g  # full up-and-down flight time
            ts = np.linspace(0, tb, nb)
            traj = v0 * ts - 0.5 * g * ts ** 2   # starts at 0, peaks at v0^2/(2g), returns to 0
            traj = np.clip(traj, 0, None)
            xtraj = x + np.linspace(0, 0.7, nb)
            ax.plot(xtraj, traj, color=col, lw=2.0, alpha=0.9)
            ax.add_patch(Circle((xtraj[0], 0.0), 0.16, facecolor=col, edgecolor=INK, zorder=5))
            peak = e * e * peak  # next rebound peak shrinks by e^2
            x = xtraj[-1]
        ax.set_xlim(0, 4.0); ax.set_ylim(0, 4.0)
        ax.set_aspect("equal"); clean_ax(ax)
        ax.set_title(title, fontsize=11.5, weight="bold")
    fig.suptitle("Coefficient of restitution e controls rebound energy",
                 fontsize=14, weight="bold", y=1.03)
    fig.savefig(os.path.join(OUT, "fig-p5_15-restitution.png"))
    plt.close(fig)


# --------------------------------------------------------------- fig-03
# Derivation of the impulse magnitude: momentum conservation + restitution
# definition -> solve for j.
def fig_impulse_derivation():
    fig, ax = plt.subplots(figsize=(12.5, 6.2))

    def panel(x0, y0, w, h, title, lines, fc="white", ec=INK, tc=INK):
        ax.add_patch(FancyBboxPatch((x0, y0), w, h, boxstyle="round,pad=0.03,rounding_size=0.06",
                                    facecolor=fc, edgecolor=ec, linewidth=1.6))
        ax.text(x0 + w / 2, y0 + h - 0.28, title, ha="center", va="top",
                fontsize=12.5, weight="bold", color=tc)
        for i, ln in enumerate(lines):
            ax.text(x0 + 0.18, y0 + h - 0.78 - i * 0.42, ln, ha="left", va="top",
                    fontsize=12, color=INK, family="DejaVu Sans")

    # panel 1: setup
    panel(0.2, 3.2, 5.8, 2.7,
          "1. Apply impulse J = j n along normal",
          ["Impulse is a vector along the contact normal  n",
           "",
           "  v_A' = v_A - (j / m_A) n",
           "  v_B' = v_B + (j / m_B) n",
           "",
           "(j > 0 pushes B away from A)"],
          fc="#eef4ff", ec=BLUE)
    # panel 2: momentum conservation
    panel(6.2, 3.2, 5.8, 2.7,
          "2. Momentum is conserved automatically",
          ["Total linear momentum before = after:",
           "",
           "  m_A v_A + m_B v_B = m_A v_A' + m_B v_B'",
           "",
           "The +/-j n form makes this an identity,",
           "for ANY j.  Impulse => momentum conserved."],
          fc="#eefbee", ec=GREEN)
    # panel 3: restitution defines post-impact rel velocity
    panel(0.2, 0.3, 5.8, 2.7,
          "3. Restitution fixes the new relative speed",
          ["Definition of coefficient of restitution e:",
           "",
           "  (v_B' - v_A') . n  =  -e (v_B - v_A) . n",
           "",
           "i.e. the relative normal velocity flips",
           "sign and shrinks by factor e."],
          fc="#fff4e6", ec=AMBER)
    # panel 4: solve
    panel(6.2, 0.3, 5.8, 2.7,
          "4. Solve the two equations for j",
          ["Substitute step 1 into step 3, solve:",
           "",
           "  j = -(1+e) (v_rel . n) / (1/m_A + 1/m_B)",
           "",
           "denominator = sum of inverse masses",
           "(effective-mass trick from P2-05)"],
          fc="#f6eaff", ec=PURP)
    # arrows down the derivation chain
    arrow(ax, (3.1, 3.2), (3.1, 3.05), color=INK, lw=1.6)
    arrow(ax, (9.1, 3.2), (9.1, 3.05), color=INK, lw=1.6)
    arrow(ax, (5.95, 1.65), (6.25, 1.65), color=INK, lw=1.8)

    ax.set_xlim(0, 12.2); ax.set_ylim(0, 6.2)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("Deriving the normal impulse magnitude j  (momentum conserved + restitution)",
                 fontsize=13.5, weight="bold")
    fig.savefig(os.path.join(OUT, "fig-p5_15-derivation.png"))
    plt.close(fig)


# --------------------------------------------------------------- fig-04
# Numerical verification: two-ball collision, check momentum conservation
# and energy behavior for e in {1, 0.5, 0}. Boxed bars.
def fig_verify_conservation():
    # setup: 1D head-on collision, mA=2, mB=1, vA=+3, vB=-1
    mA, mB = 2.0, 1.0
    vA0, vB0 = 3.0, -1.0
    K0 = 0.5 * mA * vA0 ** 2 + 0.5 * mB * vB0 ** 2
    P0 = mA * vA0 + mB * vB0
    es = [1.0, 0.5, 0.0]
    labels = ["e=1.0\n(elastic)", "e=0.5", "e=0.0\n(inelastic)"]
    p_ratios, k_ratios = [], []
    vAs, vBs = [], []
    for e in es:
        # 1D impulse: j = -(1+e)(vB-vA) / (1/mA + 1/mB)
        j = -(1 + e) * (vB0 - vA0) / (1.0 / mA + 1.0 / mB)
        vA1 = vA0 - j / mA
        vB1 = vB0 + j / mB
        vAs.append(vA1); vBs.append(vB1)
        P1 = mA * vA1 + mB * vB1
        K1 = 0.5 * mA * vA1 ** 2 + 0.5 * mB * vB1 ** 2
        p_ratios.append(P1 / P0)
        k_ratios.append(K1 / K0)

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.0))

    # left: momentum ratio (should be 1.0 for all e) + velocity vectors
    ax = axes[0]
    x = np.arange(len(es))
    w = 0.36
    ax.bar(x - w / 2, vAs, w, color=BLUE, label="v_A after")
    ax.bar(x + w / 2, vBs, w, color=RED, label="v_B after")
    ax.axhline(0, color=INK, lw=0.8)
    ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=11)
    ax.set_ylabel("velocity after impact", fontsize=12)
    ax.set_ylim(-4, 6)
    for xi, va, vb in zip(x, vAs, vBs):
        ax.text(xi - w / 2, va + (0.15 if va >= 0 else -0.45), f"{va:+.2f}",
                ha="center", fontsize=10.5, color=BLUE, weight="bold")
        ax.text(xi + w / 2, vb + (0.15 if vb >= 0 else -0.45), f"{vb:+.2f}",
                ha="center", fontsize=10.5, color=RED, weight="bold")
    ax.set_title("Post-impact velocities  (mA=2, mB=1, vA=+3, vB=-1)",
                 fontsize=12, weight="bold")
    ax.legend(fontsize=10.5, loc="upper right")
    ax.grid(True, axis="y", alpha=0.3)

    # right: momentum ratio and kinetic-energy ratio
    ax = axes[1]
    ax.bar(x - w / 2, p_ratios, w, color=GREEN, label="momentum ratio  P'/P0")
    ax.bar(x + w / 2, k_ratios, w, color=AMBER, label="kinetic energy ratio  K'/K0")
    ax.axhline(1.0, color=GREEN, lw=1.2, linestyle="--", alpha=0.7)
    ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=11)
    ax.set_ylim(0, 1.25)
    ax.set_ylabel("ratio (after / before)", fontsize=12)
    for xi, pr, kr in zip(x, p_ratios, k_ratios):
        ax.text(xi - w / 2, pr + 0.03, f"{pr:.3f}", ha="center", fontsize=10.5,
                color=GREEN, weight="bold")
        ax.text(xi + w / 2, kr + 0.03, f"{kr:.3f}", ha="center", fontsize=10.5,
                color=AMBER, weight="bold")
    ax.set_title("Momentum always conserved; energy lost = (1-e^2)",
                 fontsize=12, weight="bold")
    ax.legend(fontsize=10.5, loc="upper right")
    ax.grid(True, axis="y", alpha=0.3)

    fig.suptitle("Impulse method: momentum conserved for all e, energy correct",
                 fontsize=14, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p5_15-verify-conservation.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_velocity_flip()
    fig_restitution_compare()
    fig_impulse_derivation()
    fig_verify_conservation()
    print("done:", sorted(f for f in os.listdir(OUT) if f.startswith("fig-p5_15")))
