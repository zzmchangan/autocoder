# -*- coding: utf-8 -*-
"""为《物理引擎》P5-17 关节约束 生成一组 PNG 示意图。运行: python gen_p5_17_figures.py
本文件独占, 不碰其他 gen_p*.py. 风格沿用 gen_p0_figures.py."""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch, FancyArrowPatch, Circle, Polygon, Arc, FancyArrow

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 12.0,
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
GREY  = (0.55, 0.55, 0.58)
LGREY = (0.82, 0.82, 0.85)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)
BSOFT = (0.90, 0.94, 1.0)
GSOFT = (0.90, 0.95, 0.90)
YSOFT = (0.99, 0.95, 0.82)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def arrow(ax, p1, p2, color=INK, lw=1.6, rad=0.0, mut=16):
    cs = f"arc3,rad={rad}"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=mut))


def body_box(ax, cx, cy, w, h, label, fc=BSOFT, ec=BLUE, ang=0.0):
    """画一个带标签的物体(矩形), ang 是旋转角(度)."""
    rect = Rectangle((cx - w/2, cy - h/2), w, h, angle=ang,
                     facecolor=fc, edgecolor=ec, linewidth=1.6, zorder=2)
    ax.add_patch(rect)
    ax.text(cx, cy, label, ha="center", va="center", fontsize=11, color=INK, zorder=3)
    # 质心点
    ax.plot(cx, cy, "o", color=INK, ms=3, zorder=4)


def pin(ax, x, y, color=RED, r=0.09):
    """销钉/铰链点."""
    ax.add_patch(Circle((x, y), r, facecolor=color, edgecolor=INK, lw=1.3, zorder=5))


def spring(ax, p1, p2, coils=6, amp=0.12, color=AMBER, lw=1.6):
    """画一根弹簧(锯齿状)从 p1 到 p2."""
    p1 = np.array(p1, dtype=float)
    p2 = np.array(p2, dtype=float)
    d = p2 - p1
    L = np.hypot(*d)
    if L < 1e-6:
        return
    t = d / L
    n = np.array([-t[1], t[0]])
    # 起止各留一段直线
    pad = 0.18
    a0 = p1 + t * pad
    a1 = p2 - t * pad
    inner = np.hypot(*(a1 - a0))
    xs = np.linspace(0, inner, coils * 2 + 1)
    pts = [p1, a0]
    for i, s in enumerate(xs):
        sign = 1.0 if i % 2 == 0 else -1.0
        if i == 0 or i == len(xs) - 1:
            sign = 0.0
        pts.append(a0 + t * s + n * sign * amp)
    pts.append(a1)
    pts.append(p2)
    pts = np.array(pts)
    ax.plot(pts[:, 0], pts[:, 1], color=color, lw=lw, zorder=4)


