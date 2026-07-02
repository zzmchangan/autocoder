import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle, FancyArrowPatch

# Colors (consistent with series)
C_RELAY  = '#2F6DB5'   # blue  - Relay (light)
C_AUTH   = '#E8743B'   # orange - Authoritative (heavy)
C_GOOD   = '#2E7D32'   # green - has the capability
C_BAD    = '#C62828'   # red   - lacks the capability
C_BG     = '#F5F7FA'
C_EDGE   = '#33415C'
C_DIM    = '#6B7280'

# ============================================================
# Figure 14-1: Relay vs Authoritative full-dimension comparison
# ============================================================
def fig_14_1():
    fig, ax = plt.subplots(figsize=(13.5, 8.4))
    ax.set_xlim(0, 13.5)
    ax.set_ylim(0, 8.4)
    ax.axis('off')
    ax.set_facecolor('white')

    # ---- Title ----
    ax.text(6.75, 8.05,
            'Relay vs Authoritative: Two Server Modes Across Four Dimensions',
            ha='center', va='center', fontsize=13.5, weight='bold', color=C_EDGE)
    ax.text(6.75, 7.62,
            'Both share the SAME fixed-tick metronome. Difference = does the server run game logic?',
            ha='center', va='center', fontsize=10.5, color=C_DIM, style='italic')

    # ---- Layout coordinates ----
    col_w = 5.2          # each mode column width
    col_gap = 0.6
    row_h = 0.82         # dimension row height
    x_relay = 1.6
    x_auth  = x_relay + col_w + col_gap     # 7.4
    y_top = 7.05          # top of header row
    n_rows = 6            # 6 dimension rows

    # ---- Header row (mode names) ----
    # Relay header
    p = FancyBboxPatch((x_relay, y_top), col_w, 0.7,
                       boxstyle="round,pad=0.03,rounding_size=0.10",
                       linewidth=1.8, edgecolor=C_EDGE, facecolor=C_RELAY, alpha=0.95)
    ax.add_patch(p)
    ax.text(x_relay + col_w/2, y_top + 0.35,
            "RELAY  (relay / forward only)",
            ha='center', va='center', fontsize=12, weight='bold', color='white')

    # Authoritative header
    p = FancyBboxPatch((x_auth, y_top), col_w, 0.7,
                       boxstyle="round,pad=0.03,rounding_size=0.10",
                       linewidth=1.8, edgecolor=C_EDGE, facecolor=C_AUTH, alpha=0.95)
    ax.add_patch(p)
    ax.text(x_auth + col_w/2, y_top + 0.35,
            "AUTHORITATIVE  (server also runs ISimulation)",
            ha='center', va='center', fontsize=12, weight='bold', color='white')

    # ---- Dimension rows ----
    # Each row: (label, relay_text, auth_text)
    rows = [
        ("WHO COMPUTES STATE",
         "Clients only.\nServer never runs ISimulation.",
         "Clients + server.\nServer runs a full ISimulation copy."),
        ("DESYNC DETECTION",
         "NONE.\nServer does not know the state,\ncannot verify hashes.",
         "YES (OnDesyncDetected).\nServer hash vs all client hashes,\nflag mismatch on diverge."),
        ("RECONNECT COST",
         "Replay from frame history\n(capped 3600 frames = 3 min).\nBeyond that: unrecoverable.",
         "Latest snapshot + delta frames.\nAny outage length recovered\nin ~1 round trip."),
        ("ANTI-CHEAT",
         "WEAK.\nSurface checks only (length / rate).\nFake inputs forwarded as-is.",
         "STRONG.\nAuthoritative reconciliation.\nSpeed-hack / fake-state caught\nby hash compare."),
        ("SERVER CPU",
         "VERY LOW.\nForward bytes, ~zero compute.\nScales trivially.",
         "HIGH.\nFull sim per room.\nGrows linearly with rooms\nand entity count."),
        ("FAILURE MODE",
         "Server crash = all rooms gone.\nSimple: almost nothing to break.",
         "Poisoned-snapshot circuit-breaker:\nsim disabled + snapshots cleared,\nroom degrades to relay, keeps forwarding."),
    ]

    for i, (label, relay_t, auth_t) in enumerate(rows):
        y = y_top - 0.05 - (i + 1) * row_h
        # alternating row background
        if i % 2 == 0:
            bg = Rectangle((x_relay - 0.05, y + 0.02), col_w*2 + col_gap + 0.1, row_h - 0.08,
                           linewidth=0, facecolor=C_BG, alpha=0.55, zorder=0)
            ax.add_patch(bg)

        # dimension label (left of both columns)
        ax.text(x_relay - 0.25, y + row_h/2 - 0.04, label,
                ha='right', va='center', fontsize=9.5, weight='bold', color=C_EDGE)

        # Relay cell
        relay_fc = '#FDECEC' if 'NONE' in relay_t or 'WEAK' in relay_t or 'crash' in relay_t else '#E8F0FB'
        relay_ec = C_BAD if ('NONE' in relay_t or 'WEAK' in relay_t) else C_RELAY
        p = FancyBboxPatch((x_relay, y), col_w, row_h - 0.10,
                           boxstyle="round,pad=0.02,rounding_size=0.06",
                           linewidth=1.4, edgecolor=relay_ec, facecolor=relay_fc, alpha=0.95)
        ax.add_patch(p)
        ax.text(x_relay + col_w/2, y + (row_h - 0.10)/2, relay_t,
                ha='center', va='center', fontsize=9.2, color=C_EDGE)

        # Authoritative cell
        auth_fc = '#EAF6EE' if ('YES' in auth_t or 'STRONG' in auth_t or 'circuit-breaker' in auth_t) else '#FDEEE6'
        auth_ec = C_GOOD if ('YES' in auth_t or 'STRONG' in auth_t or 'circuit-breaker' in auth_t) else C_AUTH
        p = FancyBboxPatch((x_auth, y), col_w, row_h - 0.10,
                           boxstyle="round,pad=0.02,rounding_size=0.06",
                           linewidth=1.4, edgecolor=auth_ec, facecolor=auth_fc, alpha=0.95)
        ax.add_patch(p)
        ax.text(x_auth + col_w/2, y + (row_h - 0.10)/2, auth_t,
                ha='center', va='center', fontsize=9.2, color=C_EDGE)

    # ---- Builder API line at the bottom ----
    y_api = 0.55
    banner = FancyBboxPatch((1.6, 0.20), 10.4, 0.72,
                            boxstyle="round,pad=0.04,rounding_size=0.10",
                            linewidth=1.4, edgecolor=C_EDGE, facecolor='#FFFBE6')
    ax.add_patch(banner)
    ax.text(6.75, 0.56,
            "Builder API switch:   "
            ".UseInput<TankInput>()                         "
            "vs   .UseInput<TankInput>().UseSimulation<TankGameSimulation>()",
            ha='center', va='center', fontsize=10.2, weight='bold', color=C_EDGE,
            family='monospace')

    plt.tight_layout()
    out = r'c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-14-01-relay-vs-authoritative.png'
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print("saved", out)


