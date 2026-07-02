# -*- coding: utf-8 -*-
"""
P5-19 Reconnect chapter figures.
fig-19-02: Process-restart reconnect five-step root-cause chain.
  Drawn right -> left (effect on right, root cause on left) so the reader follows
  the disaster backward, then the fix is shown as a single cut at Step 1.
  Steps (right -> left):
    5  Chain reaction -- can never return to Room 1
    4  CreateRoom -> Room 2 (new token)
    3  P0-2 TryJoin rejected (token mismatch)
    2  Bare JoinRequest (no token)
    1  Credential memory-only, lost on process exit   <-- ROOT CAUSE (cut here)
  Bottom: fix = persist credential (IReconnectCredentialStore) -> prefer
          ReconnectRequest -> bypass steps 2-5.
Style: causal chain (boxes + arrows), English labels, dpi 150, Agg backend.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle
from matplotlib.lines import Line2D

plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

OUT_DIR = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images"


def gen_fig_19_02():
    fig, ax = plt.subplots(figsize=(16, 8.8))

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

    def arrow(x1, y1, x2, y2, color="#444", lw=1.9, style="->", ls="-", z=4, rad=0.0):
        a = FancyArrowPatch((x1, y1), (x2, y2),
                            arrowstyle=style, color=color, lw=lw,
                            linestyle=ls, zorder=z,
                            connectionstyle=f"arc3,rad={rad}",
                            mutation_scale=18)
        ax.add_artist(a)

    # =======================================================================
    # Five step boxes laid out LEFT -> RIGHT in numeric order (1..5).
    # Causality flows rightward (step1 -> step2 -> ... -> step5).
    # A header strip on top reads the chain in the REVERSE (effect -> root) direction,
    # i.e. "why can't return? <- because new room <- because top-login rejected ...".
    # The root cause (Step 1) is highlighted; the FIX cuts at Step 1 (bottom panel).
    # =======================================================================
    y_step = 5.0       # main chain y
    box_w = 2.85
    box_h = 1.7
    xs = [1.7, 4.7, 7.7, 10.7, 13.7]   # centers of steps 1..5

    step_data = [
        # (number, title, body, facecolor, edgecolor, textcolor)
        (1,
         "STEP 1 (ROOT)\nCredential memory-only",
         "_reconnectToken / PlayerId\n/ _roomId are in-RAM fields.\nOn process exit -> ALL LOST.\nReconnectRequest path\nunusable (self-check fails).",
         "#f5b8b8", "#a33a00", "#5c1a00"),
        (2,
         "STEP 2\nDegrade to bare JoinRequest",
         "No credential to send.\nClient falls back to\nJoinRequest(name only,\ntoken gone). Goes through\nfresh-join path, NOT reconnect.",
         "#fde2b0", "#a33a00", "#5c1a00"),
        (3,
         "STEP 3\nP0-2 TryJoin rejects",
         "Server finds Room 1 by name,\nwants top-login. But P0-2\nrequires token match:\nexistingPlayer.ReconnectToken\n!= \"\" -> REJECT.",
         "#fde2b0", "#a33a00", "#5c1a00"),
        (4,
         "STEP 4\nCreateRoom -> Room 2",
         "Top-login failed AND\nroomId==0 (client does\nnot know its room).\nJoinHandler CreateRoom\nassigns NEW Room 2 + new token.",
         "#fdf2c0", "#C8860B", "#5c3a06"),
        (5,
         "STEP 5 (DISASTER)\nChain: never returns",
         "Client now holds Room 2 token.\nRoom 1's old record (old token)\nstays ~30s. Each reconnect:\nFindRoomByDisconnectedPlayer\nmay hit Room 1 first ->\ntoken NEVER matches ->\nrepeated new rooms.",
         "#f5b8b8", "#a33a00", "#5c1a00"),
    ]

    # ---- header strip: reverse-direction reading guide --------------------
    # arrow pointing LEFT under the title showing "trace back: why? <--- because <---"
    rev_strip = Rectangle((1.0, 6.7), 13.4, 0.62,
                          facecolor="#0b3d91", edgecolor="#06245e", zorder=4)
    ax.add_patch(rev_strip)
    ax.text(7.7, 7.01,
            "READ RIGHT -> LEFT (effect back to root):   "
            "can never return  <-  new room created  <-  "
            "top-login rejected  <-  bare JoinRequest  <-  credential lost",
            ha="center", va="center", fontsize=8.7, fontweight="bold",
            color="white", zorder=5)

    # ---- draw the five step boxes -----------------------------------------
    for (num, title, body, fc, ec, tc), cx in zip(step_data, xs):
        # outer box
        box(cx, y_step, box_w, box_h, "", fc, ec, lw=1.8, z=5)
        # title band at top of the box
        title_rect = FancyBboxPatch((cx - box_w / 2 + 0.06, y_step + box_h / 2 - 0.5),
                                    box_w - 0.12, 0.42,
                                    boxstyle="round,pad=0.01,rounding_size=0.06",
                                    facecolor=ec, edgecolor=ec,
                                    linewidth=0, zorder=6, alpha=0.85)
        ax.add_patch(title_rect)
        ax.text(cx, y_step + box_h / 2 - 0.29, title,
                ha="center", va="center", fontsize=8.6, fontweight="bold",
                color="white", zorder=7)
        # body text
        ax.text(cx, y_step - 0.18, body, ha="center", va="center",
                fontsize=8.0, color=tc, zorder=7)

    # ---- causal arrows between steps (rightward) --------------------------
    for i in range(4):
        x_from = xs[i] + box_w / 2
        x_to = xs[i + 1] - box_w / 2
        arrow(x_from + 0.05, y_step, x_to - 0.05, y_step,
              color="#a33a00", lw=2.4)
    # "causes" label above each arrow
    for i in range(4):
        xm = (xs[i] + xs[i + 1]) / 2
        ax.text(xm, y_step + 0.42, "causes", ha="center", va="center",
                fontsize=8, fontweight="bold", color="#a33a00", style="italic",
                bbox=dict(boxstyle="round,pad=0.15", facecolor="white",
                          edgecolor="#a33a00", linewidth=0.8))

    # ---- highlight ring around Step 1 (root cause) ------------------------
    root_ring = FancyBboxPatch((xs[0] - box_w / 2 - 0.12, y_step - box_h / 2 - 0.12),
                               box_w + 0.24, box_h + 0.24,
                               boxstyle="round,pad=0.02,rounding_size=0.12",
                               facecolor="none", edgecolor="#a33a00",
                               linewidth=2.6, linestyle="--", zorder=8)
    ax.add_patch(root_ring)
    ax.text(xs[0], y_step - box_h / 2 - 0.42,
            "ROOT CAUSE  (cut here)", ha="center", va="center",
            fontsize=9, fontweight="bold", color="#a33a00",
            bbox=dict(boxstyle="round,pad=0.25", facecolor="#fff0ec",
                      edgecolor="#a33a00", linewidth=1.4))

    # =======================================================================
    # FIX PANEL (bottom): cut at Step 1 -> persist credential
    # =======================================================================
    fix_y = 2.1
    fix_band = Rectangle((1.0, fix_y - 1.05), 13.4, 2.0,
                         facecolor="#eef7f0", edgecolor="#2a7a2a",
                         linewidth=1.4, zorder=3)
    ax.add_patch(fix_band)

    # band title
    ax.text(7.7, fix_y + 0.72,
            "FIX:  cut at Step 1  --  persist credential, prefer ReconnectRequest",
            ha="center", va="center", fontsize=10.5, fontweight="bold",
            color="#1E5E29", zorder=5)

    # three fix sub-boxes
    box(3.4, fix_y - 0.15, 3.6, 1.05,
        "IReconnectCredentialStore\n(Save / Load / Clear)\n"
        "interface in zero-dep core,\nhost injects impl",
        "#cfe8d4", "#2a7a2a", textcolor="#1E5E29",
        fontsize=8.4, fontweight="bold")
    box(7.7, fix_y - 0.15, 3.6, 1.05,
        "ReconnectCredential (4 fields)\n"
        "PlayerName / PlayerId /\nRoomId / ReconnectToken\n(NO lastAckTick)",
        "#cfe8d4", "#2a7a2a", textcolor="#1E5E29",
        fontsize=8.4, fontweight="bold")
    box(12.0, fix_y - 0.15, 3.6, 1.05,
        "ConnectAsync: load credential\n-> ReconnectRequest first\n"
        "(by roomId, no State==Playing limit)\n"
        "fail -> Clear + degrade to Join",
        "#cfe8d4", "#2a7a2a", textcolor="#1E5E29",
        fontsize=8.2, fontweight="bold")

    # arrows between fix boxes
    arrow(5.25, fix_y - 0.15, 5.85, fix_y - 0.15, color="#2a7a2a", lw=1.7)
    arrow(9.55, fix_y - 0.15, 10.15, fix_y - 0.15, color="#2a7a2a", lw=1.7)

    # downward arrow from Step1 root ring to fix band (the "cut")
    arrow(xs[0], y_step - box_h / 2 - 0.78, xs[0], fix_y + 1.0,
          color="#a33a00", lw=2.2, style="-|>")
    ax.text(xs[0] + 0.15, (y_step - box_h / 2 - 0.78 + fix_y + 1.0) / 2,
            "fix enters here", fontsize=8.4, color="#a33a00",
            fontweight="bold", style="italic", va="center")

    # ---- bypass banner: steps 2-5 avoided ----------------------------------
    bypass_y = 0.45
    bypass = Rectangle((xs[1] - box_w / 2 - 0.1, bypass_y - 0.28),
                       (xs[4] + box_w / 2 + 0.1) - (xs[1] - box_w / 2 - 0.1), 0.56,
                       facecolor="#0b3d91", edgecolor="#06245e", zorder=4)
    ax.add_patch(bypass)
    ax.text((xs[1] + xs[4]) / 2, bypass_y,
            "WITH FIX:  steps 2-5 entirely BYPASSED  "
            "-- credential survives process restart, "
            "ReconnectRequest locates Room 1 by roomId, "
            "P0-2 token check passes",
            ha="center", va="center", fontsize=8.6, fontweight="bold",
            color="white", zorder=5)
    # big bypass arc over steps 2..5
    arrow(xs[1] - box_w / 2, y_step - box_h / 2 - 0.5,
          xs[4] + box_w / 2, y_step - box_h / 2 - 0.5,
          color="#0b3d91", lw=2.6, style="-|>", rad=-0.18)

    # =======================================================================
    # Axes cosmetics
    # =======================================================================
    ax.set_xlim(0.0, 15.6)
    ax.set_ylim(-0.1, 7.6)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ("left", "right", "top", "bottom"):
        ax.spines[spine].set_visible(False)

    ax.set_title(
        "Process-Restart Reconnect: Five-Step Root-Cause Chain\n"
        "(disaster traced right<-left to root cause; fix cuts at Step 1)",
        fontsize=12.5, fontweight="bold", pad=14)

    # ---- Legend ------------------------------------------------------------
    legend_handles = [
        Line2D([0], [0], color="#a33a00", lw=2.4, marker=">", markersize=8,
               label="causal arrow (step N causes step N+1)"),
        Line2D([0], [0], color="#0b3d91", lw=2.6, marker=">",
               markersize=8, label="fix bypasses steps 2-5"),
        Line2D([0], [0], marker="s", color="#a33a00", markerfacecolor="#f5b8b8",
               markeredgecolor="#a33a00", markersize=11, linewidth=0,
               label="root / disaster step (red)"),
        Line2D([0], [0], marker="s", color="#C8860B", markerfacecolor="#fdf2c0",
               markeredgecolor="#C8860B", markersize=11, linewidth=0,
               label="intermediate step (yellow)"),
        Line2D([0], [0], marker="s", color="#2a7a2a", markerfacecolor="#cfe8d4",
               markeredgecolor="#2a7a2a", markersize=11, linewidth=0,
               label="fix mechanism (green)"),
    ]
    ax.legend(handles=legend_handles, loc="upper left",
              bbox_to_anchor=(1.005, 1.0), fontsize=8,
              framealpha=0.95, edgecolor="#CCCCCC")

    plt.tight_layout()
    out = f"{OUT_DIR}/fig-19-02-process-restart-root-cause-chain.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"[saved] {out}")


if __name__ == "__main__":
    gen_fig_19_02()
