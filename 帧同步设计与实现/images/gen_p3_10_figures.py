# -*- coding: utf-8 -*-
"""Generate PNG figures for chapter P3-10 (LockstepController).

Two signature figures:
  fig-10-02  Pursue-frame throttle: three-tier step (smooth/normal/fast-forward)
             x-axis: tickGap = curTickInServer - confirmedTick (frames behind)
             y-axis: maxTicksThisUpdate (frames pursued per DoUpdate)
  fig-10-03  RingBuffer wrap-around & stale-slot aliasing (C-5 contract)
             capacity=2048, current tick=3000; Get(952) aliases slot holding
             frame 3000 data -> silent desync unless payload.Frame==tick check.

Style aligned with the lockstep series (DejaVu Sans, fixed palette,
clean_ax / box / arrow helpers, English labels, dpi 150, Agg backend).

Run: python gen_p3_10_figures.py
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Wedge, Circle, Rectangle

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 12.0,
    "axes.linewidth": 1.2,
    "figure.dpi": 150,
    "savefig.dpi": 150,
    "savefig.bbox": "tight",
    "savefig.facecolor": "white",
    "savefig.pad_inches": 0.15,
})

OUT = os.path.dirname(os.path.abspath(__file__))

# ---- palette ----
RED    = (0.86, 0.18, 0.18)
GREEN  = (0.20, 0.70, 0.32)
BLUE   = (0.20, 0.45, 0.88)
AMBER  = (0.95, 0.70, 0.18)
GREY   = (0.78, 0.78, 0.80)
INK    = (0.12, 0.12, 0.12)
SOFT   = (0.95, 0.95, 0.97)
GSOFT  = (0.90, 0.95, 0.90)   # smooth zone light green
ASOFT  = (0.98, 0.94, 0.82)   # normal zone light amber
RSOFT  = (0.98, 0.90, 0.90)   # fast-forward zone light red
BSOFT  = (0.90, 0.94, 1.0)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=11, weight="normal", tc=INK, lw=1.4):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=fc, edgecolor=ec, linewidth=lw))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.7, rad=0.0, style="-|>"):
    cs = f"arc3,rad={rad}"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle=style, color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=16))


def label_on(ax, x, y, text, color=INK, fs=10.5, weight="normal", ha="center", va="center"):
    ax.text(x, y, text, ha=ha, va=va, fontsize=fs, weight=weight, color=color)


# ============================================================ fig-10-02
# Pursue-frame throttle: three-tier step.
#   tickGap <= 20        -> maxTicksThisUpdate = 20   (Smooth zone)
#   20 < tickGap <= 100  -> maxTicksThisUpdate = 50   (Normal zone)
#   tickGap > 100        -> maxTicksThisUpdate = 100  (Fast-forward zone)
# Compare against "unlimited" (y=x, DoS risk at tickGap=600 -> 240ms freeze).
def fig_pursue_throttle():
    fig, ax = plt.subplots(figsize=(11.8, 7.0))

    # ---- step function: maxTicksThisUpdate vs tickGap ----
    x = np.linspace(0, 700, 2000)
    y = np.where(x <= 20, 20,
        np.where(x <= 100, 50, 100))

    # zone backgrounds (shaded bands on x)
    ax.axvspan(0,    20,  color=GSOFT, alpha=0.65)  # smooth
    ax.axvspan(20,  100,  color=ASOFT, alpha=0.65)  # normal
    ax.axvspan(100, 700,  color=RSOFT, alpha=0.55)  # fast-forward

    # the throttle step curve
    ax.step(x, y, where="post", color=INK, lw=2.6, zorder=5,
            label="maxTicksThisUpdate  (3-tier throttle)")

    # unlimited reference y = x (DoS risk). The line shoots off-chart fast,
    # so clip it to the visible y-range and mark where it crosses the top.
    Y_TOP = 135
    y_unlim = np.minimum(x, Y_TOP)
    # only draw unlimited where it is within view (x <= Y_TOP); beyond that
    # the line has left the chart -- we mark the exit point instead.
    mask = x <= Y_TOP
    ax.plot(x[mask], y_unlim[mask], "--", color=RED, lw=1.8, alpha=0.85,
            label="Unlimited  (y = tickGap, one-shot catch-up)")

    # tier value markers
    for xv, yv in [(20, 20), (100, 50), (300, 100)]:
        ax.plot(xv, yv, "o", color=BLUE, ms=8, zorder=6,
                markeredgecolor="white", markeredgewidth=1.4)
    # plateau value labels
    ax.text(10,  26, "20", ha="center", color=GREEN, weight="bold", fontsize=11)
    ax.text(60,  56, "50", ha="center", color=(0.65, 0.50, 0.05), weight="bold", fontsize=11)
    ax.text(400, 106, "100", ha="center", color=(0.62, 0.10, 0.10), weight="bold", fontsize=11)

    # ---- DoS marker: unlimited line exits chart at (135, 135); mark that ----
    exit_x, exit_y = 135, 135
    ax.plot(exit_x, exit_y, "X", color=RED, ms=14, zorder=7,
            markeredgecolor="white", markeredgewidth=1.6)
    # leader line along the (clipped) unlimited trajectory up to the X
    # annotation explaining the DoS at a representative tickGap=600
    ax.annotate("UNLIMITED shoots off-chart:\n"
                "tickGap=600 (30s reconnect)\n"
                "-> 600 frames x 0.4ms = 240ms freeze\n"
                "3-tier throttle caps it: 100 x 0.4ms = 40ms",
                xy=(exit_x, exit_y), xytext=(255, 70),
                fontsize=9.8, color=RED, weight="bold",
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.4))

    # zone labels at top
    ax.text(10,  118, "SMOOTH zone\n(gap small, catch up gently,\nplayer barely notices)",
            ha="center", va="bottom", color=(0.10, 0.45, 0.18), weight="bold", fontsize=9.8)
    ax.text(60,  118, "NORMAL zone\n(gap medium, accelerate catch-up)",
            ha="center", va="bottom", color=(0.55, 0.40, 0.05), weight="bold", fontsize=9.8)
    ax.text(400, 118, "FAST-FORWARD zone\n(reconnect, catch up ASAP,\nbrief fast-forward feel)",
            ha="center", va="bottom", color=(0.62, 0.10, 0.10), weight="bold", fontsize=9.8)

    # axis labels
    ax.set_xlabel("tickGap  =  curTickInServer - confirmedTick   (frames behind server)",
                  fontsize=11.5, weight="bold")
    ax.set_ylabel("maxTicksThisUpdate   (frames pursued in one DoUpdate)",
                  fontsize=11.5, weight="bold")
    ax.set_title("Pursue-frame dynamic throttle: bigger gap -> more frames per update\n"
                 "(smooth vs catch-up-ASAP tradeoff; caps single-DoUpdate CPU to avoid DoS)",
                 fontsize=12.5, weight="bold", pad=12)

    ax.set_xlim(0, 700)
    ax.set_ylim(0, 135)
    ax.set_xticks([0, 20, 100, 200, 300, 400, 500, 600, 700])
    ax.set_yticks([0, 20, 50, 100])
    ax.grid(True, axis="both", linestyle=":", alpha=0.45)
    ax.legend(loc="lower right", fontsize=10.5, framealpha=0.95)

    # threshold tick marks on top spine
    for xv in (20, 100):
        ax.axvline(xv, color=INK, lw=0.9, ls=":", alpha=0.55)

    # annotation: only NEW pursue frames are throttled
    ax.text(695, 12,
            "Only NEW pursue frames are throttled\n"
            "(isPursue = oldPredictedTick >= 0 && tick > oldPredictedTick);\n"
            "re-confirmed (already-predicted) frames skip this limit.",
            ha="right", va="bottom", fontsize=9, color=(0.30, 0.30, 0.30),
            style="italic",
            bbox=dict(boxstyle="round,pad=0.35", fc="white", ec=GREY, lw=0.8, alpha=0.9))

    fig.savefig(os.path.join(OUT, "fig-10-02-pursue-frame-throttle.png"))
    plt.close(fig)
    print("saved fig-10-02-pursue-frame-throttle.png")


# ============================================================ fig-10-03
# RingBuffer wrap-around & stale-slot aliasing (C-5 contract).
# capacity=2048 (2^11), mask=2047. Current tick=3000.
# Set(3000, ...) wrote to slot 3000 & 2047 = 952.
# Get(952) reads slot 952 -> returns frame 3000 data (stale alias) -> silent desync
# unless caller checks payload.Frame == tick.
def fig_ringbuffer_stale_slot():
    fig = plt.figure(figsize=(12.8, 7.6))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.35, 1.0], wspace=0.18)
    ax_ring = fig.add_subplot(gs[0, 0])
    ax_code = fig.add_subplot(gs[0, 1])

    # ============ LEFT: ring diagram ============
    ax = ax_ring
    clean_ax(ax)
    ax.set_xlim(-1.55, 1.55)
    ax.set_ylim(-1.55, 1.55)

    ax.text(0, 1.42,
            "RingBuffer  capacity=2048  (2^11),  mask=2047\n"
            "current tick = 3000   ->   Set(3000, ...) lands at slot 3000 & 2047 = 952",
            ha="center", va="center", fontsize=10.8, weight="bold", color=INK)

    # draw the ring as a thick circle of slots (use wedges)
    R_OUT = 1.05
    R_IN = 0.78
    N_VIS = 48  # visual slots (representing 2048 abstractly)
    theta0 = 90  # start at top
    # color logic: most slots grey, slot 952 region red (frame 3000 data),
    # a green "valid window" arc, slot for frame 3001 etc.
    for i in range(N_VIS):
        a0 = theta0 - (i / N_VIS) * 360
        a1 = theta0 - ((i + 1) / N_VIS) * 360
        # mark a few specific slots for annotation:
        # slot index visual i corresponds to abstract slot ~ i*(2048/N_VIS)
        slot_abs = int(i * (2048 / N_VIS))
        if 940 <= slot_abs <= 970:  # the danger zone (slot 952 lives here)
            fc = RED
            alpha = 0.55
        elif 0 <= slot_abs <= 100 or 1900 <= slot_abs:  # near window edges
            fc = BLUE
            alpha = 0.18
        else:
            fc = GREY
            alpha = 0.30
        w = Wedge((0, 0), R_OUT, a1, a0, width=R_OUT - R_IN,
                  facecolor=fc, edgecolor="white", linewidth=0.6, alpha=alpha)
        ax.add_patch(w)

    # center label
    ax.add_patch(Circle((0, 0), R_IN - 0.02, facecolor="white", edgecolor=INK, lw=1.4))
    ax.text(0, 0.08, "RingBuffer<T>", ha="center", va="center",
            fontsize=12, weight="bold", color=INK)
    ax.text(0, -0.10, "index & mask", ha="center", va="center",
            fontsize=10, color=(0.35, 0.35, 0.35))
    ax.text(0, -0.26, "(branchless wrap)", ha="center", va="center",
            fontsize=8.5, color=(0.45, 0.45, 0.45), style="italic")

    # ---- arrow 1: Set(3000) writes to slot 952 ----
    # slot 952 visual angle: theta = 90 - (952/2048)*360
    def slot_xy(slot, r):
        # map abstract slot -> visual angle (top start, clockwise)
        frac = slot / 2048.0
        ang = np.radians(90 - frac * 360)
        return r * np.cos(ang), r * np.sin(ang)

    # the danger slot 952 sits in the red zone; point a label to it
    sx, sy = slot_xy(952, (R_OUT + R_IN) / 2)
    # arrow from "Set(3000)" callout to the slot
    ax.annotate("",
                xy=(sx, sy), xytext=(-1.30, 0.55),
                arrowprops=dict(arrowstyle="-|>", color=RED, lw=2.0))
    ax.text(-1.45, 0.78,
            "Set(3000, frame3000Data)\n"
            "3000 & 2047 = 952\n"
            "-> writes slot 952",
            ha="left", va="center", fontsize=9.5, color=RED, weight="bold",
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=RED, lw=1.0))

    # ---- arrow 2: Get(952) reads same slot 952 ----
    ax.annotate("",
                xy=(sx, sy), xytext=(1.30, -0.55),
                arrowprops=dict(arrowstyle="-|>", color=AMBER, lw=2.0))
    ax.text(1.45, -0.78,
            "Get(952)\n"
            "952 & 2047 = 952\n"
            "-> reads SAME slot\n"
            "returns frame 3000 data!",
            ha="right", va="center", fontsize=9.5, color=(0.65, 0.45, 0.05), weight="bold",
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=AMBER, lw=1.0))

    # ---- valid window annotation ----
    # valid tick window = [3000-2048+1, 3000] = [953, 3000]
    ax.text(0, -1.18,
            "Valid tick window  =  [3000 - 2048 + 1, 3000]  =  [953, 3000]\n"
            "Only indices in this window can be read (still need payload.Frame==tick).\n"
            "Outside (e.g. 952, 500, -1) -> wraps to a STALE slot overwritten long ago.",
            ha="center", va="center", fontsize=9.5, color=INK,
            bbox=dict(boxstyle="round,pad=0.35", fc=SOFT, ec=INK, lw=1.0))

    # big red X for the silent desync hazard
    ax.text(-1.30, -0.50, "SILENT DESYNC",
            ha="left", va="center", fontsize=10.5, weight="bold", color=RED,
            bbox=dict(boxstyle="round,pad=0.3", fc=RSOFT, ec=RED, lw=1.4))

    ax.set_title("RingBuffer wrap-around: Get(952) aliases the slot that now holds frame 3000",
                 fontsize=11.5, weight="bold", pad=8)

    # ============ RIGHT: caller-discipline code pattern ============
    ax = ax_code
    clean_ax(ax)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)

    ax.text(5, 9.55, "C-5 contract: caller must validate payload.Frame",
            ha="center", va="center", fontsize=12, weight="bold", color=INK)
    ax.text(5, 9.05, "RingBuffer has NO bounds check by design (perf + generic)",
            ha="center", va="center", fontsize=9.8, color=(0.35, 0.35, 0.35), style="italic")

    # ---- WRONG: no frame check ----
    box(ax, 0.4, 6.55, 9.2, 1.85,
        "// WRONG: no freshness check\n"
        "var frame = serverFrames.Get(952);\n"
        "Console.WriteLine(frame.PlayerInputs[0]);\n"
        "//   ^ reads frame 3000 inputs as if they were frame 952!",
        fc=RSOFT, ec=RED, fs=9.8, weight="normal", tc=(0.30, 0.10, 0.10), lw=1.6)
    ax.text(0.7, 8.15, "X  no check",
            ha="left", va="center", fontsize=11, weight="bold", color=RED)

    # ---- RIGHT: payload.Frame == tick ----
    box(ax, 0.4, 3.55, 9.2, 2.35,
        "// CORRECT: validate payload.Frame == tick\n"
        "var frame = serverFrames.Get(tick);\n"
        "if (frame != null && frame.Frame == tick)   // C-5 freshness\n"
        "{\n"
        "    use(frame);   // genuine tick data, safe\n"
        "}",
        fc=GSOFT, ec=GREEN, fs=9.8, weight="normal", tc=(0.10, 0.30, 0.15), lw=1.6)
    ax.text(0.7, 5.65, "v  payload.Frame == tick",
            ha="left", va="center", fontsize=11, weight="bold", color=GREEN)

    # ---- the danger: uint has no embedded frame ----
    box(ax, 0.4, 1.10, 9.2, 1.95,
        "MOST DANGEROUS:  RingBuffer<uint>  _frameHashes\n"
        "uint has NO .Frame field -> cannot use payload.Frame==tick.\n"
        "Guarded only by range check:\n"
        "    tick > _confirmedTick - _frameHistorySize   (ValidateHash @810)",
        fc=ASOFT, ec=AMBER, fs=9.5, weight="normal", tc=(0.40, 0.30, 0.05), lw=1.6)
    ax.text(0.7, 2.90, "!  uint: range-check only",
            ha="left", va="center", fontsize=10.5, weight="bold", color=(0.65, 0.45, 0.05))

    # ---- bottom banner ----
    ax.text(5, 0.50,
            "Contract is upheld by DISCIPLINE, not by the type.\n"
            "Skip the check once -> silent desync thousands of frames later.",
            ha="center", va="center", fontsize=9.8, weight="bold", color=RED,
            bbox=dict(boxstyle="round,pad=0.35", fc="white", ec=RED, lw=1.2))

    fig.savefig(os.path.join(OUT, "fig-10-03-ringbuffer-stale-slot.png"))
    plt.close(fig)
    print("saved fig-10-03-ringbuffer-stale-slot.png")


if __name__ == "__main__":
    fig_pursue_throttle()
    fig_ringbuffer_stale_slot()
    print("done")
