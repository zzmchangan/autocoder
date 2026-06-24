"""Fig 11.1 / 11.2 generator for Chapter 11 (P3-11) - Four Fundamental Subspaces.

Independent script. Only READS _plot_utils.py; never modifies shared files.

Fig 11.1: The classic "four subspaces" diagram for an m x n matrix A of rank r.
  Left rectangle  = input space R^n  (split into Row space C(A^T), dim r,
                                     and Null space N(A), dim n - r).
  Right rectangle = output space R^m (split into Column space C(A), dim r,
                                     and Left null space N(A^T), dim m - r).
  Arrow A: Row space ->> Column space (one-to-one, onto).
  Arrow A: Null space -> 0 (crushed to origin).

Fig 11.2: Concrete check on A = [[1,2,3],[2,4,6]] (2x3, rank 1).
  Left: input R^3 drawn as two perpendicular planes --
        row space = line along (1,2,3) (dim 1),
        null space = plane of vectors with A x = 0 (dim 2).
  Right: output R^2 --
        column space = line y = 2x (dim 1),
        left null space = line y = -x/2 ... = direction (2,-1) (dim 1).
  Arrows: row space -> column space (one-to-one); null space -> origin.
"""
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle, FancyBboxPatch
from _plot_utils import GREEN, RED, BLUE, PURPLE, ORANGE, GRAY

OUT = HERE

# A small set of extra colors for the diagram
ROW_COLOR   = BLUE     # row space / column space both 'survive' side
NULL_COLOR  = PURPLE   # null space / left null space both 'crushed' side
COL_COLOR   = BLUE
LN_COLOR    = PURPLE

# =====================================================================
# Fig 11.1: the classic four-subspaces schematic (generic, for any A m x n, rank r)
# =====================================================================
fig, ax = plt.subplots(figsize=(13, 8))
ax.set_xlim(0, 13)
ax.set_ylim(0, 8.4)
ax.axis('off')

# --- Left rectangle: input space R^n ---
# Outer box
left_box = FancyBboxPatch((0.3, 0.6), 4.0, 6.8,
                          boxstyle="round,pad=0.02,rounding_size=0.1",
                          linewidth=2, edgecolor='black', facecolor='#f7f7f7')
ax.add_patch(left_box)
# Split: bottom (row space, dim r) + top (null space, dim n-r). Make row space bigger.
row_rect = Rectangle((0.5, 0.8), 3.6, 4.0, facecolor=ROW_COLOR, alpha=0.18,
                     edgecolor=ROW_COLOR, linewidth=1.5)
null_rect = Rectangle((0.5, 5.0), 3.6, 2.2, facecolor=NULL_COLOR, alpha=0.18,
                      edgecolor=NULL_COLOR, linewidth=1.5)
ax.add_patch(row_rect)
ax.add_patch(null_rect)

ax.text(2.3, 7.15, "Input space  R^n", ha='center', fontsize=14, fontweight='bold')
ax.text(2.3, 0.30, "(dim n)", ha='center', fontsize=11, color='black')

# Row space label
ax.text(2.3, 4.55, "Row space  C(A^T)", ha='center', fontsize=12.5, fontweight='bold', color=ROW_COLOR)
ax.text(2.3, 4.15, "dim = r   (survives)", ha='center', fontsize=10.5, color=ROW_COLOR)
ax.text(2.3, 2.4, "linearly independent\nrows of A", ha='center', fontsize=9.5, color=ROW_COLOR, alpha=0.9)

# Null space label
ax.text(2.3, 6.65, "Null space  N(A)", ha='center', fontsize=12.5, fontweight='bold', color=NULL_COLOR)
ax.text(2.3, 6.25, "dim = n - r   (crushed)", ha='center', fontsize=10.5, color=NULL_COLOR)

# Orthogonal marker between them
ax.annotate("", xy=(4.05, 5.0), xytext=(4.05, 4.8),
            arrowprops=dict(arrowstyle='<->', color='black', lw=1.2))
ax.text(4.15, 4.9, "perpendicular", fontsize=8.5, color='black', va='center')

# --- Right rectangle: output space R^m ---
right_box = FancyBboxPatch((8.7, 0.6), 4.0, 6.8,
                           boxstyle="round,pad=0.02,rounding_size=0.1",
                           linewidth=2, edgecolor='black', facecolor='#f7f7f7')
ax.add_patch(right_box)
col_rect = Rectangle((8.9, 0.8), 3.6, 4.0, facecolor=COL_COLOR, alpha=0.18,
                     edgecolor=COL_COLOR, linewidth=1.5)
ln_rect = Rectangle((8.9, 5.0), 3.6, 2.2, facecolor=LN_COLOR, alpha=0.18,
                    edgecolor=LN_COLOR, linewidth=1.5)
ax.add_patch(col_rect)
ax.add_patch(ln_rect)

ax.text(10.7, 7.15, "Output space  R^m", ha='center', fontsize=14, fontweight='bold')
ax.text(10.7, 0.30, "(dim m)", ha='center', fontsize=11, color='black')

