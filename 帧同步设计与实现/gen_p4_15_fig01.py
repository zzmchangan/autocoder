# -*- coding: utf-8 -*-
"""
P4-15 GameRoom chapter figures.
fig-15-01: Reconnect-token identity-hijack defense.
Four-lane swimlane timeline (Attacker / Server.TryJoin / Victim Alice / Room R state).
Style matches fig-08-01 prediction timeline (horizontal lanes, shaded regions,
arrows, English labels, dpi 150, Agg backend).
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle
from matplotlib.lines import Line2D

plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

OUT_DIR = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images"


def gen_fig_15_01():
    fig, ax = plt.subplots(figsize=(14.5, 7.8))

    # ---- Geometry: 4 horizontal swimlanes ----------------------------------
    # y positions of the four lanes (top -> bottom)
    y_attacker = 4.0
    y_server   = 3.0
    y_victim   = 2.0
    y_room     = 1.0

    t_min, t_max = 0, 11   # time axis

    # Lane baselines (dashed)
    for y, col in [(y_attacker, "#a33a00"), (y_server, "#0b3d91"),
                   (y_victim, "#2a7a2a"), (y_room, "#5a3a8a")]:
        ax.hlines(y, t_min, t_max, colors="#bbbbbb",
                  linestyles="--", linewidth=1.0, zorder=1)

    # ---- Swimlane bands (alternating subtle fills) -------------------------
    band_colors = ["#fdf2ee", "#eef4fc", "#eef7f0", "#f4f0f9"]
    band_ys = [y_attacker, y_server, y_victim, y_room]
    for i, (yc, c) in enumerate(zip(band_ys, band_colors)):
        ax.axhspan(yc - 0.42, yc + 0.42, color=c, alpha=0.6, zorder=0)

    # ---- Lane labels (left margin) ----------------------------------------
    lane_labels = [
        (y_attacker, "Attacker\n(Malloy)", "#a33a00"),
        (y_server,   "Server\nTryJoin", "#0b3d91"),
        (y_victim,   "Victim\nAlice", "#2a7a2a"),
        (y_room,     "Room R\nstate", "#5a3a8a"),
    ]
    for y, lbl, col in lane_labels:
        ax.text(-0.35, y, lbl, ha="right", va="center",
                fontsize=11, fontweight="bold", color=col)

    # ---- Event nodes -------------------------------------------------------
    def node(t, y, marker="o", color="#0b3d91", edge="#1F3F6B", size=130, z=6):
        ax.scatter([t], [y], marker=marker, s=size, color=color,
                   edgecolors=edge, linewidths=1.0, zorder=z)

    def box(t, y, w, h, text, facecolor, edgecolor, textcolor="black",
            fontsize=8.5, fontweight="normal"):
        rect = FancyBboxPatch((t - w / 2, y - h / 2), w, h,
                              boxstyle="round,pad=0.02,rounding_size=0.08",
                              facecolor=facecolor, edgecolor=edgecolor,
                              linewidth=1.3, zorder=5)
        ax.add_patch(rect)
        ax.text(t, y, text, ha="center", va="center",
                fontsize=fontsize, fontweight=fontweight, color=textcolor,
                zorder=6)

    def arrow(t1, y1, t2, y2, color="#444", style="->", lw=1.6, ls="-"):
        a = FancyArrowPatch((t1, y1), (t2, y2),
                            arrowstyle=style, color=color, lw=lw,
                            linestyle=ls, zorder=4,
                            connectionstyle="arc3,rad=0.0",
                            mutation_scale=14)
        ax.add_artist(a)

    # ---- Time-step shaded guides (vertical) -------------------------------
    step_ts = [2, 4, 6, 8, 10]
    for ts in step_ts:
        ax.axvline(ts, color="#dddddd", linestyle=":", linewidth=0.9, zorder=0)

    # =======================================================================
    # Step 1: Attacker sends JoinRequest(name="Alice", token="")
    # =======================================================================
    box(1.0, y_attacker + 0.62, 2.4, 0.5,
        "knows Alice's name\n(no token)", "#fde2b0", "#a33a00",
        fontsize=8, fontweight="bold")
    # t=2 : attacker fires JoinRequest
    box(2.0, y_attacker + 0.55, 2.6, 0.5,
        "Step 1: JoinRequest\nname=\"Alice\" token=\"\"",
        "#f5b8b8", "#a33a00", fontsize=8, fontweight="bold")
    node(2.0, y_attacker, marker="v", color="#a33a00", edge="#5c1a00", size=150)
    # arrow down to server
    arrow(2.0, y_attacker - 0.05, 2.0, y_server + 0.5, color="#a33a00", lw=1.8)
    ax.text(2.15, (y_attacker + y_server) / 2, "send",
            fontsize=7.5, color="#a33a00", style="italic", va="center")

    # =======================================================================
    # Step 2: Server.TryJoin finds same-name player -> top-login branch
    # =======================================================================
    box(4.0, y_server + 0.6, 3.0, 0.55,
        "Step 2: same-name hit\n-> top-login branch",
        "#cfe8d4", "#0b3d91", fontsize=8, fontweight="bold")
    node(4.0, y_server, marker="s", color="#0b3d91", edge="#1F3F6B", size=150)
    # internal: compare tokens
    # t=4 also: token mismatch detected
    box(4.0, y_server - 0.62, 3.4, 0.5,
        "existingPlayer.ReconnectToken\n(GUID) != request.token(\"\")",
        "#fde2b0", "#a33a00", fontsize=7.8, fontweight="bold")
    arrow(4.0, y_server - 0.18, 4.0, y_server - 0.37, color="#a33a00", lw=1.3)

    # =======================================================================
    # Step 3: Reject + fallback CreateRoom
    # =======================================================================
    box(6.0, y_server + 0.6, 3.0, 0.55,
        "Step 3: REJECT\nreturn (false, \"name taken\")",
        "#f5b8b8", "#a33a00", fontsize=8, fontweight="bold")
    node(6.0, y_server, marker="X", color="#a33a00", edge="#5c1a00",
         size=170, z=7)
    # fallback: JoinHandler creates new room R'
    box(8.0, y_server + 0.6, 3.1, 0.55,
        "Step 3b: fallback\nCreateRoom -> R'",
        "#cfe8d4", "#0b3d91", fontsize=8, fontweight="bold")
    node(8.0, y_server, marker="o", color="#0b3d91", edge="#1F3F6B", size=150)
    # server internal arrows 4->6->8
    arrow(4.6, y_server, 5.4, y_server, color="#0b3d91", lw=1.5)
    arrow(6.6, y_server, 7.4, y_server, color="#0b3d91", lw=1.5)

    # arrow back up to attacker with new JoinResponse (room R')
    arrow(8.0, y_server + 0.5, 8.0, y_attacker - 0.05, color="#0b3d91", lw=1.8)
    ax.text(8.18, (y_attacker + y_server) / 2,
            "JoinResponse\n(new playerId,\nnew token, room R')",
            fontsize=7.8, color="#0b3d91", style="italic", va="center")
    # attacker lands in his own room R'
    box(10.0, y_attacker + 0.55, 2.6, 0.5,
        "Attacker plays in R'\n(unaware hijack failed)",
        "#cfe8d4", "#2a7a2a", fontsize=8, fontweight="bold")
    node(10.0, y_attacker, marker="o", color="#2a7a2a", edge="#1E5E29", size=150)
    arrow(8.0, y_attacker, 9.4, y_attacker, color="#2a7a2a", lw=1.5)

    # =======================================================================
    # Step 4: Alice unaffected -- she keeps receiving frames in R
    # =======================================================================
    # Alice lane: steady dots across whole timeline = online & receiving frames
    for t in range(1, 11):
        ax.scatter([t], [y_victim], marker="o", s=70,
                   color="#2a7a2a", edgecolors="#1E5E29",
                   linewidths=0.8, zorder=5)
    box(5.0, y_victim + 0.58, 4.2, 0.5,
        "Step 4: Alice UNAFFECTED -- keeps receiving frames in R",
        "#cfe8d4", "#2a7a2a", fontsize=8.3, fontweight="bold")
    # small annotation: token never left server
    ax.text(9.5, y_victim - 0.55,
            "token is server-private (GUID via JoinResponse only)",
            fontsize=7.5, color="#2a7a2a", style="italic", ha="center")

    # =======================================================================
    # Room R state lane: stays "Playing" throughout
    # =======================================================================
    # room R stays Playing the entire time
    rect = Rectangle((1.0, y_room - 0.22), 9.0, 0.44,
                     facecolor="#dccfeb", edgecolor="#5a3a8a",
                     linewidth=1.3, zorder=4)
    ax.add_patch(rect)
    ax.text(5.5, y_room, "Room R: Playing  (broadcast continues, no disruption)",
            ha="center", va="center", fontsize=9, fontweight="bold",
            color="#3a2a6a", zorder=5)
    # room R' created at step 3b (t=8)
    node(8.0, y_room, marker="*", color="#0b3d91", edge="#1F3F6B", size=220, z=7)
    ax.text(8.0, y_room - 0.55, "R' created",
            ha="center", fontsize=7.8, color="#0b3d91", fontweight="bold")

    # =======================================================================
    # Defense banner at bottom
    # =======================================================================
    banner = Rectangle((1.0, 0.05), 9.0, 0.42,
                       facecolor="#0b3d91", edgecolor="#06245e", zorder=4)
    ax.add_patch(banner)
    ax.text(5.5, 0.26,
            "P0-2 Defense: top-login MUST self-prove ReconnectToken  "
            "--  empty/wrong token always rejected  ->  graceful fallback to new room",
            ha="center", va="center", fontsize=9, fontweight="bold",
            color="white", zorder=5)

    # =======================================================================
    # Axes cosmetics --------------------------------------------------------
    ax.set_xlim(-0.6, t_max + 0.6)
    ax.set_ylim(-0.05, 5.2)
    ax.set_xlabel("Time  ---->", fontsize=11, fontweight="bold")
    ax.set_xticks(range(0, t_max + 1))
    ax.set_yticks([])
    for spine in ("left", "right", "top"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color("#888888")

    # emphasize step tick labels
    for lbl in ax.get_xticklabels():
        try:
            v = int(lbl.get_text())
        except ValueError:
            continue
        if v in (2, 4, 6, 8, 10):
            lbl.set_color("#a33a00"); lbl.set_fontweight("bold")

    ax.set_title(
        "Reconnect-Token Identity-Hijack Defense  "
        "(top-login rejected without valid token -> graceful fallback)",
        fontsize=12.5, fontweight="bold", pad=12)

    # ---- Legend ------------------------------------------------------------
    legend_handles = [
        Line2D([0], [0], marker="v", color="none", markerfacecolor="#a33a00",
               markeredgecolor="#5c1a00", markersize=10,
               label="Attacker sends malicious request"),
        Line2D([0], [0], marker="X", color="none", markerfacecolor="#a33a00",
               markeredgecolor="#5c1a00", markersize=10,
               label="Server REJECTS (token mismatch)"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#0b3d91",
               markeredgecolor="#1F3F6B", markersize=10,
               label="Server fallback: create new room R'"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#2a7a2a",
               markeredgecolor="#1E5E29", markersize=9,
               label="Victim Alice online (receiving frames)"),
        Line2D([0], [0], marker="*", color="none", markerfacecolor="#0b3d91",
               markeredgecolor="#1F3F6B", markersize=13,
               label="New room R' created"),
    ]
    ax.legend(handles=legend_handles, loc="upper left",
              bbox_to_anchor=(1.005, 1.0), fontsize=8,
              framealpha=0.95, edgecolor="#CCCCCC")

    plt.tight_layout()
    out = f"{OUT_DIR}/fig-15-reconnect-token.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"[saved] {out}")


if __name__ == "__main__":
    gen_fig_15_01()
