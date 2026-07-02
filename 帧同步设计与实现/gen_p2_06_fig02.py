"""fig-06-02: How swap-and-pop breaks rollback - dual-swimlane timeline.
Client A (predicted) deletes entity 2 at frame 10 -> _dense order shuffled ->
random consumption misaligned vs Client B (no delete) -> HashDrift.
Bottom: SafeECS contrast - _activeEntities stays ascending on both lanes."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch

fig, ax = plt.subplots(figsize=(16, 11))
ax.set_xlim(0, 16)
ax.set_ylim(0, 11)
ax.axis("off")

# ============ Title ============
ax.text(8, 10.55, "How swap-and-pop Breaks Rollback  (UnsafeECS, _dense[] order matters)",
        ha="center", va="center", fontsize=15, fontweight="bold", color="#1a1a1a")
ax.text(8, 10.15, "Traversal order = _dense[] order  =  random-number consumption order",
        ha="center", va="center", fontsize=11, color="#666", style="italic")

# ============ Frame axis header ============
frames = [9, 10, 11, 12, 15]
x_start = 3.2
col_w = 2.4
y_top = 9.5

# frame column headers
for i, f in enumerate(frames):
    x = x_start + i * col_w
    ax.text(x + col_w/2, y_top, f"frame {f}", ha="center", va="center",
            fontsize=12, fontweight="bold", color="#0b3d91")
    # connecting tick
    ax.plot([x + col_w/2, x + col_w/2], [y_top - 0.15, y_top - 0.3],
            color="#0b3d91", lw=1.5)

# ============ Swimlane A: Client A (predicted) ============
laneA_y = 8.3
ax.text(0.2, laneA_y + 0.6, "Client A", ha="left", va="center",
        fontsize=12, fontweight="bold", color="#a33a00")
ax.text(0.2, laneA_y + 0.2, "(predicted", ha="left", va="center",
        fontsize=9, color="#a33a00")
ax.text(0.2, laneA_y - 0.05, "  path)", ha="left", va="center",
        fontsize=9, color="#a33a00")
# lane background
ax.add_patch(Rectangle((3.0, laneA_y - 0.5), 5*col_w + 0.4, 1.6,
                       facecolor="#fff7f2", edgecolor="#e8c4b0", lw=1))

# Frame-by-frame _dense for client A
# frame 9: [1,2,3,4]
# frame 10: A predicts kill entity 2 -> swap-pop -> [1,4,3]
# frame 11: [1,4,3]
# frame 12: (rollback will rewrite this)
# frame 15: after rollback replay
denseA = {
    9:  ("[1,2,3,4]", ["r0->e1","r1->e2","r2->e3","r3->e4"], False, "#cfe8d4"),
    10: ("[1,4,3]",   ["r0->e1","r1->e4","r2->e3"],         True,  "#f5b8b8"),
    11: ("[1,4,3]",   ["r0->e1","r1->e4","r2->e3"],         False, "#cfe8d4"),
    12: ("[1,4,3]",   ["r0->e1","r1->e4","r2->e3"],         False, "#cfe8d4"),
    15: ("[1,4,3]\n(replay)", ["r0->e1","r1->e4","r2->e3"], True,  "#f5d9b0"),
}
for i, f in enumerate(frames):
    x = x_start + i * col_w
    d, rnd, hi, col = denseA[f]
    cell = Rectangle((x, laneA_y - 0.35), col_w - 0.2, 0.95,
                     facecolor=col, edgecolor="#a33a00" if hi else "#888", lw=1.6 if hi else 1)
    ax.add_patch(cell)
    ax.text(x + (col_w-0.2)/2, laneA_y + 0.35, f"_dense={d}",
            ha="center", va="center", fontsize=9.5, fontweight="bold",
            family="monospace", color="#1a1a1a")
    ax.text(x + (col_w-0.2)/2, laneA_y - 0.05, "\n".join(rnd),
            ha="center", va="center", fontsize=7.2, family="monospace", color="#444")

# annotation on frame 10 for client A
ax.annotate("A predicts kill e2\nswap-pop shuffles _dense",
            xy=(x_start + 1*col_w + (col_w-0.2)/2, laneA_y + 0.7),
            xytext=(x_start + 1*col_w + (col_w-0.2)/2, laneA_y + 1.5),
            ha="center", fontsize=9, color="#a33a00", fontweight="bold",
            arrowprops=dict(arrowstyle="->", color="#a33a00", lw=1.4))

# ============ Swimlane B: Client B (authoritative) ============
laneB_y = 6.2
ax.text(0.2, laneB_y + 0.6, "Client B", ha="left", va="center",
        fontsize=12, fontweight="bold", color="#0b3d91")
ax.text(0.2, laneB_y + 0.2, "(authoritative", ha="left", va="center",
        fontsize=9, color="#0b3d91")
ax.text(0.2, laneB_y - 0.05, "  path)", ha="left", va="center",
        fontsize=9, color="#0b3d91")
ax.add_patch(Rectangle((3.0, laneB_y - 0.5), 5*col_w + 0.4, 1.6,
                       facecolor="#f2f7ff", edgecolor="#b0c4e8", lw=1))

denseB = {
    9:  ("[1,2,3,4]", ["r0->e1","r1->e2","r2->e3","r3->e4"], False, "#cfe8d4"),
    10: ("[1,2,3,4]", ["r0->e1","r1->e2","r2->e3","r3->e4"], True,  "#b8d8f5"),
    11: ("[1,2,3,4]", ["r0->e1","r1->e2","r2->e3","r3->e4"], False, "#cfe8d4"),
    12: ("[1,4,3]",   ["r0->e1","r1->e4","r2->e3"],         True,  "#b8d8f5"),
    15: ("[1,4,3]",   ["r0->e1","r1->e4","r2->e3"],         False, "#cfe8d4"),
}
for i, f in enumerate(frames):
    x = x_start + i * col_w
    d, rnd, hi, col = denseB[f]
    cell = Rectangle((x, laneB_y - 0.35), col_w - 0.2, 0.95,
                     facecolor=col, edgecolor="#0b3d91" if hi else "#888", lw=1.6 if hi else 1)
    ax.add_patch(cell)
    ax.text(x + (col_w-0.2)/2, laneB_y + 0.35, f"_dense={d}",
            ha="center", va="center", fontsize=9.5, fontweight="bold",
            family="monospace", color="#1a1a1a")
    ax.text(x + (col_w-0.2)/2, laneB_y - 0.05, "\n".join(rnd),
            ha="center", va="center", fontsize=7.2, family="monospace", color="#444")

ax.annotate("B has different input:\nno delete at f10 -> _dense intact",
            xy=(x_start + 1*col_w + (col_w-0.2)/2, laneB_y - 0.35),
            xytext=(x_start + 1*col_w + (col_w-0.2)/2, laneB_y - 1.2),
            ha="center", fontsize=9, color="#0b3d91", fontweight="bold",
            arrowprops=dict(arrowstyle="->", color="#0b3d91", lw=1.4))

# ============ Divergence callout between lanes at frame 10 ============
div_x = x_start + 1*col_w + (col_w-0.2)/2
ax.add_patch(FancyBboxPatch((div_x - 1.5, 7.05), 3.0, 0.5,
                            boxstyle="round,pad=0.1", facecolor="#ffe0e0",
                            edgecolor="#cc0000", lw=1.5))
ax.text(div_x, 7.3, "r1 -> e4  on A   vs   r1 -> e2  on B     DIVERGE!",
        ha="center", va="center", fontsize=9, fontweight="bold",
        color="#cc0000", family="monospace")

# ============ Rollback arrow (frame 15 -> frame 9 on lane A) ============
rb_y = 4.6
arrow = FancyArrowPatch((x_start + 4*col_w + (col_w-0.2)/2, rb_y + 0.3),
                         (x_start + 0*col_w + (col_w-0.2)/2, rb_y + 0.3),
                         connectionstyle="arc3,rad=-0.25",
                         arrowstyle="-|>", mutation_scale=22,
                         color="#7a2a00", lw=2.2, linestyle="--")
ax.add_patch(arrow)
ax.text(x_start + 2*col_w + (col_w-0.2)/2, rb_y + 0.15,
        "frame 15: authoritative input arrives  ->  ROLLBACK to frame 9 snapshot, replay with auth input",
        ha="center", va="center", fontsize=10, fontweight="bold",
        color="#7a2a00", style="italic")

# ============ HashDrift conclusion box ============
ax.add_patch(FancyBboxPatch((0.5, 3.0), 15.0, 1.2,
                            boxstyle="round,pad=0.15", facecolor="#fff0f0",
                            edgecolor="#cc0000", lw=2))
ax.text(0.8, 3.85, "HashDrift:", ha="left", va="center",
        fontsize=12, fontweight="bold", color="#cc0000")
ax.text(0.8, 3.4,
        "Client A already SENT its predicted frame-10 hash (based on _dense=[1,4,3] + misaligned randoms).\n"
        "Server's authoritative frame-10 hash is based on _dense=[1,2,3,4].  They NEVER match  ->  desync detected.",
        ha="left", va="center", fontsize=10, color="#1a1a1a", family="monospace")

# ============ Bottom: SafeECS contrast ============
ax.text(8, 2.5, "Same scenario with SafeECS  --  _activeEntities is ALWAYS ascending, both lanes",
        ha="center", va="center", fontsize=12, fontweight="bold", color="#2a7a2a")
ax.add_patch(FancyBboxPatch((0.5, 1.0), 15.0, 1.3,
                            boxstyle="round,pad=0.15", facecolor="#f0f8f0",
                            edgecolor="#2a7a2a", lw=1.8))

ax.text(0.9, 1.95, "Client A  _activeEntities:", ha="left", va="center",
        fontsize=10, fontweight="bold", color="#a33a00", family="monospace")
ax.text(4.2, 1.95, "[1,2,3,4]  --(f10 kill e2)-->  [1,3,4]  --(rollback replay)-->  [1,3,4]",
        ha="left", va="center", fontsize=10, color="#1a1a1a", family="monospace")

ax.text(0.9, 1.55, "Client B  _activeEntities:", ha="left", va="center",
        fontsize=10, fontweight="bold", color="#0b3d91", family="monospace")
ax.text(4.2, 1.55, "[1,2,3,4]  --(no kill at f10)-->  [1,2,3,4]  --(kill e2 at f12)-->  [1,3,4]",
        ha="left", va="center", fontsize=10, color="#1a1a1a", family="monospace")

ax.text(0.9, 1.2, "Result:", ha="left", va="center",
        fontsize=10, fontweight="bold", color="#2a7a2a", family="monospace")
ax.text(1.9, 1.2, "traversal order depends ONLY on 'who is alive'  ->  both clients agree at every frame  ->  NO drift",
        ha="left", va="center", fontsize=10, fontweight="bold", color="#2a7a2a", family="monospace")

# footnote
ax.text(8, 0.5, "r0,r1,r2,... = consecutive draws from the shared LRandom ; e1..e4 = entity ids",
        ha="center", va="center", fontsize=9, color="#888", style="italic")

plt.tight_layout()
out = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-06-02-swap-pop-breaks-rollback.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
print("saved:", out)
