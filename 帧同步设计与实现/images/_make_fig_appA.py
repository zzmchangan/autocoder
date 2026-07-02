"""Appendix A figures.

fig-appA-modes-and-structure.png:
    Left half  -- solution project tree (LockstepSdk/) with 4 dependency layers.
    Right half -- three run modes (Standalone / Online / Replay), each wired to
                  the .csproj it invokes.

fig-appA-online-topology.png:
    Central Lockstep.Server box (UDP :9999, 20Hz tick), three clients around it.
    Upstream arrow = Input, Downstream arrow = Authoritative Frame (with
    redundant history). Bottom notes: Relay vs Authoritative, redundant frames.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import numpy as np

OUT_DIR = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/"

# ---------- shared palette ----------
C_CORE   = "#1565C0"   # core layer (blue)
C_NET    = "#6A1B9A"   # network/systems layer (purple)
C_GAME   = "#00838F"   # games layer (teal)
C_HOST   = "#E65100"   # host layer / server (orange)
C_REPLAY = "#2E7D32"   # replay (green)
C_STAND  = "#1565C0"   # standalone (blue)
C_ONLINE = "#E65100"   # online (orange)
C_INK    = "#212121"
C_GREY   = "#546E7A"
C_LIGHT  = "#ECEFF1"


def rbox(ax, x, y, w, h, fc, ec="none", lw=0, rad=0.012, alpha=1.0, zorder=2):
    p = FancyBboxPatch((x, y), w, h,
                       boxstyle=f"round,pad=0,rounding_size={rad}",
                       fc=fc, ec=ec, lw=lw, alpha=alpha, zorder=zorder,
                       mutation_aspect=1)
    ax.add_patch(p)
    return p


# =====================================================================
# FIG 1 : modes + project structure tree
# =====================================================================
def fig_modes_and_structure():
    OUT = OUT_DIR + "fig-appA-modes-and-structure.png"
    fig, ax = plt.subplots(figsize=(16, 10), dpi=150)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    fig.patch.set_facecolor("white")

    # ---- title ----
    ax.text(0.5, 0.975,
            "Appendix A  —  Three Run Modes  &  Solution Structure",
            ha="center", va="top", fontsize=16.5, fontweight="bold", color=C_INK)
    ax.text(0.5, 0.948,
            "Left: project tree by dependency layer.   Right: each run mode wired to the .csproj it invokes.",
            ha="center", va="top", fontsize=10.5, color=C_GREY, fontstyle="italic")

    # vertical divider
    ax.plot([0.52, 0.52], [0.05, 0.92], color=C_LIGHT, lw=1.2, ls="--", zorder=1)

    # =================================================================
    # LEFT HALF -- project structure tree
    # =================================================================
    LEFT_X = 0.03
    ax.text(LEFT_X + 0.22, 0.905, "Solution Structure  (LockstepSdk.sln, 14 projects)",
            ha="center", va="top", fontsize=12.5, fontweight="bold", color=C_CORE)

    # root
    rbox(ax, LEFT_X + 0.13, 0.85, 0.18, 0.034, C_CORE, rad=0.008, zorder=3)
    ax.text(LEFT_X + 0.22, 0.867, "LockstepSdk/", ha="center", va="center",
            fontsize=9.5, fontweight="bold", color="white", zorder=4)

    # tree connectors to src/ and tests/
    ax.plot([LEFT_X + 0.22, LEFT_X + 0.22], [0.85, 0.815], color=C_GREY, lw=1.0, zorder=2)
    ax.plot([LEFT_X + 0.22, LEFT_X + 0.06], [0.815, 0.815], color=C_GREY, lw=1.0, zorder=2)
    ax.plot([LEFT_X + 0.22, LEFT_X + 0.40], [0.815, 0.815], color=C_GREY, lw=1.0, zorder=2)
    ax.text(LEFT_X + 0.053, 0.82, "src/", ha="right", va="center",
            fontsize=9.5, fontweight="bold", color=C_INK)
    ax.text(LEFT_X + 0.407, 0.82, "tests/", ha="left", va="center",
            fontsize=9.5, fontweight="bold", color=C_INK)

    band_x = LEFT_X + 0.075
    band_w = 0.30
    # Layer bands: 4 non-overlapping horizontal stripes, top -> bottom
    # Hosts (top)   y 0.635 - 0.800
    # Games         y 0.550 - 0.620
    # Systems       y 0.350 - 0.535
    # Core (bottom) y 0.250 - 0.320
    rbox(ax, band_x, 0.635, band_w, 0.165, "#FFF3E0", rad=0.006, zorder=1)
    rbox(ax, band_x, 0.550, band_w, 0.070, "#E0F7FA", rad=0.006, zorder=1)
    rbox(ax, band_x, 0.350, band_w, 0.185, "#F3E5F5", rad=0.006, zorder=1)
    rbox(ax, band_x, 0.250, band_w, 0.070, "#E3F2FD", rad=0.006, zorder=1)

    # layer labels (right side of bands)
    ax.text(band_x + band_w + 0.006, 0.717, "Layer 4\nHosts (Exe)", ha="left", va="center",
            fontsize=8.5, color=C_HOST, fontweight="bold")
    ax.text(band_x + band_w + 0.006, 0.585, "Layer 3\nGames", ha="left", va="center",
            fontsize=8.5, color=C_GAME, fontweight="bold")
    ax.text(band_x + band_w + 0.006, 0.442, "Layer 2\nSystems", ha="left", va="center",
            fontsize=8.5, color=C_NET, fontweight="bold")
    ax.text(band_x + band_w + 0.006, 0.285, "Layer 1\nCore", ha="left", va="center",
            fontsize=8.5, color=C_CORE, fontweight="bold")

    def proj_chip(x, y, label, color, w=0.140, sub=None, sub_size=7.6):
        rbox(ax, x, y, w, 0.030, color, rad=0.006, zorder=3)
        ax.text(x + w/2, y + 0.015, label, ha="center", va="center",
                fontsize=8.0, fontweight="bold", color="white", zorder=4)
        if sub:
            ax.text(x + w + 0.005, y + 0.015, sub, ha="left", va="center",
                    fontsize=sub_size, color=C_GREY)

    chip_x = LEFT_X + 0.085

    # Layer 4: Hosts (top -> bottom): RaylibClient, ConsoleClient, Server, Benchmark
    proj_chip(chip_x, 0.760, "Clients/RaylibClient", C_HOST,
              sub="TankGame.Client (Exe)")
    proj_chip(chip_x, 0.725, "Clients/ConsoleClient", C_HOST,
              sub="minimal console client")
    proj_chip(chip_x, 0.690, "Server/", C_HOST,
              sub="Lockstep.Server (Exe, UDP/TCP/WS)")
    proj_chip(chip_x, 0.655, "Benchmark/", C_HOST,
              sub="perf bench (Ch.22)")

    # Layer 3: Games
    proj_chip(chip_x, 0.578, "Games/TankGame", C_GAME,
              sub="TankGameSimulation")
    proj_chip(chip_x, 0.552, "Games/BomberGame", C_GAME,
              sub="Bomberman (isomorphic)")

    # Layer 2: Systems
    proj_chip(chip_x, 0.500, "Lockstep.Network", C_NET,
              sub="Driver / Builder / Transports")
    proj_chip(chip_x, 0.470, "Lockstep.Collision", C_NET,
              sub="QuadTree")
    proj_chip(chip_x, 0.440, "Lockstep.Pathfinding", C_NET,
              sub="NavMesh + A*")
    proj_chip(chip_x, 0.410, "Lockstep.BehaviorTree", C_NET,
              sub="AI Builder API")
    proj_chip(chip_x, 0.380, "Lockstep.Generators", C_NET,
              sub="Source Gen (netstandard2.0)")

    # Layer 1: Core
    proj_chip(chip_x, 0.280, "Lockstep.Core", C_CORE, w=0.155,
              sub="Math / ECS / Sync / Replay / Serial.")

    # ---- tests column (right side of src bands) ----
    tx = LEFT_X + 0.40
    ax.text(tx + 0.018, 0.535, "4 test projects",
            ha="left", va="center", fontsize=8.5,
            fontweight="bold", color=C_GREY)
    for i, t in enumerate(["Core.Tests", "Network.Tests",
                           "Collision.Tests", "BehaviorTree.Tests"]):
        ty = 0.495 - i * 0.035
        rbox(ax, tx, ty, 0.135, 0.028, C_GREY, rad=0.006, zorder=3)
        ax.text(tx + 0.0675, ty + 0.014, t, ha="center", va="center",
                fontsize=7.8, fontweight="bold", color="white", zorder=4)

    # ---- dependency direction hint ----
    ax.annotate("", xy=(chip_x + 0.0675, 0.350),
                xytext=(chip_x + 0.0675, 0.320),
                arrowprops=dict(arrowstyle="-|>", color=C_CORE, lw=2.0))
    ax.text(chip_x + 0.160, 0.335, "Layer 1 supports all above",
            ha="left", va="center", fontsize=7.6, color=C_CORE,
            fontstyle="italic")

    # =================================================================
    # RIGHT HALF -- three run modes
    # =================================================================
    RX = 0.56
    ax.text(RX + 0.20, 0.905, "Three Run Modes  (input source + tick driver differ)",
            ha="center", va="top", fontsize=12.5, fontweight="bold", color=C_HOST)

    mode_y = [0.74, 0.45, 0.18]
    modes = [
        ("Standalone",       C_STAND,
         "run_standalone.bat",
         "Local keyboard + AI polled locally",
         "LockstepDriver.StartLocal  (local 20Hz)",
         "1 process",
         "Fastest env / determinism check"),
        ("Online",           C_ONLINE,
         "run_server.bat  +  run_client.bat",
         "Server aggregates inputs, broadcasts frame",
         "Server physical clock (20Hz) + net clock",
         "1 server + N clients",
         "Pred/rollback, redundant frames, HashReport"),
        ("Replay",           C_REPLAY,
         "Menu entry  (Tank Replays)",
         ".lrp file recorded input stream",
         "ReplayPlayer feeds data at play rate",
         "1 process",
         "Recording is reproducible (Ch.21)"),
    ]

    for (name, col, cmd, src, drv, procs, verif), my in zip(modes, mode_y):
        # header
        rbox(ax, RX, my, 0.40, 0.045, col, rad=0.008, zorder=3)
        ax.text(RX + 0.015, my + 0.0225, name, ha="left", va="center",
                fontsize=12, fontweight="bold", color="white", zorder=4)
        ax.text(RX + 0.385, my + 0.0225, cmd, ha="right", va="center",
                fontsize=8.2, color="white", zorder=4, fontfamily="monospace")
        # body
        rbox(ax, RX, my - 0.135, 0.40, 0.130, "#FFFFFF", ec=col, lw=1.4, rad=0.008, zorder=2)
        row_y = my - 0.020
        ax.text(RX + 0.015, row_y, "Input:", ha="left", va="top",
                fontsize=8.6, fontweight="bold", color=C_GREY)
        ax.text(RX + 0.090, row_y, src, ha="left", va="top",
                fontsize=8.6, color=C_INK)
        ax.text(RX + 0.015, row_y - 0.028, "Driver:", ha="left", va="top",
                fontsize=8.6, fontweight="bold", color=C_GREY)
        ax.text(RX + 0.090, row_y - 0.028, drv, ha="left", va="top",
                fontsize=8.6, color=C_INK)
        ax.text(RX + 0.015, row_y - 0.056, "Procs:", ha="left", va="top",
                fontsize=8.6, fontweight="bold", color=C_GREY)
        ax.text(RX + 0.090, row_y - 0.056, procs, ha="left", va="top",
                fontsize=8.6, color=C_INK)
        ax.text(RX + 0.015, row_y - 0.084, "Validates:", ha="left", va="top",
                fontsize=8.6, fontweight="bold", color=C_GREY)
        ax.text(RX + 0.090, row_y - 0.084, verif, ha="left", va="top",
                fontsize=8.6, color=C_INK)

    # ---- arrows from modes (right) to the .csproj (left) they invoke ----
    # Standalone -> TankGame.Client (layer 4 chip)
    ax.annotate("", xy=(band_x + band_w + 0.001, 0.775),
                xytext=(RX + 0.001, 0.74 + 0.022),
                arrowprops=dict(arrowstyle="->", color=C_STAND, lw=2.0,
                                connectionstyle="arc3,rad=-0.15"))
    ax.text((band_x + band_w + RX) / 2, 0.80, "TankGame.Client",
            ha="center", va="center", fontsize=7.8, color=C_STAND,
            fontweight="bold", fontfamily="monospace",
            bbox=dict(boxstyle="round,pad=0.15", fc="white", ec=C_STAND, lw=0.8))

    # Online -> Server + Client
    ax.annotate("", xy=(band_x + band_w + 0.001, 0.705),
                xytext=(RX + 0.001, 0.45 + 0.022),
                arrowprops=dict(arrowstyle="->", color=C_ONLINE, lw=2.0,
                                connectionstyle="arc3,rad=-0.15"))
    ax.text((band_x + band_w + RX) / 2, 0.60, "Lockstep.Server + TankGame.Client",
            ha="center", va="center", fontsize=7.8, color=C_ONLINE,
            fontweight="bold", fontfamily="monospace",
            bbox=dict(boxstyle="round,pad=0.15", fc="white", ec=C_ONLINE, lw=0.8))

    # Replay -> TankGame.Client (same process, scene switch)
    ax.annotate("", xy=(band_x + band_w + 0.001, 0.760),
                xytext=(RX + 0.001, 0.18 + 0.022),
                arrowprops=dict(arrowstyle="->", color=C_REPLAY, lw=2.0,
                                connectionstyle="arc3,rad=-0.25", ls="--"))
    ax.text((band_x + band_w + RX) / 2, 0.42, "TankGame.Client  (same proc, scene switch)",
            ha="center", va="center", fontsize=7.6, color=C_REPLAY,
            fontweight="bold", fontfamily="monospace",
            bbox=dict(boxstyle="round,pad=0.15", fc="white", ec=C_REPLAY, lw=0.8))

    plt.savefig(OUT, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("saved:", OUT)


# =====================================================================
# FIG 2 : online topology
# =====================================================================
def fig_online_topology():
    OUT = OUT_DIR + "fig-appA-online-topology.png"
    fig, ax = plt.subplots(figsize=(13, 10), dpi=150)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    fig.patch.set_facecolor("white")

    # ---- title ----
    ax.text(0.5, 0.97,
            "Appendix A  —  Online Mode Topology",
            ha="center", va="top", fontsize=16, fontweight="bold", color=C_INK)
    ax.text(0.5, 0.935,
            "Default transport: UDP, port 9999.   Server ticks at fixed 20 Hz.",
            ha="center", va="top", fontsize=10.5, color=C_GREY, fontstyle="italic")

    # ---- central server ----
    sx, sy, sw, sh = 0.34, 0.43, 0.32, 0.13
    rbox(ax, sx, sy, sw, sh, C_HOST, rad=0.012, zorder=4)
    ax.text(0.5, sy + sh - 0.025, "Lockstep.Server", ha="center", va="center",
            fontsize=14, fontweight="bold", color="white", zorder=5)
    ax.text(0.5, sy + 0.048, "UDP  :9999     20 Hz tick", ha="center", va="center",
            fontsize=10.5, color="white", zorder=5, fontfamily="monospace")
    ax.text(0.5, sy + 0.020, "GameRoom  aggregates inputs  ->  authoritative frame",
            ha="center", va="center", fontsize=8.6, color="#FFF3E0", zorder=5)

    # ---- three clients around the server ----
    clients = [
        # (cx, cy, label, pos)
        (0.13, 0.76, "Client 0\n(Player0)",   "top-left"),
        (0.87, 0.76, "Client 1\n(Player1)",   "top-right"),
        (0.50, 0.14, "Client 2\n(Player2)",   "bottom"),
    ]
    cw, ch = 0.18, 0.085

    for cx, cy, label, pos in clients:
        # client box
        rbox(ax, cx - cw/2, cy - ch/2, cw, ch, C_STAND, rad=0.012, zorder=4)
        ax.text(cx, cy, label, ha="center", va="center",
                fontsize=10, fontweight="bold", color="white", zorder=5)

        if pos == "bottom":
            up_xy = (cx, cy + ch/2)              # client top edge
            dn_xy = (cx, sy)                     # server bottom edge
            up_lbl = (cx + 0.085, (cy + ch/2 + sy) / 2 + 0.012)
            dn_lbl = (cx - 0.085, (cy + ch/2 + sy) / 2 - 0.012)
        elif pos == "top-left":
            up_xy = (cx + cw/2 - 0.015, cy - 0.010)
            dn_xy = (sx + 0.020, sy + sh - 0.010)
            midx = (up_xy[0] + dn_xy[0]) / 2
            midy = (up_xy[1] + dn_xy[1]) / 2
            up_lbl = (midx - 0.010, midy + 0.045)
            dn_lbl = (midx + 0.010, midy - 0.045)
        else:  # top-right
            up_xy = (cx - cw/2 + 0.015, cy - 0.010)
            dn_xy = (sx + sw - 0.020, sy + sh - 0.010)
            midx = (up_xy[0] + dn_xy[0]) / 2
            midy = (up_xy[1] + dn_xy[1]) / 2
            up_lbl = (midx + 0.010, midy + 0.045)
            dn_lbl = (midx - 0.010, midy - 0.045)

        # upstream: Input (client -> server)
        ax.annotate("", xy=dn_xy, xytext=up_xy,
                    arrowprops=dict(arrowstyle="->", color=C_CORE, lw=2.2,
                                    connectionstyle="arc3,rad=0.10"),
                    zorder=3)
        ax.text(up_lbl[0], up_lbl[1], "Input\n(up)",
                ha="center", va="center", fontsize=8.2, color=C_CORE,
                fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.18", fc="white",
                          ec=C_CORE, lw=0.9, alpha=0.96), zorder=6)

        # downstream: Authoritative Frame (server -> client)
        ax.annotate("", xy=up_xy, xytext=dn_xy,
                    arrowprops=dict(arrowstyle="->", color=C_HOST, lw=2.2,
                                    connectionstyle="arc3,rad=0.10"),
                    zorder=3)
        ax.text(dn_lbl[0], dn_lbl[1],
                "Authoritative\nFrame (down)\n+ redundant\nhistory",
                ha="center", va="center", fontsize=7.6, color=C_HOST,
                fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.18", fc="white",
                          ec=C_HOST, lw=0.9, alpha=0.96), zorder=6)

    # ---- bottom-right note: Relay vs Authoritative ----
    note_x, note_y, note_w, note_h = 0.62, 0.025, 0.36, 0.155
    rbox(ax, note_x, note_y, note_w, note_h, "#FFFDE7",
         ec="#F9A825", lw=1.3, rad=0.008, zorder=2)
    ax.text(note_x + 0.012, note_y + note_h - 0.018,
            "Relay  vs  Authoritative",
            ha="left", va="top", fontsize=9.5, fontweight="bold", color="#F9A825")
    ax.text(note_x + 0.012, note_y + note_h - 0.045,
            "Relay:         server aggregates inputs only,\n"
            "               runs NO logic, no desync check.\n"
            "Authoritative: server injects ISimulation,\n"
            "               runs full logic + HashReport.\n"
            "Switch = UseSimulation(() => new Game())",
            ha="left", va="top", fontsize=8.0, color=C_INK,
            fontfamily="monospace")

    # ---- bottom-left note: redundant frame purpose ----
    n2_x, n2_y, n2_w, n2_h = 0.02, 0.025, 0.34, 0.155
    rbox(ax, n2_x, n2_y, n2_w, n2_h, "#E8F5E9",
         ec=C_REPLAY, lw=1.3, rad=0.008, zorder=2)
    ax.text(n2_x + 0.012, n2_y + n2_h - 0.018,
            "Why redundant history frames?",
            ha="left", va="top", fontsize=9.5, fontweight="bold", color=C_REPLAY)
    ax.text(n2_x + 0.012, n2_y + n2_h - 0.045,
            "Each UDP packet piggybacks the previous\n"
            "few frames' inputs.\n"
            "Lose one packet -> next packet restores it,\n"
            "zero RTT recovery.\n"
            "KCP ARQ left as a stub (Ch.17).",
            ha="left", va="top", fontsize=8.0, color=C_INK,
            fontfamily="monospace")

    plt.savefig(OUT, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("saved:", OUT)


if __name__ == "__main__":
    fig_modes_and_structure()
    fig_online_topology()
