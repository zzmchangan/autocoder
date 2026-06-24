"""P4-09 扩充配图生成脚本(只增不删,不碰原 fig-4-09-gen.py).

两张图:
  fig-4-09-4-integral-test.png      —— 积分判别法:用 ∫_1^∞ 1/x^p dx 判 Σ 1/n^p 的收敛
                                       (p=1 调和发散, ∫=oo; p=2 收敛, ∫=1)
  fig-4-09-5-riemann-rearrange.png  —— Riemann 重排:交错调和级数正常和=ln2,
                                       重排后和被挪到任意目标(如 1.5, 0.3)
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, plot_curve, band, marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt

# ========== 图 4 · 积分判别法:Σ 1/n^p 与 ∫_1^∞ 1/x^p dx 同生共死 ==========
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11.2, 4.5))

# 左:p=1 调和级数 vs ∫ 1/x dx —— 都发散
n_int = np.arange(1, 16)
xg = np.linspace(1, 16, 400)
# 阶梯:1/n 的左端矩形(下和)与函数 1/x
axL.bar(n_int - 0.5, 1.0 / n_int, width=1.0, color=ORANGE, alpha=0.45,
        edgecolor=ORANGE, lw=0.4, label=r'bars $1/n$  (sum $\sum 1/n$)')
plot_fn(axL, lambda t: 1.0 / t, 1, 16, color=RED, lw=2.4,
        label=r'$f(x)=1/x$  ($\int_1^\infty 1/x\,dx=\infty$)')
axL.text(8.5, 0.62,
         r'$p=1$:  $\sum 1/n$  diverges' + '\n' + r'$\int_1^\infty 1/x\,dx=\infty$' + '\n(same fate:  both diverge)',
         fontsize=9, color=RED,
         bbox=dict(boxstyle='round', fc='white', ec=RED, alpha=0.9))
setup_axes(axL, xlim=(1, 16), ylim=(0, 1.05),
           title=r'Integral test, $p=1$:  series and integral both diverge',
           xlabel='n  /  x', ylabel='value', grid=True)
axL.legend(fontsize=8.5, loc='upper right')

# 右:p=2 巴塞尔级数 vs ∫ 1/x^2 dx —— 都收敛(∫=1)
axR.bar(n_int - 0.5, 1.0 / n_int**2, width=1.0, color=GREEN, alpha=0.45,
        edgecolor=GREEN, lw=0.4, label=r'bars $1/n^2$  (sum $\to \pi^2/6$)')
plot_fn(axR, lambda t: 1.0 / t**2, 1, 16, color=BLUE, lw=2.4,
        label=r'$f(x)=1/x^2$  ($\int_1^\infty 1/x^2\,dx=1$)')
axR.text(7.0, 0.55,
         r'$p=2$:  $\sum 1/n^2$  converges' + '\n' + r'$\int_1^\infty 1/x^2\,dx=1$' + '\n(same fate:  both converge)',
         fontsize=9, color=BLUE,
         bbox=dict(boxstyle='round', fc='white', ec=BLUE, alpha=0.9))
setup_axes(axR, xlim=(1, 16), ylim=(0, 1.05),
           title=r'Integral test, $p=2$:  series and integral both converge',
           xlabel='n  /  x', ylabel='value', grid=True)
axR.legend(fontsize=8.5, loc='upper right')

save(fig, 'fig-4-09-4-integral-test.png')

# ========== 图 5 · Riemann 重排:同样的项、不同顺序、不同和 ==========
fig, ax = plt.subplots(figsize=(8.4, 4.8))

# 正项序列 1, 1/3, 1/5, ...  负项序列 1/2, 1/4, 1/6, ...
pos = 1.0 / np.arange(1, 40001, 2)
neg = 1.0 / np.arange(2, 40002, 2)


def riemann_rearrange(target):
    """先挑正项直到和 > target,再挑一个负项,再挑正项……"""
    s = 0.0
    ip, ineg = 0, 0
    trail = [0.0]
    steps = 0
    max_steps = 60000
    while ip < len(pos) and ineg < len(neg) and steps < max_steps:
        while s < target and ip < len(pos):
            s += pos[ip]; ip += 1
            trail.append(s)
            steps += 1
        if ineg < len(neg):
            s -= neg[ineg]; ineg += 1
            trail.append(s)
            steps += 1
    return np.array(trail)


for tgt, c in [(np.log(2), BLUE), (1.5, RED), (0.3, GREEN)]:
    tr = riemann_rearrange(tgt)
    ax.plot(np.arange(len(tr)), tr, color=c, lw=1.4, alpha=0.9,
            label=('rearranged, target %.2f  ->  final %.4f' % (tgt, tr[-1])))
    ax.axhline(tgt, color=c, ls=':', lw=1.0, alpha=0.5)

# 正常交错调和的部分和(对照)
N0 = 400
nn = np.arange(1, N0 + 1)
normal = np.cumsum((-1.0)**(nn + 1) / nn)
ax.plot(np.arange(N0), normal, color=PURPLE, lw=1.4, alpha=0.8,
        label='normal order  1-1/2+1/3-...  ->  ln2=%.4f' % np.log(2))
ax.axhline(np.log(2), color=PURPLE, ls=':', lw=1.2, alpha=0.6)

ax.text(0.02, 0.84,
        'same terms, different order:\n' +
        'ln2  (natural order)\n' +
        '1.5  (more positives first)\n' +
        '0.3  (more negatives first)',
        transform=ax.transAxes, fontsize=9, color='black',
        bbox=dict(boxstyle='round', fc='white', ec='black', alpha=0.85))
setup_axes(ax, xlim=(0, 6000), ylim=(-0.4, 2.0),
           title=r"Riemann rearrangement:  conditionally convergent series can sum to ANY value",
           xlabel='number of terms added (in rearranged order)', ylabel='running sum', grid=True)
ax.legend(fontsize=8.5, loc='lower right')
save(fig, 'fig-4-09-5-riemann-rearrange.png')

# 数值自检
print('check: normal alternating harmonic -> ln2 = %.5f' % normal[-1])
for tgt in [np.log(2), 1.5, 0.3]:
    tr = riemann_rearrange(tgt)
    print('rearranged target %.2f -> final %.5f' % (tgt, tr[-1]))
print('done · 2 figures generated')
