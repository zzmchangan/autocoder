"""fig-18-driver-presets: LockstepClientBuilder chain API + three presets
(Default / LowLatency / HighTolerance) parameter comparison."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch, FancyArrowPatch
from matplotlib.lines import Line2D

fig = plt.figure(figsize=(16, 11))

# ============ TOP: Builder chain (spans full width) ============
ax_chain = fig.add_axes([0.03, 0.70, 0.94, 0.28])
ax_chain.set_xlim(0, 16)
ax_chain.set_ylim(0, 4.5)
ax_chain.axis("off")

ax_chain.text(8.0, 4.25, "LockstepClientBuilder  --  fluent chain + Build()-time validation",
              ha="center", va="center", fontsize=14, fontweight="bold", color="#0b3d91")

# The chain of builder calls
chain = [
    ("new\nLockstepClientBuilder()", "#1a1a1a", "white"),
    (".WithServer(\n  host, port)", "#3a3a3a", "white"),
    (".UseUdp()\n/ UseKcp() / UseTcp()\n/ UseWebSocket()", "#0b3d91", "white"),
    (".WithHeartbeat(\n  interval, timeout)", "#3a3a3a", "white"),
    (".WithCredentialStore(\n  IReconnectCredentialStore)", "#3a3a3a", "white"),
    (".WithLogger(\n  ILockstepLogger)", "#3a3a3a", "white"),
    (".Build()", "#2a7a2a", "white"),
]

n = len(chain)
x_start = 0.3
box_w = 2.05
gap = 0.18
# total = 0.3 + 7*2.05 + 6*0.18 = 0.3 + 14.35 + 1.08 = 15.73 -> fits in 16
y_box = 2.2
box_h = 1.25

centers = []
for i, (label, fc, tc) in enumerate(chain):
    x = x_start + i * (box_w + gap)
    rect = FancyBboxPatch((x, y_box), box_w, box_h,
                          boxstyle="round,pad=0.02,rounding_size=0.08",
                          facecolor=fc, edgecolor="black", linewidth=1.2)
    ax_chain.add_patch(rect)
    ax_chain.text(x + box_w / 2, y_box + box_h / 2, label,
                  ha="center", va="center", fontsize=8.8, fontweight="bold",
                  color=tc, family="monospace", linespacing=1.2)
    centers.append((x + box_w, x + box_w + gap))

# arrows between boxes
for i in range(n - 1):
    ax_chain.annotate("", xy=(centers[i][1] - 0.02, y_box + box_h / 2),
                      xytext=(centers[i][0] + 0.02, y_box + box_h / 2),
                      arrowprops=dict(arrowstyle="->", color="#444", lw=1.6))

# "returns this" annotation under the chain
ax_chain.text(8.0, 1.75, "each WithXxx() returns this  ->  chainable",
              ha="center", va="center", fontsize=10, color="#444",
              fontstyle="italic", family="monospace")

# Build() validation box
val_box = FancyBboxPatch((0.3, 0.45), 7.6, 1.05,
                         boxstyle="round,pad=0.02,rounding_size=0.06",
                         facecolor="#fff4e0", edgecolor="#a06000", linewidth=1.3)
ax_chain.add_patch(val_box)
ax_chain.text(0.5, 1.28, "Build()  runs  ValidateConfig():",
              ha="left", va="center", fontsize=9.5, fontweight="bold",
              color="#a06000", family="monospace")
val_lines = [
    "- host non-empty",
    "- port in 1..65535",
    "- heartbeat interval > 0",
    "- heartbeat timeout > interval",
    "- connect timeout > 0",
]
for k, v in enumerate(val_lines):
    ax_chain.text(0.55, 1.05 - k * 0.18, v, ha="left", va="center",
                  fontsize=9, color="#1a1a1a", family="monospace")

# Fail-fast box
ff_box = FancyBboxPatch((8.2, 0.45), 7.5, 1.05,
                        boxstyle="round,pad=0.02,rounding_size=0.06",
                        facecolor="#fde2e0", edgecolor="#7a2a2a", linewidth=1.3)
ax_chain.add_patch(ff_box)
ax_chain.text(8.4, 1.28, "config error  ->  BUILD-TIME exception",
              ha="left", va="center", fontsize=9.5, fontweight="bold",
              color="#7a2a2a", family="monospace")
ax_chain.text(8.4, 1.0,
              "ArgumentException @ Build()",
              ha="left", va="center", fontsize=9, color="#7a2a2a",
              family="monospace", fontweight="bold")
ax_chain.text(8.4, 0.78,
              "NOT a NullReferenceException at frame N",
              ha="left", va="center", fontsize=8.8, color="#7a2a2a",
              family="monospace")
ax_chain.text(8.4, 0.6,
              "(runtime fault  ->  assembly-time fault)",
              ha="left", va="center", fontsize=8.5, color="#7a2a2a",
              family="monospace", fontstyle="italic")

# ============ BOTTOM: Three presets parameter table ============
ax_tbl = fig.add_axes([0.03, 0.02, 0.94, 0.62])
ax_tbl.set_xlim(0, 16)
ax_tbl.set_ylim(0, 8.2)
ax_tbl.axis("off")

ax_tbl.text(8.0, 7.95,
            "Three Presets  --  Default / LowLatency / HighTolerance  (network-environment recipes)",
            ha="center", va="center", fontsize=14, fontweight="bold", color="#0b3d91")

# Table columns: param | Default | LowLatency | HighTolerance | intent
col_widths = [3.4, 1.8, 2.0, 2.2, 6.0]   # sums to 15.4
col_x = [0.3]
for w in col_widths[:-1]:
    col_x.append(col_x[-1] + w)

headers = ["Parameter", "Default", "LowLatency\n(esports)", "HighTolerance\n(weak net)", "Design Intent"]
header_h = 0.95
row_h = 0.62
n_rows_data = 7
table_top = 7.0
table_bottom = table_top - header_h - n_rows_data * row_h  # = 7.0 - 0.95 - 4.34 = 1.71

# headers
for j, h in enumerate(headers):
    rect = Rectangle((col_x[j], table_top - header_h), col_widths[j], header_h,
                     facecolor="#1a1a1a", edgecolor="black", linewidth=1.2)
    ax_tbl.add_patch(rect)
    ax_tbl.text(col_x[j] + col_widths[j] / 2, table_top - header_h / 2, h,
                ha="center", va="center", fontsize=10.5, fontweight="bold",
                color="white", linespacing=1.2)

# rows: (param, default, low, high, intent, highlight_col)
# highlight_col: which preset stands out (0=none, 1=default, 2=low, 3=high)
rows = [
    ("MaxPredictionFrames",   "30",  "20", "50",
     "Low: don't run far (rollback costly)\nHigh: predict far to survive server gaps", 2, 3),
    ("ClockCorrectionRate",   "0.1", "0.15","0.05",
     "Low: snap to server fast\nHigh: slow, ignore weak-net jitter", 2, 3),
    ("MaxClockCorrectionMs",  "2.0", "3.0","2.0",
     "Low: allow larger single-frame nudge", 2, 0),
    ("MissFrameRequestRatePerSec", "2", "4", "2",
     "Low: request missing frames immediately", 2, 0),
    ("ReconnectIntervalSec",  "2",   "2",  "1",
     "High: reconnect more aggressively", 0, 3),
    ("MaxReconnectAttempts",  "10",  "10", "20",
     "High: give weak-net more chances", 0, 3),
    ("SnapshotThresholdFrames","300", "300","200",
     "High: abandon per-frame catch-up earlier", 0, 3),
]

preset_colors = {
    "default": ("#3a3a3a", "white"),
    "low":     ("#0b3d91", "white"),
    "high":    ("#2a7a2a", "white"),
    "standout_bg": {
        "low":  "#d6e4f5",   # light blue
        "high": "#d6ecd6",   # light green
    },
}

for i, row in enumerate(rows):
    param, dv, lv, hv, intent, low_hl, high_hl = row
    y = table_top - header_h - (i + 1) * row_h
    # row zebra for param + intent cells
    zebra = "#f5f5f5" if i % 2 == 0 else "#ececec"
    # param cell
    rect = Rectangle((col_x[0], y), col_widths[0], row_h,
                     facecolor=zebra, edgecolor="black", linewidth=0.9)
    ax_tbl.add_patch(rect)
    ax_tbl.text(col_x[0] + 0.15, y + row_h / 2, param,
                ha="left", va="center", fontsize=9.5, fontweight="bold",
                color="#1a1a1a", family="monospace")
    # preset value cells
    vals = [(dv, "default", 0), (lv, "low", low_hl), (hv, "high", high_hl)]
    for j, (val, key, hl) in enumerate(vals, start=1):
        fc_bg = preset_colors["standout_bg"][key] if hl else zebra
        rect = Rectangle((col_x[j], y), col_widths[j], row_h,
                         facecolor=fc_bg, edgecolor="black", linewidth=0.9)
        ax_tbl.add_patch(rect)
        tc = "#1a1a1a"
        fw = "bold" if hl else "normal"
        ax_tbl.text(col_x[j] + col_widths[j] / 2, y + row_h / 2, val,
                    ha="center", va="center", fontsize=10, fontweight=fw,
                    color=tc, family="monospace")
    # intent cell
    rect = Rectangle((col_x[4], y), col_widths[4], row_h,
                     facecolor=zebra, edgecolor="black", linewidth=0.9)
    ax_tbl.add_patch(rect)
    ax_tbl.text(col_x[4] + 0.15, y + row_h / 2, intent,
                ha="left", va="center", fontsize=8.6, color="#444",
                family="monospace", linespacing=1.15)

# Legend strip under the table
leg_y = table_bottom - 0.5
ax_tbl.text(0.3, leg_y, "Highlighted cells = the value that DIFFERS from Default for that preset's network profile:",
            ha="left", va="center", fontsize=9.5, fontweight="bold", color="#1a1a1a")

# LowLatency summary
ll_box = FancyBboxPatch((0.3, leg_y - 0.95), 7.5, 0.7,
                        boxstyle="round,pad=0.02,rounding_size=0.05",
                        facecolor=preset_colors["standout_bg"]["low"],
                        edgecolor="#0b3d91", linewidth=1.3)
ax_tbl.add_patch(ll_box)
ax_tbl.text(0.5, leg_y - 0.6,
            "LowLatency:  AGGRESSIVE correction + SHORT prediction",
            ha="left", va="center", fontsize=9.5, fontweight="bold",
            color="#0b3d91", family="monospace")

# HighTolerance summary
ht_box = FancyBboxPatch((8.2, leg_y - 0.95), 7.5, 0.7,
                        boxstyle="round,pad=0.02,rounding_size=0.05",
                        facecolor=preset_colors["standout_bg"]["high"],
                        edgecolor="#2a7a2a", linewidth=1.3)
ax_tbl.add_patch(ht_box)
ax_tbl.text(8.4, leg_y - 0.6,
            "HighTolerance:  LENIENT prediction + SLOW correction + RESILIENT reconnect",
            ha="left", va="center", fontsize=9.5, fontweight="bold",
            color="#1a5a1a", family="monospace")

out = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-18-driver-presets.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
print("saved:", out)
