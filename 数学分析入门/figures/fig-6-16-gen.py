"""fig-6-16-gen.py · 第 16 章 勒贝格积分 配图脚本

图 16.1  Riemann (vertical slicing) vs Lebesgue (horizontal slicing)
图 16.2  A pathological function Riemann can't integrate but Lebesgue can
         (Dirichlet-like) + Cantor-set measure shrinking to 0

图内标注一律英文; 严禁改 _plot_utils.py.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (plot_fn, plot_curve, riemann_rects, marker,
                         setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE)
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


# ============================================================
# 图 16.1  Riemann (vertical) vs Lebesgue (horizontal)
# ============================================================
fig, axes = plt.subplots(1, 2, figsize=(11, 4.6))

# A smooth bump f(x) = sin(pi x) on [0,1], positive
f = lambda x: np.sin(np.pi * x)

# ---- left: Riemann, vertical slices ----
ax = axes[0]
riemann_rects(ax, f, 0.0, 1.0, n=14, pos='mid', color=ORANGE, alpha=0.55,
              edge=ORANGE)
plot_fn(ax, f, 0, 1, color=BLUE, lw=2.4, label=r'$f(x)=\sin(\pi x)$')
# a couple of vertical dashed lines to emphasize the vertical cut
for xi in np.linspace(0, 1, 8)[1:-1]:
    ax.plot([xi, xi], [0, f(xi)], color=RED, lw=0.8, ls=':', alpha=0.6)
setup_axes(ax, xlim=(0, 1), ylim=(0, 1.15), xlabel='x  (slice along x-axis)',
           ylabel='f(x)', title='Riemann: slice VERTICALLY (along x)')
ax.annotate('fixed x position\nsample f(x), width dx',
            xy=(0.5, f(0.5)), xytext=(0.55, 0.35),
            color=RED, fontsize=9,
            arrowprops=dict(arrowstyle='->', color=RED, lw=1))
ax.legend(loc='upper right', fontsize=9)

# ---- right: Lebesgue, horizontal slices ----
ax = axes[1]
plot_fn(ax, f, 0, 1, color=BLUE, lw=2.4, label=r'$f(x)=\sin(\pi x)$')
# horizontal bands at several y levels; width = measure of {x: f(x) in [y,y+dy]}
ys = np.linspace(0.05, 0.95, 10)
dy = 0.06
for y in ys:
    # find x-values whose f(x) falls in [y, y+dy]; for sin(pi x) on [0,1]
    # f is symmetric; measure = |x2-x1| where sin(pi x1)=y, sin(pi x2)=y+dy (both branches)
    x1_lo = np.arcsin(y) / np.pi
    x1_hi = np.arcsin(min(y + dy, 1.0)) / np.pi
    x2_lo = 1 - x1_hi
    x2_hi = 1 - x1_lo
    # draw the two horizontal segments
    ax.add_patch(Rectangle((x1_lo, y), x1_hi - x1_lo, dy,
                           facecolor=ORANGE, alpha=0.5, edgecolor=ORANGE, lw=0.4))
    ax.add_patch(Rectangle((x2_lo, y), x2_hi - x2_lo, dy,
                           facecolor=ORANGE, alpha=0.5, edgecolor=ORANGE, lw=0.4))
# horizontal dashed lines to emphasize horizontal cut
for y in [0.2, 0.5, 0.8]:
    ax.plot([0, 1], [y, y], color=RED, lw=0.8, ls=':', alpha=0.5)
setup_axes(ax, xlim=(0, 1), ylim=(0, 1.15),
           xlabel='x  (measure of level set)',
           ylabel='y = f(x)', title='Lebesgue: slice HORIZONTALLY (along y)')
ax.annotate('fixed value y\nmeasure of {x : f(x) ~= y}',
            xy=(0.06, 0.5), xytext=(0.30, 0.55),
            color=RED, fontsize=9,
            arrowprops=dict(arrowstyle='->', color=RED, lw=1))
ax.legend(loc='upper right', fontsize=9)

fig.suptitle('Same area, two slicing strategies', fontsize=12, y=1.02)
save(fig, 'fig-6-16-1-riemann-vs-lebesgue.png')


# ============================================================
# 图 16.2  Pathological function + Cantor measure -> 0
# ============================================================
fig, axes = plt.subplots(1, 2, figsize=(11, 4.6))

# ---- left: Dirichlet-like function, Riemann fails, Lebesgue = 0 ----
ax = axes[0]
# Dirichlet is 1 on Q, 0 on irrational. Floats are all rational, so we
# visualize a faithful proxy: f(x)=1 if x is a "dyadic rational of low order"
# We instead draw the *idea*: scatter 1s densely (rationals) and 0s (irrationals).
xx = np.linspace(0, 1, 4000)
# proxy: f=1 on a dense-but-measure-zero set (rationals approximated by m/2^k with small k)
def dirichlet_proxy(x):
    # f=1 when x is a low-denominator rational (a measure-zero subset in the limit)
    # use np close to k/2^10 grid
    g = np.round(x * 1024) / 1024
    return (np.abs(x - g) < 1e-9).astype(float)
vals = dirichlet_proxy(xx)
ax.scatter(xx[vals > 0.5], np.ones(np.sum(vals > 0.5)), s=2, color=RED,
           label='f=1 on a dense measure-0 set')
ax.scatter(xx[vals <= 0.5], np.zeros(np.sum(vals <= 0.5)), s=2, color=GREEN,
           label='f=0 elsewhere')
setup_axes(ax, xlim=(0, 1), ylim=(-0.3, 1.3),
           xlabel='x in [0,1]', ylabel='f(x)',
           title='Dirichlet-type: Riemann fails, Lebesgue = 0')
ax.text(0.5, -0.18,
        'Riemann: sample jumps 0<->1, no limit\n'
        r'Lebesgue: $1\cdot m(Q) + 0\cdot m(\mathrm{Irr}) = 1\cdot 0 + 0\cdot 1 = 0$',
        ha='center', va='top', fontsize=9, color=PURPLE,
        bbox=dict(boxstyle='round', facecolor='#f4f0fa', edgecolor=PURPLE, lw=0.6))
ax.legend(loc='upper center', fontsize=8)

# ---- right: Cantor set measure -> 0 ----
ax = axes[1]
steps = np.arange(1, 16)
removed_each = 2.0 ** (steps - 1) / 3.0 ** steps
remaining = 1.0 - np.cumsum(removed_each)
ax.plot(steps, remaining, 'o-', color=BLUE, lw=2, label='measure of Cantor set')
ax.axhline(0, color=RED, ls='--', lw=1, label='limit = 0')
for n, r in zip([1, 3, 6, 10, 15], remaining[[0, 2, 5, 9, 14]]):
    ax.annotate(f'{r:.4g}', (n, r), xytext=(n, r + 0.06),
                color=PURPLE, fontsize=8, ha='center')
setup_axes(ax, xlim=(0.5, 15.5), ylim=(-0.05, 1.05),
           xlabel='iteration step n', ylabel='remaining measure',
           title='Cantor set: uncountably many points, measure -> 0')
ax.legend(loc='upper right', fontsize=9)

fig.suptitle('When Riemann breaks, Lebesgue (and measure) keep working',
             fontsize=12, y=1.02)
save(fig, 'fig-6-16-2-pathological-and-cantor.png')

print('OK: generated fig-6-16-1 and fig-6-16-2')
