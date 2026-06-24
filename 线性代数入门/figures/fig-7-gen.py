"""Fig 7.1 — Invertible vs singular (collapsed) transform.

Left  : A = [[2,1],[1,3]]  (det=5, full rank) — grid twisted but still a 2D grid;
        its inverse A^-1 untwists it back to the original square grid.
Right : S = [[1,2],[2,4]]  (det=0, rank 1) — whole plane collapsed onto a single line;
        no inverse exists (information lost).

Read-only import of shared helpers; this script never touches _plot_utils.py / make_figures.py.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

A  = np.array([[2., 1.],
               [1., 3.]])
Ai = np.linalg.inv(A)            # [[ 0.6,-0.2],[-0.2,0.4]]
S  = np.array([[1., 2.],
               [2., 4.]])        # rank-1, collapses plane onto line y=2x

fig, axes = plt.subplots(1, 2, figsize=(12, 6))

# ---------------- Left : invertible ----------------
ax = axes[0]
grid(ax, np.eye(2), 'gray', 0.35, '--')        # original square grid (faint)
grid(ax, A, BLUE, 0.55)                         # twisted by A
arrow(ax, (0, 0), A[:, 0], GREEN)
arrow(ax, (0, 0), A[:, 1], RED)
ax.text(A[0, 0] * 1.12, A[1, 0] * 1.12, "i' = A col0", color=GREEN, fontsize=11, fontweight='bold')
ax.text(A[0, 1] * 1.05 + 0.1, A[1, 1] * 1.05, "j' = A col1", color=RED, fontsize=11, fontweight='bold')
ax.text(-2.7, -2.4, "det(A)=5, full rank\n-> grid twisted but still 2D\n-> A^-1 untwists it back",
        color='black', fontsize=10.5,
        bbox=dict(boxstyle='round', fc='#f6f6f6', ec='gray', alpha=0.9))
frame(ax, 4.6)
ax.set_title("Invertible:  A = [[2,1],[1,3]],  det=5  (can undo)", fontsize=11.5)

# ---------------- Right : singular (collapsed) ----------------
ax = axes[1]
grid(ax, np.eye(2), 'gray', 0.30, '--')        # original grid (faint)
# S's "grid" collapses onto the line spanned by (1,2)
t = np.linspace(-2.2, 2.2, 2)
line = np.outer(t, np.array([1., 2.]))          # y = 2x
ax.plot(line[:, 0], line[:, 1], color=BLUE, lw=3.5, alpha=0.6)
arrow(ax, (0, 0), S[:, 0], GREEN)               # (1,2)
arrow(ax, (0, 0), S[:, 1], RED)                 # (2,4) = 2*(1,2), collinear
ax.text(S[0, 0] + 0.15, S[1, 0] + 0.1, "i' = (1,2)", color=GREEN, fontsize=11, fontweight='bold')
ax.text(S[0, 1] + 0.15, S[1, 1] - 0.4, "j' = (2,4) = 2.(1,2)", color=RED, fontsize=10.5, fontweight='bold')
ax.text(-2.7, -2.6, "det(S)=0, rank=1\n-> whole plane crushed onto one line\n-> info lost, NO A^-1",
        color='black', fontsize=10.5,
        bbox=dict(boxstyle='round', fc='#fdecec', ec=RED, alpha=0.9))
frame(ax, 4.6)
ax.set_title("Singular:  S = [[1,2],[2,4]],  det=0  (cannot undo)", fontsize=11.5)

fig.suptitle("Inverse exists  <=>  the transform did NOT crush space to a lower dimension",
             fontsize=13.5)
fig.tight_layout(rect=[0, 0, 1, 0.95])
save(fig, "fig-7-1-invertible-vs-singular.png")
print("OK saved fig-7-1-invertible-vs-singular.png to", HERE)