# ---------------------------------------------------------------- fig-01
def fig_joint_zoo():
    """六种关节示意总图 (filter joint 不画, 它不约束)."""
    fig, axes = plt.subplots(3, 2, figsize=(12.5, 11.5))
    fig.suptitle("Six joint types in Box2D v3.2 — each is a different constraint equation",
                 fontsize=14, weight="bold", y=0.97)

    # 1. Distance 距离关节: 两物体间一根连杆, |pA - pB| = L
    ax = axes[0, 0]
    ax.set_title("(a) Distance joint  |pA - pB| = L", fontsize=12, weight="bold")
    body_box(ax, 1.6, 2.5, 1.4, 1.0, "body A", fc=BSOFT, ec=BLUE)
    body_box(ax, 6.4, 2.5, 1.4, 1.0, "body B", fc=BSOFT, ec=BLUE)
    pA = np.array([2.3, 2.5]); pB = np.array([5.7, 2.5])
    ax.plot([pA[0], pB[0]], [pA[1], pB[1]], color=RED, lw=2.6, zorder=3)
    ax.plot([pA[0]], [pA[1]], "o", color=INK, ms=5, zorder=5)
    ax.plot([pB[0]], [pB[1]], "o", color=INK, ms=5, zorder=5)
    ax.text(4.0, 2.75, "L (fixed length)", ha="center", color=RED, fontsize=11)
    ax.text(4.0, 2.15, "1 scalar constraint", ha="center", color=GREY, fontsize=10, style="italic")
    ax.set_xlim(0, 8); ax.set_ylim(1, 4); ax.set_aspect("equal"); clean_ax(ax)

    # 2. Revolute 铰链: 两点重合 pA = pB (+ 可选角度 motor/limit)
    ax = axes[0, 1]
    ax.set_title("(b) Revolute joint  pA = pB  (hinge)", fontsize=12, weight="bold")
    body_box(ax, 2.6, 2.5, 2.0, 1.2, "body A", fc=BSOFT, ec=BLUE, ang=-12)
    body_box(ax, 5.2, 2.5, 2.0, 1.2, "body B", fc=GSOFT, ec=GREEN, ang=18)
    pin(ax, 3.7, 2.5, color=RED, r=0.13)
    ax.text(3.7, 3.35, "shared pivot\n(2 linear constraints)", ha="center", color=RED, fontsize=10.5)
    ax.text(4.0, 1.35, "can rotate freely around pin", ha="center", color=GREY, fontsize=10, style="italic")
    ax.set_xlim(0, 8); ax.set_ylim(0.8, 4); ax.set_aspect("equal"); clean_ax(ax)

    # 3. Prismatic 平移/滑轨: 沿轴滑动, 垂直轴锁住
    ax = axes[1, 0]
    ax.set_title("(c) Prismatic joint  slide along axis", fontsize=12, weight="bold")
    # 导轨(虚线)
    ax.plot([0.6, 7.4], [2.5, 2.5], color=GREY, lw=2.2, linestyle="--", zorder=1)
    ax.text(0.6, 2.2, "axis (free translation)", color=GREY, fontsize=10)
    body_box(ax, 2.3, 3.1, 1.6, 1.0, "body A", fc=BSOFT, ec=BLUE)
    body_box(ax, 5.6, 1.9, 1.6, 1.0, "body B", fc=GSOFT, ec=GREEN)
    # 垂直方向锁住的箭头(双向禁)
    ax.text(2.3, 3.95, "perp locked", ha="center", color=RED, fontsize=10)
    ax.text(5.6, 1.15, "perp locked", ha="center", color=RED, fontsize=10)
    ax.annotate("", xy=(2.0, 3.7), xytext=(2.0, 3.45), arrowprops=dict(arrowstyle="<->", color=RED, lw=1.4))
    ax.annotate("", xy=(5.9, 1.55), xytext=(5.9, 1.3), arrowprops=dict(arrowstyle="<->", color=RED, lw=1.4))
    # 沿轴可滑的箭头
    arrow(ax, (4.3, 2.5), (4.9, 2.5), color=GREEN, lw=1.8)
    arrow(ax, (4.3, 2.5), (3.7, 2.5), color=GREEN, lw=1.8)
    ax.text(4.3, 2.7, "free slide", ha="center", color=GREEN, fontsize=10)
    ax.set_xlim(0, 8); ax.set_ylim(0.6, 4.2); ax.set_aspect("equal"); clean_ax(ax)

    # 4. Weld 焊接: 完全刚性连接, 3 个标量约束
    ax = axes[1, 1]
    ax.set_title("(d) Weld joint  relative pose locked", fontsize=12, weight="bold")
    body_box(ax, 2.8, 2.5, 1.8, 1.2, "body A", fc=BSOFT, ec=BLUE, ang=10)
    body_box(ax, 4.7, 2.5, 1.8, 1.2, "body B", fc=GSOFT, ec=GREEN, ang=10)
    # 焊缝(粗黑线 + 锯齿)
    ax.plot([3.7, 3.8], [1.9, 3.1], color=INK, lw=2.8, zorder=3)
    for y in np.linspace(1.95, 3.05, 6):
        ax.plot([3.55, 3.7], [y, y + 0.12], color=INK, lw=1.2)
        ax.plot([3.8, 3.95], [y - 0.12, y], color=INK, lw=1.2)
    ax.text(3.75, 3.5, "welded\n(2 linear + 1 angle)", ha="center", color=RED, fontsize=10.5)
    ax.text(4.0, 1.2, "moves as one rigid body", ha="center", color=GREY, fontsize=10, style="italic")
    ax.set_xlim(0, 8); ax.set_ylim(0.6, 4); ax.set_aspect("equal"); clean_ax(ax)

    # 5. Wheel 轮: 沿轴弹簧滑动 + 垂直锁 + 角度电机
    ax = axes[2, 0]
    ax.set_title("(e) Wheel joint  axis + spring (suspension)", fontsize=12, weight="bold")
    # 车身
    body_box(ax, 2.5, 3.3, 2.4, 1.0, "chassis", fc=BSOFT, ec=BLUE)
    # 悬挂轴(虚线, 垂直)
    ax.plot([2.5, 2.5], [2.7, 1.5], color=GREY, lw=2.0, linestyle="--", zorder=1)
    # 弹簧
    spring(ax, (2.5, 2.75), (2.5, 1.7), coils=5, amp=0.16, color=AMBER)
    # 车轮(圆)
    ax.add_patch(Circle((2.5, 1.2), 0.45, facecolor=LGREY, edgecolor=INK, lw=1.6, zorder=2))
    ax.add_patch(Circle((2.5, 1.2), 0.10, facecolor=INK, zorder=3))
    ax.text(2.5, 1.2, "", ha="center", va="center")
    # 标注
    ax.text(3.6, 2.2, "spring\n(soft, hertz/damping)", ha="left", color=AMBER, fontsize=10)
    ax.text(2.5, 0.4, "wheel (rotates freely + motor)", ha="center", color=INK, fontsize=10)
    ax.set_xlim(0, 8); ax.set_ylim(0, 4.2); ax.set_aspect("equal"); clean_ax(ax)

    # 6. Motor 电机: 纯控制, 无几何约束
    ax = axes[2, 1]
    ax.set_title("(f) Motor joint  pure control (no geometry lock)", fontsize=12, weight="bold")
    body_box(ax, 2.8, 2.5, 1.6, 1.0, "body A", fc=BSOFT, ec=BLUE, ang=5)
    body_box(ax, 5.4, 2.5, 1.6, 1.0, "body B", fc=GSOFT, ec=GREEN, ang=-15)
    # 电机箭头(旋转)
    for (cx, cy) in [(2.8, 2.5), (5.4, 2.5)]:
        ax.add_patch(Arc((cx, cy), 1.1, 1.1, angle=0, theta1=40, theta2=320,
                         color=AMBER, lw=1.8))
        arrow(ax, (cx + 0.42, cy + 0.32), (cx + 0.55, cy + 0.42), color=AMBER, lw=1.4, mut=10)
    ax.text(4.1, 3.4, "target relative velocity / pose", ha="center", color=AMBER, fontsize=10.5)
    ax.text(4.1, 1.3, "no fixed anchor — just a motor force/torque", ha="center",
            color=GREY, fontsize=10, style="italic")
    ax.set_xlim(0, 8); ax.set_ylim(0.6, 4); ax.set_aspect("equal"); clean_ax(ax)

    fig.savefig(os.path.join(OUT, "fig-p5_17-01-joint-zoo.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-02
def fig_distance_constraint_math():
    """距离关节约束方程推导: 两点 pA, pB, |pA-pB|=L, Jacobian 和有效质量."""
    fig, ax = plt.subplots(figsize=(12, 6.5))

    # 左半: 几何
    ax.text(3.0, 6.0, "Geometry", ha="center", fontsize=13, weight="bold", color=INK)
    # body A
    body_box(ax, 1.6, 3.0, 1.5, 1.0, "body A", fc=BSOFT, ec=BLUE)
    # body B
    body_box(ax, 5.6, 3.8, 1.5, 1.0, "body B", fc=GSOFT, ec=GREEN)
    # 质心
    cA = np.array([1.6, 3.0]); cB = np.array([5.6, 3.8])
    # 锚点(关节连接点)
    pA = np.array([2.3, 3.0]); pB = np.array([4.9, 3.8])
    # rA = pA - cA, rB = pB - cB
    ax.plot([cA[0], pA[0]], [cA[1], pA[1]], color=BLUE, lw=1.4, linestyle=":")
    ax.plot([cB[0], pB[0]], [cB[1], pB[1]], color=GREEN, lw=1.4, linestyle=":")
    ax.text(1.95, 2.78, "rA", color=BLUE, fontsize=11, style="italic")
    ax.text(5.25, 3.58, "rB", color=GREEN, fontsize=11, style="italic")
    # 连杆 (约束方向 u)
    ax.plot([pA[0], pB[0]], [pA[1], pB[1]], color=RED, lw=2.4, zorder=3)
    ax.plot(pA[0], pA[1], "o", color=INK, ms=6, zorder=5)
    ax.plot(pB[0], pB[1], "o", color=INK, ms=6, zorder=5)
    ax.text(pA[0] - 0.15, pA[1] + 0.15, "pA", fontsize=11, color=INK)
    ax.text(pB[0] + 0.08, pB[1] + 0.15, "pB", fontsize=11, color=INK)
    # L 标注
    mid = (pA + pB) / 2
    ax.text(mid[0], mid[1] - 0.35, "constraint length L", ha="center", color=RED, fontsize=11)
    # 单位方向 u
    u = (pB - pA) / np.hypot(*(pB - pA))
    ax.annotate("", xy=(mid[0] + u[0]*0.6, mid[1] + u[1]*0.6),
                xytext=(mid[0] - u[0]*0.6, mid[1] - u[1]*0.6),
                arrowprops=dict(arrowstyle="->", color=AMBER, lw=1.6))
    ax.text(mid[0] + u[0]*0.9 + 0.05, mid[1] + u[1]*0.9, "u (unit axis)",
            color=AMBER, fontsize=10.5)

    ax.set_xlim(0, 6.5); ax.set_ylim(1.5, 6.5); ax.set_aspect("equal"); clean_ax(ax)

    # 右半: 公式
    formula_x = 6.9
    ax.text(formula_x + 2.5, 6.0, "Constraint equation & Jacobian",
            ha="center", fontsize=13, weight="bold", color=INK)

    ax.text(formula_x, 5.3,
            r"$C = \| p_B - p_A \| - L = 0$",
            fontsize=15, color=INK)
    ax.text(formula_x, 4.7,
            r"velocity error:  $\dot{C} = u \cdot (v_B + \omega_B \times r_B - v_A - \omega_A \times r_A)$",
            fontsize=12, color=INK)
    ax.text(formula_x + 0.2, 4.42, r"where  $u = (p_B - p_A) / \| p_B - p_A \|$",
            fontsize=11, color=GREY)

    ax.text(formula_x, 3.85,
            r"Jacobian row:  $J = [-u,\ -r_A \times u,\ +u,\ +r_B \times u]$",
            fontsize=12, color=INK)
    ax.text(formula_x + 0.2, 3.6, "(maps body velocities to constraint velocity)",
            fontsize=10, color=GREY, style="italic")

    ax.text(formula_x, 3.05,
            r"effective mass:  $K = J M^{-1} J^T$",
            fontsize=12, color=INK)
    ax.text(formula_x + 0.2, 2.78,
            r"$= \frac{1}{m_A} + \frac{1}{m_B} + \frac{(r_A \times u)^2}{I_A} + \frac{(r_B \times u)^2}{I_B}$",
            fontsize=13, color=BLUE)

    ax.text(formula_x, 2.05,
            r"impulse (soft / TGS):",
            fontsize=12, color=INK)
    ax.text(formula_x + 0.2, 1.75,
            r"$\Delta\lambda = -\mathrm{massScale} \cdot K^{-1}(\dot{C} + \mathrm{bias})"
            r" - \mathrm{impulseScale} \cdot \lambda_{old}$",
            fontsize=12.5, color=GREEN)

    ax.text(formula_x, 1.15,
            "=> same shape as contact impulse:  -effMass * (vn + bias)",
            fontsize=10.5, color=GREY, style="italic")

    ax.set_xlim(0, 13); ax.set_ylim(0.5, 6.5)

    fig.suptitle("Distance joint: from geometry |pB - pA| = L  to  impulse via effective mass",
                 fontsize=13.5, weight="bold", y=0.98)
    fig.savefig(os.path.join(OUT, "fig-p5_17-02-distance-constraint-math.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-03
def fig_hard_vs_soft():
    """硬约束直接位置修正 vs SI 软约束冲量: 用 numpy 模拟一个简单距离关节的收敛."""
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.2))

    # 左: 位置硬修正(直接 snap 到 L) -> 抖动
    ax = axes[0]
    L = 1.0
    mA, mB = 1.0, 1.0  # 质量
    # 初始位置: A 固定在 0, B 在 1.4 (比 L 远 0.4)
    xB = 1.4
    vB = 0.0
    dt = 1/60
    frames = 40
    traj_snap = [xB]
    for _ in range(frames - 1):
        # 朴素: 直接把 B snap 回 L
        xB = L
        vB = -vB * 0.5  # 顺手反转速度(魔法)
        # 再积分一步重力让它离开
        vB += 2.0 * dt
        xB += vB * dt
        traj_snap.append(xB)
    ax.plot(range(frames), traj_snap, color=RED, lw=2.0, marker="o", ms=3,
            label="naive: snap xB = L each frame")
    ax.axhline(L, color=GREY, lw=1.2, linestyle="--", label="target L")
    ax.set_xlabel("frame", fontsize=11)
    ax.set_ylabel("xB position", fontsize=11)
    ax.set_title("Hard position snap: jitter, no momentum conservation",
                 fontsize=11.5, weight="bold")
    ax.legend(fontsize=9.5, loc="upper right")
    ax.grid(True, alpha=0.3)
    ax.set_ylim(0.6, 1.7)

    # 右: SI 软约束冲量 -> 平滑收敛
    ax = axes[1]
    # 距离关节: K = 1/mA + 1/mB, effMass = 1/K
    K = 1.0/mA + 1.0/mB
    effMass = 1.0/K
    hertz = 6.0
    zeta = 1.0
    h = dt
    omega = 2*np.pi*hertz
    a1 = 2*zeta + h*omega
    a2 = h*omega*a1
    a3 = 1.0/(1.0+a2)
    massScale = a2*a3
    impulseScale = a3
    biasRate = omega/a1

    xB = 1.4; vB = 0.0
    lam = 0.0
    traj_si = [xB]
    for _ in range(frames - 1):
        # 1) 积分速度(重力)
        vB += 2.0 * h
        # 2) 约束求解 (单约束, 多轮迭代效果近似一轮)
        for _it in range(4):
            C = (xB - L)            # 位置违反(此处 A 在 0)
            Cdot = vB               # 速度违反 (B 相对 A)
            bias = biasRate * C
            dlam = -massScale * effMass * (Cdot + bias) - impulseScale * lam
            lam += dlam
            vB += dlam / mB        # 冲量改变速度
        # 3) 积分位置
        xB += vB * h
        traj_si.append(xB)
    ax.plot(range(frames), traj_si, color=GREEN, lw=2.0, marker="o", ms=3,
            label="SI soft impulse (hertz=6, zeta=1)")
    ax.axhline(L, color=GREY, lw=1.2, linestyle="--", label="target L")
    ax.set_xlabel("frame", fontsize=11)
    ax.set_ylabel("xB position", fontsize=11)
    ax.set_title("Sequential Impulse (soft): smooth, momentum-conserving",
                 fontsize=11.5, weight="bold")
    ax.legend(fontsize=9.5, loc="upper right")
    ax.grid(True, alpha=0.3)
    ax.set_ylim(0.6, 1.7)

    fig.suptitle("Why joints use SI impulse, not hard position correction",
                 fontsize=13, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p5_17-03-hard-vs-soft.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-04
def fig_si_pipeline_joints():
    """关节怎么进 SI 流水线: PrepareJoints / WarmStart / Solve / Relax 都有 joint 分支."""
    fig, ax = plt.subplots(figsize=(12.5, 6.0))

    stages = [
        ("PrepareJoints\n(effective mass\n+ softness)", BSOFT, BLUE),
        ("PrepareContacts\n(effective mass\n+ softness)", BSOFT, BLUE),
        ("IntegrateVelocities\nv += h*a", YSOFT, AMBER),
        ("WarmStart\n(joints + contacts\nshare colors)", GSOFT, GREEN),
        ("Solve x iter x color\n(joints + contacts\ntogether)", GSOFT, GREEN),
        ("IntegratePositions\nx += h*v", YSOFT, AMBER),
        ("Relax x iter x color\n(useBias=false)", GSOFT, GREEN),
        ("Restitution\n(contacts only)", LGREY, GREY),
        ("StoreImpulses\n(contacts only;\njoints already stored)", LGREY, GREY),
    ]
    n = len(stages)
    w = 1.25; h = 1.1; gap = 0.18
    x0 = 0.4
    y0 = 2.4
    centers = []
    for i, (label, fc, ec) in enumerate(stages):
        x = x0 + i * (w + gap)
        ax.add_patch(FancyBboxPatch((x, y0), w, h,
                                    boxstyle="round,pad=0.02,rounding_size=0.06",
                                    facecolor=fc, edgecolor=ec, linewidth=1.5))
        ax.text(x + w/2, y0 + h/2, label, ha="center", va="center",
                fontsize=8.8, color=INK)
        centers.append((x + w/2, y0))
        centers.append((x + w/2, y0 + h))
        if i < n - 1:
            ax.annotate("", xy=(x + w + gap*0.1, y0 + h/2),
                        xytext=(x + w - gap*0.05, y0 + h/2),
                        arrowprops=dict(arrowstyle="->", color=INK, lw=1.4))

    # 子步循环框
    sub_x0 = x0 + 2*(w+gap) - 0.12
    sub_x1 = x0 + 6*(w+gap) + w + 0.12
    ax.add_patch(Rectangle((sub_x0, y0 - 0.18), sub_x1 - sub_x0, h + 0.36,
                           facecolor="none", edgecolor=RED, lw=1.6, linestyle="--"))
    ax.text((sub_x0 + sub_x1)/2, y0 + h + 0.5,
            "repeated subStepCount times per b2World_Step",
            ha="center", fontsize=10.5, color=RED, weight="bold")

    # 上方注释: 关节参与的阶段
    ax.text(x0 + 0.5*(w+gap) + w/2, y0 + h + 1.15, "joints",
            ha="center", fontsize=10, color=BLUE, weight="bold")
    ax.text(x0 + 3.5*(w+gap) + w/2, y0 + h + 1.15, "joints + contacts  (same SI iteration, colored graph)",
            ha="center", fontsize=10, color=GREEN, weight="bold")
    ax.text(x0 + 7*(w+gap) + w/2, y0 + h + 1.15, "contacts only",
            ha="center", fontsize=10, color=GREY, weight="bold")

    # 底部说明
    ax.text((x0 + (n-1)*(w+gap) + w)/2 + 0.2, 0.6,
            "Joints enter the SAME Sequential Impulse pipeline as contacts —\n"
            "only the constraint equation (C, Jacobian J, effective mass K) differs.",
            ha="center", fontsize=11, color=INK, style="italic")

    ax.set_xlim(0, x0 + n*(w+gap) + 0.3)
    ax.set_ylim(0, 4.5)
    ax.set_aspect("equal"); clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p5_17-04-si-pipeline-joints.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_joint_zoo()
    fig_distance_constraint_math()
    fig_hard_vs_soft()
    fig_si_pipeline_joints()
    print("done:", sorted(f for f in os.listdir(OUT) if f.startswith("fig-p5_17")))
