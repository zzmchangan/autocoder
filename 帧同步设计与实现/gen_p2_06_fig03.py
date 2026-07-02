"""fig-06-03: Serialize three-path decision flow.
EnableCaching? -> IsDirty? -> _cachedData!=null?
Path 1 Cache Hit (green) / Path 2 Direct Write (blue) / Path 3 Rebuild Cache (orange).
Right side: component-type to path mapping table."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Polygon

fig, ax = plt.subplots(figsize=(15, 10))
ax.set_xlim(0, 15)
ax.set_ylim(0, 10)
ax.axis("off")

# Title
ax.text(7.5, 9.6, "Serialize  Dirty-Flag Cache  --  Three Paths",
        ha="center", va="center", fontsize=15, fontweight="bold", color="#1a1a1a")
ax.text(7.5, 9.2, "Decouple snapshot frequency from component mutation frequency",
        ha="center", va="center", fontsize=11, color="#666", style="italic")

# State variables box (top-left)
ax.add_patch(FancyBboxPatch((0.3, 7.9), 4.0, 1.0, boxstyle="round,pad=0.12",
                            facecolor="#f5f5f5", edgecolor="#888", lw=1.2))
ax.text(0.5, 8.7, "Pool state:", ha="left", va="center", fontsize=10,
        fontweight="bold", color="#1a1a1a")
ax.text(0.5, 8.4, "IsDirty        (Set/Update/Remove/Clear set true)",
        ha="left", va="center", fontsize=9, family="monospace", color="#333")
ax.text(0.5, 8.1, "EnableCaching  (default true; [HighFrequencyComponent] -> false)",
        ha="left", va="center", fontsize=9, family="monospace", color="#333")

# ============ Decision diamond 1: EnableCaching? ============
def diamond(ax, cx, cy, w, h, text, facecolor="#fff8d6", edgecolor="#a07a00"):
    pts = [(cx, cy+h/2), (cx+w/2, cy), (cx, cy-h/2), (cx-w/2, cy)]
    ax.add_patch(Polygon(pts, closed=True, facecolor=facecolor,
                         edgecolor=edgecolor, lw=1.6))
    ax.text(cx, cy, text, ha="center", va="center", fontsize=10,
            fontweight="bold", color="#1a1a1a")

diamond(ax, 5.5, 6.8, 2.0, 1.0, "EnableCaching ?")

# arrow from state box to diamond
ax.add_patch(FancyArrowPatch((4.3, 8.4), (5.5, 7.3),
            arrowstyle="-|>", mutation_scale=14, color="#444", lw=1.2))

# NO branch -> Path 2 (right, blue)
ax.add_patch(FancyArrowPatch((6.5, 6.8), (9.3, 6.8),
            arrowstyle="-|>", mutation_scale=16, color="#0b3d91", lw=1.8))
ax.text(7.9, 7.0, "NO", ha="center", va="center", fontsize=10,
        fontweight="bold", color="#0b3d91")
ax.text(7.9, 6.75, "(high-freq)", ha="center", va="center", fontsize=8,
        color="#0b3d91", style="italic")

# Path 2 box
ax.add_patch(FancyBboxPatch((9.3, 6.1), 5.2, 1.5, boxstyle="round,pad=0.15",
            facecolor="#dce8f7", edgecolor="#0b3d91", lw=1.8))
ax.text(9.5, 7.35, "PATH 2  --  Direct Write  (no cache)",
        ha="left", va="center", fontsize=11, fontweight="bold", color="#0b3d91")
p2_lines = [
    "for each active entityId:",
    "    writer.WriteInt32(entityId)",
    "    _components[id].Serialize(writer)",
    "// NO intermediate buffer",
]
for j, l in enumerate(p2_lines):
    ax.text(9.5, 7.0 - j*0.25, l, ha="left", va="center", fontsize=8.5,
            family="monospace", color="#1a1a1a")

# YES branch down -> diamond 2
ax.add_patch(FancyArrowPatch((5.5, 6.3), (5.5, 5.55),
            arrowstyle="-|>", mutation_scale=16, color="#444", lw=1.8))
ax.text(5.75, 5.95, "YES", ha="left", va="center", fontsize=10,
        fontweight="bold", color="#444")

# Decision diamond 2: IsDirty?
diamond(ax, 5.5, 5.0, 2.0, 1.0, "IsDirty ?")

# NO branch from IsDirty -> check cache exists -> Path 1
ax.add_patch(FancyArrowPatch((4.5, 5.0), (3.0, 5.0),
            arrowstyle="-|>", mutation_scale=16, color="#2a7a2a", lw=1.8))
ax.text(3.75, 5.2, "NO", ha="center", va="center", fontsize=10,
        fontweight="bold", color="#2a7a2a")

# small cache-exists gate
diamond(ax, 2.0, 5.0, 1.6, 0.8, "_cachedData\n!= null ?", facecolor="#eaf6ea", edgecolor="#2a7a2a")
ax.add_patch(FancyArrowPatch((2.8, 5.0), (2.0, 5.0),
            arrowstyle="-|>", mutation_scale=14, color="#2a7a2a", lw=1.0))

# Path 1 box (green)
ax.add_patch(FancyArrowPatch((2.0, 4.6), (2.0, 3.7),
            arrowstyle="-|>", mutation_scale=16, color="#2a7a2a", lw=1.8))
ax.text(2.25, 4.15, "YES", ha="left", va="center", fontsize=9,
        fontweight="bold", color="#2a7a2a")

ax.add_patch(FancyBboxPatch((0.3, 2.2), 4.6, 1.5, boxstyle="round,pad=0.15",
            facecolor="#e0f0e0", edgecolor="#2a7a2a", lw=1.8))
ax.text(0.5, 3.45, "PATH 1  --  Cache Hit",
        ha="left", va="center", fontsize=11, fontweight="bold", color="#2a7a2a")
p1_lines = [
    "writer.WriteBytes(_cachedData)",
    "// ONE memcpy, zero field iteration",
    "// static components live here",
]
for j, l in enumerate(p1_lines):
    ax.text(0.5, 3.1 - j*0.28, l, ha="left", va="center", fontsize=9,
            family="monospace", color="#1a1a1a")

# YES branch from IsDirty -> Path 3 (down)
ax.add_patch(FancyArrowPatch((5.5, 4.5), (5.5, 3.7),
            arrowstyle="-|>", mutation_scale=16, color="#c46a00", lw=1.8))
ax.text(5.75, 4.1, "YES", ha="left", va="center", fontsize=10,
        fontweight="bold", color="#c46a00")

ax.add_patch(FancyBboxPatch((3.2, 2.2), 5.6, 1.5, boxstyle="round,pad=0.15",
            facecolor="#fbe8cf", edgecolor="#c46a00", lw=1.8))
ax.text(3.4, 3.45, "PATH 3  --  Rebuild Cache  (cache invalidated)",
        ha="left", va="center", fontsize=11, fontweight="bold", color="#c46a00")
p3_lines = [
    "tempWriter = BitWriterPool.Get()",
    "  ... write all entities/fields to temp ...",
    "_cachedData = tempWriter.AsSpan()  // store",
    "writer.WriteBytes(_cachedData)     // then copy",
    "IsDirty = false                    // cleared",
]
for j, l in enumerate(p3_lines):
    ax.text(3.4, 3.1 - j*0.22, l, ha="left", va="center", fontsize=8.5,
            family="monospace", color="#1a1a1a")

# ============ Right-side mapping table ============
ax.text(12.0, 5.55, "Component type  ->  Path", ha="center", va="center",
        fontsize=12, fontweight="bold", color="#1a1a1a")

rows = [
    ("Static (Wall/Terrain)",      "IsDirty almost always false", "Path 1 (cache hit)",   "#2a7a2a"),
    ("Low-freq dynamic (HP)",      "occasionally dirty",          "Path 1 or 3",          "#2a7a2a"),
    ("High-freq dynamic (Transform)", "dirty every frame\n[HighFrequencyComponent] -> caching OFF",
                                                              "Path 2 (direct write)","#0b3d91"),
]
y0 = 5.1
rowh = 0.75
for i, (typ, note, path, col) in enumerate(rows):
    yy = y0 - i*rowh
    ax.add_patch(Rectangle((8.6, yy - rowh + 0.15), 6.0, rowh - 0.1,
                facecolor="#fafafa", edgecolor="#ccc", lw=0.8))
    ax.text(8.75, yy - 0.1, typ, ha="left", va="center", fontsize=9.5,
            fontweight="bold", color="#1a1a1a")
    ax.text(8.75, yy - 0.42, note, ha="left", va="center", fontsize=8,
            color="#555", family="monospace")
    ax.text(14.4, yy - 0.25, path, ha="right", va="center", fontsize=9,
            fontweight="bold", color=col)

# ============ Key insight banner ============
ax.add_patch(FancyBboxPatch((0.3, 0.4), 14.4, 1.3, boxstyle="round,pad=0.15",
            facecolor="#fff8e1", edgecolor="#a07a00", lw=1.8))
ax.text(0.6, 1.35, "Key insight:", ha="left", va="center", fontsize=11,
        fontweight="bold", color="#a07a00")
ax.text(0.6, 0.95,
        "Caching is for things that change RARELY, not things that change OFTEN.",
        ha="left", va="center", fontsize=10, fontweight="bold", color="#1a1a1a")
ax.text(0.6, 0.65,
        "Static  ->  cache hit (near-zero cost)            High-freq  ->  cache OFF (skip extra copy)\n"
        "Only components that ACTUALLY changed pay serialization cost  =  pay-per-mutation.",
        ha="left", va="center", fontsize=9.5, color="#1a1a1a", family="monospace")

plt.tight_layout()
out = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-06-03-serialize-three-paths.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
print("saved:", out)
