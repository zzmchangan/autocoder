"""fig-16-messagetype-handler-map.png
MessageType 17-value classification table.
Columns: Decimal Band | Semantic Category | MessageType (value) | Direction | Handler | Notes
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

OUT = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-16-messagetype-handler-map.png"

# Color palette per semantic category band
BAND_COLORS = {
    "conn":  "#E3F2FD",  # light blue - connection
    "ctrl":  "#FFF3E0",  # light amber - game control
    "sync":  "#E8F5E9",  # light green - frame sync
    "state": "#F3E5F5",  # light purple - state
    "hb":    "#ECEFF1",  # light gray-blue - heartbeat
    "rc":    "#FCE4EC",  # light pink - reconnect
}
BAND_EDGE = {
    "conn":  "#1565C0",
    "ctrl":  "#E65100",
    "sync":  "#2E7D32",
    "state": "#6A1B9A",
    "hb":    "#455A64",
    "rc":    "#AD1457",
}

# Rows: (band_key, category, MessageType, value, direction, handler, note)
rows = [
    ("conn",  "Connection (1-3)",       "JoinRequest",      "1",  "C -> S", "JoinHandler",            "ProtocolVersion check, build/find room, TryJoin (topple token)"),
    ("conn",  "",                       "JoinResponse",     "2",  "S -> C", "-",                       "Reply: success + roomId + playerId + token"),
    ("conn",  "",                       "LeaveNotify",      "3",  "C -> S", "LeaveHandler",           "RemovePlayer (physical remove, clear name)"),
    ("ctrl",  "Game Control (10-11)",   "GameStart",        "10", "S -> C", "-",                       "Server pushes start (room full / owner start)"),
    ("ctrl",  "",                       "GameEnd",          "11", "(none)", "(placeholder)",          "PLACEHOLDER: enum value exists, no Message class, no parser case"),
    ("sync",  "Frame Sync (20-23)",     "ClientInput",      "20", "C -> S", "InputHandler",           "Validate authoritative playerId, forward redundant input"),
    ("sync",  "",                       "ServerFrame",      "21", "S -> C", "-",                       "Server broadcasts authoritative frame inputs (20Hz)"),
    ("sync",  "",                       "HashReport",       "22", "C -> S", "HashReportHandler",      "Forward redundant hashes -> OnHashReport (desync audit)"),
    ("sync",  "",                       "HashMismatch",     "23", "S -> C", "-",                       "Server notifies desync detected"),
    ("state", "State (30-31)",          "StateRequest",     "30", "C -> S", "StateRequestHandler",    "Query snapshot, reply StateResponse (tick=-1 if breaker tripped)"),
    ("state", "",                       "StateResponse",    "31", "S -> C", "-",                       "Reply full World snapshot (SerializationVersion=2)"),
    ("hb",    "Heartbeat (40-41)",      "Ping",             "40", "C -> S", "PingHandler",            "Refresh LastActiveTime, reply Pong"),
    ("hb",    "",                       "Pong",             "41", "S -> C", "-",                       "Reply to Ping"),
    ("rc",    "Reconnect (50-54)",      "ReconnectRequest", "50", "C -> S", "ReconnectHandler",       "Process-level reconnect, verify playerId+token, resend GameStart"),
    ("rc",    "",                       "ReconnectResponse","51", "S -> C", "-",                       "Reply: success + snapshot"),
    ("rc",    "",                       "MissFrameRequest", "52", "C -> S", "MissFrameRequestHandler","Query history frame, reply MissFrameResponse (IsExpired if stale)"),
    ("rc",    "",                       "MissFrameResponse","53", "S -> C", "-",                       "Reply missing historical frame"),
    ("rc",    "",                       "MissFrameAck",     "54", "C -> S", "MissFrameAckHandler",    "Log only (server does not gate eviction on ACK)"),
]

# Column x positions and widths (in axes coords, 0..1)
# cols: Band, MessageType, Value, Direction, Handler, Notes
col_x = [0.000, 0.140, 0.290, 0.355, 0.440, 0.600]  # left edges
col_w = [0.140, 0.150, 0.065, 0.085, 0.160, 0.400]
headers = ["Decimal Band", "MessageType", "Val", "Direction", "Handler", "Notes"]

n = len(rows)
row_h = 0.045
header_h = 0.052
top = 0.93
bottom = top - header_h - n * row_h - 0.10  # extra room for footnote

fig, ax = plt.subplots(figsize=(15, 0.46 * n + 3.2), dpi=150)
ax.set_xlim(0, 1)
ax.set_ylim(bottom - 0.04, 1.0)
ax.axis("off")

# Title
ax.text(0.5, 0.975, "MessageType Enum: 18 Values  =  9 Handlers (C->S)  +  8 Push (S->C)  +  1 Placeholder, by Decimal Band",
        ha="center", va="top", fontsize=14, fontweight="bold", color="#212121")

# Header row
y = top - header_h
for i, htext in enumerate(headers):
    ax.add_patch(Rectangle((col_x[i], y), col_w[i], header_h,
                           facecolor="#263238", edgecolor="white", linewidth=1.2))
    ax.text(col_x[i] + col_w[i] / 2, y + header_h / 2, htext,
            ha="center", va="center", fontsize=10.5, fontweight="bold", color="white")

# Data rows
y -= row_h
for r in rows:
    band_key, cat, mt, val, direction, handler, note = r
    fc = BAND_COLORS[band_key]
    ec = BAND_EDGE[band_key]
    # special highlight for placeholder row
    placeholder = (handler == "(placeholder)")
    vals = [cat, mt, val, direction, handler, note]
    for i, v in enumerate(vals):
        cell_fc = "#FFEBEE" if placeholder and i >= 4 else fc
        ax.add_patch(Rectangle((col_x[i], y), col_w[i], row_h,
                               facecolor=cell_fc, edgecolor=ec, linewidth=0.8))
        ha = "left" if i in (1, 4, 5) else "center"
        padx = 0.008 if ha == "left" else 0
        fw = "bold" if i == 1 else "normal"
        color = "#B71C1C" if placeholder and i == 2 else ("#B71C1C" if placeholder and i == 4 else "#212121")
        it = "italic" if placeholder and i in (4, 5) else "normal"
        ax.text(col_x[i] + padx + (col_w[i] - 2 * padx) / 2 if ha == "center" else col_x[i] + padx,
                y + row_h / 2, v,
                ha=ha, va="center",
                fontsize=9.2 if i != 5 else 8.7,
                fontweight=fw, color=color, fontstyle=it,
                wrap=True)
    y -= row_h

# Direction legend strip
legend_y = y - 0.025
ax.text(col_x[0], legend_y,
        "C -> S = client to server (9 types, each has a Handler)    "
        "S -> C = server to client (8 types, no Handler needed, server pushes)    "
        "(none) = placeholder, no direction assigned",
        ha="left", va="top", fontsize=9.5, color="#37474F", fontstyle="italic")

# Footnote
foot_y = legend_y - 0.06
ax.add_patch(Rectangle((col_x[0], foot_y - 0.055), sum(col_w), 0.055,
                       facecolor="#FFF8E1", edgecolor="#F57F17", linewidth=1.0))
ax.text(col_x[0] + 0.01, foot_y - 0.005,
        "18 enum values  =  9 client-to-server (have Handler)  +  8 server-to-client (pushed, no Handler)  +  1 placeholder.\n"
        "GameEnd = 11 is a PLACEHOLDER: enum value reserved, no GameEndMessage class, no parser case.\n"
        "MissFrameHandler.cs holds 2 classes (Request + Ack) -> source has 9 Handlers, docs sometimes say 7.",
        ha="left", va="top", fontsize=9, color="#5D4037", linespacing=1.5)

plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.15)
print("saved:", OUT)
