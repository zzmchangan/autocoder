"""fig-06-01: SafeECS ComponentPool three arrays + order-preserving delete,
contrasted with UnsafeECS Sparse Set swap-and-pop."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle
from matplotlib.lines import Line2D

fig, (ax_safe, ax_unsafe) = plt.subplots(1, 2, figsize=(15, 9))

# ============ Common helpers ============
def draw_cell(ax, x, y, w, h, text, facecolor, edgecolor="black", textcolor="black",
              fontsize=11, fontweight="normal", lw=1.2, linestyle="-"):
    rect = Rectangle((x, y), w, h, facecolor=facecolor, edgecolor=edgecolor,
                     linewidth=lw, linestyle=linestyle)
    ax.add_patch(rect)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fontsize, fontweight=fontweight, color=textcolor)

def draw_label(ax, x, y, text, fontsize=11, fontweight="bold", color="#1a1a1a"):
    ax.text(x, y, text, ha="left", va="center", fontsize=fontsize,
            fontweight=fontweight, color=color, family="monospace")

def draw_title(ax, text, y, color="#0b3d91"):
    ax.text(0.5, y, text, ha="center", va="center", fontsize=14,
            fontweight="bold", color=color, transform=ax.transAxes)

# ============ LEFT: SafeECS ============
ax_safe.set_xlim(0, 10)
ax_safe.set_ylim(0, 10)
ax_safe.axis("off")
draw_title(ax_safe, "SafeECS  ComponentPool<T>   (order-preserving)", 9.5)

# State: entities 1,2,3,4 active, removing entity 2
# _components[entityId]
draw_label(ax_safe, 0.3, 8.5, "_components[entityId]", fontsize=11)
# header row: index 0..4
for i in range(5):
    draw_cell(ax_safe, 1.3 + i * 1.0, 8.7, 1.0, 0.4, f"[{i}]",
              "#e8e8e8", fontsize=10, fontweight="bold")
# data row
data = ["-", "P1", "P2", "P3", "P4"]
colors_data = ["#f5f5f5", "#cfe8d4", "#fde2b0", "#cfe8d4", "#cfe8d4"]
for i, (d, c) in enumerate(zip(data, colors_data)):
    draw_cell(ax_safe, 1.3 + i * 1.0, 8.3, 1.0, 0.4, d, c, fontsize=10)

# _active[]
draw_label(ax_safe, 0.3, 7.5, "_active[entityId]", fontsize=11)
for i in range(5):
    draw_cell(ax_safe, 1.3 + i * 1.0, 7.7, 1.0, 0.4, f"[{i}]",
              "#e8e8e8", fontsize=10, fontweight="bold")
act = ["F", "T", "F", "T", "T"]
act_colors = ["#f5f5f5", "#cfe8d4", "#f5cccc", "#cfe8d4", "#cfe8d4"]
for i, (a, c) in enumerate(zip(act, act_colors)):
    draw_cell(ax_safe, 1.3 + i * 1.0, 7.3, 1.0, 0.4, a, c, fontsize=10)

# _activeEntities (ordered list)
draw_label(ax_safe, 0.3, 6.5, "_activeEntities", fontsize=11)
draw_label(ax_safe, 0.3, 6.2, "(ascending by", fontsize=8.5, color="#666")
draw_label(ax_safe, 0.3, 5.95, " entityId)", fontsize=8.5, color="#666")
ae = ["1", "3", "4"]
for i, v in enumerate(ae):
    draw_cell(ax_safe, 1.3 + i * 0.9, 6.3, 0.9, 0.4, v, "#cfe8d4", fontsize=10)
ax_safe.annotate("", xy=(1.3, 6.1), xytext=(1.3, 7.3),
                 arrowprops=dict(arrowstyle="->", color="#2a7a2a", lw=1.3))
ax_safe.annotate("", xy=(2.2, 6.1), xytext=(3.3, 7.3),
                 arrowprops=dict(arrowstyle="->", color="#2a7a2a", lw=1.3))
ax_safe.annotate("", xy=(3.1, 6.1), xytext=(4.3, 7.3),
                 arrowprops=dict(arrowstyle="->", color="#2a7a2a", lw=1.3))

# Remove(2) steps box
draw_title(ax_safe, "Remove(entityId=2)", 5.4, color="#a33a00")
steps = [
    "1. _active[2] = false          (flag, no data move)",
    "2. _components[2].Reset()      (clear, prevent stale read)",
    "3. IsDirty = true              (serialize cache invalidated)",
    "4. _activeEntities.Remove(2)   (BinarySearch O(log n) + RemoveAt O(n))",
    "   -> [1,2,3,4]  becomes  [1,3,4]   still ASCENDING",
]
for j, s in enumerate(steps):
    ax_safe.text(0.5, 4.8 - j * 0.4, s, ha="left", va="center",
                 fontsize=9.5, family="monospace", color="#1a1a1a")

# Result banner
banner = Rectangle((0.5, 2.3), 8.8, 0.8, facecolor="#2a7a2a", edgecolor="#1a5a1a")
ax_safe.add_patch(banner)
ax_safe.text(4.9, 2.7, "Traversal order = ascending entityId\nINDEPENDENT of deletion path  ->  ROLLBACK SAFE",
             ha="center", va="center", fontsize=10, fontweight="bold", color="white")

# Tradeoff note
ax_safe.text(0.5, 1.6, "Tradeoff:  Remove = O(n)  (List.RemoveAt shifts)",
             ha="left", va="center", fontsize=9.5, color="#a33a00",
             fontweight="bold", family="monospace")
ax_safe.text(0.5, 1.2, "Memory:    holes exist (sparse entityId inflates array)",
             ha="left", va="center", fontsize=9.5, color="#a33a00",
             family="monospace")
ax_safe.text(0.5, 0.7, "WIN: order depends only on 'who is alive'",
             ha="left", va="center", fontsize=10, fontweight="bold",
             color="#0b3d91", family="monospace")

# ============ RIGHT: UnsafeECS ============
ax_unsafe.set_xlim(0, 10)
ax_unsafe.set_ylim(0, 10)
ax_unsafe.axis("off")
draw_title(ax_unsafe, "UnsafeECS  UnsafeComponentPool<T>   (swap-and-pop)", 9.5, color="#a33a00")

# State before: _dense=[1,2,3,4], _count=4 ; removing 2
draw_label(ax_unsafe, 0.3, 8.5, "_dense[denseIdx]", fontsize=11)
for i in range(4):
    draw_cell(ax_unsafe, 1.3 + i * 1.0, 8.7, 1.0, 0.4, f"[{i}]",
              "#e8e8e8", fontsize=10, fontweight="bold")
dense = ["1", "2", "3", "4"]
for i, d in enumerate(dense):
    draw_cell(ax_unsafe, 1.3 + i * 1.0, 8.3, 1.0, 0.4, d, "#cfe8d4", fontsize=10)

draw_label(ax_unsafe, 0.3, 7.5, "_components[denseIdx]", fontsize=11)
for i in range(4):
    draw_cell(ax_unsafe, 1.3 + i * 1.0, 7.7, 1.0, 0.4, f"[{i}]",
              "#e8e8e8", fontsize=10, fontweight="bold")
comp = ["P1", "P2", "P3", "P4"]
for i, d in enumerate(comp):
    draw_cell(ax_unsafe, 1.3 + i * 1.0, 7.3, 1.0, 0.4, d, "#cfe8d4", fontsize=10)

draw_label(ax_unsafe, 0.3, 6.5, "_sparse[entityId]", fontsize=11)
draw_label(ax_unsafe, 0.3, 6.2, "(entityId -> dense)", fontsize=8.5, color="#666")
for i in range(4):
    draw_cell(ax_unsafe, 1.3 + i * 1.0, 6.7, 1.0, 0.35, f"[{i+1}]",
              "#e8e8e8", fontsize=9, fontweight="bold")
sp = ["0", "1", "2", "3"]
for i, d in enumerate(sp):
    draw_cell(ax_unsafe, 1.3 + i * 1.0, 6.35, 1.0, 0.35, d, "#fde2b0", fontsize=9)

draw_title(ax_unsafe, "Remove(entityId=2)  ->  swap-and-pop", 5.4, color="#a33a00")
usteps = [
    "1. denseIdx = _sparse[2] = 1",
    "2. lastIdx  = --_count   = 3",
    "3. _dense[1]     = _dense[3]     = 4    // LAST moves to hole",
    "4. _components[1] = _components[3] = P4",
    "5. _sparse[4] = 1 ; _sparse[2] = -1",
    "   O(1)  -- only two slots touched",
]
for j, s in enumerate(usteps):
    ax_unsafe.text(0.5, 4.9 - j * 0.38, s, ha="left", va="center",
                   fontsize=9.5, family="monospace", color="#1a1a1a")

# After state
draw_label(ax_unsafe, 0.3, 2.55, "_dense after:", fontsize=10)
after_dense = ["1", "4", "3", "?"]
after_colors = ["#cfe8d4", "#f5b8b8", "#cfe8d4", "#eeeeee"]
for i, (d, c) in enumerate(zip(after_dense, after_colors)):
    draw_cell(ax_unsafe, 2.3 + i * 0.9, 2.35, 0.9, 0.4, d, c, fontsize=10)
ax_unsafe.text(2.3, 1.95, "index 1 = 4  (was 2)  ->  ORDER SHUFFLED",
               ha="left", va="center", fontsize=9, color="#a33a00",
               fontweight="bold", family="monospace")

banner2 = Rectangle((0.5, 1.2), 8.8, 0.6, facecolor="#a33a00", edgecolor="#7a2a00")
ax_unsafe.add_patch(banner2)
ax_unsafe.text(4.9, 1.5, "Traversal order now  [1, 4, 3]  -- depends on DELETE PATH",
               ha="center", va="center", fontsize=10, fontweight="bold", color="white")

ax_unsafe.text(0.5, 0.6, "WIN:  Remove O(1)  +  raw-memcpy serialize (200x faster)",
               ha="left", va="center", fontsize=9.5, color="#0b3d91",
               fontweight="bold", family="monospace")

plt.tight_layout()
out = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-06-01-safecs-three-arrays.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
print("saved:", out)
