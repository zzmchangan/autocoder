import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Polygon

OUT = os.path.dirname(os.path.abspath(__file__))
GREEN = '#2ca02c'; RED = '#d62728'; BLUE = '#1f77b4'; PURPLE = '#9467bd'

def arrow(ax, start, end, color, lw=2.2, ms=18):
    ax.add_patch(FancyArrowPatch(tuple(start), tuple(end),
                 arrowstyle='-|>', mutation_scale=ms, color=color, lw=lw, zorder=5))

def grid(ax, M, color, alpha, ls='-', lw=0.9):
    M = np.asarray(M, float); n = 2
    for k in range(-n, n + 1):
        v = np.array([[k, -n], [k, n]]) @ M.T
        h = np.array([[-n, k], [n, k]]) @ M.T
        ax.plot(v[:, 0], v[:, 1], color=color, alpha=alpha, ls=ls, lw=lw)
        ax.plot(h[:, 0], h[:, 1], color=color, alpha=alpha, ls=ls, lw=lw)

def frame(ax, lim):
    ax.set_aspect('equal'); ax.set_xlim(-lim, lim); ax.set_ylim(-lim, lim)
    ax.axhline(0, color='gray', lw=0.6, alpha=0.4)
    ax.axvline(0, color='gray', lw=0.6, alpha=0.4)

# ---- Fig 1: four linear transforms ----
ts = [("Identity  I", np.eye(2)),
      ("Rotation 90 deg", np.array([[0., -1], [1, 0]])),
      ("Shear", np.array([[1., 1], [0, 1]])),
      ("Scale x2", np.array([[2., 0], [0, 2]]))]
fig, axes = plt.subplots(2, 2, figsize=(9, 9))
for ax, (name, M) in zip(axes.ravel(), ts):
    grid(ax, np.eye(2), 'gray', 0.35, '--')
    grid(ax, M, BLUE, 0.5)
    arrow(ax, (0, 0), M[:, 0], GREEN)
    arrow(ax, (0, 0), M[:, 1], RED)
    ax.text(M[0, 0] * 1.15, M[1, 0] * 1.15, "i'", color=GREEN, fontsize=12, fontweight='bold')
    ax.text(M[0, 1] * 1.15, M[1, 1] * 1.15, "j'", color=RED, fontsize=12, fontweight='bold')
    frame(ax, 4.3); ax.set_title(name, fontsize=13)
fig.suptitle("Matrix = transformation:   gray dashed = before,   blue solid = after", fontsize=14)
fig.tight_layout(rect=[0, 0, 1, 0.97])
fig.savefig(os.path.join(OUT, "fig-1-1-transforms.png"), dpi=150); plt.close(fig)

# ---- Fig 2: linear combination ----
v = np.array([1., 2]); w = np.array([3., 1]); comb = 2 * v - w; mid = 2 * v
fig, ax = plt.subplots(figsize=(7, 7))
arrow(ax, (0, 0), mid, GREEN, 2.2); arrow(ax, mid, comb, RED, 2.2)
arrow(ax, (0, 0), v, GREEN, 1.5); arrow(ax, (0, 0), w, RED, 1.5)
arrow(ax, (0, 0), comb, BLUE, 3.0)
ax.text(v[0] + .12, v[1] + .12, "v", color=GREEN, fontsize=13)
ax.text(w[0] + .12, w[1] + .12, "w", color=RED, fontsize=13)
ax.text(mid[0] + .12, mid[1] + .12, "2v", color=GREEN, fontsize=13)
ax.text(comb[0] + .15, comb[1] - .25, "2v - w", color=BLUE, fontsize=14, fontweight='bold')
ax.set_aspect('equal'); ax.set_xlim(-1.8, 4); ax.set_ylim(-1.2, 4.6)
ax.axhline(0, color='gray', lw=.6, alpha=.4); ax.axvline(0, color='gray', lw=.6, alpha=.4)
ax.set_title("Linear combination:  walk 2v,  then  -w", fontsize=13)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig-2-1-linear-combination.png"), dpi=150); plt.close(fig)

# ---- Fig 3.1: span (plane vs line) ----
fig, axes = plt.subplots(1, 2, figsize=(11, 5.5))
# left: non-parallel -> whole plane
ax = axes[0]
va = np.array([2., 1]); wa = np.array([-1., 2.])
pts = np.array([0 * va, va, va + wa, wa])
ax.add_patch(Polygon(pts, color=BLUE, alpha=0.13))
for a in np.linspace(-1.6, 1.6, 9):
    for b in np.linspace(-1.6, 1.6, 9):
        p = a * va + b * wa
        ax.plot(*p, '.', color=BLUE, alpha=0.35, ms=2)
arrow(ax, (0, 0), va, GREEN); arrow(ax, (0, 0), wa, RED)
ax.text(va[0] + .1, va[1] + .1, "v", color=GREEN, fontsize=13)
ax.text(wa[0] - .5, wa[1] + .1, "w", color=RED, fontsize=13)
frame(ax, 4.6); ax.set_title("span(v, w) = whole plane", fontsize=12)
# right: collinear -> a line
ax = axes[1]
vb = np.array([2., 1]); wb = 2 * vb
t = np.array([-2., 2.]); line = np.outer(t, vb)
ax.plot(line[:, 0], line[:, 1], color=BLUE, lw=4, alpha=0.35)
arrow(ax, (0, 0), vb, GREEN); arrow(ax, (0, 0), wb, RED, lw=1.6)
ax.text(vb[0] + .1, vb[1] + .1, "v", color=GREEN, fontsize=13)
ax.text(wb[0] + .1, wb[1] - .25, "w=2v", color=RED, fontsize=13)
frame(ax, 4.6); ax.set_title("span(v, 2v) = just a line (redundant!)", fontsize=12)
fig.suptitle("Span: all linear combinations of given vectors", fontsize=14)
fig.tight_layout(rect=[0, 0, 1, 0.95])
fig.savefig(os.path.join(OUT, "fig-3-1-span.png"), dpi=150); plt.close(fig)

# ---- Fig 3.2: linear dependence (3 vectors in 2D, one redundant) ----
fig, ax = plt.subplots(figsize=(7, 7))
v1 = np.array([2.5, 0.5]); v2 = np.array([0.5, 2.]); v3 = v1 + v2
arrow(ax, (0, 0), v1, GREEN, 2.2)
arrow(ax, v1, v3, RED, 2.0)              # v2 step from v1
arrow(ax, (0, 0), v3, PURPLE, 2.8)       # v3 (redundant)
arrow(ax, (0, 0), v2, RED, 1.4)          # v2 from origin
ax.text(v1[0] + .1, v1[1] - .3, "v1", color=GREEN, fontsize=13)
ax.text(v2[0] - .35, v2[1] + .1, "v2", color=RED, fontsize=13)
ax.text(v3[0] + .1, v3[1] + .1, "v3 = v1 + v2", color=PURPLE, fontsize=12, fontweight='bold')
frame(ax, 4.0)
ax.set_title("Linear dependence:  v3 is redundant (it's already v1 + v2)", fontsize=12)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig-3-2-linear-dependence.png"), dpi=150); plt.close(fig)

print("OK all figures saved to", OUT)
