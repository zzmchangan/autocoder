"""Fig 19 (Chapter 19, SVD) generator -- INDEPENDENT script.

Read-only w.r.t. _plot_utils.py and make_figures.py.
Produces:
  fig-19-1-svd-three-steps.png   -- A = U Sigma V^T  decomposed into
                                    rotate (V^T) -> stretch (Sigma) -> rotate (U)
  fig-19-2-singular-value-compression.png -- image compression via truncated SVD
                                              (left: full vs k=1,3,10,20 ;
                                               right: singular value bar chart)
All in-figure labels are in English to avoid CJK font issues.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _plot_utils import arrow, grid, frame, save, GREEN, RED, BLUE, PURPLE, ORANGE, GRAY
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle

# ============================================================
# Figure 19.1 : SVD three-step decomposition for a non-symmetric matrix
# A = [[2,1],[0,3]]  (non-symmetric, det U = det Vt = +1  => pure rotations)
# ============================================================
A = np.array([[2., 1.],
              [0., 3.]])
U, s, Vt = np.linalg.svd(A)
Sigma = np.diag(s)
V = Vt.T

# the four panels:
#   (a) original A : gray unit grid -> blue grid after A
#   (b) step 1 : V^T  (rotate input so right singular vectors align to axes)
#   (c) step 2 : Sigma  (stretch along axes by sigma1, sigma2)
#   (d) step 3 : U  (rotate again to left singular vectors)
fig, axes = plt.subplots(1, 4, figsize=(18, 5.2))

def draw_panel(ax, M, title, subtitle, v1_color=PURPLE, v2_color=ORANGE,
               show_input_arrows=True):
    """Draw gray (identity) grid + M-transformed blue grid, plus the images
    of the two right singular directions v1, v2 under M.
    For the full A, also draw where v1,v2 land (= sigma_i * u_i)."""
    grid(ax, np.eye(2), GRAY, 0.30, '--')
    grid(ax, M, BLUE, 0.55, '-')
    if show_input_arrows:
        # draw the right singular vectors v1, v2 and their images under M
        # (these are the axes that stay perpendicular through the transform)
        Mv1 = M @ V[:, 0]
        Mv2 = M @ V[:, 1]
        # also draw v1,v2 in their original (input-space) position faintly
        arrow(ax, (0, 0), V[:, 0], v1_color, lw=2.0)
        arrow(ax, (0, 0), V[:, 1], v2_color, lw=2.0)
        ax.text(V[0, 0]*1.15-0.10, V[1, 0]*1.15, "v1", color=v1_color,
                fontsize=12, fontweight='bold')
        ax.text(V[0, 1]*1.15, V[1, 1]*1.15+0.08, "v2", color=v2_color,
                fontsize=12, fontweight='bold')
        # their images
        arrow(ax, (0, 0), Mv1, v1_color, lw=2.8)
        arrow(ax, (0, 0), Mv2, v2_color, lw=2.8)
    frame(ax, 4.6)
    ax.set_title(title, fontsize=12.5)
    ax.text(0.5, -0.16, subtitle, transform=ax.transAxes, ha='center',
            fontsize=10.5, color='#333')

# Panel 1: the full A
draw_panel(axes[0], A,
           "(0)  A  (any shear+stretch)",
           "gray = before,  blue = after one squeeze")
axes[0].text(0.5, -0.27,
             "the v1, v2 arrows stay perpendicular AFTER A:\n"
             "they are the right singular vectors (input axes)",
             transform=axes[0].transAxes, ha='center', fontsize=9, color='#555')

# Panel 2: step 1 -- V^T  (rotate input so v1,v2 align to coordinate axes)
draw_panel(axes[1], Vt,
           "(1)  V^T   =  rotate",
           "turn the input so v1, v2 line up with e1, e2")
axes[1].text(0.5, -0.27,
             "after V^T,  v1 -> e1=(1,0),  v2 -> e2=(0,1)\n"
             "grid is still a square (no stretching): pure rotation",
             transform=axes[1].transAxes, ha='center', fontsize=9, color='#555')

# Panel 3: step 2 -- Sigma (stretch along the now-aligned axes)
# but Sigma alone acts on the *aligned* frame, so we draw Sigma applied to identity grid,
# and show that sigma1*e1, sigma2*e2 are the (still perpendicular) stretched axes.
grid(axes[2], np.eye(2), GRAY, 0.30, '--')
grid(axes[2], Sigma, BLUE, 0.55, '-')
# the stretched axes
arrow(axes[2], (0, 0), (s[0], 0), PURPLE, lw=2.8)
arrow(axes[2], (0, 0), (0, s[1]), ORANGE, lw=2.8)
axes[2].text(s[0]*1.05, -0.35, f"sigma1={s[0]:.2f}", color=PURPLE,
             fontsize=11, fontweight='bold')
axes[2].text(-1.1, s[1]*1.08, f"sigma2={s[1]:.2f}", color=ORANGE,
             fontsize=11, fontweight='bold')
frame(axes[2], 4.6)
axes[2].set_title("(2)  Sigma  =  stretch along axes", fontsize=12.5)
axes[2].text(0.5, -0.16, "stretch e1 by sigma1, e2 by sigma2", transform=axes[2].transAxes,
             ha='center', fontsize=10.5, color='#333')
axes[2].text(0.5, -0.27,
             "axes stay perpendicular, just rescaled.\n"
             "this is the ONLY step that changes lengths",
             transform=axes[2].transAxes, ha='center', fontsize=9, color='#555')

# Panel 4: step 3 -- U  (rotate the stretched frame to the left singular vectors)
grid(axes[3], Sigma, GRAY, 0.30, '--')   # the stretched frame from step 2
M = U @ Sigma
grid(axes[3], M, BLUE, 0.55, '-')
# show where sigma1*e1 and sigma2*e2 land: = sigma1*u1, sigma2*u2
arrow(axes[3], (0, 0), s[0]*U[:, 0], PURPLE, lw=2.8)
arrow(axes[3], (0, 0), s[1]*U[:, 1], ORANGE, lw=2.8)
axes[3].text(s[0]*U[0, 0]-0.2, s[0]*U[1, 0]-0.55,
             f"sigma1*u1", color=PURPLE, fontsize=10.5, fontweight='bold')
axes[3].text(s[1]*U[0, 1]+0.1, s[1]*U[1, 1]+0.1,
             f"sigma2*u2", color=ORANGE, fontsize=10.5, fontweight='bold')
frame(axes[3], 4.6)
axes[3].set_title("(3)  U  =  rotate again", fontsize=12.5)
axes[3].text(0.5, -0.16, "rotate the stretched frame to the output axes", transform=axes[3].transAxes,
             ha='center', fontsize=10.5, color='#333')
axes[3].text(0.5, -0.27,
             "after U: result == A's grid (compare panel 0).\n"
             "u1, u2 = left singular vectors (output axes)",
             transform=axes[3].transAxes, ha='center', fontsize=9, color='#555')

fig.suptitle("SVD:  ANY 2x2 matrix  A = U Sigma V^T  =  rotate  ->  stretch  ->  rotate",
             fontsize=14, y=1.02)
save(fig, "fig-19-1-svd-three-steps.png")
print("saved fig-19-1-svd-three-steps.png")


# ============================================================
# Figure 19.2 : singular-value compression
#   left  : full image vs truncated-SVD reconstructions (k = 1, 3, 10, 20)
#   right : singular value bar chart (a few big, then a long tail of small)
# ============================================================
n = 96
yy, xx = np.mgrid[0:n, 0:n].astype(float)
cx = cy = n / 2
r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
theta = np.arctan2(yy - cy, xx - cx)
# a structured image with a smooth, decaying singular-value spectrum
img = (np.cos(3 * theta) * np.exp(-r / 30.0)
       + 0.5 * np.sin(5 * theta) * np.exp(-r / 25.0) + 1.0) / 2.0
img = np.clip(img, 0, 1)

U2, s2, Vt2 = np.linalg.svd(img, full_matrices=False)

ks = [1, 3, 10, 20]
fig = plt.figure(figsize=(14, 6.2))
# left: 2x3 grid of subplots
gs = fig.add_gridspec(2, 3, left=0.04, right=0.62, bottom=0.06, top=0.92, hspace=0.35, wspace=0.25)
ax_full = fig.add_subplot(gs[0, 0])
ax_full.imshow(img, cmap='gray', vmin=0, vmax=1)
ax_full.set_title("full image (rank 43)", fontsize=11)
ax_full.set_xticks([]); ax_full.set_yticks([])

recon_axes = []
for idx, k in enumerate(ks):
    r, c = divmod(idx + 1, 3)
    ax = fig.add_subplot(gs[r, c])
    recon = U2[:, :k] @ np.diag(s2[:k]) @ Vt2[:k]
    err = np.linalg.norm(img - recon) / np.linalg.norm(img)
    stored = k * (n + n + 1)
    frac = stored / img.size
    ax.imshow(recon, cmap='gray', vmin=0, vmax=1)
    ax.set_title(f"k={k}:  err={err:.3f},  data={frac*100:.0f}%", fontsize=10.5)
    ax.set_xticks([]); ax.set_yticks([])
    recon_axes.append(ax)

# right: singular value bar chart
ax_bar = fig.add_axes([0.66, 0.12, 0.32, 0.78])
nb = 30
ax_bar.bar(range(1, nb + 1), s2[:nb], color=BLUE, alpha=0.85, width=0.8)
ax_bar.axvline(10.5, color=RED, ls='--', lw=1.2)
ax_bar.text(11, s2[0]*0.85, "k=10 keeps 99.98%\nof the energy,\nonly ~21% of the data",
            color=RED, fontsize=10)
ax_bar.set_xlabel("singular value index  i", fontsize=11)
ax_bar.set_ylabel("sigma_i  (size)", fontsize=11)
ax_bar.set_title("singular values:  a few big,  a long tail of small", fontsize=11.5)
ax_bar.set_xlim(0.3, nb + 0.7)

fig.suptitle("Truncated SVD = image compression:  keep the k biggest singular values",
             fontsize=14, y=0.99)
save(fig, "fig-19-2-singular-value-compression.png")
print("saved fig-19-2-singular-value-compression.png")

# sanity: print the numbers that go into the prose
print()
print("matrix A = [[2,1],[0,3]]")
print("singular values:", s)
print("rotation of Vt (deg):", np.degrees(np.arctan2(Vt[1,0], Vt[0,0])))
print("rotation of U  (deg):", np.degrees(np.arctan2(U[1,0], U[0,0])))
print("image rank:", np.linalg.matrix_rank(img, tol=1e-9))
for k in [1, 3, 10, 20]:
    recon = U2[:, :k] @ np.diag(s2[:k]) @ Vt2[:k]
    err = np.linalg.norm(img - recon) / np.linalg.norm(img)
    en = (s2[:k]**2).sum() / (s2**2).sum()
    stored = k * (n + n + 1)
    print(f"  k={k:2d}  rel_err={err:.4f}  energy={en:.4f}  data_frac={stored/img.size:.3f}")
