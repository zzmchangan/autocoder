"""第 7 篇 · 第 20 章 · Hilbert 空间 —— 配图脚本.

两张图:
  fig-7-20-1-projection.png  —— 投影 = 最佳逼近(有限维示意 + 锯齿函数在正弦基上的 N 项最佳逼近)
  fig-7-20-2-fourier-L2.png  —— 傅里叶 = L^2 正交分解: 锯齿函数及其前 N 项正弦级数逼近
注意: 图内标注一律英文, 正文用中文.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, plot_curve, marker, setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# =====================================================================
# 图 1: 投影 = 最佳逼近
# =====================================================================
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.6))

# 左图: 有限维直觉 —— f 投影到一维子空间 span{e}, 投影点 p 是最近点, 误差正交
# 在二维平面里画: e = (1, 0) 方向, f = (1.2, 0.9)
e = np.array([1.0, 0.0])
f = np.array([1.2, 0.9])
proj = (np.dot(f, e)) * e           # 投影 p
axL.annotate('', xy=e, xytext=(0, 0),
             arrowprops=dict(arrowstyle='->', color=GREEN, lw=2.2))
axL.text(0.55, -0.12, 'basis $e$', color=GREEN, fontsize=11)
axL.annotate('', xy=f, xytext=(0, 0),
             arrowprops=dict(arrowstyle='->', color=BLUE, lw=2.2))
axL.text(0.95, 0.95, '$f$', color=BLUE, fontsize=13)
# 投影点
marker(axL, proj[0], proj[1], color=PURPLE, text='projection $p$', dx=0.05, dy=-0.18, fs=10)
# 误差线 f - p (正交于 e)
axL.plot([proj[0], f[0]], [proj[1], f[1]], color=RED, lw=2.0, ls='--')
axL.text(1.25, 0.42, 'error $f-p \\perp e$', color=RED, fontsize=10)
# 直角标记
axL.plot([proj[0]+0.08, proj[0]+0.08, proj[0]],
         [proj[1], proj[1]+0.08, proj[1]+0.08], color=RED, lw=1.2)
setup_axes(axL, xlim=(-0.3, 1.8), ylim=(-0.5, 1.3), equal=True,
           title='projection = closest point, error is orthogonal',
           xlabel='', ylabel='', grid=True)

# 右图: 锯齿函数 f(x)=x-pi 在正弦基上的 N 项最佳逼近 —— 投影越多越贴合
x = np.linspace(0, 2 * np.pi, 1200)
f = x - np.pi

def partial(N):
    s = np.zeros_like(x)
    for n in range(1, N + 1):
        bn = 2 * (-1) ** (n + 1) / n
        s = s + bn * np.sin(n * x)
    return s

axR.plot(x, f, color=BLUE, lw=2.2, label='$f(x)=x-\\pi$')
for N, c in [(1, ORANGE), (3, PURPLE), (10, GREEN)]:
    axR.plot(x, partial(N), color=c, lw=1.5,
             label='L² projection, %d terms' % N)
setup_axes(axR, xlim=(0, 2 * np.pi), ylim=(-np.pi - 0.3, np.pi + 0.3),
           title='best L² approximation by sine basis',
           xlabel='$x$', ylabel='$y$', grid=True)
axR.legend(loc='upper left', fontsize=9)

fig.suptitle('Inner product -> orthogonal projection -> best approximation',
             fontsize=12.5, y=1.02)
save(fig, 'fig-7-20-1-projection.png')


# =====================================================================
# 图 2: 傅里叶 = L^2 正交分解
# =====================================================================
fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.6))

# 左图: 锯齿函数被分解成正弦基上的坐标(系数)
x = np.linspace(0, 2 * np.pi, 1200)
f = x - np.pi
axL.plot(x, f, color=BLUE, lw=2.2, label='$f(x)=x-\\pi$')

ns = np.arange(1, 9)
coeffs = 2 * (-1) ** (ns + 1) / ns            # b_n = 2(-1)^(n+1)/n
parts = np.zeros_like(x)
for n, bn in zip(ns, coeffs):
    comp = bn * np.sin(n * x)
    parts = parts + comp
axL.plot(x, parts, color=GREEN, lw=1.8,
         label='sum of %d orthogonal components' % len(ns))
setup_axes(axL, xlim=(0, 2 * np.pi), ylim=(-np.pi - 0.3, np.pi + 0.3),
           title='f equals its Fourier series (in L²)',
           xlabel='$x$', ylabel='$y$', grid=True)
axL.legend(loc='upper left', fontsize=9)

# 右图: 系数 b_n 作为正交基上的"坐标" —— 频谱
ns2 = np.arange(1, 16)
coeffs2 = 2 * (-1) ** (ns2 + 1) / ns2
marker_h, stems, base = axR.stem(ns2, coeffs2, linefmt=PURPLE,
                                 markerfmt='o', basefmt=' ')
plt.setp(stems, 'linewidth', 1.8)
setup_axes(axR, xlim=(0, 16), ylim=(-2.3, 2.3),
           title='coordinates on the orthogonal basis (spectrum)',
           xlabel='harmonic $n$', ylabel='$b_n$', grid=True)
axR.set_title('coordinates on orthogonal basis $\\{\\sin nx\\}$ (spectrum)',
              fontsize=11)

fig.suptitle('Fourier series = orthogonal decomposition in $L^2$',
             fontsize=12.5, y=1.02)
save(fig, 'fig-7-20-2-fourier-L2.png')

print('done: fig-7-20-1, fig-7-20-2')
