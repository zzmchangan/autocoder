import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle, FancyArrowPatch, FancyArrow
import numpy as np

# Series-consistent palette
C_TCP  = '#2F6DB5'   # blue  - TCP
C_UDP  = '#2E7D32'   # green - UDP (preferred)
C_WS   = '#7B4FA8'   # purple - WebSocket
C_KCP  = '#E8743B'   # orange - KCP stub
C_GOOD = '#2E7D32'
C_BAD  = '#C62828'
C_WARN = '#B26A00'
C_BG   = '#F5F7FA'
C_EDGE = '#33415C'
C_DIM  = '#6B7280'
C_LIGHT= '#EEF2F7'

# ============================================================
# Figure 17-1: Four transports comparison
# ============================================================
def fig_17_1():
    fig, ax = plt.subplots(figsize=(14.0, 8.6))
    ax.set_xlim(0, 14.0)
    ax.set_ylim(0, 8.6)
    ax.axis('off')
    ax.set_facecolor('white')

    # Title
    ax.text(7.0, 8.30,
            'Four Transports: TCP / UDP / WebSocket / KCP-stub',
            ha='center', va='center', fontsize=13.5, weight='bold', color=C_EDGE)
    ax.text(7.0, 7.92,
            'Same INetworkClient / IServerTransport interface, four implementations with different temperaments',
            ha='center', va='center', fontsize=10.5, color=C_DIM, style='italic')

    # Column layout: 4 transport columns x 5 attribute rows
    col_w = 2.95
    col_gap = 0.30
    n_cols = 4
    x0 = 0.55
    xs = [x0 + i*(col_w + col_gap) for i in range(n_cols)]

    label_w = 1.95  # left attribute label column
    y_top = 7.35
    row_h = 0.92
    n_rows = 5  # reliability / order / framing / use-case / note
    header_h = 0.78

    # ---- Header row (transport names) ----
    headers = [
        ("TCP",            C_TCP, "stream / reliable"),
        ("UDP",            C_UDP, "datagram / unreliable  (DEFAULT)"),
        ("WebSocket",      C_WS,  "msg-framed over TCP"),
        ("KCP-stub",       C_KCP, "datagram / = UDP + 4B conv"),
    ]
    for i,(name,color,sub) in enumerate(headers):
        x = xs[i]
        p = FancyBboxPatch((x, y_top), col_w, header_h,
                           boxstyle="round,pad=0.03,rounding_size=0.10",
                           linewidth=1.8, edgecolor=C_EDGE, facecolor=color, alpha=0.95)
        ax.add_patch(p)
        ax.text(x + col_w/2, y_top + header_h*0.62, name,
                ha='center', va='center', fontsize=12.5, weight='bold', color='white')
        ax.text(x + col_w/2, y_top + header_h*0.24, sub,
                ha='center', va='center', fontsize=8.3, color='white', style='italic')

    # ---- Attribute labels (left column) + cells ----
    attrs = [
        "Reliability",
        "Ordering",
        "Framing",
        "Best for",
        "Cost / catch",
    ]
    cells = [
        # TCP
        ["Reliable\n(underlying TCP)",
         "Ordered\n(in-order byte stream)",
         "Self-framing\n4B big-endian length prefix\n+ 1MB cap",
         "Debugging / simple scenes",
         "Head-of-line blocking\nslowloris defense (3 lines)"],
        # UDP
        ["Unreliable\nno ACK, no retransmit",
         "Unordered\narrive-order processing",
         "Datagram boundary\nno prefix (MTU-limited)",
         "Real-time lockstep\n(DEFAULT TransportType)",
         "NAT drift, endpoint flood\nredundancy frames cover loss"],
        # WebSocket
        ["Reliable\n(over TCP)",
         "Ordered\n(over TCP)",
         "Protocol-native msg frame\ndo-while merge fragments",
         "Web / mini-program\nONLY choice in browser",
         "Plaintext by default (ws://)\nwss needs reverse proxy"],
        # KCP-stub
        ["Unreliable (= UDP)\nNO ARQ engine",
         "Unordered (= UDP)\nframe no. self-check",
         "Datagram + 4B conv\n(empty Update/SetConfig)",
         "Reserved extension slot\ncurrently = UDP + conv",
         "Stub: interface complete,\nalgorithm empty  (YAGNI)"],
    ]

    for r, attr in enumerate(attrs):
        y = y_top - 0.05 - (r+1)*row_h
        # left label cell
        p = FancyBboxPatch((x0 - label_w, y), label_w, row_h - 0.08,
                           boxstyle="round,pad=0.03,rounding_size=0.08",
                           linewidth=1.4, edgecolor=C_EDGE, facecolor=C_LIGHT, alpha=0.95)
        ax.add_patch(p)
        ax.text(x0 - label_w/2, y + (row_h-0.08)/2, attr,
                ha='center', va='center', fontsize=10.5, weight='bold', color=C_EDGE)

        for c in range(n_cols):
            x = xs[c]
            txt = cells[c][r]
            # color-code the row by content
            face = 'white'
            edge = C_EDGE
            lw = 1.2
            txtcolor = C_EDGE
            weight = 'normal'
            # reliability / ordering rows -> green/red tint based on good/bad
            if r == 0:  # reliability
                if c in (0,2):  # reliable
                    face = '#E8F5E9'
                else:
                    face = '#FFEBEE'
            elif r == 1:  # ordering
                if c in (0,2):
                    face = '#E8F5E9'
                else:
                    face = '#FFF4E5'
            elif r == 3:  # best for
                if c == 1:  # UDP default/preferred
                    face = '#E8F5E9'; weight='bold'
                elif c == 3:
                    face = '#FFF3E0'
            elif r == 4:  # catch
                face = '#FFFDE7'
            p = FancyBboxPatch((x, y), col_w, row_h - 0.08,
                               boxstyle="round,pad=0.03,rounding_size=0.08",
                               linewidth=lw, edgecolor=edge, facecolor=face, alpha=0.95)
            ax.add_patch(p)
            ax.text(x + col_w/2, y + (row_h-0.08)/2, txt,
                    ha='center', va='center', fontsize=8.7, color=txtcolor, weight=weight)

    # ---- Bottom summary bar ----
    yb = 0.55
    p = FancyBboxPatch((x0 - label_w, yb), label_w + n_cols*col_w + (n_cols-1)*col_gap, 0.55,
                       boxstyle="round,pad=0.03,rounding_size=0.08",
                       linewidth=1.5, edgecolor=C_EDGE, facecolor='#E3F2FD', alpha=0.9)
    ax.add_patch(p)
    ax.text(x0 - label_w/2 + (label_w + n_cols*col_w + (n_cols-1)*col_gap)/2, yb + 0.27,
            "All four plug into the SAME INetworkClient / IServerTransport — upper layer never knows which one is running.  "
            "Reliability gap of UDP/KCP is covered at PROTOCOL layer (redundancy frames), not transport.",
            ha='center', va='center', fontsize=9.0, color=C_EDGE, style='italic')

    plt.savefig('fig-17-01-transport-comparison.png', dpi=150, bbox_inches='tight',
                facecolor='white', pad_inches=0.15)
    plt.close()
    print("saved fig-17-01-transport-comparison.png")


