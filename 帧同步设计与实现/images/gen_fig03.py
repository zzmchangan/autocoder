import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

# Colors
C_KERNEL  = '#1F6FB2'   # blue - determinism kernel
C_SYNC    = '#C0563E'   # orange/terracotta - sync mechanism
C_BRIDGE  = '#2E8B57'   # green - predict/rollback bridge
C_PAPER   = '#F7F9FC'
C_EDGE    = '#33415C'
C_TXT     = '#1B2733'

fig, ax = plt.subplots(figsize=(13.5, 8.2))
ax.set_xlim(0, 13.5)
ax.set_ylim(0, 8.2)
ax.axis('off')
fig.patch.set_facecolor('white')

def panel(x, y, w, h, fc, ec, title, title_color='white'):
    # title strip
    th = 0.55
    ax.add_patch(FancyBboxPatch((x, y+h-th), w, th,
                 boxstyle="round,pad=0.02,rounding_size=0.08",
                 linewidth=0, facecolor=ec, alpha=1.0))
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                 boxstyle="round,pad=0.02,rounding_size=0.08",
                 linewidth=1.4, edgecolor=ec, facecolor=fc, alpha=1.0))
    # redraw title strip on top to keep round top only
    ax.add_patch(FancyBboxPatch((x, y+h-th), w, th,
                 boxstyle="round,pad=0.02,rounding_size=0.08",
                 linewidth=0, facecolor=ec))
    ax.text(x+w/2, y+h-th/2, title, ha='center', va='center',
            fontsize=11.5, weight='bold', color=title_color)

def chip(cx, cy, text, color, w=1.7, h=0.42, fs=8.5, tc='white', weight='bold'):
    ax.add_patch(FancyBboxPatch((cx-w/2, cy-h/2), w, h,
                 boxstyle="round,pad=0.02,rounding_size=0.10",
                 linewidth=0.6, edgecolor=color, facecolor=color, alpha=0.92))
    ax.text(cx, cy, text, ha='center', va='center', fontsize=fs, color=tc, weight=weight)

# ===== Title =====
ax.text(6.75, 7.85, 'The Book in One Diagram: Determinism Kernel  vs  Sync Mechanism',
        ha='center', va='center', fontsize=13.5, weight='bold', color=C_EDGE)
ax.text(6.75, 7.5,
        'same initial state  +  same input sequence  +  deterministic logic  ==>  identical state',
        ha='center', va='center', fontsize=10.5, style='italic', color='#5A6B7B')

# ===== Left column: Determinism Kernel =====
lx, lw = 0.4, 5.2
panel(lx, 1.4, lw, 5.6, C_PAPER, C_KERNEL,
      "Determinism Kernel  (works on ONE machine)")

ax.text(lx+lw/2, 6.35,
        '"Even offline, re-running the same input must match bit-for-bit"',
        ha='center', va='center', fontsize=8.8, style='italic', color=C_KERNEL)

# Part label
ax.text(lx+0.25, 5.95, "Part 1-2  (Ch.2-7)", ha='left', va='center',
        fontsize=9, weight='bold', color=C_KERNEL)
kchips = [
    ("Ch.2  LFloat fixed-point", 5.55),
    ("Ch.3  Deterministic math lib", 5.10),
    ("Ch.4  LRandom (Xorshift128+)", 4.65),
    ("Ch.5  Ordered ECS + World", 4.20),
    ("Ch.6  Rollback-safe component pool", 3.75),
    ("Ch.7  Byte-level serialization", 3.30),
]
for t, cy in kchips:
    chip(lx+lw/2, cy, t, C_KERNEL, w=4.6, h=0.40, fs=9.0)
ax.text(lx+lw/2, 2.55, "Build a machine that is\nbit-deterministic by itself",
        ha='center', va='center', fontsize=9.5, weight='bold', color=C_KERNEL)

# ===== Right column: Sync Mechanism =====
rx, rw = 7.9, 5.2
panel(rx, 1.4, rw, 5.6, C_PAPER, C_SYNC,
      "Sync Mechanism  (needs MANY machines)")

