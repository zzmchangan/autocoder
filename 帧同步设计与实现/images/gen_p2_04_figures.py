import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

# Shared palette (match series convention)
C_STATE  = '#1F6FB2'   # blue - state boxes
C_OP     = '#C0563E'   # orange - bit operations
C_OUT    = '#2E8B57'   # green - output / result
C_PAPER  = '#F7F9FC'
C_EDGE   = '#33415C'
C_TXT    = '#1B2733'
C_GREY   = '#6B7A8A'
C_LGREY  = '#D8DEE6'

# ============================================================
# Figure 1: Xorshift128+ state-advance flow
#   Two ulong state (s0, s1) -> 5 fixed bit ops -> output + next state
# ============================================================
fig, ax = plt.subplots(figsize=(13.5, 8.6))
ax.set_xlim(0, 13.5)
ax.set_ylim(0, 8.6)
ax.axis('off')
fig.patch.set_facecolor('white')

def rbox(x, y, w, h, fc, ec, lw=1.4, alpha=1.0, rounding=0.10):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                 boxstyle=f"round,pad=0.02,rounding_size={rounding}",
                 linewidth=lw, edgecolor=ec, facecolor=fc, alpha=alpha))

def chip(cx, cy, text, color, w=1.9, h=0.5, fs=9.5, tc='white', weight='bold'):
    rbox(cx-w/2, cy-h/2, w, h, color, color, lw=0.6, rounding=0.12)
    ax.text(cx, cy, text, ha='center', va='center', fontsize=fs, color=tc, weight=weight)

def arrow(x1, y1, x2, y2, color=C_EDGE, lw=1.6, style='-|>', ms=14, ls='-'):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2),
                 arrowstyle=style, mutation_scale=ms,
                 linewidth=lw, color=color, linestyle=ls,
                 shrinkA=2, shrinkB=2))

# ---- Title ----
ax.text(6.75, 8.2,
        'Xorshift128+: One Call to NextUInt64()',
        ha='center', va='center', fontsize=14, weight='bold', color=C_EDGE)
ax.text(6.75, 7.82,
        'Two ulong state  ->  5 fixed integer bit ops  ->  one output + next state (bit-identical on every platform)',
        ha='center', va='center', fontsize=10, style='italic', color='#5A6B7B')

# ---- Current state panel (top-left) ----
ax.text(0.45, 7.25, 'Current state', ha='left', va='center',
        fontsize=11, weight='bold', color=C_STATE)

rbox(0.45, 6.15, 2.7, 0.95, C_PAPER, C_STATE)
ax.text(1.80, 6.78, '_state0', ha='center', va='center', fontsize=10, weight='bold', color=C_STATE)
ax.text(1.80, 6.40, 'ulong  s0', ha='center', va='center', fontsize=9, color=C_TXT, family='monospace')

rbox(0.45, 5.05, 2.7, 0.95, C_PAPER, C_STATE)
ax.text(1.80, 5.68, '_state1', ha='center', va='center', fontsize=10, weight='bold', color=C_STATE)
ax.text(1.80, 5.30, 'ulong  s1', ha='center', va='center', fontsize=9, color=C_TXT, family='monospace')

# copy arrows
arrow(3.15, 6.62, 3.95, 6.62, color=C_GREY)
ax.text(3.55, 6.80, 'read', ha='center', va='center', fontsize=8, color=C_GREY, style='italic')
arrow(3.15, 5.52, 3.95, 5.52, color=C_GREY)
ax.text(3.55, 5.70, 'read', ha='center', va='center', fontsize=8, color=C_GREY, style='italic')

# ---- 5-step operation column (center) ----
ax.text(6.75, 7.25, '5 integer bit operations  (no multiply, no float)',
        ha='center', va='center', fontsize=11, weight='bold', color=C_OP)

# Step 1: output = s0 + s1  (Vigna's key improvement)
chip(6.75, 6.62, '(1)  result = s0 + s1', C_OUT, w=4.6, h=0.55, fs=10)
ax.text(9.25, 6.62, 'output (Vigna +: hides linear structure)',
        ha='left', va='center', fontsize=8.5, color=C_OUT, style='italic')

# Step 2: s1 ^= s0
chip(6.75, 5.80, '(2)  s1 = s1 ^ s0', C_OP, w=4.6, h=0.5, fs=9.5)
ax.text(9.25, 5.80, 'mix s0 into s1 (reversible XOR)',
        ha='left', va='center', fontsize=8.5, color=C_GREY, style='italic')

