"""fig-18-competitor-matrix: LockstepSdk vs Photon Fusion / Mirror / Fish-Net
comparison matrix. LockstepSdk is the only deterministic lockstep SDK."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np

fig, ax = plt.subplots(figsize=(15, 8.5))

# Row labels (dimensions) and column headers
dims = [
    "Sync Model",
    "Determinism\n(bit-exact)",
    "Fixed-point\nMath",
    "Built-in\nReplay",
    "Anti-cheat\n(server-authoritative)",
    "Engine\nBinding",
    "Source\nDeliverable",
]
products = ["LockstepSdk\n(this book)", "Photon\nFusion", "Mirror", "Fish-Net"]

# Cell contents: (text, category)  category in {yes, no, partial, text}
# rows = dimensions, cols = products
cells = [
    # LockstepSdk,        Fusion,                Mirror,            Fish-Net
    [("Deterministic\nlockstep", "text"),
     ("State sync\n+ prediction", "text"),
     ("State sync", "text"),
     ("State sync\n+ prediction", "text")],
    [("YES", "yes"), ("NO", "no"), ("NO", "no"), ("NO", "no")],
    [("YES  Q48.16", "yes"), ("NO", "no"), ("NO", "no"), ("NO", "no")],
    [("YES  +CRC", "yes"), ("3rd-party", "partial"), ("NO", "no"), ("NO", "no")],
    [("Native\n(hash audit)", "yes"), ("Extra work", "partial"), ("Extra work", "partial"), ("Extra work", "partial")],
    [("Engine-agnostic\n(Core/Network\nzero-dep)", "text"),
     ("Unity", "text"), ("Unity", "text"), ("Unity", "text")],
    [("YES  full C#", "yes"), ("Partial", "partial"), ("Open", "text"), ("Open", "text")],
]

cat_color = {
    "yes":     ("#2a7a2a", "white"),   # green fill, white text
    "no":      ("#b3403a", "white"),   # red fill, white text
    "partial": ("#e0a020", "#1a1a1a"), # amber fill, dark text
    "text":    ("#f2f2f2", "#1a1a1a"), # neutral fill, dark text
}
# highlight LockstepSdk column header
header_color_lockstep = "#0b3d91"
header_color_other = "#555555"

n_rows = len(dims)
n_cols = len(products)

# Layout
left, top = 0.5, 0.5
col_label_w = 3.0          # leftmost dimension label column width
col_w = 2.55               # product column width
row_h = 1.0                # row height
header_h = 1.1

total_w = col_label_w + n_cols * col_w
total_h = header_h + n_rows * row_h

ax.set_xlim(0, total_w)
ax.set_ylim(0, total_h + 0.6)
ax.axis("off")

# Title
ax.text(total_w / 2, total_h + 0.35,
        "Competitor Matrix  --  Why no independent deterministic lockstep SDK exists",
        ha="center", va="center", fontsize=14, fontweight="bold", color="#0b3d91")

# Column headers
for j, prod in enumerate(products):
    x = left + col_label_w + j * col_w
    is_lock = (j == 0)
    fc = header_color_lockstep if is_lock else header_color_other
    rect = Rectangle((x, top + (n_rows - 1) * row_h + 0.0), col_w, header_h,
                     facecolor=fc, edgecolor="black", linewidth=1.2)
    ax.add_patch(rect)
    ax.text(x + col_w / 2, top + (n_rows - 1) * row_h + header_h / 2, prod,
            ha="center", va="center", fontsize=11.5, fontweight="bold", color="white")

# Dimension label header (top-left corner)
rect = Rectangle((left, top + (n_rows - 1) * row_h + 0.0), col_label_w, header_h,
                 facecolor="#1a1a1a", edgecolor="black", linewidth=1.2)
ax.add_patch(rect)
ax.text(left + col_label_w / 2, top + (n_rows - 1) * row_h + header_h / 2,
        "Dimension", ha="center", va="center", fontsize=11.5,
        fontweight="bold", color="white")

# Rows
for i, dim in enumerate(dims):
    # row index from top: i=0 is top row
    y_top = top + (n_rows - 1 - i) * row_h
    # dimension label cell
    fc_dim = "#3a3a3a" if i % 2 == 0 else "#2e2e2e"
    rect = Rectangle((left, y_top), col_label_w, row_h,
                     facecolor=fc_dim, edgecolor="black", linewidth=1.0)
    ax.add_patch(rect)
    ax.text(left + col_label_w / 2, y_top + row_h / 2, dim,
            ha="center", va="center", fontsize=9.8, fontweight="bold",
            color="white", linespacing=1.2)
    # product cells
    for j in range(n_cols):
        x = left + col_label_w + j * col_w
        text, cat = cells[i][j]
        fc, tc = cat_color[cat]
        # subtle highlight for lockstep column
        if j == 0 and cat == "text":
            fc = "#d6e4f5"  # light blue tint for lockstep text cells
        rect = Rectangle((x, y_top), col_w, row_h,
                         facecolor=fc, edgecolor="black", linewidth=1.0)
        ax.add_patch(rect)
        fw = "bold" if cat in ("yes", "no") else "normal"
        fs = 9.8 if cat in ("yes", "no") else 9.2
        ax.text(x + col_w / 2, y_top + row_h / 2, text,
                ha="center", va="center", fontsize=fs, fontweight=fw,
                color=tc, linespacing=1.15)

# Highlight border around LockstepSdk column
hl_x = left + col_label_w
hl_y = top
hl_w = col_w
hl_h = n_rows * row_h + header_h
from matplotlib.patches import FancyBboxPatch
ax.add_patch(FancyBboxPatch(
    (hl_x - 0.06, hl_y - 0.06), hl_w + 0.12, hl_h + 0.12,
    boxstyle="round,pad=0.0,rounding_size=0.08",
    facecolor="none", edgecolor="#0b3d91", linewidth=2.8, zorder=5))

# Legend
leg_y = -0.1
legend_items = [
    ("#2a7a2a", "white", "YES / native"),
    ("#b3403a", "white", "NO"),
    ("#e0a020", "#1a1a1a", "Partial / extra work"),
    ("#f2f2f2", "#1a1a1a", "(text / value)"),
]
lx = left
for fc, tc, label in legend_items:
    rect = Rectangle((lx, leg_y), 0.5, 0.4, facecolor=fc, edgecolor="black",
                     linewidth=1.0, transform=ax.transData, clip_on=False)
    ax.add_patch(rect)
    ax.text(lx + 0.6, leg_y + 0.2, label, ha="left", va="center",
            fontsize=9.5, color="#1a1a1a")
    lx += 2.6

# Key takeaway banner under the table
banner_y = top - 0.95
banner = Rectangle((left, banner_y), total_w - left, 0.7,
                   facecolor="#0b3d91", edgecolor="#07256a", clip_on=False)
ax.add_patch(banner)
ax.text(left + (total_w - left) / 2, banner_y + 0.35,
        "Fusion / Mirror / Fish-Net are ALL state-sync.  No determinism, no fixed-point, no replay.  "
        "They CANNOT build fighting / RTS / MOBA.  ->  LockstepSdk fills the gap.",
        ha="center", va="center", fontsize=10, fontweight="bold", color="white")

plt.tight_layout()
out = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-18-competitor-matrix.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
print("saved:", out)