ax.text(10.7, 4.55, "Column space  C(A)", ha='center', fontsize=12.5, fontweight='bold', color=COL_COLOR)
ax.text(10.7, 4.15, "dim = r   (where output lands)", ha='center', fontsize=10.5, color=COL_COLOR)
ax.text(10.7, 2.4, "all combinations\nof columns of A", ha='center', fontsize=9.5, color=COL_COLOR, alpha=0.9)

ax.text(10.7, 6.65, "Left null space  N(A^T)", ha='center', fontsize=12.5, fontweight='bold', color=LN_COLOR)
ax.text(10.7, 6.25, "dim = m - r   (never reached)", ha='center', fontsize=10.5, color=LN_COLOR)

ax.annotate("", xy=(12.45, 5.0), xytext=(12.45, 4.8),
            arrowprops=dict(arrowstyle='<->', color='black', lw=1.2))
ax.text(12.5, 4.9, "perpendicular", fontsize=8.5, color='black', va='center')

# --- Arrows in the middle: A maps row space -> column space (one-to-one, onto)
arr1 = FancyArrowPatch((4.2, 2.8), (8.8, 2.8),
                       arrowstyle='-|>', mutation_scale=28, color=GREEN, lw=3.2, zorder=6)
ax.add_patch(arr1)
ax.text(6.5, 3.15, "A :  row space  ->>  column space", ha='center',
        fontsize=12, fontweight='bold', color=GREEN)
ax.text(6.5, 2.55, "one-to-one  &  onto  (no info lost)", ha='center',
        fontsize=10, color=GREEN, style='italic')

# Arrow: null space -> origin (crushed)
arr2 = FancyArrowPatch((4.2, 6.0), (8.8, 6.0),
                       arrowstyle='-|>', mutation_scale=28, color=RED, lw=3.0, zorder=6)
ax.add_patch(arr2)
ax.text(6.5, 6.35, "A :  null space  ->  0", ha='center',
        fontsize=12, fontweight='bold', color=RED)
ax.text(6.5, 5.75, "entire subspace crushed to the origin", ha='center',
        fontsize=10, color=RED, style='italic')

# Title and rank summary
ax.text(6.5, 8.15,
        "The Four Fundamental Subspaces of  A  (m x n,  rank = r)",
        ha='center', fontsize=15, fontweight='bold')
ax.text(6.5, 7.75,
        "r  ties them all together:   dim C(A) = dim C(A^T) = r,   dim N(A) = n - r,   dim N(A^T) = m - r",
        ha='center', fontsize=11, color='#333333')

# Legend note at the very bottom
ax.text(6.5, 0.05,
        "Input = Row space (r)  +  Null space (n-r),  perpendicular.    "
        "Output = Column space (r)  +  Left null space (m-r),  perpendicular.",
        ha='center', fontsize=9.5, color='#555555')

fig.tight_layout()
fig.savefig(os.path.join(OUT, "fig-11-1-four-subspaces.png"), dpi=150, bbox_inches='tight')
plt.close(fig)
print("wrote fig-11-1-four-subspaces.png")


# =====================================================================
# Fig 11.2: concrete matrix A = [[1,2,3],[2,4,6]] (2x3, rank 1)
#   Left: input R^3 as row-space line + null-space plane (perpendicular).
#   Right: output R^2 as column-space line + left-null-space line (perpendicular).
#   We draw a schematic (not a true 3D render) -- two perpendicular boxes per side.
# =====================================================================
fig, axes = plt.subplots(1, 2, figsize=(13, 6.5))

# ---- LEFT: input R^3, rank 1 ----
ax = axes[0]
ax.set_xlim(0, 6.5); ax.set_ylim(0, 6.5); ax.axis('off')
# Box for R^3
ax.add_patch(FancyBboxPatch((0.2, 0.4), 6.0, 5.7,
             boxstyle="round,pad=0.02,rounding_size=0.1",
             linewidth=2, edgecolor='black', facecolor='#fafafa'))
ax.text(3.2, 5.95, "Input  R^3   (n = 3)", ha='center', fontsize=13, fontweight='bold')

# Row space: a 1-D line along (1,2,3). Draw as a thick green segment.
# null space: a 2-D plane (dim 2). Draw as a translucent purple parallelogram.
# Perpendicular to each other.

# Place the null-space plane as a parallelogram centered at origin
# Use two basis vectors of the null space (perpendicular to (1,2,3)):
#   n1 = (2,-1,0) projected to 2D for display, n2 = (3,0,-1).
# For a clean schematic, draw the plane as a tilted parallelogram,
# and the row-space line sticking straight out of it.
center = np.array([3.0, 3.0])
# null space plane (drawn as a parallelogram)
n_a = np.array([1.5, 0.45])   # one in-plane direction (schematic)
n_b = np.array([-0.5, 1.5])   # another in-plane direction (schematic)
plane_pts = np.array([center - n_a - n_b, center + n_a - n_b,
                      center + n_a + n_b, center - n_a + n_b])
from matplotlib.patches import Polygon
ax.add_patch(Polygon(plane_pts, closed=True, facecolor=NULL_COLOR, alpha=0.20,
                     edgecolor=NULL_COLOR, linewidth=1.6))
