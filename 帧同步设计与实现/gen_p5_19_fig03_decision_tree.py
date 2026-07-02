# -*- coding: utf-8 -*-
"""
P5-19 Reconnect chapter figures.
fig-19-03: Catch-up decision tree (from-breakpoint vs jump-to-now).
  Two layers:
    Layer 1 (SOFT threshold, pre-judge at reconnect success):
        gap = serverTick - nextTick
        gap <= SnapshotThresholdFrames(300)  -> RequestMissFrames (incremental)
        gap  > SnapshotThresholdFrames(300)  -> RequestState (snapshot)
    Layer 2 (HARD wall, fallback when MissFrameResponse.IsExpired):
        incremental path -> server history (3600-frame ring) overwritten
        -> IsExpired=true -> RequestState (snapshot)
  Both snapshot branches: after LoadState (jump to checkpoint) STILL run
  incremental RequestMissFrames to fill the tail up to current frame.
Style: decision tree (diamonds + boxes), English labels, dpi 150, Agg backend.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle, Polygon
from matplotlib.lines import Line2D

plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

OUT_DIR = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images"


def gen_fig_19_03():
    fig, ax = plt.subplots(figsize=(15, 10.5))

    # ---- helpers -----------------------------------------------------------
    def box(cx, cy, w, h, text, facecolor, edgecolor, textcolor="black",
            fontsize=9, fontweight="normal", rounding=0.10, lw=1.5, z=5):
        rect = FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                              boxstyle=f"round,pad=0.02,rounding_size={rounding}",
                              facecolor=facecolor, edgecolor=edgecolor,
                              linewidth=lw, zorder=z)
        ax.add_patch(rect)
        ax.text(cx, cy, text, ha="center", va="center",
                fontsize=fontsize, fontweight=fontweight, color=textcolor, zorder=z + 1)

    def diamond(cx, cy, w, h, text, facecolor, edgecolor, textcolor="black",
                fontsize=8.6, fontweight="bold", z=5):
        pts = [(cx, cy + h / 2), (cx + w / 2, cy),
               (cx, cy - h / 2), (cx - w / 2, cy)]
        p = Polygon(pts, closed=True, facecolor=facecolor, edgecolor=edgecolor,
                    linewidth=1.6, zorder=z)
        ax.add_patch(p)
        ax.text(cx, cy, text, ha="center", va="center",
                fontsize=fontsize, fontweight=fontweight, color=textcolor, zorder=z + 1)

    def arrow(x1, y1, x2, y2, color="#444", lw=1.8, style="->", ls="-", z=4, rad=0.0):
        a = FancyArrowPatch((x1, y1), (x2, y2),
                            arrowstyle=style, color=color, lw=lw,
                            linestyle=ls, zorder=z,
                            connectionstyle=f"arc3,rad={rad}",
                            mutation_scale=16)
        ax.add_artist(a)

    # =======================================================================
    # ROOT: reconnect success entry
    # =======================================================================
    root_cx, root_cy = 7.5, 9.6
    box(root_cx, root_cy, 5.6, 0.85,
        "RECONNECT SUCCESS\nHandleReconnectedInternal  "
        "(serverTick from CurrentFrame, nextTick = GetNextNeededTick)",
        "#0b3d91", "#06245e", textcolor="white",
        fontsize=9.2, fontweight="bold")

    # gap computation note
    box(root_cx, root_cy - 0.85, 4.0, 0.42,
        "compute gap = serverTick - nextTick",
        "#eef4fc", "#0b3d91", fontsize=8.4)
    arrow(root_cx, root_cy - 0.43, root_cx, root_cy - 0.64, color="#0b3d91", lw=1.5)

    # =======================================================================
    # LAYER 1 diamond: SOFT threshold pre-judge
    # =======================================================================
    d1_cx, d1_cy = 7.5, 7.7
    diamond(d1_cx, d1_cy, 6.6, 1.5,
            "LAYER 1 (SOFT threshold):\n"
            "gap > SnapshotThresholdFrames?\n(default 300 = 15s @20fps)",
            "#fdf2c0", "#C8860B", textcolor="#5c3a06", fontsize=8.8)
    arrow(root_cx, root_cy - 1.07, d1_cx, d1_cy + 0.78, color="#0b3d91", lw=1.9)

    # layer-1 label tag
    ax.text(d1_cx - 3.6, d1_cy + 0.4,
            "LAYER 1\nsoft pre-judge",
            ha="center", va="center", fontsize=8.4, fontweight="bold",
            color="#C8860B", style="italic",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="#fff7d6",
                      edgecolor="#C8860B", linewidth=1.0))

    # =======================================================================
    # LAYER 1 LEFT branch (NO): incremental path
    # =======================================================================
    inc_cx, inc_cy = 3.0, 5.5
    box(inc_cx, inc_cy, 4.6, 0.9,
        "NO  ->  RequestMissFrames(nextTick)\nINCREMENTAL catch-up path",
        "#cfe8d4", "#2a7a2a", textcolor="#1E5E29",
        fontsize=9, fontweight="bold")
    arrow(d1_cx - 3.3, d1_cy, inc_cx + 0.3, inc_cy + 0.48,
          color="#2a7a2a", lw=2.0, rad=0.0)
    ax.text(4.4, 6.55, "NO (gap <= 300)", ha="center", va="center",
            fontsize=8.6, fontweight="bold", color="#2a7a2a",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="white",
                      edgecolor="#2a7a2a", linewidth=1.0))

    # incremental detail: server returns up to 600 frames/batch
    box(inc_cx, inc_cy - 1.15, 4.6, 0.85,
        "server: history ring buffer\nfetch max 600 frames/batch\n(MaxMissFramesPerRequest)",
        "#eef7f0", "#2a7a2a", textcolor="#1E5E29", fontsize=8.2)
    arrow(inc_cx, inc_cy - 0.48, inc_cx, inc_cy - 0.74, color="#2a7a2a", lw=1.5)

    # =======================================================================
    # LAYER 2 diamond: IsExpired hard-wall fallback (on incremental path)
    # =======================================================================
    d2_cx, d2_cy = 3.0, 3.1
    diamond(d2_cx, d2_cy, 5.0, 1.35,
            "LAYER 2 (HARD wall):\nMissFrameResponse\n.IsExpired?\n(startTick < minRetainedTick)",
            "#f5b8b8", "#a33a00", textcolor="#5c1a00", fontsize=8.0)
    arrow(inc_cx, inc_cy - 1.6, d2_cx, d2_cy + 0.7, color="#2a7a2a", lw=1.7)

    # layer-2 label tag
    ax.text(d2_cx - 3.0, d2_cy + 0.35,
            "LAYER 2\nhard-wall fallback",
            ha="center", va="center", fontsize=8.4, fontweight="bold",
            color="#a33a00", style="italic",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="#fff0ec",
                      edgecolor="#a33a00", linewidth=1.0))

    # =======================================================================
    # LAYER 1 RIGHT branch (YES): snapshot path
    # =======================================================================
    snap_cx, snap_cy = 12.0, 5.5
    box(snap_cx, snap_cy, 4.6, 0.9,
        "YES ->  RequestState()\nSNAPSHOT path (jump to now)",
        "#fde2b0", "#a33a00", textcolor="#5c1a00",
        fontsize=9, fontweight="bold")
    arrow(d1_cx + 3.3, d1_cy, snap_cx - 0.3, snap_cy + 0.48,
          color="#a33a00", lw=2.0)
    ax.text(10.6, 6.55, "YES (gap > 300)", ha="center", va="center",
            fontsize=8.6, fontweight="bold", color="#a33a00",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="white",
                      edgecolor="#a33a00", linewidth=1.0))

    # snapshot detail: server returns latest snapshot + hash
    box(snap_cx, snap_cy - 1.15, 4.6, 0.85,
        "server: latest snapshot\nSnapshotTick + data + hash\n(interval 60 frames)",
        "#fdf2c0", "#C8860B", textcolor="#5c3a06", fontsize=8.2)
    arrow(snap_cx, snap_cy - 0.48, snap_cx, snap_cy - 0.74, color="#a33a00", lw=1.5)

    # =======================================================================
    # IsExpired diamond: NO -> happy incremental result; YES -> fall to snapshot
    # =======================================================================
    # NO branch -> PushServerFrames caught up (incremental happy path)
    happy_cx, happy_cy = 0.9, 1.0
    box(happy_cx, happy_cy, 3.0, 0.95,
        "NO -> PushServerFrames\nlocal fast re-sim\nCAUGHT UP",
        "#cfe8d4", "#2a7a2a", textcolor="#1E5E29",
        fontsize=8.2, fontweight="bold")
    arrow(d2_cx - 2.5, d2_cy, happy_cx + 0.5, happy_cy + 0.5,
          color="#2a7a2a", lw=1.8, rad=0.1)
    ax.text(1.4, 2.5, "NO", ha="center", va="center",
            fontsize=8.4, fontweight="bold", color="#2a7a2a",
            bbox=dict(boxstyle="round,pad=0.15", facecolor="white",
                      edgecolor="#2a7a2a", linewidth=0.8))

    # YES branch -> RequestState (snapshot) -- joins the snapshot path
    box(7.5, d2_cy, 4.2, 0.85,
        "YES -> IsExpired: history ring\n(3600 frames) overwritten.\nFall to RequestState",
        "#f5b8b8", "#a33a00", textcolor="#5c1a00",
        fontsize=8.0, fontweight="bold")
    arrow(d2_cx + 2.5, d2_cy, 7.5 - 2.1, d2_cy,
          color="#a33a00", lw=2.0)
    ax.text(5.0, d2_cy + 0.42, "YES (rare)", ha="center", va="center",
            fontsize=8.4, fontweight="bold", color="#a33a00",
            bbox=dict(boxstyle="round,pad=0.15", facecolor="white",
                      edgecolor="#a33a00", linewidth=0.8))

    # =======================================================================
    # Snapshot path: LoadState -> reset -> STILL needs incremental tail
    # =======================================================================
    load_cx, load_cy = 12.0, 3.1
    box(load_cx, load_cy, 4.6, 1.0,
        "LoadState(snapshot)\nverify hash -> ResetTo(SnapshotTick)\njump to checkpoint",
        "#fde2b0", "#a33a00", textcolor="#5c1a00",
        fontsize=8.2, fontweight="bold")
    arrow(snap_cx, snap_cy - 1.6, load_cx, load_cy + 0.55, color="#a33a00", lw=1.7)
    # the IsExpired->RequestState also feeds into LoadState
    arrow(7.5 + 2.1, d2_cy, load_cx - 2.3, load_cy,
          color="#a33a00", lw=1.7, rad=-0.12)

    # =======================================================================
    # Both snapshot sources converge to: incremental tail fill
    # =======================================================================
    tail_cx, tail_cy = 12.0, 1.4
    box(tail_cx, tail_cy, 5.2, 0.95,
        "STILL run RequestMissFrames\nfill SnapshotTick -> serverTick tail\n(snapshot + incremental NOT mutually exclusive)",
        "#0b3d91", "#06245e", textcolor="white",
        fontsize=8.4, fontweight="bold")
    arrow(load_cx, load_cy - 0.55, tail_cx, tail_cy + 0.52, color="#0b3d91", lw=1.9)

    # final caught-up node (snapshot path)
    box(tail_cx, tail_cy - 1.35, 3.4, 0.7,
        "CAUGHT UP\n(jump + tail)",
        "#cfe8d4", "#2a7a2a", textcolor="#1E5E29",
        fontsize=9, fontweight="bold")
    arrow(tail_cx, tail_cy - 0.52, tail_cx, tail_cy - 1.0, color="#2a7a2a", lw=1.7)

    # ---- constraint side notes (the two hard constraints) -----------------
    # server history window 3600
    note1 = FancyBboxPatch((0.3, 7.7), 3.0, 1.2,
                           boxstyle="round,pad=0.02,rounding_size=0.08",
                           facecolor="#f4f0f9", edgecolor="#5a3a8a",
                           linewidth=1.3, zorder=4)
    ax.add_patch(note1)
    ax.text(1.8, 8.3,
            "HARD CONSTRAINT 1\nserver history ring\n= 3600 frames (3 min)\n_minRetainedTick",
            ha="center", va="center", fontsize=7.8, fontweight="bold",
            color="#3a2a6a", zorder=5)
    ax.text(1.8, 7.55, "defines the HARD wall",
            ha="center", fontsize=7.4, color="#5a3a8a", style="italic")

    # snapshot interval
    note2 = FancyBboxPatch((11.7, 7.7), 3.0, 1.2,
                           boxstyle="round,pad=0.02,rounding_size=0.08",
                           facecolor="#f4f0f9", edgecolor="#5a3a8a",
                           linewidth=1.3, zorder=4)
    ax.add_patch(note2)
    ax.text(13.2, 8.3,
            "HARD CONSTRAINT 2\nserver snapshot\nevery 60 frames (3s)\nbounded cache",
            ha="center", va="center", fontsize=7.8, fontweight="bold",
            color="#3a2a6a", zorder=5)
    ax.text(13.2, 7.55, "defines the SNAPSHOT path",
            ha="center", fontsize=7.4, color="#5a3a8a", style="italic")

    # =======================================================================
    # Bottom banner: soft + hard rationale
    # =======================================================================
    banner = Rectangle((0.3, -0.15), 14.4, 0.55,
                       facecolor="#5a3a8a", edgecolor="#3a2a6a", zorder=4)
    ax.add_patch(banner)
    ax.text(7.5, 0.12,
            "SOFT threshold (300) << HARD wall (3600):  "
            "pre-judge avoids 'try incremental -> hit wall -> retry snapshot' waste;  "
            "hard wall catches the rare boundary case",
            ha="center", va="center", fontsize=8.6, fontweight="bold",
            color="white", zorder=5)

    # =======================================================================
    # Axes cosmetics
    # =======================================================================
    ax.set_xlim(-0.2, 15.2)
    ax.set_ylim(-0.4, 10.4)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ("left", "right", "top", "bottom"):
        ax.spines[spine].set_visible(False)

    ax.set_title(
        "Catch-Up Decision Tree: From Breakpoint vs Jump to Now\n"
        "two-layer judge -- soft threshold pre-judge + hard-wall IsExpired fallback",
        fontsize=12.5, fontweight="bold", pad=14)

    # ---- Legend ------------------------------------------------------------
    legend_handles = [
        Line2D([0], [0], marker="D", color="#C8860B",
               markerfacecolor="#fdf2c0", markeredgecolor="#C8860B",
               markersize=11, linewidth=0,
               label="Layer 1 soft threshold (300 frames)"),
        Line2D([0], [0], marker="D", color="#a33a00",
               markerfacecolor="#f5b8b8", markeredgecolor="#a33a00",
               markersize=11, linewidth=0,
               label="Layer 2 hard-wall (IsExpired)"),
        Line2D([0], [0], marker="s", color="#2a7a2a",
               markerfacecolor="#cfe8d4", markeredgecolor="#2a7a2a",
               markersize=11, linewidth=0,
               label="Incremental path (RequestMissFrames)"),
        Line2D([0], [0], marker="s", color="#a33a00",
               markerfacecolor="#fde2b0", markeredgecolor="#a33a00",
               markersize=11, linewidth=0,
               label="Snapshot path (RequestState)"),
        Line2D([0], [0], marker="s", color="#5a3a8a",
               markerfacecolor="#f4f0f9", markeredgecolor="#5a3a8a",
               markersize=10, linewidth=0,
               label="Hard constraint note"),
    ]
    ax.legend(handles=legend_handles, loc="upper left",
              bbox_to_anchor=(1.005, 1.0), fontsize=8,
              framealpha=0.95, edgecolor="#CCCCCC")

    plt.tight_layout()
    out = f"{OUT_DIR}/fig-19-03-catchup-decision-tree.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"[saved] {out}")


if __name__ == "__main__":
    gen_fig_19_03()
