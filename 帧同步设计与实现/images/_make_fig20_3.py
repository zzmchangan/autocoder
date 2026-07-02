"""fig-20-03-double-return.png
Double-return -> silent data corruption.
Left: timeline of how the bug unfolds (Return x2 -> pool holds 2 refs -> two Rent
return the SAME instance -> two callers write the same buffer concurrently).
Right: why it's the most insidious desync source (no error, no crash, timing-dependent)
+ how BufferPool's DEBUG ConditionalWeakTable catches it as a thrown exception.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle, FancyArrowPatch

OUT = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-20-03-double-return.png"

fig, ax = plt.subplots(figsize=(16, 10), dpi=150)
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.axis("off")

# Title
ax.text(0.5, 0.975,
        "Double-Return: The Most Insidious Silent-Corruption Source in Lockstep",
        ha="center", va="top", fontsize=15.5, fontweight="bold", color="#212121")
ax.text(0.5, 0.94,
        "Same byte[] returned twice -> pool hands the SAME instance to two callers -> concurrent writes corrupt data silently",
        ha="center", va="top", fontsize=10.5, color="#546E7A", fontstyle="italic")

# ---------- LEFT: timeline (pool state machine) ----------
LX, LW = 0.03, 0.62
ax.add_patch(FancyBboxPatch((LX, 0.10), LW, 0.80,
                            boxstyle="round,pad=0.008,rounding_size=0.010",
                            facecolor="#FAFAFA", edgecolor="#37474F", lw=1.6))
ax.text(LX + LW / 2, 0.875, "How the corruption unfolds  (pool state over time)",
        ha="center", va="top", fontsize=12.5, fontweight="bold", color="#263238")

# Pool box (center column) - represents the pool's contents
pool_cx = LX + LW * 0.50
pool_top = 0.80
pool_bot = 0.16
ax.plot([pool_cx, pool_cx], [pool_bot, pool_top], color="#90A4AE", lw=1.2, ls="--", zorder=1)

def pool_state(y, items, note, note_color="#37474F"):
    """Draw pool contents at vertical position y. items = list of labels or None for empty."""
    # pool container
    ax.add_patch(FancyBboxPatch((pool_cx - 0.075, y - 0.022), 0.150, 0.044,
                                boxstyle="round,pad=0.003,rounding_size=0.006",
                                facecolor="white", edgecolor="#37474F", lw=1.3, zorder=3))
    if items:
        ax.text(pool_cx, y, "  ".join(items), ha="center", va="center",
                fontsize=8.5, fontweight="bold", color="#263238", zorder=4)
    else:
        ax.text(pool_cx, y, "(empty)", ha="center", va="center",
                fontsize=8.5, color="#90A4AE", fontstyle="italic", zorder=4)
    # note label to the right of pool
    ax.text(pool_cx + 0.090, y, note, ha="left", va="center",
            fontsize=8.6, color=note_color, fontstyle="italic")

# Callers A / B / C lanes (left side, time axis)
lane_x = {
    "A": LX + 0.025,
    "pool_state_note": LX + 0.005,
}
B_x = LX + 0.030
C_x = LX + 0.030

# Vertical time axis on far left
ax.annotate("", xy=(LX + 0.015, 0.18), xytext=(LX + 0.015, 0.82),
            arrowprops=dict(arrowstyle="->", color="#263238", lw=1.6))
ax.text(LX + 0.010, 0.83, "time", ha="right", va="bottom",
        fontsize=9, color="#263238", fontstyle="italic", rotation=90)

# Step rows (top to bottom = t1..t6)
# tuple = (y, t_label, pool_items, pool_note, note_color, caller_action)
steps = [
    (0.76, "t1",
     ["buf#7 (out)"], "buf#7 leased to A", "#1565C0",
     "A:  Rent()  ->  buf#7"),
    (0.66, "t2",
     ["buf#7"], "buf#7 back in pool", "#2E7D32",
     "A:  Return(buf#7)   [normal]"),
    (0.56, "t3",
     ["buf#7", "buf#7"], "buf#7 in pool TWICE  <- INVARIANT BROKEN", "#B71C1C",
     "A (bug):  Return(buf#7) AGAIN  <-- DOUBLE"),
    (0.46, "t4",
     ["buf#7"], "buf#7 leased to B", "#1565C0",
     "B:  Rent()  ->  buf#7"),
    (0.36, "t5",
     [], "buf#7 leased to C TOO  <- same instance to 2 callers", "#B71C1C",
     "C:  Rent()  ->  buf#7   [SAME as B!]"),
    (0.26, "t6",
     [], "data silently corrupted  ->  desync", "#7F1818",
     "B + C:  concurrent write  ->  CORRUPTION"),
]

for y, t, items, note, ncol, caller_act in steps:
    # time label
    ax.text(LX + 0.030, y + 0.030, t, ha="left", va="center",
            fontsize=9.5, fontweight="bold", color="#263238")
    # caller lane + action (left) - color matches the step's accent
    ax.text(LX + 0.030, y, caller_act, ha="left", va="center",
            fontsize=8.8, fontweight="bold", color=ncol)
    # pool state
    pool_state(y, items if items else None, note, ncol)

# arrows from caller action to pool (visual flow), one per step near the action
# (omit heavy arrows to keep clean; the layout already implies flow)

# Disaster callout at t6
ax.add_patch(FancyBboxPatch((LX + 0.005, 0.135), LW - 0.010, 0.052,
                            boxstyle="round,pad=0.005,rounding_size=0.008",
                            facecolor="#FFEBEE", edgecolor="#B71C1C", lw=1.6))
ax.text(LX + 0.015, 0.161,
        "DISASTER at t6:  B and C each think they own a private buffer,",
        ha="left", va="center", fontsize=9.6, fontweight="bold", color="#B71C1C")
ax.text(LX + 0.015, 0.146,
        "but they share ONE byte[]. Writes interleave -> the serialized byte stream is a garbled mix of both. No exception, no crash.",
        ha="left", va="center", fontsize=9.0, color="#7F1818")

# ---------- RIGHT TOP: why it's the worst desync source ----------
RX, RW = 0.68, 0.30
ax.add_patch(FancyBboxPatch((RX, 0.49), RW, 0.41,
                            boxstyle="round,pad=0.008,rounding_size=0.010",
                            facecolor="#FFF3E0", edgecolor="#E65100", lw=1.6))
ax.text(RX + RW / 2, 0.875, "Why it's the worst\ndesync source",
        ha="center", va="top", fontsize=12, fontweight="bold", color="#BF360C", linespacing=1.3)

reasons = [
    ("No error", "No exception, no crash. Both callers\n\"successfully\" finish serializing -\njust with wrong bytes."),
    ("Not immediate", "Wrong byte[] this frame, but the error\nsurfaces only when deserialized + hashed\n- tens or hundreds of frames later."),
    ("Not reproducible", "Triggered by a specific Rent timing\n(two callers race). Local test runs it\n10k times, never hits it; ships, then\nit appears in production."),
    ("Hardest to localize", "Hash mismatch at frame ~5000 points\nnowhere near the t6 root cause.\nBreakpoints can't catch a ghost."),
]
ry = 0.83
for title, body in reasons:
    ax.text(RX + 0.012, ry, title, ha="left", va="top",
            fontsize=9.8, fontweight="bold", color="#BF360C")
    ax.text(RX + 0.012, ry - 0.022, body, ha="left", va="top",
            fontsize=8.4, color="#5D4037", linespacing=1.35)
    ry -= 0.086

# ---------- RIGHT BOTTOM: DEBUG catch mechanism ----------
ax.add_patch(FancyBboxPatch((RX, 0.10), RW, 0.36,
                            boxstyle="round,pad=0.008,rounding_size=0.010",
                            facecolor="#E3F2FD", edgecolor="#1565C0", lw=1.6))
ax.text(RX + RW / 2, 0.435, "DEBUG catch:\nConditionalWeakTable",
        ha="center", va="top", fontsize=11.5, fontweight="bold", color="#0D47A1", linespacing=1.3)

ax.text(RX + 0.012, 0.385,
        "Rent():  add  byte[] -> LeaseMarker",
        ha="left", va="top", fontsize=9.0, fontweight="bold", color="#0D47A1", family="monospace")
ax.text(RX + 0.012, 0.362,
        "        if key already present -> THROW",
        ha="left", va="top", fontsize=8.6, color="#1565C0", family="monospace")
ax.text(RX + 0.012, 0.330,
        "Return(): try Remove(byte[])",
        ha="left", va="top", fontsize=9.0, fontweight="bold", color="#0D47A1", family="monospace")
ax.text(RX + 0.012, 0.307,
        "        if remove FAILS (already gone)",
        ha="left", va="top", fontsize=8.6, color="#1565C0", family="monospace")
ax.text(RX + 0.012, 0.287,
        "             -> DOUBLE-RETURN -> THROW",
        ha="left", va="top", fontsize=8.6, fontweight="bold", color="#B71C1C", family="monospace")

ax.text(RX + 0.012, 0.250,
        "Weak-ref key: GC can still reclaim the",
        ha="left", va="top", fontsize=8.6, color="#37474F")
ax.text(RX + 0.012, 0.234,
        "byte[] when no one holds it - the table",
        ha="left", va="top", fontsize=8.6, color="#37474F")
ax.text(RX + 0.012, 0.218,
        "self-heals, never leaks memory.",
        ha="left", va="top", fontsize=8.6, color="#37474F")

# #if DEBUG note
ax.add_patch(FancyBboxPatch((RX + 0.010, 0.125), RW - 0.020, 0.068,
                            boxstyle="round,pad=0.004,rounding_size=0.006",
                            facecolor="#FFF8E1", edgecolor="#F57F17", lw=1.0))
ax.text(RX + RW / 2, 0.175,
        "#if DEBUG  ...  #endif",
        ha="center", va="center", fontsize=9.2, fontweight="bold", color="#E65100", family="monospace")
ax.text(RX + RW / 2, 0.148,
        "DEBUG: catches the ghost (throws).\nRelease: compiled out, ZERO overhead.",
        ha="center", va="center", fontsize=8.4, color="#5D4037", linespacing=1.4)

plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.18)
print("saved:", OUT)
