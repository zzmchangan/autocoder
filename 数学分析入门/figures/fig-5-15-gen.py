"""第 5 篇 第 15 章 配图脚本(P5-15 · DFT 与 FFT:数字世界的傅里叶).

生成两张图:
  fig-5-15-1-dft-spectrum.png   : 对一段合成信号(50Hz + 120Hz 正弦波 + 噪声)做 DFT.
                                    上: 时域波形(一团乱); 下: 频谱(两个清清楚楚的峰 + 噪声底).
  fig-5-15-2-fft-vs-dft.png      : FFT vs 直接 DFT 的耗时对比.
                                    左: 双对数图(红线 O(N^2) 斜率2, 绿线 O(N log N));
                                    右: 加速比(speedup)随 N 飙升.

严禁修改 _plot_utils.py; 本脚本独立运行, 产出 PNG.
所有数字先用 numpy 算对再画.
"""
import sys, os, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_curve, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from numpy.fft import fft, rfft, rfftfreq


def naive_dft(x):
    """直接 DFT (O(N^2)), 用矩阵实现."""
    N = len(x)
    n = np.arange(N).reshape(-1, 1)
    k = np.arange(N).reshape(1, -1)
    M = np.exp(-2j * np.pi * n * k / N)
    return M @ x


# ============================================================
# 图 1: 合成信号的 DFT 频谱(时域一团乱 -> 频域两个清清楚楚的峰)
# ============================================================
np.random.seed(42)
N = 1000
fs = 1000.0                                # 采样率 1000 Hz
t = np.arange(N) / fs                       # 时间 0 ~ 1s
signal = (1.0 * np.sin(2 * np.pi * 50 * t) +     # 50 Hz 分量, 振幅 1.0
          0.5 * np.sin(2 * np.pi * 120 * t) +    # 120 Hz 分量, 振幅 0.5
          0.3 * np.random.randn(N))              # 高斯噪声

X = rfft(signal)
freqs = rfftfreq(N, d=1.0 / fs)
mag = np.abs(X) / (N / 2)                   # 归一化到振幅

fig, axes = plt.subplots(2, 1, figsize=(9.2, 6.4))

# 上: 时域波形(前 0.2 秒, 看清细节)
axT = axes[0]
mask = t < 0.2
plot_curve(axT, t[mask] * 1000, signal[mask], color=BLUE, lw=1.0,
           label='signal (time domain)')
setup_axes(axT, xlim=(0, 200), ylim=(-2.2, 2.2),
           xlabel='time  (ms)', ylabel='amplitude',
           title='Time domain: a mess of two sines + noise')
axT.legend(loc='upper right', fontsize=9)
axT.text(0.02, 0.04, 'looks chaotic — cannot see the components', transform=axT.transAxes,
         fontsize=9, color=RED)

# 下: 频谱(两个清清楚楚的峰)
axF = axes[1]
axF.plot(freqs, mag, color=PURPLE, lw=1.4, zorder=3)
axF.fill_between(freqs, 0, mag, color=PURPLE, alpha=0.25, zorder=2)
setup_axes(axF, xlim=(0, 250), ylim=(0, 1.2),
           xlabel='frequency  (Hz)', ylabel='magnitude  |X[k]|',
           title='Frequency domain (via DFT/FFT): two clean peaks at 50 Hz and 120 Hz')
# 标注两个峰
axF.annotate('50 Hz  (amplitude ≈ 1.0)', xy=(50, mag[np.argmin(np.abs(freqs-50))]),
             xytext=(80, 1.0), fontsize=9.5, color=RED,
             arrowprops=dict(arrowstyle='->', color=RED))
axF.annotate('120 Hz  (amplitude ≈ 0.5)', xy=(120, mag[np.argmin(np.abs(freqs-120))]),
             xytext=(150, 0.55), fontsize=9.5, color=RED,
             arrowprops=dict(arrowstyle='->', color=RED))
axF.text(0.55, 0.75, 'noise floor\n(uniform)', transform=axF.transAxes,
         fontsize=9, color=ORANGE)

fig.suptitle('DFT reveals hidden frequency components that time domain cannot show',
             fontsize=11.5, y=1.00)
