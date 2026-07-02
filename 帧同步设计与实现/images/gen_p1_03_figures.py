# -*- coding: utf-8 -*-
"""Generate PNG figures for chapter P1-03 (Fixed-point math library).

Three signature figures:
  fig-03-01  multiply three-paths decision diagram (long fast / Int128 slow / LInt128 soft)
  fig-03-02  truncate (toward zero) vs floor (toward -inf): the P0-1 root cause
  fig-03-03  trig LUT: stepwise table vs float sin, zero floating point at runtime

Style aligned with the physics-engine series (DejaVu Sans, fixed palette,
clean_ax / box / arrow helpers, English labels, dpi 150, Agg backend).

Run: python gen_p1_03_figures.py
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

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

# ---- palette (aligned with physics-engine series) ----
RED   = (0.86, 0.18, 0.18)
GREEN = (0.20, 0.70, 0.32)
BLUE  = (0.20, 0.45, 0.88)
AMBER = (0.95, 0.70, 0.18)
GREY  = (0.78, 0.78, 0.80)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)
GSOFT = (0.90, 0.95, 0.90)   # fast path light green
BSOFT = (0.90, 0.94, 1.0)    # slow path light blue
RSOFT = (0.98, 0.90, 0.90)   # root-cause light red
ASOFT = (0.98, 0.94, 0.82)   # amber soft


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=12, weight="normal", tc=INK, lw=1.4):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=fc, edgecolor=ec, linewidth=lw))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def diamond(ax, cx, cy, w, h, text, fc=ASOFT, ec=INK, fs=11.5, weight="bold"):
    pts = [(cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2), (cx - w / 2, cy)]
    poly = plt.Polygon(pts, closed=True, facecolor=fc, edgecolor=ec, linewidth=1.4)
    ax.add_patch(poly)
    ax.text(cx, cy, text, ha="center", va="center", fontsize=fs, weight=weight, color=INK)


def arrow(ax, p1, p2, color=INK, lw=1.7, rad=0.0):
    cs = f"arc3,rad={rad}"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=16))


def label_on(ax, x, y, text, color=INK, fs=10.5, weight="normal", ha="center", va="center"):
    ax.text(x, y, text, ha=ha, va=va, fontsize=fs, weight=weight, color=color)


# ============================================================ fig-03-01
# Multiply three-paths decision diagram.
# Inputs |a|, |b|: if both < 2^31 -> long fast path (>99% hit);
#                  else Int128 slow path (NET8) OR LInt128 soft (non-NET8).
# All three paths converge to identical bit-level result (proven by equivalence tests).
def fig_mul_three_paths():
    fig, ax = plt.subplots(figsize=(11.5, 7.6))
    clean_ax(ax)
    ax.set_xlim(0, 11.5)
    ax.set_ylim(0, 7.6)

    # ---- title ----
    ax.text(5.75, 7.25, "MulShiftFast: three paths, one bit-identical result",
            ha="center", va="center", fontsize=15, weight="bold", color=INK)
    ax.text(5.75, 6.85,
            "Inputs: two 64-bit RawValues  a, b   (each ~2^N, product overflows long at 2^63)",
            ha="center", va="center", fontsize=11, color=(0.35, 0.35, 0.35))

    # ---- input box ----
    box(ax, 4.25, 5.65, 3.0, 0.75,
        "long a, long b\n(RawValue of two LFloat)",
        fc=SOFT, fs=11.5, weight="bold")

    # ---- decision diamond: are both small? ----
    diamond(ax, 5.75, 4.55, 5.4, 1.35,
            "both  |a| < 2^31  AND  |b| < 2^31 ?\n(product < 2^62, fits in long)",
            fc=ASOFT, fs=11)
    arrow(ax, (5.75, 5.65), (5.75, 5.23))

    # ---- YES branch (left): long fast path ----
    # hit-rate badge
    label_on(ax, 1.35, 4.75, "YES  (>99%)", color=GREEN, fs=12, weight="bold")
    box(ax, 0.35, 3.30, 3.5, 1.15,
        "PATH 1  -  long fast path\nreturn (a * b) >> 16\n(one hardware imul + shift)",
        fc=GSOFT, ec=GREEN, fs=11, weight="bold", tc=INK)
    arrow(ax, (3.05, 4.55), (3.85, 4.45), color=GREEN, lw=2.0, rad=0.0)
    # actually arrow from diamond left vertex to fast-path box top
    arrow(ax, (3.05, 4.55), (2.10, 4.45), color=GREEN, lw=2.0)

    # ---- NO branch (right): big value ----
    label_on(ax, 8.45, 4.75, "NO  (<1%, big value)", color=RED, fs=12, weight="bold")
    # second-level decision: NET8 or not?
    diamond(ax, 9.35, 3.55, 3.7, 1.15, "target == NET 8 ?\n(Int128 available)",
            fc=ASOFT, fs=10.5)
    arrow(ax, (8.45, 4.55), (7.50, 3.95), color=RED, lw=2.0)

    # NET8 YES -> Int128 slow path
    label_on(ax, 7.65, 2.75, "YES", color=BLUE, fs=11, weight="bold")
    box(ax, 6.05, 1.55, 3.0, 1.05,
        "PATH 2  -  Int128 slow\n(Int128)a*b >> 16\n(software 128-bit mul)",
        fc=BSOFT, ec=BLUE, fs=10.5, weight="bold")
    arrow(ax, (7.50, 3.00), (7.55, 2.60), color=BLUE, lw=2.0)

    # NET8 NO -> LInt128 soft path
    label_on(ax, 11.05, 2.75, "NO", color=AMBER, fs=11, weight="bold")
    box(ax, 9.30, 1.55, 2.10, 1.05,
        "PATH 3\nLInt128.Mul\n(4x 32x32)",
        fc=ASOFT, ec=AMBER, fs=10.5, weight="bold")
    arrow(ax, (11.20, 3.00), (10.35, 2.60), color=AMBER, lw=2.0)

    # ---- convergence box at bottom ----
    box(ax, 3.25, 0.30, 5.0, 0.85,
        "bit-identical long result\n(proven by MathEquivalenceTests vs MulFallback)",
        fc=SOFT, ec=INK, fs=11, weight="bold")
    arrow(ax, (2.10, 3.30), (4.50, 1.15), color=GREEN, lw=1.8, rad=-0.12)
    arrow(ax, (7.55, 1.55), (6.30, 1.15), color=BLUE, lw=1.8, rad=0.10)
    arrow(ax, (10.35, 1.55), (7.20, 1.15), color=AMBER, lw=1.8, rad=0.18)

    # ---- annotation: threshold only here ----
    label_on(ax, 1.10, 5.95,
             "threshold logic\nlives in ONE place\n(single entry point)",
             color=(0.30, 0.30, 0.30), fs=9.5, ha="left")

    fig.savefig(os.path.join(OUT, "fig-03-01-mul-three-paths.png"))
    plt.close(fig)
    print("saved fig-03-01-mul-three-paths.png")


# ============================================================ fig-03-02
# P0-1 root cause: truncate (toward zero) vs floor (toward -inf).
# Product = -491520.5 (non-integer after >>16 in fixed-point):
#   truncate -> -491520   (.NET old LInt128: abs-then-shift-then-negate)
#   floor    -> -491521   (.NET 8 Int128 >>n arithmetic shift)
# Difference of 1 in the lowest bit = desync across runtimes.
def fig_truncate_vs_floor():
    fig, (ax_top, ax_bot) = plt.subplots(2, 1, figsize=(11.5, 7.2),
                                         gridspec_kw={"height_ratios": [1.0, 1.0]})
    fig.subplots_adjust(hspace=0.55)

    # ---------- top axis: number line for the raw product ----------
    ax = ax_top
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_position(("data", 0.5))
    ax.set_yticks([])
    ax.set_xlim(-4, 4)
    ax.set_ylim(-0.35, 1.6)

    # main number line
    ax.annotate("", xy=(3.7, 0.5), xytext=(-3.7, 0.5),
                arrowprops=dict(arrowstyle="->", lw=1.6, color=INK))

    # integer ticks
    for k in range(-3, 4):
        ax.plot([k, k], [0.42, 0.58], color=INK, lw=1.3)
        ax.text(k, 0.62, str(k), ha="center", va="bottom", fontsize=11, color=INK)

    # the product lands at -1.5 (representing -491520.5 scaled)
    prod_x = -1.5
    ax.plot([prod_x], [0.5], "o", color=RED, ms=12, zorder=5)
    ax.text(prod_x, 1.18,
            "raw product after shift = -491520.5\n(non-integer; the lowest 16 bits are non-zero)",
            ha="center", va="bottom", fontsize=10.5, color=RED, weight="bold")

    # truncate arrow: -1.5 -> -1 (toward zero), label below-right
    ax.annotate("", xy=(-1.0, 0.5), xytext=(-1.5, 0.5),
                arrowprops=dict(arrowstyle="-|>", lw=2.2, color=GREEN,
                                shrinkA=0, shrinkB=0))
    ax.text(-0.30, -0.10, "truncate  ->  -491520\n(toward zero)",
            ha="center", va="top", fontsize=10, color=GREEN, weight="bold")
    ax.plot([-0.95, -0.30], [0.42, -0.02], color=GREEN, lw=0.9, ls=":")

    # floor arrow: -1.5 -> -2 (toward -inf), label below-left
    ax.annotate("", xy=(-2.0, 0.5), xytext=(-1.5, 0.5),
                arrowprops=dict(arrowstyle="-|>", lw=2.2, color=BLUE,
                                shrinkA=0, shrinkB=0))
    ax.text(-2.85, -0.10, "floor  ->  -491521\n(toward -inf)",
            ha="center", va="top", fontsize=10, color=BLUE, weight="bold")
    ax.plot([-2.05, -2.85], [0.42, -0.02], color=BLUE, lw=0.9, ls=":")

    # the +1 / -1 diff badge between -1 and -2 (above the line)
    ax.annotate("", xy=(-1.0, 0.92), xytext=(-2.0, 0.92),
                arrowprops=dict(arrowstyle="<->", lw=1.8, color=RED))
    ax.text(-1.5, 0.97, "differ by 1 in the lowest bit",
            ha="center", va="bottom", fontsize=10.5, color=RED, weight="bold")

    ax.set_title("Negative right-shift has two valid integer semantics  (P0-1 root cause)",
                 fontsize=13.5, weight="bold", pad=12)

    # ---------- bottom axis: runtime mapping ----------
    ax = ax_bot
    clean_ax(ax)
    ax.set_xlim(0, 11.5)
    ax.set_ylim(0, 4.0)

    # left column: .NET 8 (floor)
    box(ax, 0.4, 2.55, 5.1, 1.15,
        ".NET 8   Int128 >> n\n= arithmetic shift (sign-extension)\nresult = -491521   (floor)",
        fc=BSOFT, ec=BLUE, fs=11, weight="bold", tc=INK)
    # right column: netstandard2.1 old LInt128 (truncate)
    box(ax, 6.0, 2.55, 5.1, 1.15,
        "netstandard2.1   old LInt128\n= abs -> shift -> negate\nresult = -491520   (truncate)",
        fc=RSOFT, ec=RED, fs=11, weight="bold", tc=INK)

    # center: desync
    box(ax, 3.85, 0.95, 3.8, 1.10,
        "DESYNC\nsame input, two runtimes\n-> differ by 1 RawValue",
        fc=ASOFT, ec=AMBER, fs=11, weight="bold")
    arrow(ax, (2.95, 2.55), (5.0, 2.05), color=BLUE, lw=1.8, rad=0.10)
    arrow(ax, (8.55, 2.55), (6.50, 2.05), color=RED, lw=1.8, rad=-0.10)

    # fix box
    box(ax, 2.5, 0.0, 6.5, 0.75,
        "FIX: LInt128.ArithmeticShiftToLong  -> unified to floor (matches .NET 8)",
        fc=GSOFT, ec=GREEN, fs=11, weight="bold")
    arrow(ax, (5.75, 0.95), (5.75, 0.75), color=GREEN, lw=1.8)

    fig.savefig(os.path.join(OUT, "fig-03-02-truncate-vs-floor.png"))
    plt.close(fig)
    print("saved fig-03-02-truncate-vs-floor.png")


# ============================================================ fig-03-03
# Trig LUT: precomputed table of round(65536 * sin(i/4096 * 2pi)),
# i = 0..4095. Runtime: integer index -> table lookup, zero floating point.
# Show: (a) the 4096-entry step curve overlaid on the true sin,
#       (b) the per-sample error (quantized to 1/65536),
#       (c) a small inset schema: radians -> getIndex -> table[index].
def fig_trig_lut():
    fig = plt.figure(figsize=(12.0, 7.4))
    gs = fig.add_gridspec(2, 2, height_ratios=[1.6, 1.0], width_ratios=[1.0, 1.0],
                          hspace=0.45, wspace=0.30)
    ax_curve = fig.add_subplot(gs[0, :])
    ax_err   = fig.add_subplot(gs[1, 0])
    ax_schema = fig.add_subplot(gs[1, 1])

    N = 4096
    SCALE = 65536  # Q16.16 fixed-point scale
    i = np.arange(N)
    theta = i / N * 2 * np.pi
    true_sin = np.sin(theta)
    # table stores round(65536 * sin(theta)) as int16-capable values (clamp)
    table = np.round(SCALE * true_sin).astype(np.int64)
    recon = table / SCALE  # what runtime gets back as LFloat

    # ---------- top: curve overlay ----------
    ax = ax_curve
    th_fine = np.linspace(0, 2 * np.pi, 4000)
    ax.plot(th_fine, np.sin(th_fine), color=BLUE, lw=2.2, alpha=0.85,
            label="true  sin(theta)   (never computed at runtime)")
    ax.plot(theta, recon, color=RED, lw=1.0, drawstyle="steps-post",
            label=f"LUT[{N}]  =  round({SCALE} * sin)   (precomputed, zero float at runtime)")
    ax.set_xlim(0, 2 * np.pi)
    ax.set_xticks([0, np.pi / 2, np.pi, 3 * np.pi / 2, 2 * np.pi])
    ax.set_xticklabels(["0", "pi/2", "pi", "3pi/2", "2pi"])
    ax.set_ylim(-1.18, 1.18)
    ax.set_ylabel("sin value")
    ax.set_xlabel("radians (theta)")
    ax.set_title("Trig lookup table: 4096 precomputed samples replace every runtime sin() call",
                 fontsize=13, weight="bold", pad=10)
    ax.grid(alpha=0.25)
    ax.legend(loc="lower left", fontsize=10.5, framealpha=0.95)

    # annotate key points: sin(0)=0, sin(pi/2)=1 pinned
    ax.plot([0], [0], "o", color=GREEN, ms=8, zorder=5)
    ax.annotate("sin(0)=0 pinned\n(exact boundary)",
                xy=(0, 0), xytext=(0.25, -0.55),
                fontsize=9.5, color=GREEN,
                arrowprops=dict(arrowstyle="->", color=GREEN, lw=1.2))
    ax.plot([np.pi / 2], [1], "o", color=GREEN, ms=8, zorder=5)
    ax.annotate("sin(pi/2)=1 pinned\n(exact boundary)",
                xy=(np.pi / 2, 1), xytext=(2.0, 0.78),
                fontsize=9.5, color=GREEN,
                arrowprops=dict(arrowstyle="->", color=GREEN, lw=1.2))

    # ---------- bottom-left: quantization error ----------
    ax = ax_err
    err = recon - true_sin            # in units of sin value
    ax.plot(theta, err, color=AMBER, lw=0.8)
    ax.set_xlim(0, 2 * np.pi)
    ax.set_xticks([0, np.pi / 2, np.pi, 3 * np.pi / 2, 2 * np.pi])
    ax.set_xticklabels(["0", "pi/2", "pi", "3pi/2", "2pi"])
    ax.set_ylim(-1.0 / SCALE * 1.2, 1.0 / SCALE * 1.2)
    ax.set_ylabel("LUT - true")
    ax.set_xlabel("radians")
    ax.set_title("Quantization error  <=  1/65536  (~1.5e-5),\nidentical on every CPU",
                 fontsize=10.5, weight="bold")
    ax.grid(alpha=0.25)
    ax.ticklabel_format(axis="y", style="sci", scilimits=(-5, -5))

    # ---------- bottom-right: schema of runtime lookup ----------
    ax = ax_schema
    clean_ax(ax)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 4.6)

    box(ax, 0.3, 1.85, 2.5, 0.95,
        "radians (LFloat)\ninteger RawValue",
        fc=SOFT, fs=10.5, weight="bold")
    box(ax, 3.4, 1.85, 2.7, 0.95,
        "getIndex()\ninteger-only map\n-> 0..4095",
        fc=ASOFT, ec=AMBER, fs=10.5, weight="bold")
    box(ax, 6.7, 1.85, 3.0, 0.95,
        "sin_table[index]\nfixed-point int\n(zero float!)",
        fc=GSOFT, ec=GREEN, fs=10.5, weight="bold")
    arrow(ax, (2.8, 2.32), (3.4, 2.32), color=INK, lw=1.8)
    arrow(ax, (6.1, 2.32), (6.7, 2.32), color=INK, lw=1.8)

    label_on(ax, 5.0, 3.55,
             "Runtime path: pure integer arithmetic + 1 array read",
             color=INK, fs=11, weight="bold")
    label_on(ax, 5.0, 0.95,
             "Hardware float sin() is BANNED\n(trans-CPU lowest-bit drift = desync)",
             color=RED, fs=10.5, weight="bold")
    label_on(ax, 5.0, 0.30,
             "atan2 uses 64x64 2-D table;  asin/acos use 1024 entries",
             color=(0.30, 0.30, 0.30), fs=9.5)

    fig.savefig(os.path.join(OUT, "fig-03-03-trig-lut.png"))
    plt.close(fig)
    print("saved fig-03-03-trig-lut.png")


if __name__ == "__main__":
    fig_mul_three_paths()
    fig_truncate_vs_floor()
    fig_trig_lut()
    print("done")
