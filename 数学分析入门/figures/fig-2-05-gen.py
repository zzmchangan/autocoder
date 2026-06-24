"""P2-05《导数:放大后曲线就变直了》配图生成脚本。

三张图:
  fig-2-05-1-secant-to-tangent.png   —— 割线斜率随 h 缩小逼近切线斜率(h=1,0.5,0.1)
  fig-2-05-2-zoom-in-linear.png      —— x^2 在某点放大 1x/10x/100x,局部越来越像直线
  fig-2-05-3-derivative-function.png —— f=x^2 与 f'=2x,导数本身也是函数
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import (plot_fn, plot_curve, tangent, marker,
                         setup_axes, save, GREEN, RED, BLUE, PURPLE, ORANGE)
import numpy as np, sympy as sp, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 先用 sympy 算对关键量,再喂给绘图
sp_x = sp.symbols('x')
f_sym = sp_x**2
df_sym = sp.diff(f_sym, sp_x)              # 2x
X0 = 1.0                                   # 放大点
f_at_x0 = float(f_sym.subs(sp_x, X0))      # 1
slope = float(df_sym.subs(sp_x, X0))       # 2.0  —— 切线斜率(导数值)
print('[verify] f(x)=x^2, f(1)=%.4f, f_prime(1)=%.4f' % (f_at_x0, slope))

# 数值差商(正文数字的来源,也用于核对图里标的斜率)
print('[verify] secant slopes for f=x^2 at x0=1 (x0+h -> x0):')
for h in [1.0, 0.5, 0.1]:
    q = (f_sym.subs(sp_x, X0 + h) - f_sym.subs(sp_x, X0)) / h
    print('   h=%.2f -> secant slope = %.4f' % (h, float(q)))


# ========== 图 1 · 割线 -> 切线 ==========
fig, ax = plt.subplots(figsize=(7.6, 4.8))

def f(x):
    return x**2

# 曲线
plot_fn(ax, f, -0.3, 2.6, n=400, color=BLUE, lw=2.4, label=r'$y = x^2$')

# 3 条割线: 从 x0 到 x0+h (h 取右上方, 画面更清楚)
# 割线斜率 = (f(x0+h)-f(x0))/h, x^2 在 1 处 = 2+h
secant_colors = [ORANGE, PURPLE, GREEN]
hs = [1.0, 0.5, 0.1]
labels = [r'$h=1.0$: slope $=3.0$',
          r'$h=0.5$: slope $=2.5$',
          r'$h=0.1$: slope $=2.1$']
for h, c, lab in zip(hs, secant_colors, labels):
    x1, y1 = X0, f_at_x0
    x2, y2 = X0 + h, f(X0 + h)
    m = (y2 - y1) / h
    # 延展割线两端, 让它贯穿画面
    span_l = 0.6
    span_r = 0.6
    xa = x1 - span_l
    xb = x2 + span_r
    ya = y1 - m * span_l
    yb = y2 + m * span_r
    ax.plot([xa, xb], [ya, yb], color=c, lw=1.8, ls='-', alpha=0.9,
            label=lab, zorder=3)
    # 割线终点(曲线上)
    ax.scatter([x2], [y2], color=c, s=24, zorder=5)

# 切线: 斜率 = 2 (导数值)
tangent(ax, X0, slope, f_at_x0, span=1.0, color=RED, lw=2.2, ls='--',
        label=r'tangent: slope $=f^\prime(1)=2$')

# 切点
marker(ax, X0, f_at_x0, color=RED, text=r'$(1,1)$', dx=-0.45, dy=-0.35, fs=11)

setup_axes(ax, xlim=(-0.3, 2.7), ylim=(-0.5, 5.0),
           title=r'Secant slopes $\to$ tangent slope as $h\to 0$  (at $x_0=1$)',
           xlabel='x', ylabel='y', grid=True)
ax.legend(fontsize=9, loc='upper left')
save(fig, 'fig-2-05-1-secant-to-tangent.png')


# ========== 图 2 · 放大镜下曲线变直(x^2 在 x=1) ==========
fig, axes = plt.subplots(1, 3, figsize=(12.6, 4.5))
zooms = [(1, r'zoom $\times 1$   (window $\pm 1$)'),
         (10, r'zoom $\times 10$   (window $\pm 0.1$)'),
         (100, r'zoom $\times 100$   (window $\pm 0.01$)')]

for ax, (z, title) in zip(axes, zooms):
    win = 1.0 / z                          # 半窗口宽
    xs = np.linspace(X0 - win, X0 + win, 400)
    ys = f(xs)
    # 纵向: 为了看出"越来越直", 纵窗口也按比例缩, 让斜率看起来一致
    y_half = 2.0 * slope * win             # 纵向半高按切线斜率估
    ax.plot(xs, ys, color=BLUE, lw=2.6, label=r'$x^2$')
    # 切线
    tl = f_at_x0 + slope * (xs - X0)
    ax.plot(xs, tl, color=RED, lw=1.8, ls='--', label=r'tangent $y=1+2(x-1)$')
    ax.scatter([X0], [f_at_x0], color=RED, s=28, zorder=6)
    setup_axes(ax, xlim=(X0 - win, X0 + win),
               ylim=(f_at_x0 - y_half, f_at_x0 + y_half),
               title=title, xlabel='x', ylabel='y', grid=True, equal=False)
    # 度量"有多直": 最大偏差占窗口高比例
    max_dev = float(np.max(np.abs(ys - tl)))
    win_h = 2 * y_half
    ax.text(0.03, 0.05, r'max gap $\approx$ %.2e' % max_dev,
            transform=ax.transAxes, fontsize=9, color=GREEN,
            bbox=dict(boxstyle='round,pad=0.25', fc='white', ec=GREEN, alpha=0.85))
    if z == 1:
        ax.legend(fontsize=8.5, loc='upper left')

fig.suptitle(r'Zoom in on $y=x^2$ at $x_0=1$: curve flattens to its tangent',
             fontsize=13)
fig.tight_layout(rect=[0, 0, 1, 0.95])
fig.savefig(os.path.join(HERE, 'fig-2-05-2-zoom-in-linear.png'), dpi=150)
plt.close(fig)
# 报告三档偏差, 给正文核对
print('[verify] max deviation of x^2 from tangent inside zoom window:')
for z in [1, 10, 100]:
    win = 1.0 / z
    xs = np.linspace(X0 - win, X0 + win, 400)
    dev = np.max(np.abs(f(xs) - (f_at_x0 + slope * (xs - X0))))
    print('   zoom x%d -> max gap = %.3e  (= h^2 at edge = %.3e)' % (z, dev, win**2))


# ========== 图 3 · f 与 f' 都是函数 ==========
fig, ax = plt.subplots(figsize=(7.6, 4.8))

def f2(x):
    return x**2
def df2(x):
    return 2 * x

plot_fn(ax, f2, -2.2, 2.2, n=400, color=BLUE, lw=2.4, label=r'$f(x)=x^2$')
plot_fn(ax, df2, -2.2, 2.2, n=400, color=RED, lw=2.2, ls='--',
        label=r"$f^\prime(x)=2x$")

# 在 x=1 处画一根竖虚线, 标出 f(1) 和 f'(1)
ax.axvline(1.0, color=GRAY if False else 'gray', lw=0.9, ls=':', alpha=0.7)
marker(ax, 1.0, f2(1.0), color=BLUE, text=r'$f(1)=1$', dx=0.12, dy=0.25, fs=10)
marker(ax, 1.0, df2(1.0), color=RED, text=r"$f^\prime(1)=2$", dx=0.12, dy=-0.45, fs=10)

# 一段切线示意: 在 x=1 处, 斜率=f'(1)=2
tangent(ax, 1.0, 2.0, 1.0, span=0.55, color=ORANGE, lw=1.6, ls='-',
        label=r'tangent at $x=1$  (slope $=f^\prime(1)$)')

setup_axes(ax, xlim=(-2.2, 2.2), ylim=(-1.5, 4.6),
           title=r"The derivative is itself a function:  $f(x)=x^2,\ f^\prime(x)=2x$",
           xlabel='x', ylabel='y', grid=True)
ax.legend(fontsize=9.5, loc='upper center')
save(fig, 'fig-2-05-3-derivative-function.png')

print('\n[done] three figures generated.')
