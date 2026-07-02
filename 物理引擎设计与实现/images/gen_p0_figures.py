# -*- coding: utf-8 -*-
"""为《物理引擎》P0-01 生成一组 PNG 示意图。运行: python gen_p0_figures.py"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch, FancyArrowPatch, Circle

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 12.5,
    "axes.linewidth": 1.2,
    "figure.dpi": 150,
    "savefig.dpi": 150,
    "savefig.bbox": "tight",
    "savefig.facecolor": "white",
    "savefig.pad_inches": 0.15,
})

OUT = os.path.dirname(os.path.abspath(__file__))

RED   = (0.86, 0.18, 0.18)
GREEN = (0.20, 0.70, 0.32)
BLUE  = (0.20, 0.45, 0.88)
AMBER = (0.95, 0.70, 0.18)
GREY  = (0.78, 0.78, 0.80)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)
BSOFT = (0.90, 0.94, 1.0)   # 检测侧浅蓝
GSOFT = (0.90, 0.95, 0.90)  # 响应侧浅绿


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=12, weight="normal", tc=INK):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=fc, edgecolor=ec, linewidth=1.4))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.6, rad=0.0):
    cs = f"arc3,rad={rad}"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=16))


# ---------------------------------------------------------------- fig-01
def fig_detect_respond():
    fig, ax = plt.subplots(figsize=(11.5, 6.2))
    # 左: 检测
    ax.add_patch(FancyBboxPatch((0.3, 1.2), 5.0, 4.6, boxstyle="round,pad=0.05,rounding_size=0.12",
                                facecolor=BSOFT, edgecolor=BLUE, linewidth=2.0))
    ax.text(2.8, 5.5, "Detection", ha="center", fontsize=15,
            weight="bold", color=BLUE)
    ax.text(2.8, 5.12, "who collides, where, how deep", ha="center",
            fontsize=10.5, color=INK)
    box(ax, 0.8, 3.8, 4.0, 0.8, "Broad phase\nAABB + spatial split (coarse)", fc="white")
    box(ax, 0.8, 2.6, 4.0, 0.8, "Narrow phase\nSAT / GJK + contact manifold", fc="white")
    arrow(ax, (2.8, 3.8), (2.8, 3.45), color=BLUE, lw=1.8)
    ax.text(2.8, 1.55, "=> candidate pairs\n=> exact contacts", ha="center",
            fontsize=10.5, color=BLUE)

    # 右: 响应
    ax.add_patch(FancyBboxPatch((6.2, 1.2), 5.0, 4.6, boxstyle="round,pad=0.05,rounding_size=0.12",
                                facecolor=GSOFT, edgecolor=GREEN, linewidth=2.0))
    ax.text(8.7, 5.5, "Response", ha="center", fontsize=15,
            weight="bold", color=GREEN)
    ax.text(8.7, 5.12, "how things move after collision", ha="center",
            fontsize=10.5, color=INK)
    box(ax, 6.7, 3.8, 4.0, 0.8, "Dynamics integration\n(symplectic Euler / Verlet)", fc="white")
    box(ax, 6.7, 2.6, 4.0, 0.8, "Constraint solving\nSequential Impulse (no overlap)", fc="white")
    arrow(ax, (8.7, 3.8), (8.7, 3.45), color=GREEN, lw=1.8)
    ax.text(8.7, 1.55, "=> advance motion\n=> resolve, bounce, no tunnel", ha="center",
            fontsize=10.5, color=GREEN)

    # 底部: 一个时间步流转
    ax.text(5.85, 0.55, "one timestep:  integrate (Response) -> detect -> solve (Response) -> next dt",
            ha="center", fontsize=11.5, color=INK, weight="bold")
    ax.set_xlim(0, 11.5); ax.set_ylim(0, 6)
    ax.set_aspect("equal"); clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-01-detect-respond.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-02
def fig_integrator_stability():
    fig, ax = plt.subplots(figsize=(10, 5.4))
    dt = 0.35
    n = 60
    t = np.arange(n) * dt
    # 真实简谐 x''=-x: x=cos(t)
    x_true = np.cos(t)
    # 显式欧拉(弹簧振子): v-=x*dt; x+=v_old*dt  -> 发散
    x_exp = np.zeros(n); v = 0.0; x = 1.0
    for i in range(n):
        x_exp[i] = x
        v_new = v - x * dt
        x_new = x + v * dt
        v, x = v_new, x_new
    # 半隐式(symplectic)欧拉: v-=x*dt; x+=v_new*dt  -> 稳定
    x_sym = np.zeros(n); v = 0.0; x = 1.0
    for i in range(n):
        x_sym[i] = x
        v = v - x * dt
        x = x + v * dt
    ax.plot(t, x_true, color=BLUE, lw=2.4, label="exact physics (energy conserved)")
    ax.plot(t, x_sym, color=GREEN, lw=2.0, linestyle="--",
            label="symplectic Euler (stable, bounded)")
    ax.plot(t, x_exp, color=RED, lw=2.2, linestyle=":",
            label="explicit Euler (energy blows up!)")
    ax.set_xlim(0, t[-1]); ax.set_ylim(-8, 14)
    ax.set_xlabel("time", fontsize=12)
    ax.set_ylabel("position", fontsize=12)
    ax.axhline(0, color=GREY, lw=0.8)
    ax.set_title("Integrator stability: explicit Euler explodes, symplectic stays bounded",
                 fontsize=13, weight="bold")
    ax.legend(fontsize=11, loc="upper left")
    ax.grid(True, alpha=0.3)
    fig.savefig(os.path.join(OUT, "fig-02-integrator-stability.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-03
def fig_ball_bounce():
    fig, ax = plt.subplots(figsize=(8.5, 6.2))
    # 地面
    ax.add_patch(Rectangle((0, 0), 9, 0.5, facecolor=(0.55, 0.45, 0.35), edgecolor=INK))
    ax.text(8.7, 0.25, "ground y=0", fontsize=10, va="center", color="white", ha="right")
    # 下落的小球位置(从上到下)
    falls = [(4.5, 7.0), (4.5, 5.5), (4.5, 3.8), (4.5, 2.0)]
    for i, (bx, by) in enumerate(falls):
        ax.add_patch(Circle((bx, by), 0.4, facecolor=RED, edgecolor=INK, alpha=0.5))
    # 撞地点
    ax.add_patch(Circle((4.5, 1.0), 0.4, facecolor=RED, edgecolor=INK, lw=2))
    # 反弹位置
    rises = [(4.5, 2.6), (4.5, 4.2), (4.5, 5.6)]
    for (bx, by) in rises:
        ax.add_patch(Circle((bx, by), 0.4, facecolor=GREEN, edgecolor=INK, alpha=0.5))
    # 下落速度箭头(向下)
    arrow(ax, (5.3, 5.5), (5.3, 4.2), color=RED, lw=2.0)
    ax.text(5.5, 4.9, "before:\nv = (0, -10)\n(moving down)", fontsize=10.5,
            color=RED, va="center")
    # 撞地冲量(向上)
    arrow(ax, (3.7, 1.0), (3.7, 2.6), color=AMBER, lw=2.4)
    ax.text(2.0, 1.8, "constraint solve:\nimpulse along\ncontact normal (up)",
            fontsize=10.5, color=AMBER, va="center", ha="center")
    # 反弹速度箭头(向上)
    arrow(ax, (5.3, 2.6), (5.3, 3.9), color=GREEN, lw=2.0)
    ax.text(5.5, 3.3, "after:\nv = (0, +8)\n(bounce up)", fontsize=10.5,
            color=GREEN, va="center")
    ax.set_xlim(0, 9); ax.set_ylim(0, 8)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("Ball bounce: impulse flips velocity at impact",
                 fontsize=13, weight="bold")
    fig.savefig(os.path.join(OUT, "fig-03-ball-bounce.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_detect_respond()
    fig_integrator_stability()
    fig_ball_bounce()
    print("done:", sorted(f for f in os.listdir(OUT) if f.endswith(".png")))
