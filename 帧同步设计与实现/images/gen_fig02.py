import matplotlib
matplotlib.use('Agg')
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

# ---- Simulate desync divergence (exponential amplification) ----
# client A starts at 0, client B differs by 1 ULP at frame 1; physical
# integration amplifies the gap roughly exponentially.
frames = np.array([0, 1, 10, 100, 1000, 2000], dtype=float)
# Use a piecewise picture: tiny -> small -> visible -> huge -> divergent
gap = np.array([0.0, 1e-8, 1e-4, 1e-1, 40.0, np.nan])  # 2000 = unbounded / dead
# for plotting on log-like, use log10 of (gap+eps)
log_gap = np.log10(gap[:-1] + 1e-30)  # frames 0..1000

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.5, 6.2),
                                gridspec_kw={'width_ratios': [1.15, 1.0]})
fig.patch.set_facecolor('white')

# ============ LEFT: divergence curve ============
ax1.set_facecolor('#FAFBFC')
frame_dense = np.array([0, 1, 5, 10, 30, 60, 100, 300, 600, 1000])
gap_dense = np.array([0, 1e-8, 2e-7, 1e-4, 5e-4, 2e-3, 1e-1, 1.5, 12, 40])

ax1.semilogy(frame_dense, gap_dense, '-o', color='#C0392B', lw=2.4,
             markersize=7, markerfacecolor='#E74C3C', markeredgecolor='white',
             markeredgewidth=1.2, label='|state_A - state_B|')

# shade "invisible" vs "visible" vs "catastrophic" regions
ax1.axhspan(1e-9, 1e-5, color='#E8F8F5', alpha=0.6)   # invisible
ax1.axhspan(1e-5, 1e-2, color='#FEF9E7', alpha=0.6)   # latent
ax1.axhspan(1e-2, 1e3,  color='#FDEDEC', alpha=0.6)   # catastrophic

ax1.text(8, 2e-7, 'INVISIBLE\n(player cannot tell)', fontsize=9, color='#117A65',
         ha='center', weight='bold')
ax1.text(8, 8e-4, 'LATENT\n(barely noticeable)', fontsize=9, color='#9A7D0A',
         ha='center', weight='bold')
ax1.text(700, 8, 'CATASTROPHIC\n(tanks in different worlds)',
         fontsize=9, color='#922B21', ha='center', weight='bold')

# annotate key frames
ax1.annotate('frame 1:\n1 ULP diff\n(0.00000001)', xy=(1, 1e-8), xytext=(120, 3e-9),
             fontsize=8.5, color='#1A5276',
             arrowprops=dict(arrowstyle='->', color='#1A5276', lw=1.0))
ax1.annotate('frame 100:\n~0.1 (tiny drift)', xy=(100, 1e-1), xytext=(400, 2e-3),
             fontsize=8.5, color='#1A5276',
             arrowprops=dict(arrowstyle='->', color='#1A5276', lw=1.0))
ax1.annotate('frame 1000:\n~40 units apart\n(bullets miss, paths split)',
             xy=(1000, 40), xytext=(420, 8),
             fontsize=8.5, color='#922B21',
             arrowprops=dict(arrowstyle='->', color='#922B21', lw=1.0))

ax1.set_xlabel('Logic frame index', fontsize=11, weight='bold')
ax1.set_ylabel('State divergence  |state_A - state_B|  (log scale)',
               fontsize=11, weight='bold')
ax1.set_title('Desync: Bit-by-Bit Accumulation\n(one ULP at frame 1 -> divergent worlds)',
              fontsize=12, weight='bold', color='#2C3E50')
ax1.set_xlim(-40, 1080)
ax1.set_ylim(1e-10, 200)
ax1.grid(True, which='both', linestyle=':', alpha=0.4)
ax1.legend(loc='lower right', fontsize=9, framealpha=0.95)

# ============ RIGHT: timeline ladder + self-heal? ============
ax2.set_facecolor('#FAFBFC')
ax2.axis('off')
ax2.set_xlim(0, 10)
ax2.set_ylim(0, 10)

ax2.text(5, 9.5, 'Timeline: From 1 Bit to Two Universes',
         ha='center', va='center', fontsize=12, weight='bold', color='#2C3E50')

rows = [
    ("frame 0",     "clients perfectly in sync",                 '#2E7D32'),
    ("frame 1",     "1 float differs by 1 ULP (~1e-8)",          '#117A65'),
    ("frame 10",    "gap ~1e-4  (screen looks identical)",       '#9A7D0A'),
    ("frame 100",   "gap ~0.1   (subtle position offset)",       '#B9770E'),
    ("frame 1000",  "gap ~40 units (shots miss, paths diverge)", '#C0392B'),
    ("frame 2000",  "client A's tank is dead, B's is alive",     '#7B241C'),
]
y = 8.5
for label, desc, color in rows:
    # marker
    ax2.plot(1.0, y, 'o', markersize=11, color=color, markeredgecolor='white',
             markeredgewidth=1.4, zorder=4)
    # connector line
    if y != 1.0:
        ax2.plot([1.0, 1.0], [y, y-1.1], color=color, lw=1.6, alpha=0.55, zorder=2)
    # label
    ax2.text(1.85, y, label, fontsize=10, weight='bold', color=color, va='center')
    ax2.text(3.7, y, desc, fontsize=9.5, color='#2C3E50', va='center')
    y -= 1.1

# self-heal banner
ax2.add_patch(FancyBboxPatch((0.5, 0.35), 9.0, 0.95,
              boxstyle="round,pad=0.04,rounding_size=0.10",
              linewidth=1.4, edgecolor='#C0392B', facecolor='#FDEDEC'))
ax2.text(5, 0.83,
         "NO SELF-HEAL: drift never cancels, no error signal,\n"
         "no crash -- two parallel universes keep running.",
         ha='center', va='center', fontsize=9.5, weight='bold', color='#922B21')

plt.tight_layout()
plt.savefig(r'c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-02-desync-accumulation.png',
            dpi=150, bbox_inches='tight', facecolor='white')
print("saved fig-02")
