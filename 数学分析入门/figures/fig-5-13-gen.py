"""第 5 篇 第 13 章 配图脚本(P5-13 · 傅里叶级数:分解与收敛危机).

生成两张图:
  fig-5-13-1-coefficient-bars.png   : 方波的傅里叶系数柱(奇次谐波 1/3/5/..., 高度 4/(pi*n);
                                       偶次为空). sympy 算出的精确值(红点)与理论柱(紫)重合.
  fig-5-13-2-gibbs.png              : Gibbs 现象. 用 9/49/499 个奇次谐波逼近方波,
                                       跳变点附近过冲始终约 9%(1.09), N 增大过冲只变窄不变矮.

严禁修改 _plot_utils.py; 本脚本独立运行, 产出 PNG.
所有数字先用 sympy/numpy 算对再画.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_curve, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, sympy as sp, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ---------- 用 sympy 先把傅里叶系数算对(画图必须与正文一致) ----------
x = sp.symbols('x')
n = sp.symbols('n', positive=True, integer=True)
I1 = sp.integrate(sp.sin(n * x), (x, 0, sp.pi))
I2 = sp.integrate(sp.sin(n * x), (x, sp.pi, 2 * sp.pi))
B_N = sp.simplify((I1 - I2) / sp.pi)   # 2*(1 - (-1)**n)/(pi*n)
print('sympy b_n =', B_N)


def square_wave(t):
    """理想方波(+1 on (0,pi), -1 on (pi,2pi), 周期 2pi), 向量化."""
    t = np.asarray(t, dtype=float)
    return np.where(np.mod(t, 2 * np.pi) < np.pi, 1.0, -1.0)


def harmonic_sum(t, n_harmonics):
    """用前 n_harmonics 个奇次谐波(1,3,5,...)叠加逼近方波."""
    s = np.zeros_like(t, dtype=float)
    k = 1
    cnt = 0
    while cnt < n_harmonics:
        s += (4.0 / (np.pi * k)) * np.sin(k * t)
        k += 2
        cnt += 1
    return s


# ============================================================
# 图 1: 方波的傅里叶系数柱(奇次谐波 1/3/5/..., 高度 4/(pi*n))
#       sympy 算出的精确值(红点)与理论柱(紫)重合
# ============================================================
ks = np.arange(1, 16)                       # 谐波编号 1..15
theory_mag = np.where(ks % 2 == 1, 4.0 / (np.pi * ks), 0.0)   # b_k = 4/(pi*k) 奇, 0 偶

# sympy 精确值(红点)
sympy_vals = []
for k in ks:
    val = float(sp.nsimplify(B_N.subs(n, int(k))))
    sympy_vals.append(val)
sympy_vals = np.array(sympy_vals)

fig, ax = plt.subplots(figsize=(7.8, 4.3))
# 理论柱(PURPLE)
markerline, stemlines, baseline = ax.stem(
    ks, theory_mag, linefmt=PURPLE, markerfmt=' ', basefmt=' ')
plt.setp(stemlines, 'color', PURPLE, 'lw', 2.4, 'zorder', 3)
# sympy 精确值(RED 圆点叠上去)
ax.scatter(ks, sympy_vals, color=RED, s=55, zorder=5,
           label='exact value from sympy  $b_n = 2(1-(-1)^n)/(\\pi n)$')
# 偶数处的零点也标一下(强调"偶次谐波为零")
even_mask = (ks % 2 == 0)
ax.scatter(ks[even_mask], sympy_vals[even_mask], color=ORANGE, s=38,
           marker='x', zorder=5, label='even harmonics = 0 (symmetry)')

ax.set_xlabel('harmonic number  n', fontsize=11)
ax.set_ylabel('Fourier coefficient  $|b_n|$', fontsize=11)
ax.set_title('Square wave Fourier coefficients:  $b_n = 4/(\\pi n)$ for odd n, 0 for even n',
             fontsize=11)
ax.set_xticks(list(range(1, 16)))
ax.set_xlim(0.3, 15.7)
ax.set_ylim(-0.05, 1.5)
ax.axhline(0, color='gray', lw=0.6, alpha=0.4)
ax.grid(True, alpha=0.18)
ax.legend(loc='upper right', fontsize=9.5)
save(fig, 'fig-5-13-1-coefficient-bars.png')
print('saved fig-5-13-1')


# ============================================================
# 图 2: Gibbs 现象. 9/49/499 个奇次谐波逼近方波, 跳变点附近过冲锁在 ~9%
#       左大图叠加三条逼近曲线 + 真值; 右小图放大跳变点(看过冲高度不变)
# ============================================================
# 理论 Gibbs 常数
# Si(pi) = ∫_0^pi sin(u)/u du ≈ 1.8519
# 方波幅度从 -1 跳到 +1, 单侧过冲峰值 = (2/pi)*Si(pi) ≈ 1.17898
# 即超出真值 1 的部分 = (2/pi)*Si(pi) - 1 ≈ 0.17898
# 相对于跳变幅度 2 的比例 = 0.0895 (这就是常说的 ~9%)
from scipy import integrate as sci_integrate
gibbs_int, _ = sci_integrate.quad(lambda u: np.sin(u) / u, 0, np.pi)
peak_value_theory = (2 / np.pi) * gibbs_int          # ≈ 1.17898
overshoot_theory = peak_value_theory - 1.0            # ≈ 0.17898 (超出 1 的部分)
overshoot_fraction = overshoot_theory / 2.0           # ≈ 0.0895 (相对跳变幅度 2)
print('theoretical Gibbs peak value = %.5f  (overshoot above 1.0 = %.5f,  %.2f%% of jump)'
      % (peak_value_theory, overshoot_theory, overshoot_fraction * 100))

# 高分辨率时间轴(重点看跳变点 t=0 附近)
t_fine = np.linspace(-0.5, 2 * np.pi + 0.5, 200000)
sq_true = square_wave(t_fine)

cases = [9, 49, 499]
colors = [ORANGE, GREEN, RED]

fig, axes = plt.subplots(1, 2, figsize=(11.2, 4.6),
                         gridspec_kw={'width_ratios': [1.0, 1.0]})

# 左图: 整体 [0, 2pi] 视角, 三条逼近曲线叠在一起
axL = axes[0]
plot_curve(axL, t_fine, sq_true, color=BLUE, lw=1.8, ls='--', label='target square wave')
for N, c in zip(cases, colors):
    approx = harmonic_sum(t_fine, N)
    plot_curve(axL, t_fine, approx, color=c, lw=1.4,
               label=f'N = {N} harmonics')
setup_axes(axL, xlim=(-0.2, 2 * np.pi + 0.2), ylim=(-1.6, 1.6),
           xlabel='t', ylabel='amplitude',
           title='Fourier partial sums approach the square wave')
axL.set_xticks([0, np.pi / 2, np.pi, 3 * np.pi / 2, 2 * np.pi])
axL.set_xticklabels(['0', 'π/2', 'π', '3π/2', '2π'])
axL.legend(loc='upper right', fontsize=8.5)
axL.text(0.02, 0.04, 'overshoot ~9% near jumps', transform=axL.transAxes,
         fontsize=9, color=RED)

# 右图: 放大跳变点 t=0 附近(看过冲高度不随 N 下降, 只变窄)
axR = axes[1]
# 只看 t in [-0.05, 0.6] 这一小段, 重点看 0 右侧的过冲峰
mask = (t_fine >= -0.05) & (t_fine <= 0.6)
t_zoom = t_fine[mask]
sq_zoom = sq_true[mask]
plot_curve(axR, t_zoom, sq_zoom, color=BLUE, lw=2.0, ls='--', label='target')
peaks = []
for N, c in zip(cases, colors):
    approx = harmonic_sum(t_zoom, N)
    plot_curve(axR, t_zoom, approx, color=c, lw=1.6, label=f'N = {N}')
    # 找 t>0.001 区域的峰值
    m = t_zoom > 0.001
    pk = np.max(approx[m])
    peaks.append((N, pk))
setup_axes(axR, xlim=(-0.05, 0.6), ylim=(0.5, 1.28),
           xlabel='t  (zoomed near jump at t=0)', ylabel='amplitude',
           title='Gibbs overshoot: peak stays ~1.179 as N grows')
# 过冲水平线 (单侧峰值 ≈ 1.179, 超出 1 的部分 ≈ 0.179, 占跳变幅度 2 的 ~9%)
axR.axhline(peak_value_theory, color=RED, lw=1.2, ls=':',
            label=f'overshoot peak ≈ {peak_value_theory:.4f} ({overshoot_fraction*100:.2f}% of jump)')
axR.axhline(1.0, color='gray', lw=0.8, ls='-', alpha=0.5)
axR.legend(loc='upper right', fontsize=8)
# 标注实测峰值
txt = 'measured peaks:\n' + '\n'.join([f'  N={N}: {pk:.4f}' for N, pk in peaks])
axR.text(0.03, 0.55, txt, transform=axR.transAxes, fontsize=8.5, color=RED,
         family='monospace')

fig.suptitle('Gibbs phenomenon: overshoot near a discontinuity never vanishes',
             fontsize=12.5, y=1.02)
save(fig, 'fig-5-13-2-gibbs.png')
print('saved fig-5-13-2')
print('measured peaks:', peaks)

print('\nAll figures generated.')