# ============================================================
# Figure 17-2: KCP stub - interface complete vs algorithm empty
# ============================================================
def fig_17_2():
    fig, ax = plt.subplots(figsize=(14.0, 9.2))
    ax.set_xlim(0, 14.0)
    ax.set_ylim(0, 9.2)
    ax.axis('off')
    ax.set_facecolor('white')

    # Title
    ax.text(7.0, 8.90,
            'KCP stub: Interface Complete, Algorithm Empty',
            ha='center', va='center', fontsize=13.5, weight='bold', color=C_EDGE)
    ax.text(7.0, 8.55,
            'IKcpCore interface + KcpConfig freeze the replacement surface;  default SimpleKcpCore degrades to UDP+conv;  '
            'real KCP = swap one factory method',
            ha='center', va='center', fontsize=9.8, color=C_DIM, style='italic')

    # ---- Top: frozen interface layer ----
    y_iface = 7.15
    iface_h = 1.20
    iface_w = 12.6
    x_iface = 0.7
    p = FancyBboxPatch((x_iface, y_iface), iface_w, iface_h,
                       boxstyle="round,pad=0.03,rounding_size=0.10",
                       linewidth=2.0, edgecolor=C_EDGE, facecolor='#E8EAF6', alpha=0.95)
    ax.add_patch(p)
    ax.text(x_iface + 0.25, y_iface + iface_h - 0.22,
            'FROZEN REPLACEMENT SURFACE  (defined in KcpClient.cs / IKcpCore)',
            ha='left', va='center', fontsize=9.5, weight='bold', color='#3949AB')

    # IKcpCore box
    p = FancyBboxPatch((x_iface + 0.30, y_iface + 0.15), 4.0, iface_h - 0.55,
                       boxstyle="round,pad=0.02,rounding_size=0.06",
                       linewidth=1.3, edgecolor='#3949AB', facecolor='white')
    ax.add_patch(p)
    ax.text(x_iface + 0.45, y_iface + iface_h - 0.50, 'interface IKcpCore',
            ha='left', va='center', fontsize=9.8, weight='bold', color='#3949AB')
    ax.text(x_iface + 0.45, y_iface + 0.55,
            'void Send(byte[] data)\nvoid Input(byte[] data)\nbyte[] Receive()\nvoid Update(uint ms)\nvoid SetConfig(KcpConfig)',
            ha='left', va='center', fontsize=8.2, color=C_EDGE, family='monospace')

    # KcpConfig box
    p = FancyBboxPatch((x_iface + 4.55, y_iface + 0.15), 3.6, iface_h - 0.55,
                       boxstyle="round,pad=0.02,rounding_size=0.06",
                       linewidth=1.3, edgecolor='#3949AB', facecolor='white')
    ax.add_patch(p)
    ax.text(x_iface + 4.70, y_iface + iface_h - 0.50, 'class KcpConfig',
            ha='left', va='center', fontsize=9.8, weight='bold', color='#3949AB')
    ax.text(x_iface + 4.70, y_iface + 0.58,
            'NoDelay : bool\nResend : int\nWindowSize : int\nMTU : int\n(standard KCP names)',
            ha='left', va='center', fontsize=8.2, color=C_EDGE, family='monospace')

    # Factory method box
    p = FancyBboxPatch((x_iface + 8.40, y_iface + 0.15), 4.0, iface_h - 0.55,
                       boxstyle="round,pad=0.02,rounding_size=0.06",
                       linewidth=1.3, edgecolor=C_KCP, facecolor='white')
    ax.add_patch(p)
    ax.text(x_iface + 8.55, y_iface + iface_h - 0.50, 'CreateKcpCore()  factory',
            ha='left', va='center', fontsize=9.8, weight='bold', color=C_KCP)
    ax.text(x_iface + 8.55, y_iface + 0.50,
            'private IKcpCore CreateKcpCore(\n    uint conv,\n    Action<byte[]> output)\n// THE single swap point',
            ha='left', va='center', fontsize=8.0, color=C_EDGE, family='monospace')

    # ---- Middle: two implementations side by side ----
    y_mid = 3.55
    mid_h = 3.10
    mid_w = 5.95
    x_left  = 0.70
    x_right = 7.35

    # Left: SimpleKcpCore (current default)
    p = FancyBboxPatch((x_left, y_mid), mid_w, mid_h,
                       boxstyle="round,pad=0.03,rounding_size=0.10",
                       linewidth=1.8, edgecolor=C_DIM, facecolor='#F5F5F5', alpha=0.95)
    ax.add_patch(p)
    # header strip
    p = FancyBboxPatch((x_left, y_mid + mid_h - 0.55), mid_w, 0.55,
                       boxstyle="round,pad=0.03,rounding_size=0.10",
                       linewidth=0, facecolor=C_DIM, alpha=0.9)
    ax.add_patch(p)
    ax.text(x_left + mid_w/2, y_mid + mid_h - 0.275,
            'SimpleKcpCore   (CURRENT DEFAULT = stub)',
            ha='center', va='center', fontsize=10.5, weight='bold', color='white')

    # body
    bx = x_left + 0.25
    by = y_mid + 0.25
    bw = mid_w - 0.50
    bh = mid_h - 0.55 - 0.30
    p = FancyBboxPatch((bx, by), bw, bh,
                       boxstyle="round,pad=0.02,rounding_size=0.05",
                       linewidth=1.0, edgecolor=C_DIM, facecolor='white')
    ax.add_patch(p)

    ax.text(bx + 0.18, by + bh - 0.25, 'Send(data):',
            ha='left', va='center', fontsize=8.8, weight='bold', color=C_EDGE)
    ax.text(bx + 0.18, by + bh - 0.50,
            '  prepend 4B conv header + raw data\n  _output(packet)  // straight to UDP\n  NO slice / NO sn / NO queue',
            ha='left', va='center', fontsize=7.8, color=C_EDGE, family='monospace')

    ax.text(bx + 0.18, by + bh - 1.15, 'Input(data):',
            ha='left', va='center', fontsize=8.8, weight='bold', color=C_EDGE)
    ax.text(bx + 0.18, by + bh - 1.40,
            '  parse conv;  if conv != _conv return\n  strip 4B, enqueue payload\n  NO ack / NO window update',
            ha='left', va='center', fontsize=7.8, color=C_EDGE, family='monospace')

    ax.text(bx + 0.18, by + bh - 2.05, 'Update(ms) / SetConfig(cfg):',
            ha='left', va='center', fontsize=8.8, weight='bold', color=C_BAD)
    ax.text(bx + 0.18, by + bh - 2.30,
            '  // simplified: not implemented\n  EMPTY METHOD BODY',
            ha='left', va='center', fontsize=8.0, color=C_BAD, weight='bold', family='monospace')

    # verdict tag
    p = FancyBboxPatch((bx + bw - 2.55, by + 0.15), 2.40, 0.38,
                       boxstyle="round,pad=0.02,rounding_size=0.05",
                       linewidth=1.2, edgecolor=C_BAD, facecolor='#FFEBEE')
    ax.add_patch(p)
    ax.text(bx + bw - 1.35, by + 0.34, '= UDP + 4B conv filter',
            ha='center', va='center', fontsize=8.0, weight='bold', color=C_BAD)

    # Right: Real KCP (future swap)
    p = FancyBboxPatch((x_right, y_mid), mid_w, mid_h,
                       boxstyle="round,pad=0.03,rounding_size=0.10",
                       linewidth=1.8, edgecolor=C_KCP, facecolor='#FFF3E0', alpha=0.95)
    ax.add_patch(p)
    p = FancyBboxPatch((x_right, y_mid + mid_h - 0.55), mid_w, 0.55,
                       boxstyle="round,pad=0.03,rounding_size=0.10",
                       linewidth=0, facecolor=C_KCP, alpha=0.95)
    ax.add_patch(p)
    ax.text(x_right + mid_w/2, y_mid + mid_h - 0.275,
            'Kcp2kCore / KcpNetCore   (FUTURE swap-in)',
            ha='center', va='center', fontsize=10.5, weight='bold', color='white')

    bx = x_right + 0.25
    by = y_mid + 0.25
    bw = mid_w - 0.50
    bh = mid_h - 0.55 - 0.30
    p = FancyBboxPatch((bx, by), bw, bh,
                       boxstyle="round,pad=0.02,rounding_size=0.05",
                       linewidth=1.0, edgecolor=C_KCP, facecolor='white')
    ax.add_patch(p)

    ax.text(bx + 0.18, by + bh - 0.25, 'Send(data):',
            ha='left', va='center', fontsize=8.8, weight='bold', color=C_EDGE)
    ax.text(bx + 0.18, by + bh - 0.50,
            '  slice into MTU segments\n  assign sn, push to snd_queue\n  flush by window on Update',
            ha='left', va='center', fontsize=7.8, color=C_EDGE, family='monospace')

    ax.text(bx + 0.18, by + bh - 1.15, 'Input(data):',
            ha='left', va='center', fontsize=8.8, weight='bold', color=C_EDGE)
    ax.text(bx + 0.18, by + bh - 1.40,
            '  parse KCP header (conv/cmd/frg/wnd/sn/una)\n  ACK handling, remove confirmed from snd_buf\n  DATA -> recv queue, send ACK back',
            ha='left', va='center', fontsize=7.8, color=C_EDGE, family='monospace')

    ax.text(bx + 0.18, by + bh - 2.05, 'Update(ms) / SetConfig(cfg):',
            ha='left', va='center', fontsize=8.8, weight='bold', color=C_GOOD)
    ax.text(bx + 0.18, by + bh - 2.30,
            '  ARQ engine: timeout + fast resend\n  RTO estimate, window advance\n  apply NoDelay/Resend/WindowSize',
            ha='left', va='center', fontsize=7.8, color=C_GOOD, family='monospace')

    p = FancyBboxPatch((bx + bw - 2.55, by + 0.15), 2.40, 0.38,
                       boxstyle="round,pad=0.02,rounding_size=0.05",
                       linewidth=1.2, edgecolor=C_GOOD, facecolor='#E8F5E9')
    ax.add_patch(p)
    ax.text(bx + bw - 1.35, by + 0.34, 'real ARQ over UDP',
            ha='center', va='center', fontsize=8.0, weight='bold', color=C_GOOD)

    # ---- Arrow: swap point ----
    arrow = FancyArrowPatch((x_left + mid_w + 0.05, y_mid + mid_h/2),
                            (x_right - 0.05, y_mid + mid_h/2),
                            arrowstyle='-|>', mutation_scale=22,
                            linewidth=2.2, color=C_KCP)
    ax.add_patch(arrow)
    ax.text((x_left + mid_w + x_right)/2, y_mid + mid_h/2 + 0.28,
            'swap CreateKcpCore()',
            ha='center', va='center', fontsize=9.0, weight='bold', color=C_KCP)
    ax.text((x_left + mid_w + x_right)/2, y_mid + mid_h/2 - 0.28,
            'one method',
            ha='center', va='center', fontsize=8.0, color=C_DIM, style='italic')

    # ---- Arrows from interface layer to both implementations ----
    for xtarget in [x_left + mid_w/2, x_right + mid_w/2]:
        arrow = FancyArrowPatch((xtarget, y_iface),
                                (xtarget, y_mid + mid_h + 0.02),
                                arrowstyle='-|>', mutation_scale=14,
                                linewidth=1.3, color=C_DIM, linestyle='--')
        ax.add_patch(arrow)

    # ---- Bottom: UDP substrate ----
    y_udp = 0.55
    p = FancyBboxPatch((0.70, y_udp), 12.6, 0.65,
                       boxstyle="round,pad=0.03,rounding_size=0.10",
                       linewidth=1.8, edgecolor=C_UDP, facecolor=C_UDP, alpha=0.92)
    ax.add_patch(p)
    ax.text(7.0, y_udp + 0.325,
            'UDP  (both implementations ride on UdpClient — KCP value = ARQ engine ABOVE UDP, not a new transport)',
            ha='center', va='center', fontsize=10.0, weight='bold', color='white')

    # arrows from both impls down to UDP
    for xsrc in [x_left + mid_w/2, x_right + mid_w/2]:
        arrow = FancyArrowPatch((xsrc, y_mid),
                                (xsrc, y_udp + 0.67),
                                arrowstyle='-|>', mutation_scale=12,
                                linewidth=1.2, color=C_DIM)
        ax.add_patch(arrow)

    # ---- Side note: why stub is OK ----
    ax.text(7.0, 2.55,
            'Why empty Update/SetConfig is NOT a bug:  redundancy frames (ch.14) already push loss-recovery to PROTOCOL layer.\n'
            'At 10% loss, 3 consecutive drops (the only case redundancy cannot cover) = 0.1%.  KCP ARQ is icing, not necessity (YAGNI).',
            ha='center', va='center', fontsize=8.8, color=C_EDGE, style='italic',
            bbox=dict(boxstyle='round,pad=0.4', facecolor='#FFFDE7', edgecolor=C_WARN, linewidth=1.2))

    plt.savefig('fig-17-02-kcp-stub.png', dpi=150, bbox_inches='tight',
                facecolor='white', pad_inches=0.15)
    plt.close()
    print("saved fig-17-02-kcp-stub.png")


