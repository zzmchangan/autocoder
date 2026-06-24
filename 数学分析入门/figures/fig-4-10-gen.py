"""P4-10《一致收敛:什么时候能交换极限的顺序》配图生成脚本。

两张图:
  fig-4-10-1-pointwise-vs-uniform.png  —— x^n 在 [0,1] 各 n 的曲线:点态收敛到 0 但端点跳到 1(不一致)
  fig-4-10-2-termwise-ops.png          —— 逐项积分/逐项求导示意(幂级数 x^n 一致,可逐项操作)
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, plot_curve, band, marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt

# ========== 图 1 · x^n 在 [0,1]:点态收敛 vs 一致收敛(端点跳跃) ==========
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11.2, 4.5))

x = np.linspace(0, 1, 600)
ns = [1, 2, 4, 8, 16, 32, 64]
cmap = plt.cm.viridis

# 左:整段 [0,1],端点跳到 1 —— 看不一致
for i, k in enumerate(ns):
    axL.plot(x, x**k, color=cmap(i / (len(ns)-1)), lw=1.8, alpha=0.9,
             label='n=%d' % k if k in (1, 4, 16, 64) else None)
axL.axhline(0, color=GREEN, lw=1.0, ls='--', alpha=0.6)
axL.scatter([1], [1], color=RED, s=40, zorder=6)
axL.annotate('jump:  limit at x=1  is  1\n(pointwise limit = 0 on [0,1), 1 at x=1)',
             (1, 1), xytext=(0.32, 0.78), color=RED, fontsize=9,
             arrowprops=dict(arrowstyle='->', color=RED, lw=1))
axL.text(0.04, 0.55,
         'pointwise limit = 0\nfor every x in [0,1)\nbut sup|x^n - 0| = 1  (never shrinks)',
         fontsize=8.5, color=BLUE,
         bbox=dict(boxstyle='round', fc='white', ec=BLUE, alpha=0.85))
setup_axes(axL, xlim=(0, 1), ylim=(-0.05, 1.1),
           title=r'$f_n(x)=x^n$  on  [0,1]:  NOT uniform  (jump at endpoint)',
           xlabel='x', ylabel=r'$f_n(x)$', grid=True)
axL.legend(fontsize=8, loc='center right')

# 右:[0, 0.9],紧致子区间,一致收敛到 0
x9 = np.linspace(0, 0.9, 500)
for i, k in enumerate(ns):
    axR.plot(x9, x9**k, color=cmap(i / (len(ns)-1)), lw=1.8, alpha=0.9,
             label='n=%d' % k if k in (1, 4, 16, 64) else None)
axR.axhline(0, color=GREEN, lw=1.0, ls='--', alpha=0.6)
# sup at x=0.9 -> 0
sups = [0.9**k for k in ns]
axR.text(0.05, 0.6,
         r'on  $[0,0.9]$:  $\sup|x^n-0| = 0.9^n \to 0$' + '\n(uniform convergence here)',
         fontsize=9, color=GREEN,
         bbox=dict(boxstyle='round', fc='white', ec=GREEN, alpha=0.9))
setup_axes(axR, xlim=(0, 0.95), ylim=(-0.05, 1.05),
           title=r'$f_n(x)=x^n$  on  [0, 0.9]:  uniform  (sup shrinks to 0)',
           xlabel='x', ylabel=r'$f_n(x)$', grid=True)
axR.legend(fontsize=8, loc='center right')

save(fig, 'fig-4-10-1-pointwise-vs-uniform.png')

# ========== 图 2 · 逐项积分/逐项求导示意(用幂级数做例子) ==========
# 用 1/(1-x) = sum x^n on [0, r] (r<1), 展示逐项积分
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11.2, 4.4))

x = np.linspace(0, 0.85, 400)
f = 1.0 / (1 - x)                                   # 原函数

# 左:逐项积分 sum x^n dx = sum x^(n+1)/(n+1) = -ln(1-x)
def partial_integ(N):
    s = np.zeros_like(x)
    for n in range(N):
        s += x**(n+1) / (n+1)
    return s
exact_integ = -np.log(1 - x)                         # 积分真值
axL.plot(x, exact_integ, color=BLUE, lw=2.6, label=r'$\int_0^x \frac{dt}{1-t} = -\ln(1-x)$')
for N, c in [(2, ORANGE), (5, PURPLE), (15, GREEN)]:
    axL.plot(x, partial_integ(N), color=c, lw=1.6, ls='--',
             label=r'$\sum_{n=0}^{%d} x^{n+1}/(n+1)$' % N)
setup_axes(axL, xlim=(0, 0.85), ylim=(0, 5),
           title=r'Termwise integration:  $\int \sum x^n = \sum \int x^n$',
           xlabel='x', ylabel='value', grid=True)
axL.legend(fontsize=8.5, loc='upper left')

# 右:逐项求导 sum x^n 求导 = sum n x^(n-1) = 1/(1-x)^2
f_deriv = 1.0 / (1 - x)**2
def partial_deriv(N):
    s = np.zeros_like(x)
    for n in range(1, N+1):
        s += n * x**(n-1)
    return s
axR.plot(x, f_deriv, color=BLUE, lw=2.6, label=r'$(\frac{1}{1-x})^\prime = \frac{1}{(1-x)^2}$')
for N, c in [(2, ORANGE), (5, PURPLE), (15, GREEN)]:
    axR.plot(x, partial_deriv(N), color=c, lw=1.6, ls='--',
             label=r'$\sum_{n=1}^{%d} n\,x^{n-1}$' % N)
setup_axes(axR, xlim=(0, 0.85), ylim=(0, 8),
           title=r'Termwise differentiation:  $\frac{d}{dx}\sum x^n = \sum \frac{d}{dx} x^n$',
           xlabel='x', ylabel='value', grid=True)
axR.legend(fontsize=8.5, loc='upper left')

save(fig, 'fig-4-10-2-termwise-ops.png')

# 数值自检
print('check: sup|x^n - 0| on [0,1) = 1 always (not uniform)')
print('  on [0,0.9]: 0.9^64 = %.6f (->0, uniform)' % (0.9**64))
print('  termwise integ @x=0.85, N=15: %.5f vs -ln(0.15)=%.5f'
      % (partial_integ(15)[-1], -np.log(1-0.85)))
print('  termwise deriv @x=0.85, N=15: %.5f vs 1/(0.15)^2=%.5f'
      % (partial_deriv(15)[-1], 1/(1-0.85)**2))
print('done · 2 figures generated')
