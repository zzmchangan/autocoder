# -*- coding: utf-8 -*-
"""
P4-15 GameRoom chapter figures.
fig-15-02: Poisoned-snapshot circuit breaker (graceful degradation).
Three columns: Normal path (green) / Fault fires (red) / After circuit-break (yellow),
plus a bottom reconnect branch showing fallback to miss-frame.
Style matches the book (FancyBboxPatch, English labels, dpi 150, Agg backend).
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle
from matplotlib.lines import Line2D

plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

OUT_DIR = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images"


def gen_fig_15_02():
    fig, ax = plt.subplots(figsize=(15, 9.5))

    # ---------- helpers ----------
    def box(cx, cy, w, h, text, facecolor, edgecolor, textcolor="black",
            fontsize=9, fontweight="normal", rounding=0.10, lw=1.4, z=5):
        rect = FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                              boxstyle=f"round,pad=0.02,rounding_size={rounding}",
                              facecolor=facecolor, edgecolor=edgecolor,
                              linewidth=lw, zorder=z)
        ax.add_patch(rect)
        ax.text(cx, cy, text, ha="center", va="center",
                fontsize=fontsize, fontweight=fontweight, color=textcolor,
                zorder=z + 1)

    def diamond(cx, cy, w, h, text, facecolor, edgecolor, textcolor="black",
                fontsize=8.5, fontweight="bold", z=5):
        from matplotlib.patches import Polygon
        pts = [(cx, cy + h / 2), (cx + w / 2, cy),
               (cx, cy - h / 2), (cx - w / 2, cy)]
        p = Polygon(pts, closed=True, facecolor=facecolor, edgecolor=edgecolor,
                    linewidth=1.4, zorder=z)
        ax.add_patch(p)
        ax.text(cx, cy, text, ha="center", va="center",
                fontsize=fontsize, fontweight=fontweight, color=textcolor,
                zorder=z + 1)

    def arrow(x1, y1, x2, y2, color="#444", lw=1.7, style="->", ls="-", z=4,
              rad=0.0):
        a = FancyArrowPatch((x1, y1), (x2, y2),
                            arrowstyle=style, color=color, lw=lw,
                            linestyle=ls, zorder=z,
                            connectionstyle=f"arc3,rad={rad}",
                            mutation_scale=15)
        ax.add_artist(a)

    # ---------- column x-centers ----------
    x_normal  = 2.5     # left column: normal path
    x_fault   = 7.5     # middle column: fault fires
    x_breaker = 12.5    # right column: after circuit-break
    col_w = 3.6

    # ---------- column header bands ----------
    headers = [
        (x_normal,  "1. NORMAL PATH\n(sim healthy)",  "#cfe8d4", "#2a7a2a", "#1E5E29"),
        (x_fault,   "2. FAULT FIRES\n(sim.Tick throws)", "#f5b8b8", "#a33a00", "#5c1a00"),
        (x_breaker, "3. AFTER CIRCUIT-BREAK\n(graceful degradation)", "#fdf2c0", "#C8860B", "#5c3a06"),
    ]
    for cx, txt, fc, ec, tc in headers:
        rect = FancyBboxPatch((cx - col_w / 2, 8.55), col_w, 0.7,
                              boxstyle="round,pad=0.02,rounding_size=0.08",
                              facecolor=fc, edgecolor=ec, linewidth=1.8, zorder=4)
        ax.add_patch(rect)
        ax.text(cx, 8.9, txt, ha="center", va="center",
                fontsize=10, fontweight="bold", color=tc, zorder=5)

    # =======================================================================
    # COLUMN 1: NORMAL PATH (green)
    # =======================================================================
    box(x_normal, 7.7, col_w - 0.3, 0.55,
        "BroadcastTick (tick % SnapshotInterval)",
        "#eaf5ec", "#2a7a2a", fontsize=8.5, fontweight="bold")
    box(x_normal, 6.85, col_w - 0.3, 0.55,
        "sim.Tick(frameData)\n-> World advances (consistent)",
        "#cfe8d4", "#2a7a2a", fontsize=8.3, fontweight="bold")
    box(x_normal, 6.0, col_w - 0.3, 0.55,
        "ComputeHash() -> honest hash\nSaveState() -> honest snapshot",
        "#cfe8d4", "#2a7a2a", fontsize=8.3, fontweight="bold")
    box(x_normal, 5.15, col_w - 0.3, 0.55,
        "_snapshots[tick] / _snapshotHashes[tick]\n= clean authoritative state",
        "#eaf5ec", "#2a7a2a", fontsize=8.2)
    box(x_normal, 4.3, col_w - 0.3, 0.55,
        "broadcast ServerFrameMessage\n(+ redundant frames)",
        "#cfe8d4", "#2a7a2a", fontsize=8.3, fontweight="bold")
    # arrows down column 1
    for y1, y2 in [(7.42, 7.15), (6.57, 6.30), (5.72, 5.45), (4.87, 4.60)]:
        arrow(x_normal, y1, x_normal, y2, color="#2a7a2a", lw=1.6)

    # =======================================================================
    # COLUMN 2: FAULT FIRES (red)
    # =======================================================================
    box(x_fault, 7.7, col_w - 0.3, 0.55,
        "BroadcastTick (same loop)",
        "#fdeeee", "#a33a00", fontsize=8.5, fontweight="bold")
    box(x_fault, 6.85, col_w - 0.4, 0.65,
        "sim.Tick(frameData)  -->  THROWS\n(bug / memory corruption)",
        "#f5b8b8", "#a33a00", fontsize=8.3, fontweight="bold")
    # danger emphasis: World half-modified
    box(x_fault, 5.95, col_w - 0.2, 0.55,
        "World is HALF-MODIFIED\n(inconsistent, non-transactional)",
        "#f5d05c", "#a33a00", fontsize=8.3, fontweight="bold")
    # poisoned outputs (what naive approach would emit)
    box(x_fault, 5.0, col_w - 0.2, 0.6,
        "NAIVE: keep running sim\n-> POISONED hash + POISONED snapshot",
        "#f5b8b8", "#5c1a00", fontsize=7.8,
        fontweight="bold", textcolor="#5c1a00")
    box(x_fault, 4.1, col_w - 0.2, 0.6,
        "poisoned state would:\n* misreport all clients as desync\n* corrupt reconnect via StateRequest",
        "#fdeeee", "#a33a00", fontsize=7.5, textcolor="#5c1a00")
    # arrows down column 2
    for y1, y2 in [(7.42, 7.20), (6.50, 6.25), (5.65, 5.32), (4.68, 4.42)]:
        arrow(x_fault, y1, x_fault, y2, color="#a33a00", lw=1.6)
    # big X mark on the throw
    ax.scatter([x_fault], [6.85], marker="X", s=420,
               facecolors="none", edgecolors="#a33a00",
               linewidths=2.2, zorder=7)

    # =======================================================================
    # COLUMN 3: AFTER CIRCUIT-BREAK (yellow) -- the three steps
    # =======================================================================
    # Step 1: set flag
    box(x_breaker, 7.7, col_w - 0.3, 0.55,
        "catch (Exception)  -->  three-step breaker",
        "#fdf2c0", "#C8860B", fontsize=8.5, fontweight="bold")

    box(x_breaker, 6.8, col_w - 0.2, 0.6,
        "STEP 1:  _serverSimFaulted = true\n(volatile bool)",
        "#fdf2c0", "#C8860B", fontsize=8.3, fontweight="bold",
        textcolor="#5c3a06")

    box(x_breaker, 5.9, col_w - 0.2, 0.6,
        "STEP 2:  _snapshots.Clear()\n          _snapshotHashes.Clear()",
        "#fdf2c0", "#C8860B", fontsize=8.3, fontweight="bold",
        textcolor="#5c3a06")

    box(x_breaker, 5.0, col_w - 0.2, 0.6,
        "STEP 3:  BroadcastTick skips sim block\n(if (!_serverSimFaulted) is now false)",
        "#fdf2c0", "#C8860B", fontsize=8.2, fontweight="bold",
        textcolor="#5c3a06")

    # room keeps forwarding frames (the win)
    box(x_breaker, 4.1, col_w - 0.2, 0.62,
        "RESULT: room keeps forwarding frames\n(frame path needs only _tickInputs,\nNOT sim)  ->  clients unaffected",
        "#cfe8d4", "#2a7a2a", fontsize=8.0, fontweight="bold",
        textcolor="#1E5E29")
    # arrows down column 3
    for y1, y2 in [(7.42, 7.12), (6.50, 6.22), (5.60, 5.32), (4.70, 4.43)]:
        arrow(x_breaker, y1, x_breaker, y2, color="#C8860B", lw=1.6)

    # =======================================================================
    # Transition arrows between columns
    # =======================================================================
    # normal -> fault (fault happens)
    arrow(x_normal + col_w / 2, 6.85, x_fault - col_w / 2, 6.85,
          color="#a33a00", lw=2.0, rad=-0.08)
    ax.text((x_normal + x_fault) / 2, 7.15, "fault occurs",
            ha="center", fontsize=8.5, color="#a33a00",
            fontweight="bold", style="italic")
    # fault -> breaker (catch triggers breaker)
    arrow(x_fault + col_w / 2, 7.0, x_breaker - col_w / 2, 7.0,
          color="#C8860B", lw=2.0, rad=-0.08)
    ax.text((x_fault + x_breaker) / 2, 7.3, "catch -> breaker",
            ha="center", fontsize=8.5, color="#5c3a06",
            fontweight="bold", style="italic")

    # =======================================================================
    # BOTTOM: Reconnect degradation branch
    # =======================================================================
    # divider line
    ax.hlines(3.35, 0.5, 14.5, colors="#888888", linestyles="-",
              linewidth=1.2, zorder=2)
    ax.text(0.6, 3.5, "RECONNECT DEGRADATION  (after breaker)",
            ha="left", va="bottom", fontsize=9.5, fontweight="bold",
            color="#5c3a06")

    # reconnect client tries StateRequest
    box(2.6, 2.7, 3.6, 0.55,
        "Reconnect client -> StateRequest\n(wants snapshot)",
        "#eaf5ec", "#2a7a2a", fontsize=8.3, fontweight="bold")

    # GetSnapshot guard returns tick=-1
    diamond(7.0, 2.7, 2.6, 0.85,
            "GetSnapshot:\nif(_serverSimFaulted)\nreturn tick = -1",
            "#fdf2c0", "#C8860B", fontsize=7.8)
    arrow(4.4, 2.7, 5.7, 2.7, color="#2a7a2a", lw=1.7)

    # no snapshot -> fall back to miss-frame
    box(11.6, 2.7, 3.8, 0.55,
        "FALLBACK: MissFrameRequest\n-> GetMissFrames (HISTORY BUFFER)",
        "#cfe8d4", "#2a7a2a", fontsize=8.3, fontweight="bold",
        textcolor="#1E5E29")
    arrow(8.3, 2.7, 9.7, 2.7, color="#C8860B", lw=1.8)
    ax.text(9.0, 2.95, "no snapshot", ha="center", fontsize=7.8,
            color="#5c3a06", fontweight="bold", style="italic")

    # miss-frame is the Relay path -- validated
    box(11.6, 1.75, 4.6, 0.55,
        "= Relay mode's only reconnect path\n(healthy, frame-based, no sim)",
        "#dccfeb", "#5a3a8a", fontsize=8.0, fontweight="bold",
        textcolor="#3a2a6a")
    arrow(11.6, 2.42, 11.6, 2.05, color="#2a7a2a", lw=1.5)

    # left note: why miss-frame is reliable
    box(3.4, 1.75, 5.6, 0.55,
        "miss-frame reads _historyBuffer (frame data)\nwhich NEVER depended on the faulted sim",
        "#eef4fc", "#0b3d91", fontsize=7.8, textcolor="#1F3F6B")
    arrow(5.6, 2.42, 6.2, 2.05, color="#0b3d91", lw=1.3, ls="--")

    # =======================================================================
    # Summary banner
    # =======================================================================
    banner = Rectangle((0.5, 0.55), 14.0, 0.62,
                       facecolor="#0b3d91", edgecolor="#06245e", zorder=4)
    ax.add_patch(banner)
    ax.text(7.5, 0.86,
            "Circuit Breaker  =  dynamically degrade Authoritative mode -> Relay mode  "
            "(stop sim + purge poisoned snapshots + keep forwarding)",
            ha="center", va="center", fontsize=9.3, fontweight="bold",
            color="white", zorder=5)
    ax.text(7.5, 0.66,
            "Poisoned Snapshot: a faulty sim disguising its corruption as authoritative state",
            ha="center", va="center", fontsize=8, style="italic",
            color="#fdf2c0", zorder=5)

    # =======================================================================
    # Axes cosmetics
    # =======================================================================
    ax.set_xlim(0.3, 14.8)
    ax.set_ylim(0.4, 9.6)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ("left", "right", "top", "bottom"):
        ax.spines[spine].set_visible(False)

    ax.set_title(
        "Poisoned-Snapshot Circuit Breaker  "
        "(fault sim -> graceful degradation, NOT room crash)",
        fontsize=13, fontweight="bold", pad=12)

    # Legend
    legend_handles = [
        Line2D([0], [0], marker="s", color="none", markerfacecolor="#cfe8d4",
               markeredgecolor="#2a7a2a", markersize=11,
               label="Healthy sim path (snapshot OK)"),
        Line2D([0], [0], marker="X", color="none", markerfacecolor="none",
               markeredgecolor="#a33a00", markersize=12, markeredgewidth=2.0,
               label="sim.Tick throws (World half-modified)"),
        Line2D([0], [0], marker="s", color="none", markerfacecolor="#fdf2c0",
               markeredgecolor="#C8860B", markersize=11,
               label="Circuit-break step (flag / purge / skip)"),
        Line2D([0], [0], marker="D", color="none", markerfacecolor="#fdf2c0",
               markeredgecolor="#C8860B", markersize=10,
               label="GetSnapshot guard returns tick=-1"),
        Line2D([0], [0], marker="s", color="none", markerfacecolor="#dccfeb",
               markeredgecolor="#5a3a8a", markersize=11,
               label="Fallback to miss-frame (Relay-mode path)"),
    ]
    ax.legend(handles=legend_handles, loc="upper left",
              bbox_to_anchor=(1.005, 1.0), fontsize=8,
              framealpha=0.95, edgecolor="#CCCCCC")

    plt.tight_layout()
    out = f"{OUT_DIR}/fig-15-poisoned-snapshot-circuit-breaker.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"[saved] {out}")


if __name__ == "__main__":
    gen_fig_15_02()
