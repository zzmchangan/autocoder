"""fig-6-17-gen.py · 第 17 章 复变函数 配图脚本

图 17.1  Complex multiplication = rotation + scaling
图 17.2  Rigidity of holomorphic functions: local -> global (analytic continuation)

图内标注一律英文; 严禁改 _plot_utils.py.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_curve, marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ============================================================
# 图 17.1  Complex multiplication = rotation + scaling
# ============================================================
fig, axes = plt.subplots(1, 2, figsize=(11, 4.8))

def draw_arrow(ax, z, color, label, ls='-'):
    ax.annotate('', xy=(z.real, z.imag), xytext=(0, 0),
                arrowprops=dict(arrowstyle='->', color=color, lw=2.2,
                                linestyle=ls))
    ax.text(z.real*1.05 + 0.05, z.imag*1.05, label, color=color, fontsize=10)

# --- left: z1 = sqrt(2) e^{i pi/4} (mod sqrt2, arg 45 deg) ---
ax = axes[0]
setup_axes(ax, xlim=(-0.5, 3.2), ylim=(-0.5, 2.5), equal=True,
           xlabel='Re', ylabel='Im',
           title='z1 = sqrt(2) e^{i pi/4} : length sqrt(2), angle 45 deg')
# also draw the unit circle for reference
th = np.linspace(0, 2*np.pi, 200)
ax.plot(np.cos(th), np.sin(th), color='gray', lw=0.6, ls=':', alpha=0.5)
draw_arrow(ax, 1+1j, BLUE, r'$z_1$')
# the angle arc
arc = np.linspace(0, np.pi/4, 50)
ax.plot(0.6*np.cos(arc), 0.6*np.sin(arc), color=RED, lw=1.2)
ax.text(0.65, 0.18, r'$\pi/4$', color=RED, fontsize=9)

# --- right: z1 * z2 where z2 = 2 e^{i pi/6} -> product = 2sqrt2 e^{i 5pi/12} ---
ax = axes[1]
setup_axes(ax, xlim=(-0.5, 3.8), ylim=(-0.5, 3.2), equal=True,
           xlabel='Re', ylabel='Im',
           title=r'z1 * z2 : length * length, angle + angle')
ax.plot(np.cos(th), np.sin(th), color='gray', lw=0.6, ls=':', alpha=0.5)
z1 = 1 + 1j
z2 = np.sqrt(3) + 1j
prod = z1 * z2
draw_arrow(ax, z1, BLUE, r'$z_1$ (mod sqrt2, arg pi/4)', ls='--')
draw_arrow(ax, z2, GREEN, r'$z_2$ (mod 2, arg pi/6)', ls='--')
draw_arrow(ax, prod, RED, r'$z_1 z_2$ (mod 2sqrt2, arg 5pi/12)')
# show that the product angle = sum of angles
arc1 = np.linspace(0, np.angle(prod), 50)
ax.plot(0.4*np.cos(arc1), 0.4*np.sin(arc1), color=RED, lw=1.2)
ax.text(0.45, 0.05, r'$\pi/4+\pi/6$', color=RED, fontsize=9)

fig.suptitle('Complex multiplication = rotation + scaling', fontsize=12, y=1.02)
save(fig, 'fig-6-17-1-complex-mult.png')


# ============================================================
# 图 17.2  Rigidity: local values fix the whole region
# ============================================================
fig, axes = plt.subplots(1, 2, figsize=(11, 4.8))

# --- left: real functions can agree on a half-line yet differ elsewhere ---
ax = axes[0]
x = np.linspace(-2, 2, 400)
f0 = np.zeros_like(x)
g = np.where(x > 0, np.exp(-1.0/np.maximum(x, 1e-9)**2), 0.0)
ax.plot(x, f0, color=BLUE, lw=2.2, label=r'$f(x)=0$')
ax.plot(x, g, color=RED, lw=2.2, ls='--', label=r'$g(x)=0$ if $x\leq0$, $e^{-1/x^2}$ if $x>0$')
ax.axvspan(-2, 0, color=ORANGE, alpha=0.15, label='agree here (a whole half-line)')
setup_axes(ax, xlim=(-2, 2), ylim=(-0.1, 0.9), xlabel='x (real)', ylabel='',
           title='Real functions: agree on a half-line, differ elsewhere')
ax.legend(loc='upper left', fontsize=8)
ax.text(0.1, 0.55, 'but diverge here', color=RED, fontsize=9)

# --- right: holomorphic rigidity -- a small disk pins the whole region ---
ax = axes[1]
# Draw a big region (ellipse-ish) and a small disk inside it
th = np.linspace(0, 2*np.pi, 200)
# big region boundary
ax.plot(2.5*np.cos(th), 1.6*np.sin(th), color=BLUE, lw=2,
        label='domain D (region of holomorphy)')
# the small "seed" disk where values are known
disk_x = -1.0 + 0.5*np.cos(th)
disk_y = 0.0 + 0.5*np.sin(th)
ax.fill(disk_x, disk_y, color=ORANGE, alpha=0.5, label='known values here (a tiny disk)')
ax.plot(disk_x, disk_y, color=ORANGE, lw=1.2)
# arrows showing the values "propagate" outward uniquely
for ang in np.linspace(0, 2*np.pi, 8)[:-1]:
    p0 = np.array([-1.0 + 0.5*np.cos(ang), 0.5*np.sin(ang)])
    p1 = np.array([-1.0 + 2.3*np.cos(ang*0.6+0.3), 1.4*np.sin(ang*0.6+0.3)])
    ax.annotate('', xy=p1, xytext=p0,
                arrowprops=dict(arrowstyle='->', color=PURPLE, lw=1.0, alpha=0.7))
setup_axes(ax, xlim=(-3, 3), ylim=(-2, 2), equal=True, xlabel='Re', ylabel='Im',
           title='Holomorphic: a tiny disk fixes the WHOLE domain')
ax.legend(loc='upper right', fontsize=8)
ax.text(-1.0, 0.0, 'seed', color=RED, fontsize=9, ha='center')
ax.text(0.6, 0.0, 'unique\nextension', color=PURPLE, fontsize=9, ha='center')

fig.suptitle('Rigidity of holomorphic functions (real functions cannot do this)',
             fontsize=12, y=1.02)
save(fig, 'fig-6-17-2-rigidity.png')

print('OK: generated fig-6-17-1 and fig-6-17-2')