save(fig, 'fig-5-15-1-dft-spectrum.png')
print('saved fig-5-15-1')


# ============================================================
# 图 2: FFT vs 直接 DFT 的耗时对比
# ============================================================
sizes = [64, 128, 256, 512, 1024, 2048, 4096]
fft_times = []
dft_times = []

for N in sizes:
    x = np.random.rand(N)
    # FFT (多次平均, 用 perf_counter 提高精度)
    reps_fft = 2000 if N <= 256 else (500 if N <= 1024 else 100)
    t0 = time.perf_counter()
    for _ in range(reps_fft):
        fft(x)
    t_fft = (time.perf_counter() - t0) / reps_fft * 1000   # ms
    # 直接 DFT
    reps_dft = 20 if N <= 256 else (3 if N <= 1024 else 1)
    t0 = time.perf_counter()
    for _ in range(reps_dft):
        naive_dft(x)
    t_dft = (time.perf_counter() - t0) / reps_dft * 1000    # ms
    fft_times.append(t_fft)
    dft_times.append(t_dft)
    sp = t_dft / t_fft if t_fft > 0 else float('inf')
    print(f'N={N:5d}  FFT={t_fft:9.5f}ms  naiveDFT={t_dft:11.4f}ms  speedup={sp:9.1f}x')

fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.6))

# 左: 双对数图, FFT(绿) vs DFT(红), 加 O(N^2) 和 O(N log N) 参考线
axL = axes[0]
Ns = np.array(sizes, dtype=float)
axL.loglog(Ns, dft_times, color=RED, lw=2.2, marker='o', markersize=7,
           label='naive DFT  O(N²)')
axL.loglog(Ns, fft_times, color=GREEN, lw=2.2, marker='s', markersize=7,
           label='FFT  O(N log N)')
# 参考线 (拟合斜率)
ref_N = np.array([64, 4096])
# O(N^2) 参考线: 用第一个点标定
ref_dft = dft_times[0] * (ref_N / sizes[0])**2
axL.loglog(ref_N, ref_dft, color=RED, lw=1.0, ls=':', alpha=0.6, label='slope 2  (N²)')
# O(N log N) 参考线
ref_fft = fft_times[0] * (ref_N * np.log2(ref_N)) / (sizes[0] * np.log2(sizes[0]))
axL.loglog(ref_N, ref_fft, color=GREEN, lw=1.0, ls=':', alpha=0.6, label='slope ~1  (N log N)')
axL.set_xlabel('number of points  N  (log scale)', fontsize=11)
axL.set_ylabel('time  (ms, log scale)', fontsize=11)
axL.set_title('FFT vs naive DFT: complexity', fontsize=11)
axL.grid(True, which='both', alpha=0.2)
axL.legend(loc='upper left', fontsize=9)

# 右: 加速比(speedup)随 N 飙升
axR = axes[1]
speedup = np.array(dft_times) / np.array(fft_times)
axR.semilogx(Ns, speedup, color=PURPLE, lw=2.4, marker='o', markersize=8,
             label='measured speedup')
# 理论 N/log2(N) 相对值 (归一化到第一个点)
theory = (Ns / np.log2(Ns)) / (Ns[0] / np.log2(Ns[0]))
theory_scaled = theory * speedup[0]
axR.semilogx(Ns, theory_scaled, color=ORANGE, lw=1.4, ls='--',
             label='∝ N / log₂N  (theory)')
axR.set_xlabel('number of points  N', fontsize=11)
axR.set_ylabel('speedup  (naive DFT time / FFT time)', fontsize=11)
axR.set_title('Speedup grows with N', fontsize=11)
axR.grid(True, which='both', alpha=0.2)
axR.legend(loc='upper left', fontsize=9)
# 标注几个数值
for N, sp in zip(sizes, speedup):
    axR.annotate(f'{sp:.0f}×', xy=(N, sp), xytext=(5, 5),
                 textcoords='offset points', fontsize=8, color=PURPLE)

fig.suptitle('FFT turns O(N²) into O(N log N): speedup explodes as N grows',
             fontsize=11.5, y=1.02)
save(fig, 'fig-5-15-2-fft-vs-dft.png')
print('saved fig-5-15-2')

print('\nAll figures generated.')
