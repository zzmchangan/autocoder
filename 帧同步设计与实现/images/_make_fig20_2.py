"""fig-20-02-five-pools.png
Five-pool system comparison table.
Columns: Pool | Pooled type | Underlying container | Double-return detection | Typical use
Rows: BufferPool, BitWriterPool, BitReaderPool, ObjectPool<T>, FrameDataPool
Bottom annotation: why not one universal pool - four dimensions vary.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch

OUT = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-20-02-five-pools.png"

# Per-row accent color (one hue per pool)
POOL_COLORS = [
    ("#E3F2FD", "#1565C0"),  # BufferPool - blue
    ("#E8F5E9", "#2E7D32"),  # BitWriterPool - green
    ("#E0F2F1", "#00695C"),  # BitReaderPool - teal
    ("#FFF3E0", "#E65100"),  # ObjectPool - amber
    ("#F3E5F5", "#6A1B9A"),  # FrameDataPool - purple
]

# rows: (pool, type, container, detection, use)
rows = [
    ("BufferPool",
     "byte[]",
     "ArrayPool<byte>.Shared\n(BCL shared, array-tuned)",
     "ConditionalWeakTable<byte[],\nLeaseMarker>   (DEBUG)",
     "serialize / network send\ntemporary buffers"),
    ("BitWriterPool",
     "BitWriter  (class)",
     "Stack<BitWriter> + lock\n(single-thread, LIFO hot)",
     "internal bool _pooledInUse\n(can add field to own class)",
     "serializer reuse\n(SaveState each frame)"),
    ("BitReaderPool",
     "BitReader  (class)",
     "Stack<BitReader> + lock\n(SetBuffer swaps data source)",
     "internal bool _pooledInUse\n(reset + unbind on return)",
     "deserializer reuse\n(rollback / reconnect)"),
    ("ObjectPool<T>",
     "any class T  (generic)",
     "ConcurrentBag<T>\n(concurrent, factory+reset)",
     "ConditionalWeakTable<T,\nLeaseMarker>   (DEBUG)",
     "general objects\n(StringBuilder, temp sets)"),
    ("FrameDataPool",
     "FrameData  (class)",
     "Dictionary<int, Bag<FrameData>>\n(buckets keyed by PlayerCount)",
     "(none - caller discipline)\nno DEBUG guard",
     "per-frame player-input sets\n(buckets match array len)"),
]

col_x = [0.000, 0.135, 0.275, 0.535, 0.760]
col_w = [0.135, 0.140, 0.260, 0.225, 0.240]
headers = ["Pool", "Pooled type", "Underlying container", "Double-return detection", "Typical use"]

n = len(rows)
row_h = 0.092
header_h = 0.052
top = 0.90
bottom = top - header_h - n * row_h - 0.14

fig, ax = plt.subplots(figsize=(16, 0.34 * n + 4.2), dpi=150)
ax.set_xlim(0, 1)
ax.set_ylim(bottom - 0.04, 1.0)
ax.axis("off")

# Title
ax.text(0.5, 0.975,
        "Five-Pool System: One Idea, Five Shapes  (storage / detection / reset / anti-bloat differ by object)",
        ha="center", va="top", fontsize=15, fontweight="bold", color="#212121")
ax.text(0.5, 0.937,
        "Different objects need different containers and double-return guards - no single universal pool fits all",
        ha="center", va="top", fontsize=10.5, color="#546E7A", fontstyle="italic")

# Header row
y = top - header_h
for i, htext in enumerate(headers):
    ax.add_patch(Rectangle((col_x[i], y), col_w[i], header_h,
                           facecolor="#263238", edgecolor="white", linewidth=1.2))
    ax.text(col_x[i] + col_w[i] / 2, y + header_h / 2, htext,
            ha="center", va="center", fontsize=10.5, fontweight="bold", color="white")

# Data rows
y -= row_h
for r_idx, r in enumerate(rows):
    fc, ec = POOL_COLORS[r_idx]
    pool, typ, cont, det, use = r
    vals = [pool, typ, cont, det, use]
    for i, v in enumerate(vals):
        ax.add_patch(Rectangle((col_x[i], y), col_w[i], row_h,
                               facecolor=fc, edgecolor=ec, linewidth=0.9))
        ha = "left" if i >= 2 else "center"
        padx = 0.010 if ha == "left" else 0
        if i == 0:
            fw = "bold"; fs = 10.5; col = ec
        elif i == 1:
            fw = "normal"; fs = 9.3; col = "#212121"
        else:
            fw = "normal"; fs = 8.8; col = "#212121"
        tx = (col_x[i] + padx + (col_w[i] - 2 * padx) / 2) if ha == "center" else (col_x[i] + padx)
        ax.text(tx, y + row_h / 2, v,
                ha=ha, va="center", fontsize=fs, fontweight=fw, color=col, linespacing=1.4)
    y -= row_h

# Detection-mechanism legend strip
leg_y = y - 0.025
ax.add_patch(FancyBboxPatch((col_x[0], leg_y - 0.055), sum(col_w), 0.055,
                            boxstyle="round,pad=0.006,rounding_size=0.008",
                            facecolor="#FFF8E1", edgecolor="#F57F17", linewidth=1.0))
ax.text(col_x[0] + 0.012, leg_y - 0.008,
        "Two detection strategies, chosen by \"can we add a field to this type?\":",
        ha="left", va="top", fontsize=9.6, fontweight="bold", color="#5D4037")
ax.text(col_x[0] + 0.012, leg_y - 0.028,
        "(1) Object we own (BitWriter / BitReader)  ->  add internal bool _pooledInUse, one memory access.   "
        "(2) Foreign type (byte[], generic T)  ->  external ConditionalWeakTable<TKey, LeaseMarker> weak-ref map, "
        "DEBUG-only.",
        ha="left", va="top", fontsize=8.8, color="#5D4037", linespacing=1.4)

# Bottom: four dimensions note
note_y = leg_y - 0.10
ax.add_patch(FancyBboxPatch((col_x[0], note_y - 0.075), sum(col_w), 0.075,
                            boxstyle="round,pad=0.006,rounding_size=0.008",
                            facecolor="#ECEFF1", edgecolor="#37474F", linewidth=1.2))
ax.text(col_x[0] + 0.012, note_y - 0.010,
        "Why NOT one universal pool - four dimensions vary across object kinds:",
        ha="left", va="top", fontsize=9.8, fontweight="bold", color="#263238")
ax.text(col_x[0] + 0.012, note_y - 0.030,
        "1. Container:  Stack+lock (single-thread, LIFO)  vs  ConcurrentBag (concurrent)  vs  ArrayPool (BCL, array-tuned)  vs  Dictionary+Bag (bucketed).",
        ha="left", va="top", fontsize=8.8, color="#37474F")
ax.text(col_x[0] + 0.012, note_y - 0.048,
        "2. Detection:  ConditionalWeakTable (can't add field)  vs  internal bool (can add field).     "
        "3. Reset:  reset delegate / Reset() / SetBuffer / Array.Clear.     "
        "4. Anti-bloat:  _maxSize / capacity-cap Dispose / per-bucket cap.",
        ha="left", va="top", fontsize=8.8, color="#37474F")

plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.16)
print("saved:", OUT)
