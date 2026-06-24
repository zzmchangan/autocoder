"""P4-11 扩充配图生成脚本(只增不删,不碰原 fig-4-11-gen.py).

两张图:
  fig-4-11-4-euler-formula.png        —— 欧拉公式:把 ix 代入 e^x 的级数,实部=cos x, 虚部=sin x
                                        (e^(i pi) + 1 = 0)
  fig-4-11-5-complex-singularity.png  —— 1/(1+x^2) 收敛半径 R=1 的复平面解释:
                                        复奇点 ±i 距展开点 0 恰为 1,实轴上看不见但决定半径
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, plot_curve, band, marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt
from math import factorial as _fact
FACT = lambda k: _fact(int(k))

# ========== 图 4 · 欧拉公式:e^(ix) = cos x + i sin x(级数分奇偶项) ==========
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11.6, 4.6))

# 左:实部 = cos x 的级数  1 - x^2/2! + x^4/4! - ...
xv = np.linspace(-2 * np.pi, 2 * np.pi, 500)
plot_fn(axL, np.cos, -2 * np.pi, 2 * np.pi, color=BLUE, lw=2.6, label=r'$\cos x$  (exact)')
def cos_even_partial(N):
    s = np.zeros_like(xv)
    k = 0; sign = 1
    while k <= N:
        s += sign * xv**k / FACT(k)
        k += 2; sign *= -1
    return s
for N, c in [(0, ORANGE), (2, PURPLE), (4, GREEN), (10, RED)]:
    axL.plot(xv, cos_even_partial(N), color=c, lw=1.6, ls='--',
             label=r'$1 - x^2/2! + \cdots$  up to $x^{%d}$' % N)
axL.text(-6.0, 0.55,
         r'real part of $e^{ix}$:' + '\n' +
         r'$1 - x^2/2! + x^4/4! - \cdots = \cos x$',
         fontsize=9.5, color=RED,
         bbox=dict(boxstyle='round', fc='white', ec=RED, alpha=0.9))
setup_axes(axL, xlim=(-2 * np.pi, 2 * np.pi), ylim=(-1.6, 1.6),
           title=r"Real part:  $\mathrm{Re}\,e^{ix} = \cos x$",
           xlabel='x', ylabel='y', grid=True)
axL.legend(fontsize=8.5, loc='upper right')

# 右:虚部 = sin x 的级数  x - x^3/3! + x^5/5! - ...
plot_fn(axR, np.sin, -2 * np.pi, 2 * np.pi, color=BLUE, lw=2.6, label=r'$\sin x$  (exact)')
def sin_odd_partial(N):
    s = np.zeros_like(xv)
    k = 1; sign = 1
    while k <= N:
        s += sign * xv**k / FACT(k)
        k += 2; sign *= -1
    return s
for N, c in [(1, ORANGE), (3, PURPLE), (5, GREEN), (11, RED)]:
    axR.plot(xv, sin_odd_partial(N), color=c, lw=1.6, ls='--',
             label=r'$x - x^3/3! + \cdots$  up to $x^{%d}$' % N)
axR.text(-6.0, 0.55,
         r'imaginary part of $e^{ix}$:' + '\n' +
         r'$x - x^3/3! + x^5/5! - \cdots = \sin x$' + '\n\n' +
         r'$\Rightarrow e^{ix} = \cos x + i\sin x$' + '\n' +
         r'$x=\pi:\; e^{i\pi}+1 = 0$',
         fontsize=9, color=RED,
         bbox=dict(boxstyle='round', fc='white', ec=RED, alpha=0.9))
setup_axes(axR, xlim=(-2 * np.pi, 2 * np.pi), ylim=(-1.6, 1.6),
           title=r"Imaginary part:  $\mathrm{Im}\,e^{ix} = \sin x$",
           xlabel='x', ylabel='y', grid=True)
axR.legend(fontsize=8.5, loc='upper right')
save(fig, 'fig-4-11-4-euler-formula.png')

# ========== 图 5 · 收敛半径的复平面解释:1/(1+x^2) 的奇点在 ±i ==========
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11.6, 4.8))

# 左:实轴视角 —— 1/(1+x^2) 处处光滑,看不见任何奇点,但泰勒级数 R=1
xb = np.linspace(-2, 2, 500)
plot_fn(axL, lambda t: 1 / (1 + t**2), -2, 2, color=BLUE, lw=2.6,
        label=r'$\frac{1}{1+x^2}$  (smooth for all real $x$)')
def taylor(N):
    s = np.zeros_like(xb)
    for k in range(N + 1):
        s += (-1)**k * xb**(2 * k)
    return s
for N, c in [(1, ORANGE), (3, PURPLE), (10, GREEN)]:
    axL.plot(xb, taylor(N), color=c, lw=1.4, ls='--', label='Taylor N=%d' % N)
axL.axvline(1, color=RED, ls=':', lw=1.2)
axL.axvline(-1, color=RED, ls=':', lw=1.2)
axL.text(-1.95, 1.6,
         'real axis view:  no singularity visible\n' +
         'yet Taylor diverges for $|x|>1$\n' +
         r'radius $R=1$  is a mystery here',
         fontsize=9, color=RED,
         bbox=dict(boxstyle='round', fc='white', ec=RED, alpha=0.9))
setup_axes(axL, xlim=(-2, 2), ylim=(-2, 5),
           title=r"Real view:  $1/(1+x^2)$ smooth, yet $R=1$  (why?)",
           xlabel='x', ylabel='value', grid=True)
axL.legend(fontsize=8, loc='upper right')

# 右:复平面视角 —— 奇点在 ±i,展开点 0 到奇点距离 = 1 = R
axR.set_aspect('equal')
# 实轴
axR.axhline(0, color='black', lw=1.0, alpha=0.5)
axR.axvline(0, color='black', lw=1.0, alpha=0.5)
# 收敛圆 |z|=1
theta = np.linspace(0, 2 * np.pi, 200)
axR.plot(np.cos(theta), np.sin(theta), color=GREEN, lw=2.0, ls='--',
         label=r'convergence disk  $|z|<1$  ($R=1$)')
# 展开点 0
axR.scatter([0], [0], color=BLUE, s=60, zorder=6)
axR.annotate('expansion point  $z=0$', (0, 0), xytext=(0.35, -0.55),
             color=BLUE, fontsize=9.5,
             arrowprops=dict(arrowstyle='->', color=BLUE, lw=1))
# 奇点 +i 和 -i
axR.scatter([0, 0], [1, -1], color=RED, s=80, zorder=7, marker='X')
axR.annotate(r'singularity  $z=+i$' + '\n(runs away to infinity)', (0, 1),
             xytext=(0.5, 1.25), color=RED, fontsize=9.5,
             arrowprops=dict(arrowstyle='->', color=RED, lw=1))
axR.annotate(r'singularity  $z=-i$', (0, -1),
             xytext=(0.5, -1.55), color=RED, fontsize=9.5,
             arrowprops=dict(arrowstyle='->', color=RED, lw=1))
# 距离标线 0 -> +i
axR.plot([0, 0], [0, 1], color=ORANGE, lw=2.4)
axR.text(-0.18, 0.5, r'$|i-0|=1$', color=ORANGE, fontsize=11, rotation=90, va='center')
axR.text(-1.4, 1.35,
         'complex view:  singularities at $\\pm i$\n' +
         'distance from 0  $= 1$  $= R$\n' +
         'the real axis cannot see them,\n' +
         'but they decide the radius.',
         fontsize=9.5, color=RED,
         bbox=dict(boxstyle='round', fc='white', ec=RED, alpha=0.9))
axR.set_xlim(-1.7, 1.7)
axR.set_ylim(-1.8, 1.8)
axR.set_xlabel(r'$\mathrm{Re}\,z$', fontsize=10)
axR.set_ylabel(r'$\mathrm{Im}\,z$', fontsize=10)
axR.set_title(r"Complex view:  singularities $\pm i$  set  $R=1$", fontsize=11)
axR.legend(fontsize=8.5, loc='lower right')
axR.grid(True, alpha=0.2)
save(fig, 'fig-4-11-5-complex-singularity.png')

# 数值自检
print('check: cos_even_partial(10) @x=pi vs cos(pi):  %.6f vs %.6f' %
      (cos_even_partial(10)[np.argmin(np.abs(xv - np.pi))], np.cos(np.pi)))
print('check: sin_odd_partial(11) @x=pi/2 vs sin(pi/2):  %.6f vs %.6f' %
      (sin_odd_partial(11)[np.argmin(np.abs(xv - np.pi/2))], np.sin(np.pi/2)))
print('check: |+i - 0| = 1 = R  (singularity distance decides radius)')
print('done · 2 figures generated')
