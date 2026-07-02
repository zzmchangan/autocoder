"""fig-27-wasd-to-tank.png and fig-27-match-timeline.png for chapter 27 (TankGame).

fig-27-assembly-graph.png is a mermaid per the chapter -> SKIPPED (no PNG).

Style matches the book's existing timeline figures (e.g. fig-09-01):
  - horizontal time axis, dual swimlanes (local / server)
  - green = correct/confirm, blue = system/snapshot, red = mismatch/rollback
  - clean white background, sans-serif, thin axes
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Polygon, Circle, Rectangle
import numpy as np

DPI = 150

# palette (consistent with book)
C_GREEN      = "#2E7D32"
C_GREEN_L    = "#C8E6C9"
C_GREEN_LL   = "#E8F5E9"
C_BLUE       = "#1565C0"
C_BLUE_L     = "#BBDEFB"
C_BLUE_LL    = "#E3F2FD"
C_RED        = "#C62828"
C_RED_L      = "#FFCDD2"
C_RED_LL     = "#FFEBEE"
C_AMBER      = "#E65100"
C_AMBER_L    = "#FFE0B2"
C_PURPLE     = "#6A1B9A"
C_PURPLE_L   = "#E1BEE7"
C_GREY       = "#455A64"
C_GREY_L     = "#ECEFF1"


# =============================================================================
# fig-27-wasd-to-tank : WASD keypress -> tank moves, six-station pipeline timeline
# =============================================================================
def fig_wasd_to_tank():
    OUT = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-27-wasd-to-tank.png"

    fig, ax = plt.subplots(figsize=(15.5, 10.0), dpi=DPI)
    fig.patch.set_facecolor("white")
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")

    ax.text(50, 97.5,
            "From WASD Keypress to Tank Moving: a Six-Station Pipeline (one render frame ~16ms)",
            ha="center", va="top", fontsize=15, fontweight="bold", color="#212121")
    ax.text(50, 93.8,
            "Local prediction and server confirmation run in parallel; they converge inside the Controller.",
            ha="center", va="top", fontsize=10.5, color=C_GREY, fontstyle="italic")

    # ---- six stations as a vertical pipeline on the left/center ----
    # each station: (number, title, subtitle, detail, chapter, color)
    stations = [
        ("1", "Input Sampling",
         "OnlineGameScene.GetInput()",
         "Raylib.IsKeyDown(W) -> TankInput\nMoveX/MoveY (sbyte) + Fire (bool)\n-> packed into 1 byte",
         "Ch.14/17 bandwidth", C_BLUE),
        ("2", "Cross-thread Command Queue",
         "Driver.Update :495",
         "network callbacks (IO thread) push\nserver frames / Pong / disconnect\ninto _commandQueue; main thread drains",
         "Ch.12 main loop", C_BLUE),
        ("3", "Controller.DoUpdate",
         "LockstepController.cs:307",
         "Stage A ConfirmServerFrames :  match? -> confirm\n                                   differ? -> RollbackTo + LoadState\nStage B PredictAhead :  local input drives forward NOW",
         "Ch.8-10 predict/rollback", C_GREEN),
        ("4", "Simulation.Tick",
         "TankGameSimulation.cs:104",
         "_inputSystem.SetFrameData + _world.ExecuteFrame\nfive Systems run by Priority (0/10/20/30/40):\nInput -> Fire -> Bullet -> Collision -> Boundary",
         "Ch.5 ECS + Ch.26 collision", C_AMBER),
        ("5", "OnLogicStep Sample",
         "TankRenderer.Update :52",
         "double-buffer: _currStates -> _lastStates,\nthen sample current logic state into _currStates\n(subscribed via _driver.OnLogicStep, not *Complete)",
         "Ch.11 rendering split", C_PURPLE),
        ("6", "Render Interp + Visual Offset",
         "TankRenderer.DrawGame :154",
         "lerp between _last and _curr by Interpolation (20fps->60fps)\nVisual Offset absorbs rollback jumps,\ndecay = exp(-15*dt) -> converges to truth",
         "Ch.11 visual offset", C_PURPLE),
    ]

    # layout: station boxes stacked vertically, slight stagger
    box_w, box_h = 46.0, 12.5
    x_left = 4.0
    y_top = 88.0
    gap = 1.6

    centers = []
    for i, (num, title, sub, detail, chap, col) in enumerate(stations):
        y_bot = y_top - box_h
        yy = y_top - i * (box_h + gap)
        # main station card
        ax.add_patch(FancyBboxPatch((x_left, yy - box_h), box_w, box_h,
                     boxstyle="round,pad=0.18,rounding_size=0.6",
                     facecolor="white", edgecolor=col, linewidth=1.8))
        # number badge
        ax.add_patch(Circle((x_left + 2.6, yy - 1.8), 1.7,
                     facecolor=col, edgecolor="none", zorder=5))
        ax.text(x_left + 2.6, yy - 1.8, num, ha="center", va="center",
                fontsize=11.5, fontweight="bold", color="white", zorder=6)
        # title
        ax.text(x_left + 5.6, yy - 1.6, title,
                ha="left", va="center", fontsize=11.8, fontweight="bold", color=col)
        # source locator (monospace)
        ax.text(x_left + 5.6, yy - 4.0, sub,
                ha="left", va="center", fontsize=8.8, color="#37474F",
                family="monospace")
        # detail
        ax.text(x_left + 5.6, yy - 7.6, detail,
                ha="left", va="center", fontsize=8.6, color="#212121", linespacing=1.45)
        # chapter tag (right of card)
        ax.text(x_left + box_w + 1.6, yy - box_h/2, chap,
                ha="left", va="center", fontsize=8.8, color=col, fontstyle="italic",
                bbox=dict(boxstyle="round,pad=0.25", facecolor="white", edgecolor=col, linewidth=0.9))

        centers.append((x_left + box_w/2, yy - box_h, yy - box_h - gap))

        y_top_next = yy - box_h - gap

    # down-arrows connecting stations
    for i in range(len(stations) - 1):
        y_from = centers[i][1]
        y_to   = centers[i][2]
        x_mid  = centers[i][0]
        ax.annotate("", xy=(x_mid, y_to + 0.2), xytext=(x_mid, y_from - 0.0),
                    arrowprops=dict(arrowstyle="-|>", color=C_GREY, lw=2.0))

    # ---- right column: parallel network dark line ----
    RX = 62.0
    RW = 36.0
    ax.add_patch(FancyBboxPatch((RX, 8.0), RW, 80.0,
                 boxstyle="round,pad=0.18,rounding_size=0.6",
                 facecolor=C_BLUE_LL, edgecolor=C_BLUE, linewidth=1.4, alpha=0.55))
    ax.add_patch(FancyBboxPatch((RX, 84.0), RW, 4.0,
                 boxstyle="round,pad=0.10,rounding_size=0.4",
                 facecolor=C_BLUE, edgecolor="none"))
    ax.text(RX + RW/2, 86.0, "Parallel Network Dark Line  (async)",
            ha="center", va="center", fontsize=10.2, fontweight="bold", color="white")

    net_steps = [
        ("Pre-send local input", "Driver pushes GetInput() result to server,\nlead = NetworkClock.PreSendCount\n(Jacobson SRTT/RTTVAR, asymmetric)",
         "Ch.13 net clock", C_BLUE),
        ("Server aggregates @20Hz", "GameRoom ticks every 50ms, packs all players'\ninputs into one FrameData; late inputs -> nullInput",
         "Ch.14 server", C_BLUE),
        ("Broadcast + redundancy", "broadcast FrameData + 2 redundant history frames\n(UDP, zero-RTT loss recovery)",
         "Ch.14/17 redundancy", C_BLUE),
        ("Client receives -> queue", "pushed into _commandQueue by IO thread,\nconsumed at top of next Driver.Update (station 2)",
         "Ch.12 thread safety", C_BLUE),
    ]
    ny = 80.0
    for title, body, chap, col in net_steps:
        ax.add_patch(FancyBboxPatch((RX + 1.5, ny - 14.0), RW - 3.0, 13.0,
                     boxstyle="round,pad=0.12,rounding_size=0.4",
                     facecolor="white", edgecolor=col, linewidth=1.2))
        ax.text(RX + 3.2, ny - 2.2, title,
                ha="left", va="center", fontsize=10.0, fontweight="bold", color=col)
        ax.text(RX + 3.2, ny - 6.8, body,
                ha="left", va="center", fontsize=8.4, color="#212121", linespacing=1.4)
        ax.text(RX + RW - 2.0, ny - 12.2, chap,
                ha="right", va="center", fontsize=7.8, color=col, fontstyle="italic")
        ny -= 16.5

    # convergence arrow: server broadcast -> Controller (station 3)
    ax.annotate("", xy=(x_left + box_w, 88.0 - 2*(box_h+gap) - box_h/2),
                xytext=(RX + 1.5, 80.0 - 3*16.5 + 6),
                arrowprops=dict(arrowstyle="-|>", color=C_GREEN, lw=2.2,
                                connectionstyle="arc3,rad=-0.18"))
    ax.text((x_left + box_w + RX + 1.5)/2 + 1.0,
            88.0 - 2*(box_h+gap) - box_h/2 + 3.5,
            "converge:\nconfirm / rollback",
            ha="center", va="center", fontsize=8.8, color=C_GREEN, fontweight="bold",
            linespacing=1.3,
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      edgecolor=C_GREEN, linewidth=1.0))

    # ---- bottom takeaway band ----
    fig.text(0.5, 0.012,
             "Takeaway: every station solves one essential problem (latency / reliability / smoothness). "
             "Skip any one and the experience breaks. The same pipeline runs identically in replay --\n"
             "only the input source changes (server -> file). That is why replay is free (Ch.21).",
             ha="center", va="bottom", fontsize=9.6, color="#37474F",
             bbox=dict(boxstyle="round,pad=0.5", facecolor=C_GREY_L, edgecolor="#90A4AE"))

    plt.tight_layout(rect=[0, 0.05, 1, 1])
    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.2)
    plt.close(fig)
    print("saved:", OUT)


# =============================================================================
# fig-27-match-timeline : full match lifecycle, server (top) / client (bottom)
# =============================================================================
def fig_match_timeline():
    OUT = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-27-match-timeline.png"

    fig, ax = plt.subplots(figsize=(16.0, 9.5), dpi=DPI)
    fig.patch.set_facecolor("white")
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")

    ax.text(50, 97.5,
            "TankGame Full Match Timeline: Join -> Match -> Battle -> Hash Audit -> End -> Replay",
            ha="center", va="top", fontsize=15, fontweight="bold", color="#212121")
    ax.text(50, 93.8,
            "Top = server-side events, Bottom = client-side events, arrows = message flow.",
            ha="center", va="top", fontsize=10.5, color=C_GREY, fontstyle="italic")

    # ---- swimlane bands ----
    # server lane (top)
    srv_y = 62.0
    srv_h = 22.0
    ax.add_patch(Rectangle((3, srv_y), 94, srv_h, facecolor=C_BLUE_LL,
                           edgecolor=C_BLUE, linewidth=1.2, alpha=0.6))
    ax.text(3.6, srv_y + srv_h - 1.6, "SERVER  (GameRoom / LockstepServer)",
            ha="left", va="top", fontsize=10.5, fontweight="bold", color=C_BLUE)

    # client lane (bottom)
    cli_y = 16.0
    cli_h = 22.0
    ax.add_patch(Rectangle((3, cli_y), 94, cli_h, facecolor=C_GREEN_LL,
                           edgecolor=C_GREEN, linewidth=1.2, alpha=0.6))
    ax.text(3.6, cli_y + cli_h - 1.6, "CLIENT  (OnlineGameScene / Driver / Simulation)",
            ha="left", va="top", fontsize=10.5, fontweight="bold", color=C_GREEN)

    # ---- time axis (very bottom) ----
    ax.annotate("", xy=(98, 11.0), xytext=(3, 11.0),
                arrowprops=dict(arrowstyle="-|>", color="#263238", lw=1.4))
    ax.text(50, 8.8, "time  (one full match: tens of seconds to minutes)",
            ha="center", va="top", fontsize=9.5, color="#546E7A", fontstyle="italic")

    # ---- phase background bands (span both lanes) ----
    # phases along x: Join(5-17), Match(17-27), Battle(27-72), HashAudit(overlapped), End(72-82), Replay(82-97)
    phases = [
        # (x0, x1, label, color, y_label)
        (5,   17, "JOIN",      C_BLUE),
        (17,  27, "MATCH",     C_PURPLE),
        (27,  72, "BATTLE  (predict / confirm / rollback interleave)", C_GREEN),
        (72,  82, "END",       C_AMBER),
        (82,  97, "REPLAY",    C_GREY),
    ]
    for x0, x1, lab, col in phases:
        # phase label bar at top
        ax.add_patch(Rectangle((x0, 89.5), x1 - x0, 2.6,
                     facecolor=col, edgecolor="none", alpha=0.85))
        ax.text((x0 + x1)/2, 90.8, lab,
                ha="center", va="center", fontsize=9.2, fontweight="bold", color="white")
        # faint vertical guide
        ax.plot([x0, x0], [11.5, 89.5], ls=":", color=col, lw=0.7, alpha=0.5)
    ax.plot([97, 97], [11.5, 89.5], ls=":", color=C_GREY, lw=0.7, alpha=0.5)

    # ---- server-side event markers ----
    srv_events = [
        # (x, y_offset_from_srv_top, label, sub, color)
        (9,  -4.5, "JoinHandler",       ":101  ver=1.1 check\nalloc PlayerId + slot",          C_BLUE),
        (22, -4.5, "GameStartMessage",  "MT=10  broadcast same RandomSeed\n+ StartTimestamp + FrameRate", C_PURPLE),
        # battle region: server ticks @20Hz, aggregates, broadcasts
        (38, -4.5, "tick @20Hz",        "aggregate inputs -> FrameData\nbroadcast + 2 redundant frames", C_GREEN),
        (55, -4.5, "OnHashReport",      ":850  wait until ALL hashes arrive,\nmajority baseline, broadcast mismatch", C_RED),
        (77, -4.5, "GameRoom -> Finished", "IsFinished==true  (<=1 tank alive)\nroom enters Finished state",  C_AMBER),
    ]
    for x, dy, lab, sub, col in srv_events:
        cy = srv_y + srv_h + dy
        ax.add_patch(Circle((x, cy + 2.5), 0.9, facecolor=col, edgecolor="white", lw=0.8, zorder=5))
        ax.text(x, cy - 0.5, lab, ha="center", va="top",
                fontsize=8.8, fontweight="bold", color=col)
        ax.text(x, cy - 3.0, sub, ha="center", va="top",
                fontsize=7.6, color="#37474F", linespacing=1.35, family="monospace")

    # ---- client-side event markers ----
    cli_events = [
        (9,  17.5, "JoinRequest",      "MT=1  -> server\n(ReconnectToken if resume)",      C_BLUE),
        (22, 17.5, "Driver.Start(seed)", ":109  init NetworkClock\n+ Simulation.Initialize", C_PURPLE),
        (33, 17.5, "Driver.Update loop", "predict -> send -> confirm/rollback\nVisual Offset absorbs jumps", C_GREEN),
        (55, 17.5, "HashReportMessage", "MT=22  ComputeHash() -> server\nOnDesync if mismatch",  C_RED),
        (77, 17.5, "CheckGameOver",     ":161  IsFinished detected locally\nReplayManager.SaveReplay", C_AMBER),
        (89, 17.5, "ReplayPlayer.Play()", "same TankGameSimulation,\ninput from file not server", C_GREY),
    ]
    for x, dy, lab, sub, col in cli_events:
        cy = cli_y + dy
        ax.add_patch(Circle((x, cy), 0.9, facecolor=col, edgecolor="white", lw=0.8, zorder=5))
        ax.text(x, cy + 2.2, lab, ha="center", va="bottom",
                fontsize=8.8, fontweight="bold", color=col)
        ax.text(x, cy - 1.8, sub, ha="center", va="top",
                fontsize=7.6, color="#37474F", linespacing=1.35, family="monospace")

    # ---- message flow arrows (between lanes) ----
    flow_arrows = [
        # (x_from, y_from, x_to, y_to, color, label, label_offset)
        (9,  cli_y + 17.5, 9,  srv_y,           C_BLUE,   "JoinRequest MT=1", 3.0),
        (9,  srv_y + srv_h - 5, 9, cli_y + 17.5 + 2, C_BLUE, "JoinResponse MT=2\n(PlayerId, slot)", -4.5),
        (22, srv_y + srv_h - 5, 22, cli_y + 17.5 + 2, C_PURPLE, "GameStart MT=10\n(RandomSeed)", -5.0),
        (38, srv_y + srv_h - 5, 38, cli_y + 17.5 + 2, C_GREEN,  "FrameData broadcast\n(+ 2 redundant)", -5.0),
        (55, cli_y + 17.5, 55, srv_y,            C_RED,    "HashReport MT=22", 3.0),
        (55, srv_y + srv_h - 5, 55, cli_y + 17.5 + 2, C_RED, "HashMismatch MT=23\n(if diverge)", -5.0),
        (77, srv_y + srv_h - 5, 77, cli_y + 17.5 + 2, C_AMBER, "Finished signal\n(implicit, no GameEnd msg)", -5.0),
    ]
    for xf, yf, xt, yt, col, lab, lo in flow_arrows:
        ax.annotate("", xy=(xt, yt), xytext=(xf, yf),
                    arrowprops=dict(arrowstyle="-|>", color=col, lw=1.6,
                                    connectionstyle="arc3,rad=0.0", alpha=0.85))
        # label near the midpoint, offset
        mx, my = (xf + xt)/2, (yf + yt)/2
        ax.text(mx + 2.5, my + lo, lab, ha="left", va="center",
                fontsize=7.4, color=col, fontweight="bold", linespacing=1.25,
                bbox=dict(boxstyle="round,pad=0.2", facecolor="white",
                          edgecolor=col, linewidth=0.7, alpha=0.95))

    # ---- annotations for the three key nodes (per caption) ----
    # 1. GameStart broadcasts same randomSeed
    ax.annotate("deterministic starting point:\nALL clients share one RandomSeed",
                xy=(22, srv_y + srv_h - 6), xytext=(20.5, 78.5),
                fontsize=8.4, color=C_PURPLE, fontweight="bold", ha="center",
                arrowprops=dict(arrowstyle="->", color=C_PURPLE, lw=1.3),
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                          edgecolor=C_PURPLE, linewidth=0.9))

    # 2. predict/confirm interleave (battle region)
    ax.add_patch(FancyBboxPatch((42, 47), 14, 6.5,
                 boxstyle="round,pad=0.18,rounding_size=0.4",
                 facecolor=C_GREEN_LL, edgecolor=C_GREEN, linewidth=1.0, alpha=0.95))
    ax.text(49, 50.2, "predict <-> confirm <-> rollback\n(most mechanism-dense region: Ch.8-17)",
            ha="center", va="center", fontsize=7.8, color=C_GREEN, fontweight="bold",
            linespacing=1.3)

    # 3. HashReport periodic audit
    ax.annotate("periodic FULL-audit hash compare\n(server waits for ALL, majority baseline)",
                xy=(55, srv_y + srv_h - 6), xytext=(57, 78.5),
                fontsize=8.4, color=C_RED, fontweight="bold", ha="center",
                arrowprops=dict(arrowstyle="->", color=C_RED, lw=1.3),
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                          edgecolor=C_RED, linewidth=0.9))

    # 4. Replay is a side-branch (replays same input stream)
    ax.annotate("replay = same Simulation, input from file\n(logic layer unchanged, Ch.21)",
                xy=(89, cli_y + 17.5), xytext=(89, 47),
                fontsize=8.4, color=C_GREY, fontweight="bold", ha="center",
                arrowprops=dict(arrowstyle="->", color=C_GREY, lw=1.3,
                                connectionstyle="arc3,rad=0.0"),
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                          edgecolor=C_GREY, linewidth=0.9))

    # ---- legend (bottom-right) ----
    leg_x, leg_y = 70.0, 2.0
    ax.add_patch(FancyBboxPatch((leg_x, leg_y), 27.5, 5.5,
                 boxstyle="round,pad=0.12,rounding_size=0.3",
                 facecolor="white", edgecolor="#90A4AE", linewidth=0.9))
    items = [(C_BLUE, "join/match"), (C_GREEN, "predict/confirm"),
             (C_RED, "hash audit"), (C_AMBER, "end"), (C_GREY, "replay")]
    sx = leg_x + 1.5
    for col, lab in items:
        ax.add_patch(Circle((sx, leg_y + 2.8), 0.55, facecolor=col, edgecolor="none"))
        ax.text(sx + 1.0, leg_y + 2.8, lab, ha="left", va="center",
                fontsize=7.6, color="#37474F")
        sx += 5.4

    # ---- bottom takeaway ----
    fig.text(0.5, -0.002,
             "Three key nodes: (1) GameStart broadcasts one RandomSeed = deterministic start; "
             "(2) battle region is predict<->confirm<->rollback interleave (Ch.8-17 densest); "
             "(3) HashReport periodic full-audit.\n"
             "Replay is OFF this timeline -- same Simulation, input from file, logic layer unchanged (Ch.21).",
             ha="center", va="top", fontsize=9.4, color="#37474F",
             bbox=dict(boxstyle="round,pad=0.5", facecolor=C_GREY_L, edgecolor="#90A4AE"))

    plt.tight_layout(rect=[0, 0.04, 1, 1])
    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.2)
    plt.close(fig)
    print("saved:", OUT)


if __name__ == "__main__":
    fig_wasd_to_tank()
    fig_match_timeline()
    print("all done")
