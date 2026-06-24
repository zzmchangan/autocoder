"""P4-10 扩充配图生成脚本(只增不删,不碰原 fig-4-10-gen.py).

两张图:
  fig-4-10-3-weierstrass-mtest.png   —— M 判别:Σ sin(nx)/n^2 被 Σ 1/n^2 夹住,一致收敛
  fig-4-10-4-termwise-diff-counter.png —— 逐项求导反例:Σ sin(nx)/n 逐项求导得 Σ cos(nx)
                                       (N 增大部分和越来越大、越来越振荡 -> 不收敛)
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, plot_curve, band, marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt

# ========== 图 3 · Weierstrass M 判别:Σ sin(nx)/n^2 <= Σ 1/n^2 = π^2/6 ==========
fig, ax = plt.subplots(figsize=(8.2, 4.6))
xx = np.linspace(0, 2 * np.pi, 600)
N = 30
s = np.zeros_like(xx)
for n in range(1, N + 1):
    s += np.sin(n * xx) / n**2

# 真值(有限项已极接近,因为 Σ 1/n^2 尾巴很小)
ax.plot(xx, s, color=BLUE, lw=2.4,
        label=r'partial sum  $\sum_{n=1}^{30} \sin(nx)/n^2$  (already near limit)')
# 上下包络:± Σ 1/n^2
env = sum(1.0 / n**2 for n in range(1, 31))
ax.axhline(env,  color=GREEN, ls='--', lw=1.2,
           label=r'$+\sum 1/n^2 = \pi^2/6 \approx %.3f$  (envelope)' % env)
ax.axhline(-env, color=GREEN, ls='--', lw=1.2,
           label=r'$-\sum 1/n^2$  (envelope)')

ax.text(0.3, 2.2,
        r'$|\sin(nx)/n^2| \leq 1/n^2 =: M_n$' + '\n' +
        r'$\sum M_n = \pi^2/6$  converges' + '\n' +
        r'$\Rightarrow$  $\sum \sin(nx)/n^2$  uniformly converges',
        fontsize=9.5, color=RED,
        bbox=dict(boxstyle='round', fc='white', ec=RED, alpha=0.9))
setup_axes(ax, xlim=(0, 2 * np.pi), ylim=(-2.0, 2.6),
           title=r"Weierstrass M-test:  $|\sin(nx)/n^2|\leq 1/n^2$  $\Rightarrow$  uniform convergence",
           xlabel='x', ylabel='value', grid=True)
ax.legend(fontsize=8.5, loc='lower right')
save(fig, 'fig-4-10-3-weierstrass-mtest.png')

# ========== 图 4 · 逐项求导反例:Σ sin(nx)/n -> d/dx -> Σ cos(nx) 不收敛 ==========
fig, ax = plt.subplots(figsize=(8.2, 4.6))
xx2 = np.linspace(0, 2 * np.pi, 600)

# 对 Σ sin(nx)/n 逐项求导得到 Σ cos(nx),部分和画出来
def cos_partialsum(N):
    s = np.zeros_like(xx2)
    for n in range(1, N + 1):
        s += np.cos(n * xx2)
    return s

for N, c in [(5, ORANGE), (20, PURPLE), (50, GREEN), (200, RED)]:
    ax.plot(xx2, cos_partialsum(N), color=c, lw=1.5,
            label=r'$\sum_{n=1}^{%d} \cos(nx)$' % N)
ax.axhline(0, color='black', lw=0.8, alpha=0.5)
ax.text(0.4, 60,
        r'derivative series  $\sum \cos(nx)$' + '\n' +
        r'partials grow without bound  ($\sim N/2$ near $x=0$)' + '\n' +
        r'$\Rightarrow$  does NOT converge' + '\n' +
        r'(termwise differentiation fails for $\sum \sin(nx)/n$)',
        fontsize=9.5, color=RED,
        bbox=dict(boxstyle='round', fc='white', ec=RED, alpha=0.9))
setup_axes(ax, xlim=(0, 2 * np.pi), ylim=(-30, 110),
           title=r"Counterexample:  $\frac{d}{dx}\sum \sin(nx)/n = \sum \cos(nx)$  does not converge",
           xlabel='x', ylabel='value of partial sum', grid=True)
ax.legend(fontsize=8.5, loc='upper right')
save(fig, 'fig-4-10-4-termwise-diff-counter.png')

# 数值自检
print('check: envelope sum 1/n^2 (n=1..30) = %.5f (pi^2/6=%.5f)' %
      (sum(1.0/n**2 for n in range(1, 31)), np.pi**2/6))
print('check: cos partial sum peak @N=200 ~ %.1f (grows with N, diverges)' % np.max(np.abs(cos_partialsum(200))))
print('done · 2 figures generated')
