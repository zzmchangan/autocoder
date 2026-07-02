"""fig-dir-01-structure.png
Catalogue / navigation view of the whole book:
7 Parts / 27 chapters as a vertical reading journey, color-coded by the
dichotomy (blue = determinism kernel, green = predict/rollback bridge,
orange = sync mechanism, purple = cross-cutting, grey = framing).
Echoes P0-01 fig-03 but from the *table-of-contents* perspective
(sequential journey with per-Part chapter counts and the bridge between
kernel and sync highlighted).
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

# ---- palette (matches gen_fig03 / series) ----
C_KERNEL = '#1F6FB2'   # blue   - determinism kernel
C_BRIDGE = '#2E8B57'   # green  - predict/rollback + presentation
C_SYNC   = '#C0563E'   # orange - sync mechanism
C_CROSS  = '#7C3AED'   # purple - engineering / debugging / collisions
C_FRAME  = '#64748B'   # slate  - framing (preface + closing overview)
C_PAPER  = '#F7F9FC'
C_EDGE   = '#33415C'
C_TXT    = '#1B2733'

fig, ax = plt.subplots(figsize=(11.5, 9.2))
ax.set_xlim(0, 11.5)
ax.set_ylim(0, 9.2)
ax.axis('off')
fig.patch.set_facecolor('white')

# ---- title ----
ax.text(5.75, 8.82,
        'Book Map: 7 Parts / 27 Chapters as a Reading Journey',
        ha='center', va='center', fontsize=13.5, weight='bold', color=C_EDGE)
ax.text(5.75, 8.50,
        'determinism kernel (blue)  ->  bridge: predict/rollback (green)  ->  sync mechanism (orange)',
        ha='center', va='center', fontsize=9.8, style='italic', color='#5A6B7B')

# ---- Part blocks: (id, title, subtitle, ch_range, n_ch, color, is_bridge) ----
# stacked top -> bottom; journey flows downward
parts = [
    ("Part 0", "Framing",
     "What is lockstep",                "Ch.1",        1, C_FRAME, False),
    ("Part 1", "Determinism Kernel - I",
     "Fixed-point math (signature)",    "Ch.2-3",      2, C_KERNEL, False),
    ("Part 2", "Determinism Kernel - II",
     "RNG / ECS / serialize",           "Ch.4-7",      4, C_KERNEL, False),
    ("Part 3", "Bridge: Predict-Rollback + Smoothing",
     "The time machine",                "Ch.8-11",     4, C_BRIDGE, True),
    ("Part 4", "Sync Mechanism",
     "Onto the network",                "Ch.12-17",    6, C_SYNC,   False),
    ("Part 5", "Engineering & Robustness",
     "Make it an SDK",                  "Ch.18-22",    5, C_CROSS,  False),
    ("Part 6", "Deterministic Debugging",
     "How to find desync (unique)",     "Ch.23-25",    3, C_CROSS,  False),
    ("Part 7", "Periphery & Practice",
     "Collisions / TankGame",           "Ch.26-27",    2, C_FRAME,  False),
]

# geometry of stacked panels
top = 8.05
panel_h = 0.78
gap = 0.155
x = 1.05
w = 7.05

centers = []
for i, (pid, title, sub, ch, n, color, is_bridge) in enumerate(parts):
    y = top - i * (panel_h + gap) - panel_h
    cy = y + panel_h / 2
    centers.append((cy, color, is_bridge))

    fc = '#FFFFFF' if not is_bridge else '#F0FBF4'
    # main panel
    ax.add_patch(FancyBboxPatch((x, y), w, panel_h,
                 boxstyle="round,pad=0.02,rounding_size=0.10",
                 linewidth=1.6, edgecolor=color, facecolor=fc, alpha=1.0))
    # left color bar (category)
    bar_w = 0.18
    ax.add_patch(FancyBboxPatch((x, y), bar_w, panel_h,
                 boxstyle="round,pad=0.0,rounding_size=0.02",
                 linewidth=0, facecolor=color, alpha=1.0))
    # part id (left, on bar)
    ax.text(x + bar_w + 0.18, cy + 0.13, pid,
            ha='left', va='center', fontsize=9.6, weight='bold', color=color)
    ax.text(x + bar_w + 0.18, cy - 0.16, ch,
            ha='left', va='center', fontsize=8.2, color='#5A6B7B')
    # title (center-left)
    ax.text(x + bar_w + 1.55, cy + 0.13, title,
            ha='left', va='center', fontsize=10.3, weight='bold', color=C_TXT)
    ax.text(x + bar_w + 1.55, cy - 0.16, sub,
            ha='left', va='center', fontsize=8.6, style='italic', color='#46566A')
    # chapter count chip (right)
    chip_w = 1.55
    ax.add_patch(FancyBboxPatch((x + w - chip_w - 0.18, cy - 0.20), chip_w, 0.40,
                 boxstyle="round,pad=0.02,rounding_size=0.12",
                 linewidth=0.6, edgecolor=color, facecolor=color, alpha=0.14))
    ax.text(x + w - chip_w / 2 - 0.18, cy,
            f"{n} ch", ha='center', va='center',
            fontsize=8.8, weight='bold', color=color)

# ---- journey arrows between consecutive panels ----
for i in range(len(parts) - 1):
    (cy0, c0, _) = centers[i]
    (cy1, c1, _) = centers[i + 1]
    y_top = cy0 - panel_h / 2
    y_bot = cy1 + panel_h / 2
    arrow = FancyArrowPatch((x + w / 2, y_top - 0.005),
                            (x + w / 2, y_bot + 0.005),
                            arrowstyle='-|>', mutation_scale=11,
                            linewidth=1.5, color='#8A98A8',
                            shrinkA=0, shrinkB=0)
    ax.add_patch(arrow)

# ---- right-side dichotomy legend rail ----
lx = x + w + 0.55
ax.text(lx + 1.0, top + 0.05, 'Dichotomy',
           ha='center', va='center', fontsize=10.6, weight='bold', color=C_EDGE)
ax.text(lx + 1.0, top - 0.22, '("lost? come back here")',
           ha='center', va='center', fontsize=7.8, style='italic', color='#5A6B7B')

# legend entries tie colour to the two halves + bridge
legend = [
    ("Determinism\nKernel", C_KERNEL, "Part 1-2  (6 ch)",
     'one machine, bit-identical'),
    ("Bridge:\nPredict-Rollback", C_BRIDGE, "Part 3  (4 ch)",
     'time machine + smoothing'),
    ("Sync\nMechanism", C_SYNC, "Part 4  (6 ch)",
     'onto the network'),
    ("Cross-cutting\nEng / Debug", C_CROSS, "Part 5-6  (8 ch)",
     'SDK + find desync'),
    ("Framing", C_FRAME, "Part 0 + 7-26  (3 ch)",
     'open + close the loop'),
]
ly = top - 0.85
lh = 0.95
for name, color, rng, desc in legend:
    ax.add_patch(FancyBboxPatch((lx, ly - lh / 2), 2.0, lh,
                 boxstyle="round,pad=0.02,rounding_size=0.10",
                 linewidth=1.3, edgecolor=color, facecolor='#FFFFFF'))
    ax.add_patch(FancyBboxPatch((lx, ly - lh / 2), 0.13, lh,
                 boxstyle="round,pad=0,rounding_size=0.02",
                 linewidth=0, facecolor=color))
    ax.text(lx + 0.28, ly + 0.20, name,
            ha='left', va='center', fontsize=8.8, weight='bold', color=color)
    ax.text(lx + 0.28, ly - 0.06, rng,
            ha='left', va='center', fontsize=7.6, color='#5A6B7B')
    ax.text(lx + 0.28, ly - 0.27, desc,
            ha='left', va='center', fontsize=7.4, style='italic', color='#46566A')
    ly -= (lh + 0.13)

# ---- bottom annotation: the contract + chapter count summary ----
by = 0.55
ax.add_patch(FancyBboxPatch((1.05, by - 0.30), 9.40, 0.62,
             boxstyle="round,pad=0.02,rounding_size=0.10",
             linewidth=1.0, edgecolor=C_EDGE, facecolor=C_PAPER))
ax.text(5.75, by,
        'Contract:  same initial state  +  same input sequence  +  deterministic logic  =  identical result     '
        '|     27 chapters + appendix A/B',
        ha='center', va='center', fontsize=9.0, color=C_TXT)

# foundation callouts under parts 1 and 3 (do-not-skip)
# pointer text near Part 1 (i=1) and Part 3 (i=3)
def foundation_note(idx, text):
    cy = centers[idx][0]
    ax.annotate(text,
                xy=(x - 0.02, cy), xytext=(x - 1.02, cy),
                fontsize=7.6, color='#B91C1C', weight='bold',
                ha='right', va='center',
                arrowprops=dict(arrowstyle='-|>', color='#B91C1C', lw=1.0))

foundation_note(1, 'FOUNDATION\n(do not skip)')
foundation_note(3, 'Ch.9 essence:\n7 rollback disciplines')

plt.tight_layout()
plt.savefig('fig-dir-01-structure.png', dpi=150, bbox_inches='tight',
            facecolor='white')
print("saved fig-dir-01-structure.png")
