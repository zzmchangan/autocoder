# -*- coding: utf-8 -*-
"""
P3-08 Prediction chapter figures.
fig-08-01: Prediction timeline (local predicted tick leads server confirmed tick).
Style matches fig-09-01 rollback timeline (two horizontal lanes, shaded regions,
arrows, English labels, dpi 150, Agg backend).
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
import matplotlib.font_manager as fm

# Use a clean sans-serif; fall back silently if DejaVu isn't present
plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

OUT_DIR = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images"


# ---------------------------------------------------------------------------
# fig-08-01  Prediction timeline
# ---------------------------------------------------------------------------
def gen_fig_08_01():
    fig, ax = plt.subplots(figsize=(13, 6.2))

    # Geometry
    y_local = 1.0      # top lane: local predicted tick
    y_server = 0.0     # bottom lane: server authoritative tick

    # Tick domain
    tick_min, tick_max = 0, 14
    N = 6              # server authoritative tick that gets broadcast
    K = 4              # prediction depth in frames = RTT in frames
    arrive_tick = N + K   # tick at which client receives tick N  (== 10)

    # --- Lane baselines -----------------------------------------------------
    ax.hlines(y_local, tick_min, tick_max, colors="#888888",
              linestyles="--", linewidth=1.0, zorder=1)
    ax.hlines(y_server, tick_min, tick_max, colors="#888888",
              linestyles="--", linewidth=1.0, zorder=1)

    # --- Shaded region: prediction depth = K frames -------------------------
    # The client has predicted up to N+K while only confirmed up to N-1
    # (the authoritative tick N hasn't arrived yet). Shade frames [N, N+K].
    pred_band = ax.axvspan(N, N + K, ymin=0.08, ymax=0.92,
                           color="#FFE9B0", alpha=0.55, zorder=0,
                           label="_nolegend_")
    # Bracket + label for prediction depth
    brace_y = y_local + 0.55
    ax.annotate("", xy=(N, brace_y), xytext=(N + K, brace_y),
                arrowprops=dict(arrowstyle="<->", color="#C8860B",
                                lw=1.6), zorder=4)
    ax.text(N + K / 2, brace_y + 0.12,
            "Prediction depth = K frames  (predicted - confirmed)",
            ha="center", va="bottom", fontsize=10.5, color="#C8860B",
            fontweight="bold")

    # --- Server lane: authoritative ticks 0..N (broadcast at N) -------------
    for t in range(0, N + 1):
        ax.scatter([t], [y_server], marker="s", s=130,
                   color="#3B7CD3", edgecolors="#1F3F6B",
                   linewidths=1.0, zorder=5)
    # After N, server has authoritative frames N+1.. but they haven't arrived
    # yet at the moment shown; draw them faded as "in flight / future".
    for t in range(N + 1, N + K + 1):
        ax.scatter([t], [y_server], marker="s", s=110,
                   facecolors="white", edgecolors="#3B7CD3",
                   linewidths=1.4, zorder=5)

    # --- Local lane: predicted ticks ---------------------------------------
    # Confirmed-correct (matched server) ticks: 0 .. N-1
    for t in range(0, N):
        ax.scatter([t], [y_local], marker="o", s=130,
                   color="#3FA34D", edgecolors="#1E5E29",
                   linewidths=1.0, zorder=5)
    # Predicted-ahead ticks: N .. N+K (in the prediction-depth band)
    for t in range(N, N + K + 1):
        ax.scatter([t], [y_local], marker="o", s=120,
                   facecolors="white", edgecolors="#C8860B",
                   linewidths=1.6, zorder=5)

    # --- Confirm / Rollback fork at tick N (on local lane) -----------------
    ax.scatter([N], [y_local], marker="D", s=210,
               color="#C8860B", edgecolors="#5C3A06",
               linewidths=1.2, zorder=6)
    ax.annotate("Confirm or Rollback\nat tick N",
                xy=(N, y_local), xytext=(N - 0.2, y_local + 1.15),
                fontsize=10, color="#5C3A06", ha="center", fontweight="bold",
                arrowprops=dict(arrowstyle="->", color="#5C3A06", lw=1.3))

    # --- RTT arrow: server broadcasts N at tick N, client receives at N+K ---
    rtt_y = y_server - 0.75
    # broadcast point
    ax.scatter([N], [y_server], marker="v", s=120,
               color="#1F3F6B", edgecolors="black", linewidths=0.8, zorder=6)
    # arrival point on server lane (representing receipt moment N+K)
    ax.scatter([arrive_tick], [y_server], marker="^", s=140,
               color="#1F3F6B", edgecolors="black", linewidths=0.8, zorder=6)

    rtt_arrow = FancyArrowPatch((N, rtt_y), (arrive_tick, rtt_y),
                                arrowstyle="<->", color="#1F3F6B",
                                lw=1.8, zorder=4)
    ax.add_artist(rtt_arrow)
    ax.text((N + arrive_tick) / 2, rtt_y - 0.28,
            "RTT  (broadcast tick N  ->  client receives tick N)",
            ha="center", va="top", fontsize=10, color="#1F3F6B",
            fontweight="bold")

    # small dotted leaders from rtt arrow ends to the lane points
    ax.plot([N, N], [rtt_y, y_server], color="#1F3F6B",
            linestyle=":", lw=1.0, zorder=2)
    ax.plot([arrive_tick, arrive_tick], [rtt_y, y_server], color="#1F3F6B",
            linestyle=":", lw=1.0, zorder=2)
    # also a leader from arrival (N+K) up to local predicted tick N+K
    ax.plot([arrive_tick, arrive_tick],
            [y_server, y_local], color="#5C3A06",
            linestyle="--", lw=1.1, zorder=2)

    # --- Lane labels --------------------------------------------------------
    ax.text(-0.6, y_local, "Local predicted tick\n(_predictedTick)",
            ha="right", va="center", fontsize=11, fontweight="bold",
            color="#1E5E29")
    ax.text(-0.6, y_server, "Server authoritative tick\n(_confirmedTick)",
            ha="right", va="center", fontsize=11, fontweight="bold",
            color="#1F3F6B")

    # annotations for the two categories on lanes
    ax.text(N / 2 - 0.3, y_local + 0.32, "Confirmed (matched server)",
            ha="center", fontsize=8.5, color="#1E5E29", style="italic")
    ax.text(N + K / 2, y_local + 0.32, "Predicted ahead (local injects + guessed)",
            ha="center", fontsize=8.5, color="#C8860B", style="italic")

    ax.text(N / 2 - 0.3, y_server - 0.34, "Already broadcast",
            ha="center", fontsize=8.5, color="#1F3F6B", style="italic")
    ax.text(N + K / 2 + 0.4, y_server - 0.34,
            "In flight / not yet received",
            ha="center", fontsize=8.5, color="#3B7CD3", style="italic")

    # --- Axes cosmetics -----------------------------------------------------
    ax.set_xlim(-2.2, tick_max + 0.8)
    ax.set_ylim(-1.7, 2.5)
    ax.set_xlabel("Frame tick (time)", fontsize=12)
    ax.set_xticks(range(0, tick_max + 1))
    ax.set_yticks([])
    for spine in ("left", "right", "top"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color("#888888")

    # tick label N and N+K emphasized
    for lbl in ax.get_xticklabels():
        if lbl.get_text() == str(N):
            lbl.set_color("#5C3A06"); lbl.set_fontweight("bold")
        elif lbl.get_text() == str(arrive_tick):
            lbl.set_color("#1F3F6B"); lbl.set_fontweight("bold")

    ax.set_title(
        "Prediction Timeline: Local Predicted Tick Leads Server Confirmed Tick",
        fontsize=13, fontweight="bold", pad=14)

    # --- Legend -------------------------------------------------------------
    from matplotlib.lines import Line2D
    legend_handles = [
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#3FA34D",
               markeredgecolor="#1E5E29", markersize=11,
               label="Confirmed-correct prediction"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="white",
               markeredgecolor="#C8860B", markersize=11,
               markeredgewidth=1.5, label="Predicted-ahead (unconfirmed)"),
        Line2D([0], [0], marker="D", color="none", markerfacecolor="#C8860B",
               markeredgecolor="#5C3A06", markersize=11,
               label="Confirm / Rollback fork (tick N)"),
        Line2D([0], [0], marker="s", color="none", markerfacecolor="#3B7CD3",
               markeredgecolor="#1F3F6B", markersize=10,
               label="Server authoritative (received)"),
        Line2D([0], [0], marker="s", color="none", markerfacecolor="white",
               markeredgecolor="#3B7CD3", markersize=10, markeredgewidth=1.4,
               label="Server authoritative (in flight)"),
    ]
    ax.legend(handles=legend_handles, loc="upper left",
              bbox_to_anchor=(1.0, 1.0), fontsize=8.5,
              framealpha=0.95, edgecolor="#CCCCCC")

    plt.tight_layout()
    out = f"{OUT_DIR}/fig-08-01-prediction-timeline.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"[saved] {out}")


if __name__ == "__main__":
    gen_fig_08_01()