ax.text(rx+rw/2, 6.35,
        '"Hook the deterministic machine onto an unreliable network"',
        ha='center', va='center', fontsize=8.8, style='italic', color=C_SYNC)

ax.text(rx+0.25, 5.95, "Part 4  (Ch.12-17)", ha='left', va='center',
        fontsize=9, weight='bold', color=C_SYNC)
schips = [
    ("Ch.12  LockstepDriver (main loop)", 5.55),
    ("Ch.13  NetworkClock (Jacobson RTT)", 5.10),
    ("Ch.14  Relay vs Authoritative server", 4.65),
    ("Ch.15  GameRoom / multi-room / hash", 4.20),
    ("Ch.16  Protocol + anti-cheat", 3.75),
    ("Ch.17  Transport (TCP/UDP/WS/KCP)", 3.30),
]
for t, cy in schips:
    chip(rx+rw/2, cy, t, C_SYNC, w=4.6, h=0.40, fs=9.0)
ax.text(rx+rw/2, 2.55, "Make all clients eventually agree,\n"
                       "despite latency / jitter / loss",
        ha='center', va='center', fontsize=9.5, weight='bold', color=C_SYNC)

# ===== Middle: the bridge =====
bx, bw = 5.7, 2.1
panel(bx, 1.4, bw, 5.6, '#F1F8F4', C_BRIDGE,
      "The Bridge")
ax.text(bx+bw/2, 6.35, "Part 3\n(Ch.8-11)", ha='center', va='center',
        fontsize=9, weight='bold', color=C_BRIDGE)
bridge = [
    ("Ch.8  Predict", 5.55),
    ("Ch.9  Rollback", 5.05),
    ("Ch.10 Controller", 4.55),
    ("Ch.11 Render", 4.05),
]
for t, cy in bridge:
    chip(bx+bw/2, cy, t, C_BRIDGE, w=1.75, h=0.38, fs=8.6)
ax.text(bx+bw/2, 3.35, "prediction +\nrollback +\nsmoothing",
        ha='center', va='center', fontsize=8.8, weight='bold', color=C_BRIDGE)
ax.text(bx+bw/2, 2.55,
        "Turn it into a\nrewindable machine\n& draw it smoothly",
        ha='center', va='center', fontsize=8.8, weight='bold', color=C_BRIDGE)

# ===== arrows: kernel -> bridge -> sync =====
ax.annotate('', xy=(bx, 4.2), xytext=(lx+lw, 4.2),
            arrowprops=dict(arrowstyle='-|>', color=C_EDGE, lw=2.2,
                            mutation_scale=22))
ax.annotate('', xy=(rx, 4.2), xytext=(bx+bw, 4.2),
            arrowprops=dict(arrowstyle='-|>', color=C_EDGE, lw=2.2,
                            mutation_scale=22))

# ===== Bottom row: engineering + debugging + capstone =====
def bottom_chip(x, y, w, h, label, sub, color):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                 boxstyle="round,pad=0.02,rounding_size=0.08",
                 linewidth=1.3, edgecolor=color, facecolor='white'))
    ax.text(x+w/2, y+h-0.22, label, ha='center', va='center',
            fontsize=9.5, weight='bold', color=color)
    ax.text(x+w/2, y+0.22, sub, ha='center', va='center',
            fontsize=8.2, color=C_TXT)

by = 0.25
bottom_chip(0.4,  by, 4.1, 0.95, "Engineering (Part 5, Ch.18-22)",
            "SDK / reconnect / zero-GC / replay / perf", '#7E57C2')
bottom_chip(4.7,  by, 4.1, 0.95, "Determinism Debugging (Part 6, Ch.23-25)  *",
            "hash dual-track / red-line list / bug hunt", '#D08700')
bottom_chip(9.0,  by, 4.1, 0.95, "Practice (Part 7, Ch.26-27)",
            "collision / pathfinding / TankGame", '#455A64')

plt.tight_layout()
plt.savefig(r'c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-03-book-structure.png',
            dpi=150, bbox_inches='tight', facecolor='white')
print("saved fig-03")