ax.text(4.7, 4.5, "Null space N(A)\ndim = n - r = 2",
        fontsize=11, color=NULL_COLOR, fontweight='bold', ha='center')
ax.text(4.7, 3.75, "all x with A x = 0\n"
                   "e.g. (2,-1,0), (3,0,-1)",
        fontsize=9, color=NULL_COLOR, ha='center')

# Row space: 1-D line, drawn perpendicular to the plane (sticking "up out of" it)
# Schematic: a thick green line crossing the plane vertically.
rs_top = center + np.array([0.0, 1.7])
rs_bot = center - np.array([0.0, 1.7])
ax.plot([rs_top[0], rs_bot[0]], [rs_top[1], rs_bot[1]],
        color=ROW_COLOR, lw=4.5, alpha=0.85, zorder=4)
ax.text(center[0] - 0.15, rs_top[1] + 0.18, "Row space C(A^T)",
        fontsize=11, color=ROW_COLOR, fontweight='bold', ha='right')
ax.text(center[0] - 0.15, rs_top[1] - 0.20, "dim = r = 1",
        fontsize=10, color=ROW_COLOR, ha='right')
ax.text(center[0] - 0.15, rs_top[1] - 0.55, "direction (1,2,3)",
        fontsize=9, color=ROW_COLOR, ha='right')

# Right-angle marker between row-space line and plane
ax.plot([center[0], center[0] + 0.28, center[0] + 0.28],
        [center[1] + 0.28, center[1] + 0.28, center[1]],
        color='black', lw=1.2)

# ---- RIGHT: output R^2, rank 1 ----
ax = axes[1]
ax.set_xlim(0, 6.5); ax.set_ylim(0, 6.5); ax.axis('off')
ax.add_patch(FancyBboxPatch((0.2, 0.4), 6.0, 5.7,
             boxstyle="round,pad=0.02,rounding_size=0.1",
             linewidth=2, edgecolor='black', facecolor='#fafafa'))
ax.text(3.2, 5.95, "Output  R^2   (m = 2)", ha='center', fontsize=13, fontweight='bold')

# Column space: line y = 2x (direction (1,2)).  Draw through center.
c = np.array([3.0, 3.0])
# direction (1,2) normalized for display scale
d = np.array([1.0, 2.0]); d = d / np.linalg.norm(d) * 2.1
ax.plot([c[0] - d[0], c[0] + d[0]], [c[1] - d[1], c[1] + d[1]],
        color=COL_COLOR, lw=4.5, alpha=0.85, zorder=4)
ax.text(c[0] + d[0] + 0.05, c[1] + d[1] + 0.05, "Column space C(A)",
        fontsize=11, color=COL_COLOR, fontweight='bold')
ax.text(c[0] + d[0] + 0.05, c[1] + d[1] - 0.30, "dim = r = 1",
        fontsize=10, color=COL_COLOR)
ax.text(c[0] + d[0] + 0.05, c[1] + d[1] - 0.65, "direction (1,2)\nline y = 2x",
        fontsize=9, color=COL_COLOR)

# Left null space: line perpendicular to column space, direction (2,-1).
e = np.array([2.0, -1.0]); e = e / np.linalg.norm(e) * 2.1
ax.plot([c[0] - e[0], c[0] + e[0]], [c[1] - e[1], c[1] + e[1]],
        color=LN_COLOR, lw=4.0, alpha=0.85, ls='--', zorder=4)
ax.text(c[0] + e[0] + 0.05, c[1] + e[1] - 0.05, "Left null space N(A^T)",
        fontsize=11, color=LN_COLOR, fontweight='bold')
ax.text(c[0] + e[0] + 0.05, c[1] + e[1] - 0.40, "dim = m - r = 1",
        fontsize=10, color=LN_COLOR)
ax.text(c[0] + e[0] + 0.05, c[1] + e[1] - 0.75, "direction (2,-1)\nnever reached by A",
        fontsize=9, color=LN_COLOR)

# Right-angle marker at origin (where the two lines meet)
ax.plot([c[0], c[0] + 0.25, c[0] + 0.25],
        [c[1] + 0.25, c[1] + 0.25, c[1]],
        color='black', lw=1.2)

# Big arrow in the middle (between subplots is awkward; put it inside left plot edge)
# We add the mapping annotation as text on the right plot
ax.text(3.2, 0.85,
        "A maps Row space (1,2,3)  ->  14 * (1,2)  in Column space\n"
        "A maps Null space  ->  0   (every vector in it)",
        ha='center', fontsize=10, color='#222222',
        bbox=dict(boxstyle='round,pad=0.4', facecolor='#fff7d6', edgecolor='#caa'))

fig.suptitle("A = [[1,2,3],[2,4,6]]   (2 x 3,  rank r = 1):  "
             "the four subspaces, with dimensions",
             fontsize=13.5, fontweight='bold', y=0.99)

fig.tight_layout(rect=[0, 0, 1, 0.96])
fig.savefig(os.path.join(OUT, "fig-11-2-example-rank1.png"), dpi=150, bbox_inches='tight')
plt.close(fig)
print("wrote fig-11-2-example-rank1.png")
