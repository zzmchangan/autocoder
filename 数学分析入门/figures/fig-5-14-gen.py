"""第 5 篇 第 14 章 配图脚本(P5-14 · 傅里叶变换:非周期信号的频谱).

生成两张图:
  fig-5-14-1-rect-sinc.png     : 矩形脉冲(上)及其 sinc 频谱(下).
                                   三个脉冲宽度 T=0.5/1.0/2.0, 展示时频对偶
                                   (脉冲越宽 T, sinc 主瓣越窄 1/T).
  fig-5-14-2-time-frequency.png : 时频对偶与不确定性原理. 三个高斯信号
                                   σ=0.5/1.0/2.0: 时域窄则频域宽.
                                   每个的 Δt·Δf = 1/(4π) (下界, 高斯刚好达到).

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
from scipy.fft import fft, fftfreq


# ---------- 用 sympy 先把矩形脉冲的 FT 算对 ----------
t, w, T = sp.symbols('t w T', real=True, positive=True)
F_rect = sp.integrate(sp.exp(-sp.I * w * t), (t, -T / 2, T / 2))
F_rect = sp.simplify(sp.expand_complex(F_rect))   # 2*sin(T*w/2)/w
print('sympy rect FT =', F_rect)


def rect_pulse(tt, width):
    """矩形脉冲, 中心 0, 给定宽度."""
    return np.where(np.abs(tt) < width / 2, 1.0, 0.0)


def rect_spectrum_continuous(f, width):
    """矩形脉冲的连续傅里叶变换 (用角频率 w=2 pi f): F(f) = width * sinc(f*width).
       normalized sinc: sin(pi x)/(pi x). F(f) = width * sinc(f*width) where sinc(x)=sin(pi x)/(pi x)
       (因为 2 sin(wT/2)/w = T sinc(wT/(2pi)) = T sinc(fT) when w=2pi f)
    """
    x = f * width
    return width * np.sinc(x)   # np.sinc uses normalized definition sin(pi x)/(pi x)


# ============================================================
# 图 1: 矩形脉冲(上)及其 sinc 频谱(下), 三个宽度展示时频对偶
# ============================================================
fig, axes = plt.subplots(2, 1, figsize=(9.0, 6.4))

dt = 0.002
tt = np.arange(-4, 4, dt)

# 上图: 时域的三个矩形脉冲
axT = axes[0]
widths = [0.5, 1.0, 2.0]
cols = [ORANGE, GREEN, PURPLE]
for W, c in zip(widths, cols):
    rect = rect_pulse(tt, W)
    plot_curve(axT, tt, rect, color=c, lw=2.2,
               label=f'width  T = {W}')
setup_axes(axT, xlim=(-3, 3), ylim=(-0.15, 1.25),
           xlabel='time  t', ylabel='f(t)',
           title='Rectangular pulses (time domain)')
axT.legend(loc='upper right', fontsize=9.5)

# 下图: 对应的 sinc 频谱(理论连续曲线 + FFT 数值点)
axF = axes[1]
ff = np.linspace(-4, 4, 2000)
for W, c in zip(widths, cols):
    spec = rect_spectrum_continuous(ff, W)
    plot_curve(axF, ff, spec, color=c, lw=2.2,
               label=f'T = {W}:  main lobe width  1/T = {1/W:.2f}')
    # FFT 数值点验证(取正频率侧几个点)
    rect_full = rect_pulse(tt, W)
    Y = fft(rect_full)
    freqs_fft = fftfreq(len(tt), d=dt)
    Y = np.fft.fftshift(Y) * dt
    freqs_fft = np.fft.fftshift(freqs_fft)
    # 只取靠近原点、且幅度 > 0.05 的点, 避免画太密
    mask = (np.abs(freqs_fft) < 3.5) & (np.abs(Y) > 0.05)
    axF.scatter(freqs_fft[mask], np.abs(Y[mask]), color=c, s=10, alpha=0.5, zorder=3)

setup_axes(axF, xlim=(-3, 3), ylim=(-0.4, 2.3),
           xlabel='frequency  f', ylabel='|F(f)|',
           title='Sinc spectra (frequency domain):  wider pulse → narrower main lobe')
axF.legend(loc='upper right', fontsize=9)
# 标注零点位置
for W, c in zip(widths, cols):
    axF.axvline(1.0 / W, color=c, lw=0.7, ls=':', alpha=0.6)
    axF.axvline(-1.0 / W, color=c, lw=0.7, ls=':', alpha=0.6)

fig.suptitle('Time–frequency duality:  wider pulse (T) ⟺ narrower sinc main lobe (1/T)',
             fontsize=12, y=1.00)
save(fig, 'fig-5-14-1-rect-sinc.png')
print('saved fig-5-14-1')


# ============================================================
# 图 2: 高斯信号的时频对偶 + 不确定性原理 (Δt·Δf = 1/(4π) 下界)
# ============================================================
fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.6))

sigmas = [0.5, 1.0, 2.0]
dt = 0.005
tt = np.arange(-6, 6, dt)

# 左图: 时域的高斯(三个 σ)
axL = axes[0]
for s, c in zip(sigmas, cols):
    g = np.exp(-tt**2 / (2 * s**2))
    plot_curve(axL, tt, g, color=c, lw=2.2,
               label=f'σ = {s},  Δt = {s/np.sqrt(2):.3f}')
setup_axes(axL, xlim=(-4, 4), ylim=(-0.05, 1.1),
           xlabel='time  t', ylabel='g(t)',
           title='Gaussians in time domain')
axL.legend(loc='upper right', fontsize=9)
axL.text(0.03, 0.55, 'narrow σ → wide spectrum', transform=axL.transAxes,
         fontsize=9, color=RED)

# 右图: 对应的频域高斯(也是高斯, RMS 宽度 Δf = 1/(2√2 π σ))
axR = axes[1]
ff = np.linspace(-2.5, 2.5, 2000)
for s, c in zip(sigmas, cols):
    G = (s * np.sqrt(2 * np.pi)) * np.exp(-2 * np.pi**2 * s**2 * ff**2)  # 高斯的 FT
    # 归一化到峰值方便比较
    G = G / np.max(G)
    plot_curve(axR, ff, G, color=c, lw=2.2,
               label=f'σ = {s},  Δf = {1/(2*np.sqrt(2)*np.pi*s):.3f}')
setup_axes(axR, xlim=(-2, 2), ylim=(-0.05, 1.1),
           xlabel='frequency  f', ylabel='|G(f)| (normalized)',
           title='Gaussian spectra (also Gaussian)')
axR.legend(loc='upper right', fontsize=9)

# 标注不确定性原理
prod = (0.5 / np.sqrt(2)) * (1 / (2 * np.pi * 0.5))
axR.text(0.03, 0.45,
         f'uncertainty principle:\n  Δt · Δf ≥ 1/(4π) = {1/(4*np.pi):.5f}\n'
         f'  Gaussian reaches bound:\n  Δt·Δf = {prod:.5f}',
         transform=axR.transAxes, fontsize=8.5, color=RED, family='monospace')

fig.suptitle('Uncertainty principle:  narrow in time ⟺ wide in frequency (Δt·Δf = 1/(4π) for Gaussian)',
             fontsize=11.5, y=1.02)
save(fig, 'fig-5-14-2-time-frequency.png')
print('saved fig-5-14-2')

# 验证数值
print('\nnumerical check of uncertainty principle (Gaussian):')
for s in sigmas:
    dt_s = s / np.sqrt(2)
    df_s = 1.0 / (2 * np.sqrt(2) * np.pi * s)
    print(f'  σ={s}: Δt·Δf = {dt_s * df_s:.6f}  (bound = {1/(4*np.pi):.6f})')

print('\nAll figures generated.')
