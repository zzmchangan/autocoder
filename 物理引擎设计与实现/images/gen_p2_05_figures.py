# -*- coding: utf-8 -*-
"""为《物理引擎》P2-05(刚体动力学:质量、惯性、力矩)生成 PNG 示意图。

独立成文件,避免与同篇 P2-06/07/08 的图脚本冲突。
运行: python gen_p2_05_figures.py

输出:
  fig-p205-body-state.png        刚体状态量全貌(position/theta/v/omega + b2BodySim/b2BodyState 拆分)
  fig-p205-parallel-axis.png     平行轴定理累加转动惯量(各 shape I_cm 平移到 CoM 求和)
  fig-p205-linear-vs-angular.png 平动 vs 转动物理量完全对应(F=ma <-> tau=I*alpha + 倒数技巧)
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import (FancyArrowPatch, Circle, Rectangle,
                                Polygon as MplPolygon, FancyBboxPatch)

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

RED    = (0.86, 0.18, 0.18)
GREEN  = (0.20, 0.70, 0.32)
BLUE   = (0.20, 0.45, 0.88)
AMBER  = (0.95, 0.70, 0.18)
PURPLE = (0.55, 0.30, 0.80)
GREY   = (0.78, 0.78, 0.80)
INK    = (0.12, 0.12, 0.12)
SOFT   = (0.95, 0.95, 0.97)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def arrow(ax, p1, p2, color=INK, lw=1.7, rad=0.0, mut=18):
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=f"arc3,rad={rad}",
                                  mutation_scale=mut))


# ========================================================================
#  图1: 刚体状态量全貌
# ========================================================================
def fig_p205_body_state():
    fig, (ax, axr) = plt.subplots(1, 2, figsize=(12.8, 6.2),
                                  gridspec_kw={"width_ratios": [1.35, 1.0]})

    # ---- 左:刚体几何 + 运动学量 ----
    # L 形复合刚体(两个矩形拼成),绕质心旋转约 25 度
    th = np.deg2rad(25)
    Rm = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])

    def rect_pts(cx, cy, w, h):
        return np.array([[cx - w/2, cy - h/2], [cx + w/2, cy - h/2],
                         [cx + w/2, cy + h/2], [cx - w/2, cy + h/2]])

    pivot = np.array([3.6, 3.1])
    block1 = (rect_pts(-0.4, 0.3, 1.6, 0.7) @ Rm.T) + pivot
    block2 = (rect_pts(0.1, -0.5, 0.7, 1.4) @ Rm.T) + pivot
    ax.add_patch(MplPolygon(block1, closed=True, facecolor=SOFT,
                            edgecolor=INK, linewidth=1.8))
    ax.add_patch(MplPolygon(block2, closed=True, facecolor=SOFT,
                            edgecolor=INK, linewidth=1.8))

    # 质心(旋转后大致在两块拼接处)
    com = np.array([3.62, 3.05])
    ax.add_patch(Circle(com, 0.07, color=RED, zorder=5))
    ax.text(com[0] - 0.05, com[1] - 0.42, "CoM (center of mass)",
            ha="center", fontsize=11.5, color=RED, weight="bold")

    # 世界坐标系原点
    origin = np.array([0.0, 0.0])
    ax.add_patch(Circle(origin, 0.06, color=GREY, zorder=4))
    ax.text(0.10, -0.32, "world origin", fontsize=10, color=GREY)
    ax.plot([origin[0], com[0]], [origin[1], com[1]], color=GREY,
            lw=1.0, linestyle=":", zorder=1)

    # position 标注(body origin 的世界坐标)
    ax.add_patch(Circle(pivot, 0.05, color=BLUE, zorder=5))
    ax.text(pivot[0] + 0.12, pivot[1] + 0.22,
            "body origin\nposition = (x, y)", fontsize=10.5, color=BLUE)

    # 朝向角 theta
    ref_len = 1.5
    ax.plot([com[0], com[0] + ref_len], [com[1], com[1]],
            color=GREY, lw=0.9, linestyle="--")
    arc_th = np.linspace(0, th, 40)
    ax.plot(com[0] + 0.9*np.cos(arc_th), com[1] + 0.9*np.sin(arc_th),
            color=PURPLE, lw=1.8)
    ax.text(com[0] + 0.95, com[1] + 0.42, r"$\theta$",
            fontsize=15, color=PURPLE, weight="bold")
    ax.text(com[0] + 0.55, com[1] - 0.30, "rotation q = (cos$\\theta$, sin$\\theta$)",
            fontsize=9.8, color=PURPLE)

    # 线速度向量 v(从 CoM 出发)
    v = np.array([1.7, 1.05])
    arrow(ax, com, com + v, color=GREEN, lw=2.4, mut=20)
    ax.text(com[0] + v[0] + 0.08, com[1] + v[1] + 0.06,
            "linear velocity  v", fontsize=11.5, color=GREEN, weight="bold")

    # 角速度 omega 弯箭头(绕 CoM)
    om_arc = np.linspace(np.deg2rad(110), np.deg2rad(340), 40)
    om_r = 0.55
    pts = np.stack([com[0] + om_r*np.cos(om_arc),
                    com[1] + om_r*np.sin(om_arc)], 1)
    ax.plot(pts[:, 0], pts[:, 1], color=AMBER, lw=2.2)
    arrow(ax, pts[-2], pts[-1], color=AMBER, lw=2.2, mut=16)
    ax.text(com[0] - 1.15, com[1] + 0.55,
            r"angular velocity  $\omega$" + "\n(radians / s)",
            fontsize=10.8, color=AMBER, weight="bold")

    ax.set_xlim(-0.8, 6.6); ax.set_ylim(-0.9, 5.4)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("Rigid body state:  position / rotation / v / $\\omega$",
                 fontsize=13, weight="bold")

    # ---- 右:Box2D 把状态量拆进两个结构体 ----
    axr.set_xlim(0, 10); axr.set_ylim(0, 10); clean_ax(axr)

    axr.add_patch(FancyBboxPatch((0.2, 5.2), 9.6, 4.4,
                  boxstyle="round,pad=0.05,rounding_size=0.15",
                  facecolor=(0.93, 0.95, 1.0), edgecolor=BLUE, linewidth=1.6))
    axr.text(5.0, 9.2, "b2BodySim  (slow / rarely-changing)",
             ha="center", fontsize=12.5, weight="bold", color=BLUE)
    slow = ["transform   (position + rotation)", "center, localCenter  (CoM)",
            "invMass, invInertia   (1/m, 1/I)", "force, torque   (accumulated)",
            "linearDamping, gravityScale ..."]
    for i, t in enumerate(slow):
        axr.text(0.7, 8.45 - i*0.62, "  " + t, fontsize=10.8, color=INK,
                 family="monospace")

    axr.add_patch(FancyBboxPatch((0.2, 0.3), 9.6, 4.4,
                  boxstyle="round,pad=0.05,rounding_size=0.15",
                  facecolor=(0.93, 0.96, 0.92), edgecolor=GREEN, linewidth=1.6))
    axr.text(5.0, 4.3, "b2BodyState  (fast / integrated each substep)",
             ha="center", fontsize=12.5, weight="bold", color=GREEN)
    fast = ["linearVelocity   (v)", "angularVelocity   ($\\omega$)",
            "deltaPosition    (x += h*v)", "deltaRotation    (q += h*$\\omega$)",
            "flags   (locked? dynamic?)"]
    for i, t in enumerate(fast):
        axr.text(0.7, 3.55 - i*0.62, "  " + t, fontsize=10.8, color=INK,
                 family="monospace")

    fig.savefig(os.path.join(OUT, "fig-p205-body-state.png"))
    plt.close(fig)


# ========================================================================
#  图2: 平行轴定理累加转动惯量
# ========================================================================
def fig_p205_parallel_axis():
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(13.4, 6.0))

    # 整刚体质心(两图共用位置)
    com = np.array([6.6, 3.0])

    # 三个 shape:(局部质心相对 com, 半径/尺寸, 类型, m, I_cm, 颜色)
    shapes = [
        (np.array([-1.7, 0.9]), 0.55, "circle", 1.0, 0.150, BLUE),
        (np.array([1.5, 1.1]), None,  "rect",   1.4, 0.260, GREEN),
        (np.array([0.2, -1.3]), 0.45, "circle", 0.8, 0.081, PURPLE),
    ]

    # ---------- 左:每个 shape 单独绕自己的质心旋转 ----------
    axL.set_xlim(2.2, 9.0); axL.set_ylim(0.6, 5.4)
    axL.set_aspect("equal"); clean_ax(axL)
    axL.set_title("Each shape has its own  $I_{cm}$  about its own centroid",
                  fontsize=12.5, weight="bold")
    for off, rad, kind, m, Icm, col in shapes:
        c = com + off
        if kind == "circle":
            axL.add_patch(Circle(c, rad, facecolor="white",
                                 edgecolor=col, linewidth=2.0))
        else:
            w = h = 1.0
            axL.add_patch(Rectangle((c[0]-w/2, c[1]-h/2), w, h,
                                    facecolor="white", edgecolor=col, linewidth=2.0))
        axL.add_patch(Circle(c, 0.05, color=col, zorder=5))
        axL.text(c[0], c[1] - 0.62,
                 f"m={m}\n$I_{{cm}}$={Icm:.3f}", ha="center",
                 fontsize=10, color=col, weight="bold")
        # 绕自身质心的小弯箭头
        ang = np.linspace(0.7, 4.6, 30)
        rr = 0.28
        axL.plot(c[0] + rr*np.cos(ang), c[1] + rr*np.sin(ang),
                 color=col, lw=1.6, alpha=0.8)

    # ---------- 右:平移到整刚体质心后累加 ----------
    axR.set_xlim(2.2, 9.0); axR.set_ylim(0.6, 5.4)
    axR.set_aspect("equal"); clean_ax(axR)
    axR.set_title("Shift each to the body CoM:  $I = I_{cm} + m\\,d^{2}$,  then sum",
                  fontsize=12.5, weight="bold")

    axR.add_patch(Circle(com, 0.10, color=RED, zorder=6))
    axR.text(com[0] - 0.1, com[1] - 0.42, "body CoM",
             ha="center", fontsize=11, color=RED, weight="bold")

    total_I = 0.0
    total_m = 0.0
    for off, rad, kind, m, Icm, col in shapes:
        c = com + off
        d = float(np.linalg.norm(off))
        I_shifted = Icm + m * d * d
        total_I += I_shifted
        total_m += m
        if kind == "circle":
            axR.add_patch(Circle(c, rad, facecolor=(col[0], col[1], col[2], 0.10),
                                 edgecolor=col, linewidth=1.4))
        else:
            w = h = 1.0
            axR.add_patch(Rectangle((c[0]-w/2, c[1]-h/2), w, h,
                                    facecolor=(col[0], col[1], col[2], 0.10),
                                    edgecolor=col, linewidth=1.4))
        axR.add_patch(Circle(c, 0.05, color=col, zorder=5))
        axR.plot([c[0], com[0]], [c[1], com[1]], color=col,
                 lw=1.3, linestyle="--", alpha=0.85)
        mid = (c + com) / 2
        axR.text(mid[0] + 0.10, mid[1] + 0.12,
                 f"d={d:.2f}", fontsize=9.5, color=col)
        xtext = c[0] + 0.30 if c[0] > com[0] else c[0] - 1.75
        axR.text(xtext, c[1] + 0.30,
                 f"I = {Icm:.3f} + {m}*{d**2:.2f}\n  = {I_shifted:.3f}",
                 fontsize=9.3, color=col, weight="bold")

    axR.text(2.4, 5.05,
             f"total mass      M = {total_m:.1f}\n"
             f"total inertia   I = {total_I:.3f}",
             fontsize=11.5, color=INK, weight="bold",
             family="monospace",
             bbox=dict(boxstyle="round,pad=0.4", facecolor=SOFT, edgecolor=INK))

    fig.suptitle("Parallel-axis theorem:  why each shape's $I_{cm}$ must be "
                 "shifted to the body CoM before summing",
                 fontsize=13.5, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p205-parallel-axis.png"))
    plt.close(fig)


# ========================================================================
#  图3: 平动 vs 转动物理量完全对应
# ========================================================================
def fig_p205_linear_vs_angular():
    fig, ax = plt.subplots(figsize=(11.8, 7.0))
    ax.set_xlim(0, 12); ax.set_ylim(0, 10); clean_ax(ax)

    ax.text(6.0, 9.55, "Linear motion  vs  Angular motion  (perfect parallel)",
            ha="center", fontsize=13.5, weight="bold", color=INK)

    ax.text(3.0, 8.7, "LINEAR  (translation)", ha="center",
            fontsize=12.5, weight="bold", color=BLUE)
    ax.text(9.0, 8.7, "ANGULAR  (rotation)", ha="center",
            fontsize=12.5, weight="bold", color=GREEN)

    rows = [
        ("position",  "x  (m)",                 "angle",               r"$\theta$  (rad)"),
        ("velocity",  "v = dx/dt  (m/s)",       "angular velocity",    r"$\omega = d\theta/dt$  (rad/s)"),
        ("mass",      "m  (kg)",                "moment of inertia",   r"I  (kg$\cdot$m$^2$)"),
        ("force",     "F  (N)",                 "torque",              r"$\tau$  (N$\cdot$m)"),
        ("Newton II", "F = m a",                "Newton II (rot)",     r"$\tau = I\,\alpha$"),
    ]
    y0 = 7.7
    dy = 1.25
    for i, (ln, lval, an, aval) in enumerate(rows):
        y = y0 - i*dy
        ax.add_patch(FancyBboxPatch((0.4, y-0.42), 5.2, 0.84,
                     boxstyle="round,pad=0.02,rounding_size=0.06",
                     facecolor=(0.93, 0.95, 1.0), edgecolor=BLUE, linewidth=1.2))
        ax.text(0.75, y+0.12, ln, fontsize=10.3, color=BLUE, weight="bold")
        ax.text(0.75, y-0.20, lval, fontsize=10.8, color=INK, family="monospace")
        ax.add_patch(FancyBboxPatch((6.4, y-0.42), 5.2, 0.84,
                     boxstyle="round,pad=0.02,rounding_size=0.06",
                     facecolor=(0.93, 0.96, 0.92), edgecolor=GREEN, linewidth=1.2))
        ax.text(6.75, y+0.12, an, fontsize=10.3, color=GREEN, weight="bold")
        ax.text(6.75, y-0.20, aval, fontsize=10.8, color=INK, family="monospace")
        arrow(ax, (5.7, y), (6.3, y), color=GREY, lw=1.6, mut=14)

    ax.text(6.0, 0.85,
            "Box2D stores the RECIPROCALS  invMass = 1/m  and  invInertia = 1/I\n"
            "so Newton's law becomes   a = invMass * F,   $\\alpha$ = invInertia * $\\tau$\n"
            "(no division each step — division is slow)",
            ha="center", fontsize=10.8, color=RED, weight="bold",
            bbox=dict(boxstyle="round,pad=0.45", facecolor=(1.0, 0.96, 0.93),
                      edgecolor=RED, linewidth=1.2))

    fig.savefig(os.path.join(OUT, "fig-p205-linear-vs-angular.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_p205_body_state()
    fig_p205_parallel_axis()
    fig_p205_linear_vs_angular()
    print("done:", sorted(f for f in os.listdir(OUT) if f.endswith(".png")))