# Step 3: _state0 = rotl(s0,24) ^ s1 ^ (s1 << 16)
chip(6.75, 5.00, '(3)  rotl(s0,24) ^ s1 ^ (s1<<16)', C_OP, w=5.7, h=0.5, fs=9.3)
ax.text(9.75, 5.00, '-> new _state0',
        ha='left', va='center', fontsize=8.5, color=C_STATE, weight='bold')

# Step 4: _state1 = rotl(s1, 37)
chip(6.75, 4.20, '(4)  rotl(s1, 37)', C_OP, w=4.6, h=0.5, fs=9.5)
ax.text(9.25, 4.20, '-> new _state1',
        ha='left', va='center', fontsize=8.5, color=C_STATE, weight='bold')

# rotl helper note
ax.text(6.75, 3.55,
        'rotl(x, k)  =  (x << k) | (x >> (64-k))     -- circular left shift, no bits lost',
        ha='center', va='center', fontsize=8.7, color=C_GREY, family='monospace',
        bbox=dict(boxstyle='round,pad=0.3', fc='#EEF3F8', ec=C_LGREY, lw=0.8))

# arrows down the op chain
arrow(6.75, 6.34, 6.75, 6.06, color=C_OP, lw=1.4)
arrow(6.75, 5.54, 6.75, 5.26, color=C_OP, lw=1.4)
arrow(6.75, 4.74, 6.75, 4.46, color=C_OP, lw=1.4)

# ---- Outputs (bottom) ----
# output value
rbox(2.0, 1.95, 3.2, 0.95, '#EAF6EE', C_OUT)
ax.text(3.60, 2.62, 'return  result', ha='center', va='center',
        fontsize=10.5, weight='bold', color=C_OUT)
ax.text(3.60, 2.25, 'one pseudo-random ulong', ha='center', va='center',
        fontsize=8.8, color=C_TXT, style='italic')
arrow(6.75, 6.34, 3.60, 2.92, color=C_OUT, lw=1.6, ls='--')
ax.text(4.5, 4.55, 'output', ha='center', va='center', fontsize=8.5,
        color=C_OUT, style='italic', rotation=58)

# next state
rbox(7.6, 1.95, 4.7, 0.95, C_PAPER, C_STATE)
ax.text(9.95, 2.62, 'next state  (_state0, _state1)',
        ha='center', va='center', fontsize=10.5, weight='bold', color=C_STATE)
ax.text(9.95, 2.25, 'two ulong advanced deterministically',
        ha='center', va='center', fontsize=8.8, color=C_TXT, style='italic')
arrow(6.75, 3.94, 9.95, 2.92, color=C_STATE, lw=1.6, ls='--')
ax.text(8.6, 3.5, 'state advance', ha='center', va='center', fontsize=8.5,
        color=C_STATE, style='italic', rotation=-32)

# ---- Bottom banner: determinism guarantee ----
rbox(0.45, 0.55, 12.6, 1.0, '#FFF8E1', '#C9A227', lw=1.2, rounding=0.08)
ax.text(6.75, 1.22,
        'Determinism:  given the same (s0, s1), every output bit and every next-state bit',
        ha='center', va='center', fontsize=10, weight='bold', color='#7A5C00')
ax.text(6.75, 0.82,
        'are identical across CPU / .NET runtime  --  only xor / shift / add, no FMA, no float',
        ha='center', va='center', fontsize=9.5, color='#7A5C00')

# magic-constants footnote
ax.text(6.75, 0.18,
        'shift constants 24 / 16 / 37  are Vigna\'s BigCrush-optimal values -- do not modify',
        ha='center', va='center', fontsize=8, color=C_GREY, style='italic')

plt.tight_layout()
plt.savefig('fig-04-xorshift128-state.png', dpi=150, bbox_inches='tight', facecolor='white')
plt.close()
print('saved fig-04-xorshift128-state.png')

# ============================================================
# Figure 2: PRNG state-size comparison
#   LCG / Xorshift128+ / xorshift128 / xoshiro256 / Mersenne Twister
#   x = snapshot bytes (log), color = period, annotate LRandom's sweet spot
# ============================================================
import numpy as np

