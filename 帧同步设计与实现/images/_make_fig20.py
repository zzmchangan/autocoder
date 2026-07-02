"""fig-20-01-gc-breaks-tempo.png
GC Stop-The-World breaks lockstep frame tempo.
Two parallel timelines: a normal client (steady 20Hz ticks) vs a client hit by
a Gen2 GC pause (logic clock frozen -> catch-up burst -> more garbage -> next GC).
Bottom contrast strip: ordinary program tolerates a GC hitch, lockstep does not.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle, FancyArrowPatch

OUT = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-20-01-gc-breaks-tempo.png"

fig, ax = plt.subplots(figsize=(15, 9.5), dpi=150)
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.axis("off")

# Title
ax.text(0.5, 0.975,
        "Why Lockstep Fears GC: Stop-The-World Breaks the 20Hz Tempo Alignment",
        ha="center", va="top", fontsize=15, fontweight="bold", color="#212121")
ax.text(0.5, 0.94,
        "A Gen2 pause freezes one client's logic clock; the global beat moves on -> catch-up burst -> more garbage -> next pause",
        ha="center", va="top", fontsize=10.5, color="#546E7A", fontstyle="italic")

# ---------- timeline geometry ----------
TL_LEFT = 0.06
TL_RIGHT = 0.94
TL_W = TL_RIGHT - TL_LEFT
# tick marks at 50ms cadence (frames F0..F8)
n_frames = 9
frame_xs = [TL_LEFT + TL_W * i / (n_frames - 1) for i in range(n_frames)]

def frame_label(i):
    return f"F{i}"

# ---------- TOP timeline: normal client ----------
norm_y = 0.80
ax.text(TL_LEFT - 0.005, norm_y + 0.055, "Normal client",
        ha="left", va="center", fontsize=11.5, fontweight="bold", color="#1B5E20")
ax.text(TL_LEFT - 0.005, norm_y + 0.028, "steady 20Hz, ~50ms / frame",
        ha="left", va="center", fontsize=9, color="#388E3C", fontstyle="italic")

# baseline
ax.plot([TL_LEFT, TL_RIGHT], [norm_y, norm_y], color="#2E7D32", lw=2.2, zorder=2)
# ticks + per-frame work blocks (small green squares centered on ticks)
for i, x in enumerate(frame_xs):
    ax.plot([x, x], [norm_y - 0.012, norm_y + 0.012], color="#2E7D32", lw=2.0)
    # work block (Tick executed)
    ax.add_patch(Rectangle((x - 0.012, norm_y + 0.014), 0.024, 0.030,
                           facecolor="#C8E6C9", edgecolor="#2E7D32", lw=1.0))
    ax.text(x, norm_y + 0.029, "Tick", ha="center", va="center",
            fontsize=7.5, color="#1B5E20", fontweight="bold")
    ax.text(x, norm_y - 0.030, frame_label(i), ha="center", va="top",
            fontsize=8.5, color="#1B5E20")
# right-side status
ax.text(TL_RIGHT + 0.005, norm_y + 0.020, "aligned with\nglobal beat",
        ha="left", va="center", fontsize=8.5, color="#1B5E20", fontstyle="italic", linespacing=1.3)

# global beat reference label (above normal timeline)
ax.text(TL_LEFT - 0.005, norm_y + 0.085, "Global logic beat (server authoritative, 20Hz):",
        ha="left", va="center", fontsize=9.5, color="#37474F")

# ---------- MIDDLE timeline: GC-hit client ----------
gc_y = 0.52
ax.text(TL_LEFT - 0.005, gc_y + 0.055, "GC-hit client",
        ha="left", va="center", fontsize=11.5, fontweight="bold", color="#B71C1C")
ax.text(TL_LEFT - 0.005, gc_y + 0.028, "Gen2 pause freezes logic clock -> catch-up spiral",
        ha="left", va="center", fontsize=9, color="#C62828", fontstyle="italic")

# baseline (split by GC gap)
ax.plot([TL_LEFT, TL_RIGHT], [gc_y, gc_y], color="#37474F", lw=2.2, zorder=2)

# Frames F0, F1 execute normally
for i in (0, 1):
    x = frame_xs[i]
    ax.plot([x, x], [gc_y - 0.012, gc_y + 0.012], color="#37474F", lw=2.0)
    ax.add_patch(Rectangle((x - 0.012, gc_y + 0.014), 0.024, 0.030,
                           facecolor="#C8E6C9", edgecolor="#2E7D32", lw=1.0))
    ax.text(x, gc_y + 0.029, "Tick", ha="center", va="center",
            fontsize=7.5, color="#1B5E20", fontweight="bold")
    ax.text(x, gc_y - 0.030, frame_label(i), ha="center", va="top",
            fontsize=8.5, color="#212121")

# GC pause band: from just after F1 to ~F4 (covers ~3 frames = 150ms Gen2 pause)
gc_start = frame_xs[1] + 0.022
gc_end   = frame_xs[4] - 0.018
ax.add_patch(Rectangle((gc_start, gc_y - 0.045), gc_end - gc_start, 0.105,
                       facecolor="#FFCDD2", edgecolor="#B71C1C", lw=1.6, hatch="////",
                       alpha=0.85, zorder=3))
ax.text((gc_start + gc_end) / 2, gc_y + 0.020, "Gen2 GC  (Stop-The-World, ~150ms)",
        ha="center", va="center", fontsize=11, fontweight="bold", color="#B71C1C", zorder=4)
ax.text((gc_start + gc_end) / 2, gc_y - 0.008, "logic clock FROZEN",
        ha="center", va="center", fontsize=9, color="#B71C1C", fontstyle="italic", zorder=4)
ax.text((gc_start + gc_end) / 2, gc_y - 0.030, "others advance ~3 frames",
        ha="center", va="center", fontsize=8.3, color="#7F1818", zorder=4)

# frame labels under GC band (F2,F3,F4 missed)
for i in (2, 3, 4):
    x = frame_xs[i]
    ax.text(x, gc_y - 0.058, frame_label(i), ha="center", va="top",
            fontsize=8.5, color="#B71C1C")
    ax.text(x, gc_y - 0.072, "missed", ha="center", va="top",
            fontsize=7, color="#C62828", fontstyle="italic")

# Catch-up burst: F5..F7 compressed into the remaining budget (clustered, orange)
burst_start = gc_end + 0.012
burst_xs = [burst_start + 0.022 * k for k in range(4)]
labels_burst = ["F2'", "F3'", "F4'", "F5"]
for k, x in enumerate(burst_xs):
    ax.plot([x, x], [gc_y - 0.012, gc_y + 0.012], color="#EF6C00", lw=2.0)
    ax.add_patch(Rectangle((x - 0.010, gc_y + 0.014), 0.020, 0.030,
                           facecolor="#FFE0B2", edgecolor="#EF6C00", lw=1.0))
    ax.text(x, gc_y + 0.029, "Tick", ha="center", va="center",
            fontsize=7.0, color="#BF360C", fontweight="bold")
    ax.text(x, gc_y - 0.030, labels_burst[k], ha="center", va="top",
            fontsize=8.0, color="#BF360C")
ax.text(sum(burst_xs) / len(burst_xs), gc_y - 0.058, "catch-up burst",
        ha="center", va="top", fontsize=9, color="#BF360C", fontweight="bold", fontstyle="italic")
ax.text(sum(burst_xs) / len(burst_xs), gc_y - 0.073, "CPU spike + more garbage",
        ha="center", va="top", fontsize=7.8, color="#7F1818", fontstyle="italic")

# loop-back arrow: more garbage -> next GC
ax.annotate("", xy=(frame_xs[6] + 0.045, gc_y + 0.085),
            xytext=(frame_xs[6] + 0.005, gc_y + 0.085),
            arrowprops=dict(arrowstyle="->", color="#B71C1C", lw=1.6,
                            connectionstyle="arc3,rad=0.35"))
ax.text(frame_xs[6] + 0.025, gc_y + 0.110, "next GC\ndrift",
        ha="center", va="bottom", fontsize=7.8, color="#B71C1C", fontstyle="italic", linespacing=1.2)

# right-side status
ax.text(TL_RIGHT + 0.005, gc_y + 0.020, "OUT OF STEP\n-> desync",
        ha="left", va="center", fontsize=8.5, color="#B71C1C", fontweight="bold", linespacing=1.3)

# ---------- vicious-cycle banner (between timelines) ----------
vc_y = 0.645
ax.add_patch(FancyBboxPatch((TL_LEFT, vc_y - 0.035), TL_W, 0.058,
                            boxstyle="round,pad=0.006,rounding_size=0.010",
                            facecolor="#FFF3E0", edgecolor="#E65100", lw=1.6))
ax.text(TL_LEFT + 0.015, vc_y - 0.006,
        "Vicious cycle:   GC pause  ->  logic clock falls behind  ->  catch-up burst simulates N frames at once  "
        "->  more garbage generated  ->  promotes objects to Gen2  ->  next Full GC fires sooner",
        ha="left", va="center", fontsize=9.6, color="#BF360C", fontweight="bold")
ax.text(TL_LEFT + 0.015, vc_y - 0.026,
        "Worst case: catch-up eats the whole frame budget (MaxSimulationMsPerFrame = 50ms) -> forced tick truncation -> frame sequence diverges -> desync",
        ha="left", va="center", fontsize=8.8, color="#7F1818", fontstyle="italic")

# ---------- BOTTOM contrast strip: ordinary program vs lockstep ----------
cs_top = 0.30
ax.add_patch(FancyBboxPatch((0.04, 0.045), 0.92, cs_top - 0.045,
                            boxstyle="round,pad=0.008,rounding_size=0.010",
                            facecolor="#F5F5F5", edgecolor="#455A64", lw=1.6))
ax.text(0.06, cs_top - 0.015, "Why the same GC pause is harmless elsewhere but lethal in lockstep:",
        ha="left", va="top", fontsize=11, fontweight="bold", color="#263238")

# left half: ordinary program
half_w = 0.42
lx = 0.06
ax.add_patch(FancyBboxPatch((lx, 0.065), half_w, 0.155,
                            boxstyle="round,pad=0.005,rounding_size=0.008",
                            facecolor="#E8F5E9", edgecolor="#2E7D32", lw=1.4))
ax.text(lx + half_w / 2, 0.205, "Ordinary program (single local clock)",
        ha="center", va="center", fontsize=10, fontweight="bold", color="#1B5E20")
ax.text(lx + 0.012, 0.180,
        "-  Web service: request takes ~300ms, a 20ms GC pause = 6%, unnoticed",
        ha="left", va="top", fontsize=8.6, color="#2E7D32")
ax.text(lx + 0.012, 0.158,
        "-  Single-player game: one frame dropped, next frame catches up,",
        ha="left", va="top", fontsize=8.6, color="#2E7D32")
ax.text(lx + 0.012, 0.140,
        "    state advances normally  (no peer to align with)",
        ha="left", va="top", fontsize=8.6, color="#2E7D32")
ax.text(lx + 0.012, 0.112,
        "=> GC is a PERFORMANCE issue, not a CORRECTNESS issue",
        ha="left", va="top", fontsize=9.2, color="#1B5E20", fontweight="bold", fontstyle="italic")

# right half: lockstep
rx = 0.52
ax.add_patch(FancyBboxPatch((rx, 0.065), half_w, 0.155,
                            boxstyle="round,pad=0.005,rounding_size=0.008",
                            facecolor="#FFEBEE", edgecolor="#B71C1C", lw=1.4))
ax.text(rx + half_w / 2, 0.205, "Lockstep (clients aligned to one global beat)",
        ha="center", va="center", fontsize=10, fontweight="bold", color="#B71C1C")
ax.text(rx + 0.012, 0.180,
        "-  One client frozen 80ms: peers advance ~1-2 frames, this one stalls",
        ha="left", va="top", fontsize=8.6, color="#C62828")
ax.text(rx + 0.012, 0.158,
        "-  Catch-up burst -> CPU spike -> more garbage -> next Gen2 sooner",
        ha="left", va="top", fontsize=8.6, color="#C62828")
ax.text(rx + 0.012, 0.136,
        "-  Pause can also mis-time the per-frame hash checkpoint",
        ha="left", va="top", fontsize=8.6, color="#C62828")
ax.text(rx + 0.012, 0.108,
        "=> GC is a CORRECTNESS issue  ->  desync. Core path must be zero-alloc.",
        ha="left", va="top", fontsize=9.2, color="#B71C1C", fontweight="bold", fontstyle="italic")

plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.18)
print("saved:", OUT)
