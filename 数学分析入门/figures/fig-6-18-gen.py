"""fig-6-18-gen.py · 第 18 章 留数与解析延拓 配图脚本

图 18.1  Contour integral: real axis + upper-half-plane semicircle -> closed gamma
         enclosing the pole at z=i
图 18.2  Computing a real integral by residues: 1/(1+x^2) -> pi

图内标注一律英文; 严禁改 _plot_utils.py.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ============================================================
# 图 18.1  Contour: real axis + upper semicircle enclosing z=i
# ============================================================
fig, axes = plt.subplots(1, 2, figsize=(11, 4.8))

# --- left: the contour in the complex plane ---
ax = axes[0]
R = 3.0
# real axis segment
ax.plot([-R, R], [0, 0], color=BLUE, lw=2.6, label='real-axis segment (the integral we want)')
# upper semicircle
th = np.linspace(0, np.pi, 200)
ax.plot(R*np.cos(th), R*np.sin(th), color=ORANGE, lw=2.6, ls='--',
        label='large semicircle C_R (contribution -> 0 as R -> inf)')
# arrows indicating direction
ax.annotate('', xy=(1.5, 0), xytext=(0.5, 0),
            arrowprops=dict(arrowstyle='->', color=BLUE, lw=2))
ax.annotate('', xy=(-R*0.6, R*0.05), xytext=(R*0.05, R*0.95),
            arrowprops=dict(arrowstyle='->', color=ORANGE, lw=2))
# the enclosed pole z = i
marker(ax, 0, 1, color=RED, text=r'pole $z=i$', dx=0.15, dy=0.15)
# the pole z=-i (outside the contour, in lower half-plane)
marker(ax, 0, -1, color=PURPLE, text=r'$z=-i$ (not enclosed)', dx=0.15, dy=-0.25)
setup_axes(ax, xlim=(-3.5, 3.5), ylim=(-2, 3.5), equal=True,
           xlabel='Re', ylabel='Im',
           title='Contour gamma = real axis + upper semicircle')
ax.legend(loc='upper right', fontsize=8)
ax.text(0.0, 2.2, r'$\oint_\gamma f\,dz = 2\pi i\,\mathrm{Res}(f, i)$',
        ha='center', color=RED, fontsize=11,
        bbox=dict(boxstyle='round', facecolor='#fff0f0', edgecolor=RED, lw=0.6))

# --- right: the integrand 1/(1+x^2) on the real line, area = pi ---
ax = axes[1]
x = np.linspace(-6, 6, 600)
y = 1/(1+x**2)
ax.plot(x, y, color=BLUE, lw=2.4, label=r'$1/(1+x^2)$')
ax.fill_between(x, 0, y, where=(np.abs(x) <= 6), color=ORANGE, alpha=0.35)
setup_axes(ax, xlim=(-6, 6), ylim=(0, 1.15), xlabel='x', ylabel='',
           title=r'$\int_{-\infty}^{\infty} \frac{dx}{1+x^2} = \pi$')
ax.axhline(0, color='gray', lw=0.6)
ax.legend(loc='upper right', fontsize=9)
ax.text(-3.5, 0.6, r'area $= \pi$', fontsize=12, color=RED)
# annotate the residue-route result
ax.text(0.0, 0.92, r'$= 2\pi i \cdot \frac{1}{2i} = \pi$  (by residues)',
        ha='center', color=PURPLE, fontsize=10,
        bbox=dict(boxstyle='round', facecolor='#f4f0fa', edgecolor=PURPLE, lw=0.6))

fig.suptitle('Real integral killed by one residue in the complex plane',
             fontsize=12, y=1.02)
save(fig, 'fig-6-18-1-contour-residue.png')


# ============================================================
# 图 18.2  Step-by-step residue computation visualized
# ============================================================
fig, axes = plt.subplots(1, 2, figsize=(11, 4.8))

# --- left: complex plane with both poles, only z=i enclosed ---
ax = axes[0]
R = 2.5
ax.plot([-R, R], [0, 0], color=BLUE, lw=2.4)
th = np.linspace(0, np.pi, 200)
ax.plot(R*np.cos(th), R*np.sin(th), color=ORANGE, lw=2.4, ls='--')
# shade the enclosed region lightly
ax.fill(np.concatenate([[R], R*np.cos(th), [-R]]),
        np.concatenate([[0], R*np.sin(th), [0]]),
        color=ORANGE, alpha=0.10)
marker(ax, 0, 1, color=RED, text=r'$z=i$  Res$=\frac{1}{2i}$', dx=0.2, dy=0.2)
marker(ax, 0, -1, color=PURPLE, text=r'$z=-i$ (outside $\gamma$)', dx=0.2, dy=-0.3)
setup_axes(ax, xlim=(-3, 3), ylim=(-2, 3), equal=True,
           xlabel='Re', ylabel='Im',
           title='Only z=i is inside gamma; only its residue counts')
ax.text(0, 2.3, r'$\oint_\gamma \frac{dz}{1+z^2} = 2\pi i\cdot \frac{1}{2i} = \pi$',
        ha='center', color=RED, fontsize=11,
        bbox=dict(boxstyle='round', facecolor='#fff0f0', edgecolor=RED, lw=0.6))

# --- right: comparison table-as-plot: Riemann vs Residue ---
ax = axes[1]
ax.axis('off')
txt = (
    "Two routes, same answer:  $\\int_{-\\infty}^{\\infty}\\frac{dx}{1+x^2}=\\pi$\n\n"
    "Route A (real analysis, substitution $x=\\tan\\theta$):\n"
    "    $\\int \\frac{dx}{1+x^2}=\\arctan x\\,|_{-\\infty}^{\\infty}=\\pi/2-(-\\pi/2)=\\pi$\n\n"
    "Route B (residue theorem, change battlefield):\n"
    "    pole at $z=i$,  $\\mathrm{Res}(1/(1+z^2),\\,i)=1/(2i)$\n"
    "    $\\int = 2\\pi i\\cdot \\frac{1}{2i}=\\pi$\n\n"
    "For $\\int \\frac{dx}{1+x^3}$, $\\int \\frac{\\cos x}{1+x^2}\\,dx$, ...\n"
    "Route B is often the ONLY practical route."
)
ax.text(0.05, 0.95, txt, transform=ax.transAxes, fontsize=11,
        verticalalignment='top', family='monospace',
        bbox=dict(boxstyle='round', facecolor='#f7f7fa', edgecolor=PURPLE, lw=0.8))
ax.set_title('Residue theorem: same answer, often the only practical route',
             fontsize=11)

fig.suptitle('Computing a real integral by residues (change of battlefield)',
             fontsize=12, y=1.02)
save(fig, 'fig-6-18-2-integral-by-residue.png')

print('OK: generated fig-6-18-1 and fig-6-18-2')