# ============================================================
# Figure 14-2: NTP rollback freezes wall-clock-driven room
# ============================================================
def fig_14_2():
    fig, ax = plt.subplots(figsize=(13.5, 7.2))
    ax.set_xlim(0, 13.5)
    ax.set_ylim(0, 7.2)
    ax.axis('off')
    ax.set_facecolor('white')

    # ---- Title ----
    ax.text(6.75, 6.85,
            'NTP Step-Backward Freezes a Wall-Clock-Driven Room',
            ha='center', va='center', fontsize=13.5, weight='bold', color=C_EDGE)
    ax.text(6.75, 6.48,
            'Stopwatch (monotonic) is immune; UtcNow (wall clock) breaks the tick loop forever.',
            ha='center', va='center', fontsize=10.5, color=C_DIM, style='italic')

    # =========================================================
    # TOP PANEL: Wall clock (UtcNow) -- BROKEN
    # =========================================================
    y_axis = 4.4       # timeline y for top panel
    x_left, x_right = 0.9, 12.6

    # panel label
    ax.text(x_left, y_axis + 1.55,
            "WALL CLOCK  (DateTimeOffset.UtcNow)",
            ha='left', va='center', fontsize=11.5, weight='bold', color=C_BAD)
    ax.text(x_left, y_axis + 1.18,
            "TickSinceGameStart = (UtcNow - gameStartMs) / 50ms",
            ha='left', va='center', fontsize=9.2, color=C_DIM, family='monospace')

    # main timeline arrow
    arr = FancyArrowPatch((x_left, y_axis), (x_right, y_axis),
                          arrowstyle='-|>', mutation_scale=18,
                          color=C_EDGE, lw=2.0, zorder=3)
    ax.add_patch(arr)
    ax.text(x_right + 0.05, y_axis, "t", ha='left', va='center',
            fontsize=11, color=C_EDGE, weight='bold')

    # tick markers (forward progress)
    marks = [(1.6, "t=0\nstart", 'normal'),
             (3.0, "t=500ms\nframe 10", 'normal'),
             (5.2, "t=5000ms\nframe 100", 'normal')]
    for mx, mt, _ in marks:
        ax.plot([mx, mx], [y_axis - 0.13, y_axis + 0.13], color=C_EDGE, lw=2.0, zorder=4)
        ax.text(mx, y_axis - 0.38, mt, ha='center', va='top', fontsize=9, color=C_EDGE)

    # NTP step-backward event
    ntp_x = 5.95
    ax.plot([ntp_x, ntp_x], [y_axis - 0.25, y_axis + 0.25], color=C_BAD, lw=2.6, zorder=5)
    ax.text(ntp_x, y_axis + 0.55, "NTP step backward",
            ha='center', va='bottom', fontsize=10, weight='bold', color=C_BAD)
    # curved rollback arrow from ntp_x back to a negative point
    back_x = 7.7   # where wall clock now "is" (was 5000ms, jumped back 10s -> reads -5000)
    rb = FancyArrowPatch((ntp_x, y_axis + 0.95), (back_x, y_axis + 0.95),
                         arrowstyle='-|>', mutation_scale=16,
                         color=C_BAD, lw=2.0, ls='--',
                         connectionstyle="arc3,rad=0.45", zorder=4)
    ax.add_patch(rb)
    ax.text((ntp_x + back_x)/2, y_axis + 1.55,
            "clock jumps BACK 10 s",
            ha='center', va='center', fontsize=9.5, weight='bold', color=C_BAD)

    # After rollback: UtcNow now reads -5000ms relative to start
    # elapsed = -5000, TickSinceGameStart clamps to 0
    # room frozen zone shading
    freeze_start = ntp_x + 0.15
    frz = Rectangle((freeze_start, y_axis - 0.7), x_right - freeze_start, 1.4,
                    linewidth=0, facecolor=C_BAD, alpha=0.12, zorder=1)
    ax.add_patch(frz)
    ax.text((freeze_start + x_right)/2, y_axis + 0.85,
            "ROOM FROZEN FOREVER", ha='center', va='center',
            fontsize=10.5, weight='bold', color=C_BAD)
    ax.text((freeze_start + x_right)/2, y_axis - 0.85,
            "elapsed = UtcNow - 0  =  -5000\n"
            "TickSinceGameStart  ->  0  (clamped)\n"
            "while ( _currentTick <= 0 )   // 100 <= 0  ->  FALSE",
            ha='center', va='top', fontsize=8.8, color=C_BAD, family='monospace',
            bbox=dict(boxstyle='round,pad=0.3', fc='#FDECEC', ec=C_BAD, lw=1.2))

    # note: timeout detection also fails
    ax.text(x_right - 0.1, y_axis + 1.18,
            "timeout check also reads UtcNow -> negative delta -> disabled",
            ha='right', va='center', fontsize=8.6, color=C_BAD, style='italic')

    # =========================================================
    # BOTTOM PANEL: Monotonic clock (Stopwatch) -- SAFE
    # =========================================================
    y2 = 1.55
    ax.text(x_left, y2 + 1.35,
            "MONOTONIC CLOCK  (System.Diagnostics.Stopwatch)",
            ha='left', va='center', fontsize=11.5, weight='bold', color=C_GOOD)
    ax.text(x_left, y2 + 1.0,
            "TickSinceGameStart = (stopwatch.ElapsedMilliseconds - 200) / 50ms",
            ha='left', va='center', fontsize=9.2, color=C_DIM, family='monospace')

    # timeline arrow
    arr2 = FancyArrowPatch((x_left, y2), (x_right, y2),
                           arrowstyle='-|>', mutation_scale=18,
                           color=C_EDGE, lw=2.0, zorder=3)
    ax.add_patch(arr2)
    ax.text(x_right + 0.05, y2, "t", ha='left', va='center',
            fontsize=11, color=C_EDGE, weight='bold')

    # markers continuing forward steadily
    m2 = [(1.6, "0\nstart"), (3.5, "5 s\ntick 100"), (5.95, "10 s\ntick 200"),
          (8.6, "15 s\ntick 300"), (11.0, "20 s\ntick 400")]
    for mx, mt in m2:
        ax.plot([mx, mx], [y2 - 0.13, y2 + 0.13], color=C_EDGE, lw=2.0, zorder=4)
        ax.text(mx, y2 - 0.32, mt, ha='center', va='top', fontsize=8.8, color=C_EDGE)

    # the NTP event on this timeline has NO effect
    ax.plot([ntp_x, ntp_x], [y2 - 0.30, y2 + 0.30], color=C_DIM, lw=1.6, ls=':', zorder=5)
    ax.text(ntp_x, y2 + 0.62, "same NTP event\n-> NO effect on Stopwatch",
            ha='center', va='bottom', fontsize=9, weight='bold', color=C_GOOD)

    # steady forward banner
    banner = FancyBboxPatch((x_left, y2 - 1.15), x_right - x_left, 0.55,
                            boxstyle="round,pad=0.03,rounding_size=0.08",
                            linewidth=1.4, edgecolor=C_GOOD, facecolor='#EAF6EE')
    ax.add_patch(banner)
    ax.text((x_left + x_right)/2, y2 - 0.88,
            "ROOM ADVANCES NORMALLY   (ElapsedMilliseconds strictly monotonic, never stepped back)",
            ha='center', va='center', fontsize=10, weight='bold', color=C_GOOD)

    # divider between panels
    ax.plot([x_left, x_right], [3.05, 3.05], color=C_EDGE, lw=0.8, ls=':', alpha=0.5)

    plt.tight_layout()
    out = r'c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-14-02-ntp-rollback-freeze.png'
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print("saved", out)


if __name__ == '__main__':
    fig_14_1()
    fig_14_2()