# ============================================================
# Figure 17-3: Redundancy frames loss-recovery probability
# ============================================================
def fig_17_3():
    fig, ax = plt.subplots(figsize=(12.8, 7.8))
    ax.set_facecolor('white')

    p = np.linspace(0.005, 0.35, 400)
    p2 = p**2    # RedundancyCount=1  -> need 2 consecutive drops
    p3 = p**3    # RedundancyCount=2 (DEFAULT) -> 3 consecutive
    p4 = p**4    # RedundancyCount=3 -> 4 consecutive

    ax.set_yscale('log')
    ax.plot(p*100, p2*100, color=C_WARN, linewidth=2.4,
            label='RedundancyCount = 1   ($p^2$,  need 2 consecutive drops)')
    ax.plot(p*100, p3*100, color=C_KCP, linewidth=3.0,
            label='RedundancyCount = 2   ($p^3$, DEFAULT, need 3 consecutive drops)')
    ax.plot(p*100, p4*100, color=C_UDP, linewidth=2.4,
            label='RedundancyCount = 3   ($p^4$, need 4 consecutive drops)')

    # Single-packet loss reference (the raw loss rate)
    ax.plot(p*100, p*100, color=C_DIM, linewidth=1.6, linestyle=':',
            label='raw single-packet loss  ($p$, no redundancy)')

    # Default working point: 10% loss, R=2 -> 0.1%
    wp_x = 10.0
    wp_y = 0.1
    ax.scatter([wp_x], [wp_y], s=130, color=C_KCP, zorder=6, edgecolor='white', linewidth=1.6)
    ax.annotate('DEFAULT working point\n10% loss  ->  0.1% unrecoverable\n(only this triggers MissFrameRequest, 1-RTT recovery)',
                xy=(wp_x, wp_y), xytext=(13.5, 1.5),
                fontsize=9.2, color=C_EDGE, weight='bold',
                ha='left', va='center',
                arrowprops=dict(arrowstyle='-|>', color=C_KCP, lw=1.6),
                bbox=dict(boxstyle='round,pad=0.4', facecolor='#FFF3E0', edgecolor=C_KCP, linewidth=1.4))

    # MissFrameRequest fallback line
    ax.axhline(0.1, color=C_BAD, linewidth=1.2, linestyle='--', alpha=0.7)
    ax.text(0.6, 0.13, 'MissFrameRequest fallback line (0.1%)',
            fontsize=8.2, color=C_BAD, style='italic')

    # shade typical network bands
    ax.axvspan(0, 2, alpha=0.10, color=C_UDP, zorder=0)
    ax.text(1.0, 35, 'good\nWiFi', ha='center', va='center', fontsize=8.0, color=C_UDP, weight='bold')
    ax.axvspan(2, 10, alpha=0.10, color=C_WARN, zorder=0)
    ax.text(6.0, 35, 'typical mobile / 4G-handover', ha='center', va='center', fontsize=8.0, color=C_WARN, weight='bold')
    ax.axvspan(10, 35, alpha=0.10, color=C_BAD, zorder=0)
    ax.text(22.0, 35, 'bad / congested', ha='center', va='center', fontsize=8.0, color=C_BAD, weight='bold')

    ax.set_xlabel('Per-packet loss rate  p  (%)', fontsize=11.5, color=C_EDGE)
    ax.set_ylabel('Probability redundancy CANNOT recover  (%)  [log scale]', fontsize=11.5, color=C_EDGE)
    ax.set_title('Redundancy Frames vs Loss: Why UDP is Enough and KCP ARQ is Icing',
                 fontsize=13, weight='bold', color=C_EDGE, pad=14)
    ax.set_xlim(0, 35)
    ax.set_ylim(0.00005, 50)
    ax.grid(True, which='both', alpha=0.28, linestyle='-', linewidth=0.5)
    ax.legend(loc='lower right', fontsize=9.5, framealpha=0.95, edgecolor=C_EDGE)

    # footnote
    fig.text(0.5, 0.005,
             'Model: independent per-packet loss. Real loss is bursty, so actual unrecoverable rate is slightly higher than $p^n$ (lower bound).  '
             'Bursty worst case still covered by 1-RTT MissFrameRequest.',
             ha='center', fontsize=8.2, color=C_DIM, style='italic')

    plt.tight_layout(rect=[0, 0.02, 1, 1])
    plt.savefig('fig-17-03-redundancy-probability.png', dpi=150, bbox_inches='tight',
                facecolor='white', pad_inches=0.15)
    plt.close()
    print("saved fig-17-03-redundancy-probability.png")


if __name__ == '__main__':
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    fig_17_1()
    fig_17_2()
    fig_17_3()
    print("all done")
