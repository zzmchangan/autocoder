"""第 5 篇 第 12 章 配图脚本(P5-12 · 为什么把函数拆成正弦波).

生成三张图:
  fig-5-12-1-square-wave-time.png      : 方波在时域的样子(看起来"复杂")
  fig-5-12-2-harmonic-synthesis.png    : 1/3/7/15 个奇次谐波逐步叠加逼近方波(2x2 子图)
  fig-5-12-3-spectrum.png              : 方波的频谱(柱落在奇次谐波 1/3/5/... 处, 幅度按 1/n 衰减)

严禁修改 _plot_utils.py; 本脚本独立运行, 产出 PNG.
所有数字先用 sympy/numpy 算对再画.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, plot_curve, spectrum, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, sympy as sp, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.fft import rfft, rfftfreq

# ---------- 用 sympy 先把傅里叶系数算对(画图必须与正文一致) ----------
x = sp.symbols('x')
n = sp.symbols('n', positive=True, integer=True)
I1 = sp.integrate(1 * sp.sin(n * x), (x, 0, sp.pi))
I2 = sp.integrate((-1) * sp.sin(n * x), (x, sp.pi, 2 * sp.pi))
B_N = sp.simplify((I1 + I2) / sp.pi)   # 2*(1 - (-1)**n)/(pi*n)
print('sympy b_n =', B_N)
# 奇次 b_k = 4/(pi*k), 偶次 b_k = 0


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


t = np.linspace(-0.5, 2 * np.pi + 0.5, 4000)
sq_true = square_wave(t)

# ============================================================
# 图 1: 方波在时域的样子(单独一张, 看起来"复杂、不光滑")
# ============================================================
fig, ax = plt.subplots(figsize=(7.2, 3.8))
plot_curve(ax, t, sq_true, color=BLUE, lw=2.4, label='square wave (time domain)')
setup_axes(ax, xlim=(-0.5, 2 * np.pi + 0.5), ylim=(-1.6, 1.6),
           xlabel='time  t', ylabel='amplitude',
           title='A square wave looks complicated in the time domain')
ax.set_xticks([0, np.pi / 2, np.pi, 3 * np.pi / 2, 2 * np.pi])
ax.set_xticklabels(['0', 'π/2', 'π', '3π/2', '2π'])
ax.legend(loc='upper right', fontsize=10)
save(fig, 'fig-5-12-1-square-wave-time.png')
print('saved fig-5-12-1')

# ============================================================
# 图 2: 1 / 3 / 7 / 15 个奇次谐波叠加逼近方波 (2x2 子图)
# ============================================================
fig, axes = plt.subplots(2, 2, figsize=(9.4, 6.6))
cases = [1, 3, 7, 15]
tt = np.linspace(0, 2 * np.pi, 3000)
target = square_wave(tt)
for axx, nh in zip(axes.flat, cases):
    approx = harmonic_sum(tt, nh)
    plot_curve(axx, tt, target, color=BLUE, lw=1.6, ls='--', label='target square wave')
    plot_curve(axx, tt, approx, color=RED, lw=2.2,
               label=f'sum of {nh} odd harmonic(s)')
    setup_axes(axx, xlim=(0, 2 * np.pi), ylim=(-1.5, 1.5),
               xlabel='t', ylabel='amplitude',
               title=f'{nh} harmonic(s):  sin t + sin 3t + ... ')
    axx.set_xticks([0, np.pi / 2, np.pi, 3 * np.pi / 2, 2 * np.pi])
    axx.set_xticklabels(['0', 'π/2', 'π', '3π/2', '2π'])
    axx.legend(loc='upper right', fontsize=8.5)
fig.suptitle('A complicated wave = a sum of simple sine waves', fontsize=13, y=1.00)
save(fig, 'fig-5-12-2-harmonic-synthesis.png')
print('saved fig-5-12-2')

# ============================================================
# 图 3: 方波的频谱(用 spectrum 画, 奇次谐波 1/3/5/..., 幅度 4/(pi*n))
#     同时叠加一个 scipy.fft 对合成方波算出的真实频谱做对照
# ============================================================
# 理论柱
ks = np.arange(1, 16, 2)            # 奇次谐波 1,3,...,15
theory_mag = 4.0 / (np.pi * ks)     # b_k = 4/(pi*k)

# FFT 对照: 用 31 个谐波合成一个较干净的方波, 再做 FFT
t_long = np.linspace(0, 4 * np.pi, 8192)
sq_synth = harmonic_sum(t_long, 31)
dt = t_long[1] - t_long[0]
Y = rfft(sq_synth)
freqs = rfftfreq(len(t_long), d=dt)
mag_fft = np.abs(Y) / (len(t_long) / 2)   # 归一化到振幅

fig, ax = plt.subplots(figsize=(7.6, 4.2))
# 理论柱(PURPLE) —— markerfmt 用同色圆点
markerline, stemlines, baseline = ax.stem(
    ks, theory_mag, linefmt=PURPLE, markerfmt=PURPLE,
    basefmt=' ')
plt.setp(stemlines, 'color', PURPLE, 'lw', 2.0, 'zorder', 3)
plt.setp(markerline, 'color', PURPLE, 'markersize', 7, 'zorder', 4)
# FFT 实测点(RED 叠上去)
# 取每根理论频率附近最近的一个 FFT bin
fft_pts = []
for k in ks:
    f_target = k / (2 * np.pi)        # 角频率 k 对应循环频率 k/(2pi)
    idx = np.argmin(np.abs(freqs - f_target))
    fft_pts.append(mag_fft[idx])
ax.scatter(ks, fft_pts, color=RED, s=42, zorder=5,
           label='measured by FFT (scipy.fft)')
# 理论标注
ax.scatter(ks, theory_mag, color=PURPLE, s=22, zorder=4,
           label='theory  4/(πn)  (odd n)')

ax.set_xlabel('harmonic number  n  (frequency)', fontsize=11)
ax.set_ylabel('magnitude  |b_n|', fontsize=11)
ax.set_title('Spectrum of a square wave: only odd harmonics, decaying as 1/n', fontsize=11.5)
ax.set_xticks(list(range(1, 16)))
ax.set_xlim(0.3, 15.7)
ax.set_ylim(0, 1.5)
ax.grid(True, alpha=0.18)
ax.legend(loc='upper right', fontsize=9.5)
save(fig, 'fig-5-12-3-spectrum.png')
print('saved fig-5-12-3')

print('\nAll figures generated.')
