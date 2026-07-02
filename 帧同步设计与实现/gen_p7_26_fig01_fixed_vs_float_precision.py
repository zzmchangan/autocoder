# -*- coding: utf-8 -*-
"""
P7-26 Collision & pathfinding (lightweight chapter).
fig-26-01: Fixed-point vs IEEE-float collision precision comparison.
  Left panel  -- FLOAT collision: same pair of bodies, CPU-A uses 80-bit extended
                precision (x87 FPU), CPU-B uses 64-bit double. The penetration depth
                differs in the last bit -> hash diverges -> desync.
  Right panel -- FIXED-POINT (LFloat, Q48.16) collision: RawValue is the SAME long on
                both machines. MulShiftFast drops the SAME low 16 bits every time.
                Result is bit-identical -> hash matches. The price is a 1/65536
                resolution floor: penetration below that is truncated to 0 -- which
                is exactly what GUARANTEES both machines agree.
Style: side-by-side comparison (red divergence vs green agreement).
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Circle
from matplotlib.lines import Line2D

plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

OUT_DIR = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images"


def gen_fig_26_01():
    fig, ax = plt.subplots(figsize=(16, 9.6))

    # ---- helpers -----------------------------------------------------------
    def panel(x0, y0, w, h, facecolor, edgecolor, lw=1.6, z=1):
        rect = FancyBboxPatch((x0, y0), w, h,
                              boxstyle="round,pad=0.02,rounding_size=0.12",
                              facecolor=facecolor, edgecolor=edgecolor,
                              linewidth=lw, zorder=z)
        ax.add_patch(rect)

    def box(cx, cy, w, h, text, facecolor, edgecolor, textcolor="black",
            fontsize=9, fontweight="normal", rounding=0.08, lw=1.4, z=5,
            family=None):
        rect = FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                              boxstyle=f"round,pad=0.02,rounding_size={rounding}",
                              facecolor=facecolor, edgecolor=edgecolor,
                              linewidth=lw, zorder=z)
        ax.add_patch(rect)
        ax.text(cx, cy, text, ha="center", va="center",
                fontsize=fontsize, fontweight=fontweight, color=textcolor,
                family=family if family else None, zorder=z + 1)

    def arrow(x1, y1, x2, y2, color="#444", lw=1.9, style="->", ls="-", z=4, rad=0.0):
        a = FancyArrowPatch((x1, y1), (x2, y2),
                            arrowstyle=style, color=color, lw=lw,
                            linestyle=ls, zorder=z,
                            connectionstyle=f"arc3,rad={rad}",
                            mutation_scale=16)
        ax.add_artist(a)

    # =======================================================================
    # Layout: two side-by-side panels sharing the figure.
    # Each panel: title band -> two stacked CPU rows (CPU-A / CPU-B) showing the
    #   computed penetration RawValue -> a result chip -> converges to a hash box.
    # =======================================================================

    # ---- shared title ------------------------------------------------------
    ax.text(8.0, 9.55,
            "Collision Precision:  why fixed-point (not float) in lockstep",
            ha="center", va="center", fontsize=14, fontweight="bold", color="#101010")
    ax.text(8.0, 9.12,
            "Same pair of bodies, same input -- the only question is whether two CPUs "
            "agree bit-for-bit.",
            ha="center", va="center", fontsize=9.6, color="#444", style="italic")

    # ---- vertical divider --------------------------------------------------
    ax.plot([8.0, 8.0], [0.6, 8.85], color="#bbbbbb", lw=1.2, linestyle="--", zorder=2)

    # =======================================================================
    # LEFT PANEL -- FLOAT (diverges)
    # =======================================================================
    L_X, L_Y, L_W, L_H = 0.4, 0.7, 7.3, 8.0
    panel(L_X, L_Y, L_W, L_H, "#fbeeee", "#a33a00", lw=1.8, z=1)

    # left title band
    lband = Rectangle((L_X + 0.15, L_Y + L_H - 0.78), L_W - 0.30, 0.62,
                      facecolor="#a33a00", edgecolor="#a33a00", zorder=3)
    ax.add_patch(lband)
    ax.text(L_X + L_W / 2, L_Y + L_H - 0.47,
            "FLOAT  (IEEE double / x87 extended)",
            ha="center", va="center", fontsize=11, fontweight="bold",
            color="white", zorder=4)

    # left subtitle
    ax.text(L_X + L_W / 2, L_Y + L_H - 1.12,
            "dist = Math.Sqrt(dx*dx + dy*dy)",
            ha="center", va="center", fontsize=9.2, color="#5c1a00",
            family="monospace", style="italic")

    # ---- CPU-A row (extended precision 80-bit) ----
    cpuA_y = 6.35
    box(L_X + 1.55, cpuA_y + 0.42, 2.5, 0.7,
        "CPU-A\nx87 FPU\n80-bit extended",
        "#fde2b0", "#a33a00", textcolor="#5c1a00",
        fontsize=8.4, fontweight="bold")
    # arrow to result
    arrow(L_X + 2.85, cpuA_y + 0.42, L_X + 3.65, cpuA_y + 0.42,
          color="#a33a00", lw=1.8)
    # result chip A
    box(L_X + 5.05, cpuA_y + 0.42, 2.15, 0.7,
        "penetration.RawValue\n= 0x000000C7\n(199)",
        "#ffffff", "#a33a00", textcolor="#5c1a00",
        fontsize=8.2, family="monospace", lw=1.4)

    # ---- CPU-B row (double 64-bit) ----
    cpuB_y = 4.95
    box(L_X + 1.55, cpuB_y + 0.42, 2.5, 0.7,
        "CPU-B\nSSE2 double\n64-bit",
        "#fde2b0", "#a33a00", textcolor="#5c1a00",
        fontsize=8.4, fontweight="bold")
    arrow(L_X + 2.85, cpuB_y + 0.42, L_X + 3.65, cpuB_y + 0.42,
          color="#a33a00", lw=1.8)
    box(L_X + 5.05, cpuB_y + 0.42, 2.15, 0.7,
        "penetration.RawValue\n= 0x000000C8\n(200)",
        "#ffffff", "#a33a00", textcolor="#5c1a00",
        fontsize=8.2, family="monospace", lw=1.4)

    # "last bit differs" callout between the two results
    arrow(L_X + 5.05, cpuA_y + 0.42 - 0.36, L_X + 5.05, cpuB_y + 0.42 + 0.36,
          color="#c0392b", lw=2.2, style="<->")
    ax.text(L_X + 5.05 - 1.45, (cpuA_y + cpuB_y) / 2 + 0.42,
            "last bit\ndiffers !", ha="center", va="center",
            fontsize=8.8, fontweight="bold", color="#c0392b",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="#fff0ec",
                      edgecolor="#c0392b", linewidth=1.0))

    # ---- reason note ----
    box(L_X + L_W / 2, 3.85, L_W - 0.7, 0.95,
        "Why they diverge:\n"
        "x87 keeps 80-bit extended precision mid-expression; SSE2 rounds to 64-bit "
        "every op.\nFPU rounding mode / FMA contraction / extended<->double storage "
        "all change the LSB.",
        "#fff7f5", "#c0392b", textcolor="#5c1a00",
        fontsize=8.0, lw=1.2)

    # ---- hash diverges ----
    box(L_X + L_W / 2, 2.45, L_W - 1.0, 0.95,
        "State hash XOR\n= 0x4F2A...91B7   !=   0x4F2A...91B6",
        "#f5b8b8", "#a33a00", textcolor="#5c1a00",
        fontsize=9.2, fontweight="bold", lw=1.6)
    ax.text(L_X + L_W / 2, 1.75,
            "=>  DESYNC:  clients walk different states forever",
            ha="center", va="center", fontsize=10, fontweight="bold",
            color="#a33a00",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#f5b8b8",
                      edgecolor="#a33a00", linewidth=1.6))

    # big red X badge top-right of left panel
    ax.text(L_X + L_W - 0.55, L_Y + L_H - 0.47, "X",
            ha="center", va="center", fontsize=15, fontweight="bold",
            color="#a33a00",
            bbox=dict(boxstyle="circle,pad=0.3", facecolor="#f5b8b8",
                      edgecolor="#a33a00", linewidth=1.6), zorder=6)

    # =======================================================================
    # RIGHT PANEL -- FIXED-POINT (agrees)
    # =======================================================================
    R_X, R_Y, R_W, R_H = 8.3, 0.7, 7.3, 8.0
    panel(R_X, R_Y, R_W, R_H, "#eef7f0", "#2a7a2a", lw=1.8, z=1)

    # right title band
    rband = Rectangle((R_X + 0.15, R_Y + R_H - 0.78), R_W - 0.30, 0.62,
                      facecolor="#2a7a2a", edgecolor="#2a7a2a", zorder=3)
    ax.add_patch(rband)
    ax.text(R_X + R_W / 2, R_Y + R_H - 0.47,
            "FIXED-POINT  (LFloat,  Q48.16)",
            ha="center", va="center", fontsize=11, fontweight="bold",
            color="white", zorder=4)

    # right subtitle
    ax.text(R_X + R_W / 2, R_Y + R_H - 1.12,
            "dist = LFloat.Sqrt(distSqr)   (Newton, deterministic)",
            ha="center", va="center", fontsize=9.2, color="#1E5E29",
            family="monospace", style="italic")

    # ---- CPU-A row (any CPU) ----
    box(R_X + 1.55, cpuA_y + 0.42, 2.5, 0.7,
        "CPU-A\n(x87 / ARM / x64\nANY runtime)",
        "#cfe8d4", "#2a7a2a", textcolor="#1E5E29",
        fontsize=8.4, fontweight="bold")
    arrow(R_X + 2.85, cpuA_y + 0.42, R_X + 3.65, cpuA_y + 0.42,
          color="#2a7a2a", lw=1.8)
    box(R_X + 5.05, cpuA_y + 0.42, 2.15, 0.7,
        "penetration.RawValue\n= 0x000000C7\n(199)",
        "#ffffff", "#2a7a2a", textcolor="#1E5E29",
        fontsize=8.2, family="monospace", lw=1.4)

    # ---- CPU-B row (any CPU) ----
    box(R_X + 1.55, cpuB_y + 0.42, 2.5, 0.7,
        "CPU-B\n(different vendor\nANY runtime)",
        "#cfe8d4", "#2a7a2a", textcolor="#1E5E29",
        fontsize=8.4, fontweight="bold")
    arrow(R_X + 2.85, cpuB_y + 0.42, R_X + 3.65, cpuB_y + 0.42,
          color="#2a7a2a", lw=1.8)
    box(R_X + 5.05, cpuB_y + 0.42, 2.15, 0.7,
        "penetration.RawValue\n= 0x000000C7\n(199)",
        "#ffffff", "#2a7a2a", textcolor="#1E5E29",
        fontsize=8.2, family="monospace", lw=1.4)

    # "identical" callout between the two results
    arrow(R_X + 5.05, cpuA_y + 0.42 - 0.36, R_X + 5.05, cpuB_y + 0.42 + 0.36,
          color="#2a7a2a", lw=2.2, style="<->")
    ax.text(R_X + 5.05 - 1.45, (cpuA_y + cpuB_y) / 2 + 0.42,
            "bit-identical\n(same long)", ha="center", va="center",
            fontsize=8.8, fontweight="bold", color="#1E5E29",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="#eaf6ec",
                      edgecolor="#2a7a2a", linewidth=1.0))

    # ---- reason note ----
    box(R_X + R_W / 2, 3.85, R_W - 0.7, 0.95,
        "Why they agree:\n"
        "LFloat stores a long (RawValue). + - * are integer ops. "
        "MulShiftFast shifts right\nby EXACTLY 16 bits on every CPU -- the same "
        "low bits are dropped. No FPU, no rounding mode.",
        "#f3faf4", "#2a7a2a", textcolor="#1E5E29",
        fontsize=8.0, lw=1.2)

    # ---- hash matches ----
    box(R_X + R_W / 2, 2.45, R_W - 1.0, 0.95,
        "State hash XOR\n= 0x82C1...3D4E   ==   0x82C1...3D4E",
        "#cfe8d4", "#2a7a2a", textcolor="#1E5E29",
        fontsize=9.2, fontweight="bold", lw=1.6)

    # ---- the trade-off (resolution floor) ----
    ax.text(R_X + R_W / 2, 1.75,
            "=>  IN SYNC.   Trade-off: 1/65536 floor  "
            "(tiny penetration -> 0, same on both)",
            ha="center", va="center", fontsize=9.2, fontweight="bold",
            color="#1E5E29",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#cfe8d4",
                      edgecolor="#2a7a2a", linewidth=1.6))

    # big green check badge top-right of right panel
    ax.text(R_X + R_W - 0.55, R_Y + R_H - 0.47, "OK",
            ha="center", va="center", fontsize=13, fontweight="bold",
            color="#1E5E29",
            bbox=dict(boxstyle="circle,pad=0.3", facecolor="#cfe8d4",
                      edgecolor="#2a7a2a", linewidth=1.6), zorder=6)

    # =======================================================================
    # Bottom take-away strip (spans both panels)
    # =======================================================================
    take = Rectangle((0.4, 0.12), 15.2, 0.42,
                     facecolor="#0b3d91", edgecolor="#06245e", zorder=3)
    ax.add_patch(take)
    ax.text(8.0, 0.33,
            "TAKE-AWAY:   in lockstep,  consistency  >  precision.   "
            "A deterministic resolution floor beats a non-deterministic full-mantissa.",
            ha="center", va="center", fontsize=9.8, fontweight="bold",
            color="white", zorder=4)

    # =======================================================================
    # Axes cosmetics
    # =======================================================================
    ax.set_xlim(0.0, 16.0)
    ax.set_ylim(-0.1, 9.9)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ("left", "right", "top", "bottom"):
        ax.spines[spine].set_visible(False)

    # ---- Legend ------------------------------------------------------------
    legend_handles = [
        Line2D([0], [0], marker="s", color="#a33a00", markerfacecolor="#f5b8b8",
               markeredgecolor="#a33a00", markersize=11, linewidth=0,
               label="float path (diverges)"),
        Line2D([0], [0], marker="s", color="#2a7a2a", markerfacecolor="#cfe8d4",
               markeredgecolor="#2a7a2a", markersize=11, linewidth=0,
               label="fixed-point path (agrees)"),
        Line2D([0], [0], color="#c0392b", lw=2.2, marker="<", markersize=8,
               label="last-bit mismatch"),
        Line2D([0], [0], color="#2a7a2a", lw=2.2, marker="<", markersize=8,
               label="bit-identical result"),
    ]
    ax.legend(handles=legend_handles, loc="upper left",
              bbox_to_anchor=(1.005, 1.0), fontsize=8,
              framealpha=0.95, edgecolor="#CCCCCC")

    plt.tight_layout()
    out = f"{OUT_DIR}/fig-26-01-fixed-vs-float-precision.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"[saved] {out}")


if __name__ == "__main__":
    gen_fig_26_01()
