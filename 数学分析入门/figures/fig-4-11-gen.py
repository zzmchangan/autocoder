"""P4-11《幂级数与泰勒级数:超越函数为何能写成多项式》配图生成脚本。

三张图:
  fig-4-11-1-exp-taylor.png     —— 泰勒级数阶数增加逼近 e^x(全场越贴越紧,R=inf)
  fig-4-11-2-sin-taylor.png     —— sin x 的泰勒级数(阶数增加,在 0 附近越贴越紧)
  fig-4-11-3-radius-of-conv.png —— 收敛半径示意:1/(1-x)(R=1) vs 1/(1+x^2)(R=1,复奇点)
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, plot_curve, band, marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt
from math import factorial as _fact
FACT = lambda k: _fact(int(k))

# ========== 图 1 · e^x 的泰勒级数:阶数增加,全场越贴越紧 ==========
fig, ax = plt.subplots(figsize=(7.6, 4.6))
x = np.linspace(-3, 3, 500)
plot_fn(ax, np.exp, -3, 3, color=BLUE, lw=2.8, label=r'$e^x$  (exact)')

def exp_taylor(N):
    s = np.zeros_like(x)
    for k in range(N + 1):
        s += x**k / FACT(k)
    return s

orders = [(1, ORANGE), (2, PURPLE), (4, GREEN), (7, RED)]
for N, c in orders:
    ax.plot(x, exp_taylor(N), color=c, lw=1.8, ls='--',
            label=r'Taylor order %d  ($\sum_{k=0}^{%d} x^k/k!$)' % (N, N))
ax.text(-2.8, 16, 'every extra term extends the\nrange where the polynomial\nsticks to $e^x$.\n'
                  'radius of convergence $R=\\infty$',
        fontsize=9, color=RED,
        bbox=dict(boxstyle='round', fc='white', ec=RED, alpha=0.9))
setup_axes(ax, xlim=(-3, 3), ylim=(-2, 20),
           title=r'Taylor series of $e^x$:  more terms $=>$  sticks tighter, everywhere',
           xlabel='x', ylabel='y', grid=True)
ax.legend(fontsize=8.5, loc='upper left')
save(fig, 'fig-4-11-1-exp-taylor.png')

# ========== 图 2 · sin x 的泰勒级数 ==========
fig, ax = plt.subplots(figsize=(7.6, 4.5))
xs = np.linspace(-2*np.pi, 2*np.pi, 500)
plot_fn(ax, np.sin, -2*np.pi, 2*np.pi, color=BLUE, lw=2.8, label=r'$\sin x$  (exact)')

def sin_taylor(N):
    # N = highest power; sin uses odd powers only: x - x^3/3! + x^5/5! - ...
    s = np.zeros_like(xs)
    k = 1
    sign = 1
    while k <= N:
        s += sign * xs**k / FACT(k)
        k += 2
        sign *= -1
    return s

orders = [(1, ORANGE), (3, PURPLE), (5, GREEN), (9, RED), (13, 'brown')]
for N, c in orders:
    ax.plot(xs, sin_taylor(N), color=c, lw=1.7, ls='--',
            label=r'order %d  ($x - x^3/3! + \cdots$)' % N)
ax.text(-6.0, 0.55, 'around $x=0$ all orders match.\nmore terms $=>$ wider match.\n$R=\\infty$',
        fontsize=9, color=RED,
        bbox=dict(boxstyle='round', fc='white', ec=RED, alpha=0.9))
setup_axes(ax, xlim=(-2*np.pi, 2*np.pi), ylim=(-1.6, 1.6),
           title=r'Taylor series of $\sin x$:  sticks tighter near 0, range widens with order',
           xlabel='x', ylabel='y', grid=True)
ax.legend(fontsize=8.5, loc='upper right')
save(fig, 'fig-4-11-2-sin-taylor.png')

# ========== 图 3 · 收敛半径:1/(1-x) vs 1/(1+x^2) ==========
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11.2, 4.4))

# 左: 1/(1-x) 部分和,|x|<1 收敛,|x|>=1 发散
xa = np.linspace(-0.95, 0.95, 400)
plot_fn(axL, lambda t: 1/(1-t), -0.95, 0.95, color=BLUE, lw=2.6, label=r'$\frac{1}{1-x}$  exact')
def geom_part(N):
    s = np.zeros_like(xa)
    for k in range(N + 1):
        s += xa**k
    return s
for N, c in [(1, ORANGE), (3, PURPLE), (8, GREEN), (20, RED)]:
    axL.plot(xa, geom_part(N), color=c, lw=1.5, ls='--', label='partial sum N=%d' % N)
band(axL, 'x', 0, 1.0, color=ORANGE, alpha=0.12)   # |x|<1 收敛带
axL.axvline(1, color=RED, ls=':', lw=1.2); axL.axvline(-1, color=RED, ls=':', lw=1.2)
axL.text(-0.55, 8.5, r'radius of convergence $R=1$' + '\n(singularity at $x=1$)',
         fontsize=9.5, color=RED,
         bbox=dict(boxstyle='round', fc='white', ec=RED, alpha=0.9))
setup_axes(axL, xlim=(-1.05, 1.05), ylim=(0, 10),
           title=r'$1/(1-x)=\sum x^n$:  converges only for $|x|<1$',
           xlabel='x', ylabel='value', grid=True)
axL.legend(fontsize=8, loc='upper center')

# 右: 1/(1+x^2) 在实轴处处光滑,但 Taylor 只在 |x|<1 收敛!
xb = np.linspace(-2, 2, 500)
plot_fn(axR, lambda t: 1/(1+t**2), -2, 2, color=BLUE, lw=2.6, label=r'$\frac{1}{1+x^2}$  exact (smooth everywhere)')
def arctan_part(N):  # 1/(1+x^2) = 1 - x^2 + x^4 - x^6 + ...
    s = np.zeros_like(xb)
    for k in range(N + 1):
        s += (-1)**k * xb**(2*k)
    return s
for N, c in [(1, ORANGE), (3, PURPLE), (8, GREEN), (20, RED)]:
    axR.plot(xb, arctan_part(N), color=c, lw=1.5, ls='--', label='Taylor N=%d' % N)
axR.axvline(1, color=RED, ls=':', lw=1.2); axR.axvline(-1, color=RED, ls=':', lw=1.2)
axR.set_ylim(-5, 10)
axR.text(-1.95, 6.5,
         r'smooth on all of $\mathbb{R}$, but Taylor diverges for $|x|>1$!' + '\n'
         r'(complex singularities at $x=\pm i$  $=>$  $R=1$)',
         fontsize=8.5, color=RED,
         bbox=dict(boxstyle='round', fc='white', ec=RED, alpha=0.9))
setup_axes(axR, xlim=(-2, 2), ylim=(-5, 10),
           title=r'$1/(1+x^2)$:  smooth on $\mathbb{R}$, yet $R=1$  (complex singularity!)',
           xlabel='x', ylabel='value', grid=True)
axR.legend(fontsize=8, loc='upper right')

save(fig, 'fig-4-11-3-radius-of-conv.png')

# 数值自检
print('exp taylor @x=2, order 7 = %.5f (exact e^2=%.5f)' % (exp_taylor(7)[np.searchsorted(x,2)], np.exp(2)))
xi = np.searchsorted(xs, 1.0)
print('sin taylor @x=1, order 5 = %.6f (exact sin1=%.6f)' % (sin_taylor(5)[xi], np.sin(1)))
print('1/(1-x) partial @x=0.9, N=20 = %.5f (exact=%.5f)' % (geom_part(20)[np.searchsorted(xa,0.9)], 1/(1-0.9)))
print('1/(1+x^2) partial @x=1.5, N=20 = %.3f (exacts=%.5f, diverges!)' % (arctan_part(20)[np.searchsorted(xb,1.5)], 1/(1+1.5**2)))
print('done · 3 figures generated')
