# -*- coding: utf-8 -*-
"""
P5-19 Reconnect chapter figures.
fig-19-01: Reconnect two-tier decoupling timeline.
  Transport layer (TcpClient.ReconnectCoreAsync): socket reconnect + identity proof
    (send ReconnectRequest{PlayerId,RoomId,Token}, wait ReconnectResponse via
    TaskCompletionSource + Task.Delay).
  Application layer (LockstepDriver.HandleReconnectedInternal): catch-up decision,
    reads ReconnectResponse.CurrentFrame -> RequestState (snapshot) or RequestMissFrames.
  Two lanes are bridged by ReconnectResponse.CurrentFrame (single field).
Style matches book (horizontal lanes, shaded bands, FancyBboxPatch, English labels,
dpi 150, Agg backend).
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle
from matplotlib.lines import Line2D

plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

OUT_DIR = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images"


def gen_fig_19_01():
    fig, ax = plt.subplots(figsize=(15.5, 8.2))

    # ---- Two swimlanes: transport (top) / application (bottom) -------------
    y_transport = 5.0
    y_app = 2.0

    t_min, t_max = 0, 13

    # Lane bands
    ax.axhspan(y_transport - 1.0, y_transport + 1.0,
               color="#eef4fc", alpha=0.7, zorder=0)
    ax.axhspan(y_app - 1.0, y_app + 1.0,
               color="#eef7f0", alpha=0.7, zorder=0)

    # Lane baselines
    for y in (y_transport, y_app):
        ax.hlines(y, t_min, t_max, colors="#bbbbbb",
                  linestyles="--", linewidth=1.0, zorder=1)

    # ---- Lane labels (left margin) ----------------------------------------
    ax.text(-0.4, y_transport,
            "TRANSPORT LAYER\nTcpClient.ReconnectCoreAsync\n(socket reconnect + identity proof)",
            ha="right", va="center", fontsize=10, fontweight="bold", color="#0b3d91")
    ax.text(-0.4, y_app,
            "APPLICATION LAYER\nLockstepDriver.HandleReconnectedInternal\n(catch-up decision)",
            ha="right", va="center", fontsize=10, fontweight="bold", color="#2a7a2a")

    # ---- helpers -----------------------------------------------------------
    def node(t, y, marker="o", color="#0b3d91", edge="#1F3F6B", size=140, z=6):
        ax.scatter([t], [y], marker=marker, s=size, color=color,
                   edgecolors=edge, linewidths=1.0, zorder=z)

    def box(t, y, w, h, text, facecolor, edgecolor, textcolor="black",
            fontsize=8.6, fontweight="normal"):
        rect = FancyBboxPatch((t - w / 2, y - h / 2), w, h,
                              boxstyle="round,pad=0.02,rounding_size=0.08",
                              facecolor=facecolor, edgecolor=edgecolor,
                              linewidth=1.3, zorder=5)
        ax.add_patch(rect)
        ax.text(t, y, text, ha="center", va="center",
                fontsize=fontsize, fontweight=fontweight, color=textcolor, zorder=6)

    def arrow(t1, y1, t2, y2, color="#444", style="->", lw=1.7, ls="-", rad=0.0):
        a = FancyArrowPatch((t1, y1), (t2, y2),
                            arrowstyle=style, color=color, lw=lw,
                            linestyle=ls, zorder=4,
                            connectionstyle=f"arc3,rad={rad}",
                            mutation_scale=15)
        ax.add_artist(a)

    # ---- vertical step guides ---------------------------------------------
    for ts in (2, 4, 6, 8, 10, 12):
        ax.axvline(ts, color="#dddddd", linestyle=":", linewidth=0.9, zorder=0)

    # =======================================================================
    # TRANSPORT LAYER SEQUENCE (left -> right)
    # =======================================================================
    # T1: heartbeat timeout -> Disconnect
    box(1.5, y_transport + 0.55, 2.6, 0.5,
        "heartbeat timeout 5000ms\n-> Disconnect()",
        "#fde2b0", "#a33a00", fontsize=8.2, fontweight="bold")
    node(1.5, y_transport, marker="v", color="#a33a00", edge="#5c1a00", size=160)

    # T2: new TcpClient + Connect
    box(3.5, y_transport + 0.55, 2.7, 0.55,
        "new TcpClient\n-> ConnectAsync (retry ok)",
        "#cfe8d4", "#0b3d91", fontsize=8.4, fontweight="bold")
    node(3.5, y_transport, marker="o", color="#0b3d91", edge="#1F3F6B", size=150)
    arrow(2.1, y_transport, 2.9, y_transport, color="#0b3d91", lw=1.6)

    # T3: send ReconnectRequest {PlayerId, RoomId, Token}
    box(6.0, y_transport + 0.62, 3.0, 0.66,
        "send ReconnectRequest\n{PlayerId, RoomId, Token}",
        "#cfe8d4", "#0b3d91", fontsize=8.6, fontweight="bold")
    node(6.0, y_transport, marker="o", color="#0b3d91", edge="#1F3F6B", size=150)
    arrow(4.8, y_transport, 5.4, y_transport, color="#0b3d91", lw=1.6)

    # T4: wait response via TCS + Task.Delay
    box(8.5, y_transport + 0.62, 3.2, 0.66,
        "await Task.WhenAny(\n  _handshakeTcs.Task,\n  Task.Delay(timeout))",
        "#fdf2c0", "#C8860B", fontsize=8.0, fontweight="bold")
    node(8.5, y_transport, marker="o", color="#C8860B", edge="#5c3a06", size=150)
    arrow(7.5, y_transport, 7.9, y_transport, color="#0b3d91", lw=1.6)

    # T5: server token check -> ReconnectResponse {Success, CurrentFrame}
    box(11.0, y_transport + 0.62, 3.4, 0.7,
        "server: HandleReconnect\nplayer.ReconnectToken==token -> OK\nupdate ClientId",
        "#cfe8d4", "#0b3d91", fontsize=8.0, fontweight="bold")
    node(11.0, y_transport, marker="o", color="#0b3d91", edge="#1F3F6B", size=150)
    arrow(10.1, y_transport, 10.3, y_transport, color="#0b3d91", lw=1.6)

    # T6: OnReconnected event fires -> triggers app layer
    box(12.6, y_transport + 0.55, 2.7, 0.55,
        "OnReconnected.Invoke()\n(only socket+id proven)",
        "#cfe8d4", "#2a7a2a", fontsize=8.2, fontweight="bold")
    node(12.6, y_transport, marker="*", color="#2a7a2a", edge="#1E5E29", size=220, z=7)
    arrow(12.0, y_transport, 12.0, y_transport, color="#2a7a2a", lw=1.6)

    # transport scope banner
    ax.text(7.0, y_transport - 0.62,
            "transport layer scope: socket reconnect + identity self-proof  "
            "(does NOT touch battle state)",
            ha="center", va="center", fontsize=8.6, style="italic",
            color="#0b3d91", fontweight="bold")

    # =======================================================================
    # BRIDGE: ReconnectResponse.CurrentFrame (single field)
    # =======================================================================
    # vertical bridge arrow from transport T6 down to app lane
    arrow(12.6, y_transport - 0.4, 12.6, y_app + 0.5, color="#5a3a8a", lw=2.2)
    # bridge label box
    box(12.6, (y_transport + y_app) / 2, 3.6, 0.6,
        "BRIDGE (single field):\nReconnectResponse.CurrentFrame",
        "#f4f0f9", "#5a3a8a", textcolor="#3a2a6a",
        fontsize=8.4, fontweight="bold")

    # =======================================================================
    # APPLICATION LAYER SEQUENCE (right -> left after bridge)
    # =======================================================================
    # A0: command queue -> HandleReconnectedInternal (cross-thread, P4-12)
    box(12.6, y_app + 0.5, 3.0, 0.55,
        "HandleReconnectedInternal\n(queue consumed on main thread)",
        "#cfe8d4", "#2a7a2a", fontsize=8.0, fontweight="bold")
    node(12.6, y_app, marker="o", color="#2a7a2a", edge="#1E5E29", size=150)

    # A1: set server tick from CurrentFrame
    box(10.0, y_app + 0.55, 3.2, 0.6,
        "SetCurTickInServer(CurrentFrame)\n(serverTick - nextTick = gap)",
        "#cfe8d4", "#2a7a2a", fontsize=8.2, fontweight="bold")
    node(10.0, y_app, marker="o", color="#2a7a2a", edge="#1E5E29", size=150)
    arrow(11.1, y_app, 10.5, y_app, color="#2a7a2a", lw=1.6)

    # A2: decision diamond: gap > SnapshotThresholdFrames?
    from matplotlib.patches import Polygon
    dx, dy = 7.5, y_app
    pts = [(dx, dy + 0.55), (dx + 1.6, dy), (dx, dy - 0.55), (dx - 1.6, dy)]
    p = Polygon(pts, closed=True, facecolor="#fdf2c0", edgecolor="#C8860B",
                linewidth=1.5, zorder=5)
    ax.add_patch(p)
    ax.text(dx, dy, "gap >\nSnapshotThresholdFrames?\n(300 frames)",
            ha="center", va="center", fontsize=7.8, fontweight="bold",
            color="#5c3a06", zorder=6)
    arrow(8.4, y_app, 9.1, y_app, color="#2a7a2a", lw=1.6)

    # A3 NO branch -> RequestMissFrames (incremental, from breakpoint)
    box(4.2, y_app + 0.6, 3.2, 0.6,
        "NO  -> RequestMissFrames(nextTick)\nincremental catch-up",
        "#cfe8d4", "#2a7a2a", fontsize=8.0, fontweight="bold")
    node(4.2, y_app + 0.0, marker="o", color="#2a7a2a", edge="#1E5E29", size=140)
    arrow(5.9, y_app + 0.18, 5.8, y_app + 0.18, color="#2a7a2a", lw=1.6, rad=0.0)
    ax.text(5.85, y_app + 0.42, "NO", fontsize=8, color="#2a7a2a",
            fontweight="bold", ha="center")

    # A3 YES branch -> RequestState (snapshot, jump to now)
    box(4.2, y_app - 0.6, 3.2, 0.6,
        "YES -> RequestState()\nsnapshot, jump to now",
        "#f5b8b8", "#a33a00", fontsize=8.0, fontweight="bold")
    node(4.2, y_app - 0.0, marker="o", color="#a33a00", edge="#5c1a00", size=140)
    arrow(5.9, y_app - 0.18, 5.8, y_app - 0.18, color="#a33a00", lw=1.6, rad=0.0)
    ax.text(5.85, y_app - 0.42, "YES", fontsize=8, color="#a33a00",
            fontweight="bold", ha="center")

    # A4: catch-up result (both merge)
    box(1.6, y_app, 2.6, 0.6,
        "PushServerFrames /\nLoadState -> caught up",
        "#cfe8d4", "#0b3d91", fontsize=8.0, fontweight="bold")
    node(1.6, y_app, marker="*", color="#0b3d91", edge="#1F3F6B", size=200, z=7)
    arrow(2.6, y_app + 0.18, 2.9, y_app, color="#2a7a2a", lw=1.4)
    arrow(2.6, y_app - 0.18, 2.9, y_app, color="#a33a00", lw=1.4)

    # application scope banner
    ax.text(7.0, y_app - 0.95,
            "application layer scope: catch-up only  "
            "(does NOT touch socket / heartbeat / token)",
            ha="center", va="center", fontsize=8.6, style="italic",
            color="#2a7a2a", fontweight="bold")

    # =======================================================================
    # Bottom decoupling banner
    # =======================================================================
    banner = Rectangle((1.0, -0.05), 11.6, 0.5,
                       facecolor="#5a3a8a", edgecolor="#3a2a6a", zorder=4)
    ax.add_patch(banner)
    ax.text(6.8, 0.2,
            "TWO-TIER DECOUPLING:  transport reconnect (retry N times) and "
            "catch-up strategy are INDEPENDENT  "
            "-- bridged by the single field ReconnectResponse.CurrentFrame",
            ha="center", va="center", fontsize=8.8, fontweight="bold",
            color="white", zorder=5)

    # =======================================================================
    # Axes cosmetics
    # =======================================================================
    ax.set_xlim(-0.7, t_max + 0.7)
    ax.set_ylim(-0.2, 6.4)
    ax.set_xlabel("Time  ---->", fontsize=11, fontweight="bold")
    ax.set_xticks(range(0, t_max + 1))
    ax.set_yticks([])
    for spine in ("left", "right", "top"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color("#888888")

    ax.set_title(
        "Reconnect Two-Tier Decoupling Timeline\n"
        "transport layer (socket + identity) <-- CurrentFrame --> "
        "application layer (catch-up)",
        fontsize=12.5, fontweight="bold", pad=12)

    # ---- Legend ------------------------------------------------------------
    legend_handles = [
        Line2D([0], [0], marker="v", color="none", markerfacecolor="#a33a00",
               markeredgecolor="#5c1a00", markersize=10,
               label="Disconnect trigger"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#0b3d91",
               markeredgecolor="#1F3F6B", markersize=10,
               label="Transport action"),
        Line2D([0], [0], marker="*", color="none", markerfacecolor="#2a7a2a",
               markeredgecolor="#1E5E29", markersize=13,
               label="OnReconnected event (bridge fire)"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#2a7a2a",
               markeredgecolor="#1E5E29", markersize=9,
               label="Application action"),
        Line2D([0], [0], marker="s", color="#5a3a8a", markerfacecolor="#f4f0f9",
               markeredgecolor="#5a3a8a", markersize=10, linewidth=0,
               label="Bridge: CurrentFrame (single field)"),
    ]
    ax.legend(handles=legend_handles, loc="upper left",
              bbox_to_anchor=(1.005, 1.0), fontsize=8,
              framealpha=0.95, edgecolor="#CCCCCC")

    plt.tight_layout()
    out = f"{OUT_DIR}/fig-19-01-reconnect-two-tier-timeline.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"[saved] {out}")


if __name__ == "__main__":
    gen_fig_19_01()
