"""图 3.08 · 微积分基本定理(第 3 篇第 8 章)的配图脚本.

两张图:
  1) fig-3-08-1-fundamental-theorem.png
     左: 变上限积分 F(x)=∫_0^x f(t)dt 是"面积累积曲线", 画 f(t)=sin(t) 与其
         变上限积分 F(x)=1-cos(x). 标注"F 的增长率 = f 的高度"——积分与微分互逆.
     右: 牛顿-莱布尼茨: 面积 = 原函数两端之差 F(b)-F(a). 画 f=x^2 on [0,2],
         阴影面积 = 8/3, 标注 F(2)-F(0)=8/3-0.
  2) fig-3-08-2-two-faces.png
     左右对照: 同一枚硬币的两面.
     左: 微分方向 —— 从 F 得到 f=F' (求变化率).
     右: 积分方向 —— 从 f 得到 F=∫f (求累积).
     中间一个箭头表示"互逆操作".

严禁修改 _plot_utils.py. 图内标注一律用英文.
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _plot_utils import plot_fn, plot_curve, riemann_rects, marker, setup_axes, save, \
    GREEN, RED, BLUE, PURPLE, ORANGE
import numpy as np
import sympy as sp
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch


# ---------- 图 1: 变上限积分 + 牛顿-莱布尼茨 ----------
def fig1_fundamental_theorem():
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(12, 5.2))

    # ---- 左图: 变上限积分 F(x)=∫_0^x sin(t)dt = 1 - cos(x) ----
    f = np.sin
    F = lambda x: 1 - np.cos(x)     # F(x) = ∫_0^x sin(t)dt
    a, b = 0.0, 2*np.pi
    # 画 f(t)=sin(t) 作为"被积函数" (蓝), 和 F(x)=1-cos(x) 作为"累积曲线" (绿)
    xx = np.linspace(a, b, 400)
    axL.plot(xx, f(xx), color=BLUE, lw=2.4, label='f(t) = sin(t)  (integrand)')
    axL.plot(xx, F(xx), color=GREEN, lw=2.6, label='F(x) = ∫₀ˣ sin(t)dt = 1−cos(x)')
    # 在 x=π 处, F 达到最大 (=2), 此时 f(π)=0 —— F 的增长率=f 的高度
    x_mark = np.pi
    # 涂阴影: [0, π] 上 sin 下的面积 (=2), 即 F(π)-F(0)
    axL.fill_between(xx[xx <= np.pi], 0, f(xx[xx <= np.pi]),
                     color=ORANGE, alpha=0.25, label='shaded area = F(π)−F(0) = 2')
    marker(axL, x_mark, F(x_mark), color=GREEN, text='F(π)=2 (max)', dx=-0.6, dy=0.15)
    marker(axL, x_mark, f(x_mark), color=BLUE, text="f(π)=0\n(F stops growing)", dx=0.15, dy=-0.4)
    # 竖虚线 x=π
    axL.axvline(x_mark, color='gray', ls=':', lw=1.0)
    setup_axes(axL, xlim=(a, b), ylim=(-1.3, 2.4),
               title='Variable upper limit: F(x)=∫₀ˣ f(t)dt\n'
                     "F's growth rate (slope of green) = f's height (blue)",
               xlabel='x', ylabel='y')
    axL.legend(loc='upper right', fontsize=8.5)

    # ---- 右图: 牛顿-莱布尼茨, f=x^2 on [0,2], 面积=F(2)-F(0)=8/3 ----
    g = lambda t: t**2
    a2, b2 = 0.0, 2.0
    # 用密集矩形涂出面积 (橙色)
    riemann_rects(axR, g, a2, b2, 50, pos='mid', color=ORANGE, alpha=0.30, edge=ORANGE)
    plot_fn(axR, g, a2, b2, color=BLUE, lw=2.4, label='f(x) = x²')
    exact = 8/3
    axR.axhline(exact, color=GREEN, lw=1.4, ls=':', label='area = 8/3 = %.4f' % exact)
    # 标注 F(2)-F(0): F=x^3/3
    F2 = 8/3; F0 = 0
    axR.text(1.0, 3.3, 'antiderivative F = x³/3\n'
                        'F(2) − F(0) = 8/3 − 0 = 8/3\n'
                        '= ∫₀² x² dx  (Newton-Leibniz)',
             fontsize=10, color=PURPLE,
             bbox=dict(boxstyle='round', facecolor='white', edgecolor=PURPLE, alpha=0.9))
    setup_axes(axR, xlim=(a2, b2), ylim=(0, 4.6),
               title='Newton–Leibniz: ∫ₐᵇ f = F(b) − F(a)\n'
                     'area under f = antiderivative evaluated at the two ends',
               xlabel='x', ylabel='f(x)')
    axR.legend(loc='lower right', fontsize=9)
    save(fig, 'fig-3-08-1-fundamental-theorem.png')
    print('[ok] fig-3-08-1-fundamental-theorem.png')


# ---------- 图 2: 一枚硬币的两面 —— 微分与积分互逆 ----------
def fig2_two_faces():
    fig, ax = plt.subplots(figsize=(11, 4.8))
    ax.set_axis_off()
    ax.set_xlim(0, 10); ax.set_ylim(0, 5)

    # 顶部标题
    ax.text(5, 4.6, 'Differentiation and Integration are inverse operations',
            ha='center', fontsize=13, fontweight='bold', color='#222')

    # 左圆: 微分 (从 F 得到 f=F')
    circle_L = plt.Circle((1.8, 2.3), 1.2, facecolor=BLUE, alpha=0.18,
                          edgecolor=BLUE, lw=2.2)
    ax.add_patch(circle_L)
    ax.text(1.8, 2.6, 'F', ha='center', fontsize=20, color=BLUE, fontweight='bold')
    ax.text(1.8, 1.7, '(accumulation)', ha='center', fontsize=9, color='#444')
    ax.text(1.8, 3.95, 'DIFFERENTIATE', ha='center', fontsize=11,
            color=BLUE, fontweight='bold')
    ax.text(1.8, 0.55, "f = F'\n(rate of change)", ha='center', fontsize=9.5, color=BLUE)

    # 中间互逆箭头
    arrow_r = FancyArrowPatch((3.2, 2.7), (5.0, 2.7), arrowstyle='->',
                              mutation_scale=22, color=RED, lw=2.2)
    arrow_l = FancyArrowPatch((5.0, 1.9), (3.2, 1.9), arrowstyle='->',
                              mutation_scale=22, color=GREEN, lw=2.2)
    ax.add_patch(arrow_r); ax.add_patch(arrow_l)
    ax.text(4.1, 3.05, "d/dx", ha='center', fontsize=11, color=RED)
    ax.text(4.1, 1.55, "∫ ... dx", ha='center', fontsize=11, color=GREEN)

    # 右圆: 积分 (从 f 得到 F=∫f)
    circle_R = plt.Circle((6.4, 2.3), 1.2, facecolor=GREEN, alpha=0.18,
                          edgecolor=GREEN, lw=2.2)
    ax.add_patch(circle_R)
    ax.text(6.4, 2.6, 'f', ha='center', fontsize=20, color=GREEN, fontweight='bold')
    ax.text(6.4, 1.7, '(rate)', ha='center', fontsize=9, color='#444')
    ax.text(6.4, 3.95, 'INTEGRATE', ha='center', fontsize=11,
            color=GREEN, fontweight='bold')
    ax.text(6.4, 0.55, 'F = ∫ f dx\n(accumulation)', ha='center', fontsize=9.5, color=GREEN)

    # 右侧方框: 点睛
    ax.text(8.6, 2.3,
            'FTC:\n'
            'd/dx ∫ₐˣ f(t)dt = f(x)\n'
            '∫ₐᵇ f(x)dx = F(b) − F(a)\n'
            '\n'
            'one coin, two faces',
            ha='center', va='center', fontsize=9.5, color='#222',
            bbox=dict(boxstyle='round', facecolor='#fff7e6',
                      edgecolor=ORANGE, alpha=0.95))
    save(fig, 'fig-3-08-2-two-faces.png')
    print('[ok] fig-3-08-2-two-faces.png')


if __name__ == '__main__':
    fig1_fundamental_theorem()
    fig2_two_faces()
    print('all figures generated.')