fig, ax = plt.subplots(figsize=(12.8, 7.4))
fig.patch.set_facecolor('white')

# data: name, state bytes, period (as log2), label color
algos = [
    ('LCG\n(1 x int64)',            8,    64,  C_GREY),
    ('Xorshift128+\n(LRandom)',     16,   128, C_OUT),
    ('xorshift128\n(4 x int32)',    16,   128, C_OP),
    ('xoshiro256**\n(4 x int64)',   32,   256, '#7A4F9A'),
    ('Mersenne Twister\n(Python/JS default)', 2496, 19937, '#B8860B'),
]

names  = [a[0] for a in algos]
bytes_ = np.array([a[1] for a in algos], dtype=float)
period = np.array([a[2] for a in algos], dtype=float)
colors = [a[3] for a in algos]

xpos = np.arange(len(algos))
# log scale for bytes to fit MT's 2496
bar_h = np.log10(bytes_)

bars = ax.bar(xpos, bar_h, width=0.62, color=colors, alpha=0.88,
              edgecolor=C_EDGE, linewidth=1.1)

# highlight LRandom bar with thicker edge
bars[1].set_edgecolor(C_OUT)
bars[1].set_linewidth(2.8)

# value labels on top of each bar (real byte count)
for i, b in enumerate(bytes_):
    if b >= 1024:
        label = f'{int(b)} B\n({b/1024:.1f} KB)'
    else:
        label = f'{int(b)} B'
    ax.text(i, bar_h[i] + 0.12, label, ha='center', va='bottom',
            fontsize=10.5, weight='bold', color=C_TXT)

# period annotation under each bar
for i, p in enumerate(period):
    ax.text(i, -0.18, f'period  2^{int(p)}', ha='center', va='top',
            fontsize=9, color='#444', family='monospace')

ax.set_xticks(xpos)
ax.set_xticklabels(names, fontsize=10.5, color=C_TXT)
ax.set_ylabel('Snapshot size  (log10 bytes)', fontsize=11, color=C_TXT)
ax.set_ylim(-0.6, max(bar_h)*1.25)
ax.set_yticks([0, 1, 2, 3, 4])
ax.set_yticklabels(['1 B', '10 B', '100 B', '1 KB', '10 KB'], fontsize=9, color=C_GREY)

ax.set_title('PRNG State Size vs  LRandom = 16 bytes, period 2^128  (the sweet spot)',
             fontsize=13.5, weight='bold', color=C_EDGE, pad=14)

# annotate LRandom sweet spot
ax.annotate('LRandom:  2 x ulong = 16 B\nrollback ~ free\n(SaveState writes 16 B per snapshot)',
            xy=(1, bar_h[1]), xytext=(2.55, 3.55),
            fontsize=10, color=C_OUT, weight='bold', ha='left',
            arrowprops=dict(arrowstyle='-|>', color=C_OUT, lw=1.8,
                            connectionstyle='arc3,rad=-0.25'),
            bbox=dict(boxstyle='round,pad=0.45', fc='#EAF6EE', ec=C_OUT, lw=1.3))

# annotate MT bloat
ax.annotate('Mersenne Twister:\n624 x int32 = 2496 B\nsnapshot stores a whole array',
            xy=(4, bar_h[4]), xytext=(2.4, 3.0),
            fontsize=9.5, color='#8B6914', ha='left',
            arrowprops=dict(arrowstyle='-|>', color='#B8860B', lw=1.5,
                            connectionstyle='arc3,rad=0.25'),
            bbox=dict(boxstyle='round,pad=0.4', fc='#FFF8E1', ec='#B8860B', lw=1.1))

# footnote
ax.text(0.5, -0.22,
        'At SnapshotInterval = 1, every frame writes the PRNG state -- 16 B vs 2496 B is a real cost in memory & bandwidth.',
        transform=ax.transAxes, ha='left', va='top', fontsize=9, style='italic',
        color=C_GREY)

for spine in ['top', 'right']:
    ax.spines[spine].set_visible(False)
ax.spines['left'].set_color(C_LGREY)
ax.spines['bottom'].set_color(C_LGREY)
ax.tick_params(colors=C_GREY)

plt.tight_layout()
plt.savefig('fig-04-prng-state-size.png', dpi=150, bbox_inches='tight', facecolor='white')
plt.close()
print('saved fig-04-prng-state-size.png')
