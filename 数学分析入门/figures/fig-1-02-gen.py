"""P1-02《极限:无穷靠近到底什么意思》配图生成脚本.

两张图:
  fig-1-02-1-epsilon-N.png        —— 数列 a_n 进入 y=L±eps 带(eps-N 契约)
  fig-1-02-2-sequence-to-e.png    —— (1+1/n)^n 逼近 e 的曲线
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_curve, plot_fn, band, marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, sympy as sp, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---------- 先用 sympy / numpy 核对关键数字 ----------
n = sp.symbols('n', positive=True, integer=True)
a_seq_sym = (1 + 1/n)**n
limit_e = sp.limit(a_seq_sym, n, sp.oo)
print('[verify] lim (1+1/n)^n =', limit_e)            # E
print('[verify] lim (1 + 10^-n) =', sp.limit(1 - sp.Integer(10)**(-n) + 1/sp.Integer(10)**n, n, sp.oo))

# 数列 a_n = (1+1/n)^n 的若干项(正文数字来源)
print('[verify] (1+1/n)^n  partial values:')
vals = []
for k in [1, 2, 5, 10, 100, 1000, 10000]:
    v = float((1 + sp.Rational(1, k))**k)
    vals.append((k, v))
    print('   n=%6d -> %.8f' % (k, v))

# 找一个最小 N 使 |a_n - e| < eps,用于配图标注
import math
E = math.e
print('[verify] smallest N such that |a_n - e| < eps:')
for eps in [0.1, 0.01, 0.001]:
    Nfound = None
    for k in range(1, 200000):
        if abs((1 + 1/k)**k - E) < eps:
            Nfound = k
            break
    print('   eps=%.3f -> N=%d   (a_N=%.6f)' % (eps, Nfound, (1+1/Nfound)**Nfound))


# ========== 图 1 · eps-N 契约:数列 a_n 进入 y=L±eps 带 ==========
# 选一个收敛到 0 的简单数列 a_n = 1/n,极限 L=0,演示 eps=0.1 -> N=10
fig, ax = plt.subplots(figsize=(8.0, 4.8))
Nmax = 30
ks = np.arange(1, Nmax + 1)
an = 1.0 / ks

eps = 0.1
Nstar = 11   # n>=11 时 1/n < 0.1 (1/10=0.1 不算严格小于, 取 11)
L = 0.0

# eps 带
ax.axhspan(L - eps, L + eps, color=ORANGE, alpha=0.18, zorder=1)
ax.axhline(L + eps, color=ORANGE, ls='--', lw=1.3)
ax.axhline(L - eps, color=ORANGE, ls='--', lw=1.3)
ax.text(Nmax - 0.5, L + eps + 0.012,
        r'$L+\epsilon=0.1$', color=ORANGE, fontsize=10, ha='right')
ax.text(Nmax - 0.5, L - eps - 0.035,
        r'$L-\epsilon=-0.1$', color=ORANGE, fontsize=10, ha='right')

# 数列散点 + 折线
ax.plot(ks, an, 'o-', color=BLUE, lw=1.6, ms=4.5,
        label=r'$a_n = 1/n$', zorder=4)

# 极限线 L=0
ax.axhline(L, color=GREEN, lw=1.6, ls=':', alpha=0.85)
ax.text(1.2, L + 0.012, r'limit $L=0$', color=GREEN, fontsize=10)

# N 分界竖线
ax.axvline(Nstar, color=RED, ls='--', lw=1.6)
ax.scatter([Nstar], [an[Nstar - 1]], color=RED, s=42, zorder=6)
ax.annotate(r'$N=11$' + '\n' + r'$a_{11}\approx0.091$',
            (Nstar, an[Nstar - 1]), xytext=(Nstar - 6.5, 0.16),
            color=RED, fontsize=10,
            arrowprops=dict(arrowstyle='->', color=RED, lw=1.2))
ax.text(Nstar + 0.3, 0.55, r'$n \geq N=11$' + '\n' + r'$\Rightarrow |a_n-0|<\epsilon$',
        color=RED, fontsize=10)

setup_axes(ax, xlim=(0.5, Nmax + 0.5), ylim=(-0.25, 1.1),
           title=r'epsilon-N contract:  pick $\epsilon=0.1$,  I give $N=11$   ($a_n=1/n \to 0$)',
           xlabel='n', ylabel=r'$a_n$', grid=True)
ax.legend(fontsize=10, loc='upper right')
save(fig, 'fig-1-02-1-epsilon-N.png')


# ========== 图 2 · (1+1/n)^n 逼近 e ==========
fig, ax = plt.subplots(figsize=(8.0, 4.8))
Nmax2 = 60
ks2 = np.arange(1, Nmax2 + 1)
an2 = (1 + 1.0 / ks2) ** ks2

# e 的水平线
ax.axhline(E, color=GREEN, lw=1.8, ls=':', label=r'$e \approx 2.7182818$')

# 数列曲线
ax.plot(ks2, an2, 'o-', color=BLUE, lw=1.6, ms=4.0,
        label=r'$a_n = (1+1/n)^n$', zorder=4)

# 几个早期点标注
for k_ in [1, 2, 5, 10]:
    ax.annotate('n=%d\n%.4f' % (k_, an2[k_ - 1]),
                (k_, an2[k_ - 1]),
                xytext=(k_ + 1.5, an2[k_ - 1] - 0.18),
                color=BLUE, fontsize=8.5,
                arrowprops=dict(arrowstyle='->', color=BLUE, lw=0.8))

# 在 n=50 附近点出"已经贴上 e"
ax.scatter([Nmax2], [an2[-1]], color=RED, s=42, zorder=6)
ax.annotate('n=%d\n%.6f' % (Nmax2, an2[-1]),
            (Nmax2, an2[-1]),
            xytext=(Nmax2 - 18, an2[-1] + 0.05),
            color=RED, fontsize=9,
            arrowprops=dict(arrowstyle='->', color=RED, lw=1.0))

setup_axes(ax, xlim=(0, Nmax2 + 2), ylim=(1.9, 2.85),
           title=r'$(1+1/n)^n \to e \approx 2.71828\ldots$   (a fundamental limit)',
           xlabel='n', ylabel=r'$a_n$', grid=True)
ax.legend(fontsize=10, loc='lower right')
save(fig, 'fig-1-02-2-sequence-to-e.png')

print('\n[done] two figures generated.')
