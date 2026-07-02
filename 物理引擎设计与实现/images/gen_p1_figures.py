# -*- coding: utf-8 -*-
"""为《物理引擎》P1-02(物理引擎在干什么:牛顿方程与离散时间)生成 PNG 示意图。

运行: python gen_p1_figures.py
风格对齐 P0 的 gen_p0_figures.py(固定调色 / DejaVu Sans / clean_ax / box / arrow helper / English labels)。
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Circle

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
BSOFT = (0.90, 0.94, 1.0)   # detection side light blue
GSOFT = (0.90, 0.95, 0.90)  # response side light green


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
# 自由落体 x'' = -g:解析连续轨迹 x(t) = x0 + v0 t - 0.5 g t^2
# vs 离散点轨迹(显式欧拉递推 x_{n+1}=x_n + v_n dt; v_{n+1}=v_n - g dt)
def fig_freefall_continuous_vs_discrete():
    g = 9.8
    x0 = 20.0
    v0 = 0.0
    T = 2.0  # 总时长
    # 解析连续轨迹(细密采样)
    t_cont = np.linspace(0, T, 500)
    x_cont = x0 + v0 * t_cont - 0.5 * g * t_cont ** 2

    fig, ax = plt.subplots(figsize=(10.5, 6.0))
    ax.plot(t_cont, x_cont, color=BLUE, lw=2.6, zorder=2,
            label="analytic: $x(t)=x_0+v_0t-\\frac{1}{2}gt^2$ (continuous)")

    # 三种 dt 的离散点(显式欧拉递推)
    dts = [0.5, 0.2, 0.05]
    colors = [RED, AMBER, GREEN]
    labels = ["dt=0.50 (coarse, drifts)", "dt=0.20 (medium)", "dt=0.05 (fine, on the curve)"]
    markers = ["o", "s", "^"]
    for dt, c, lab, mk in zip(dts, colors, labels, markers):
        n = int(round(T / dt)) + 1
        t = np.array([i * dt for i in range(n)])
        x = np.zeros(n)
        v = np.zeros(n)
        x[0] = x0; v[0] = v0
        for i in range(n - 1):
            v[i + 1] = v[i] - g * dt        # v_{n+1} = v_n - g dt
            x[i + 1] = x[i] + v[i] * dt      # explicit Euler: 用旧速度推位置
        ax.plot(t, x, color=c, lw=0.0, marker=mk, ms=8.5,
                markerfacecolor=c, markeredgecolor="white", mew=1.2,
                zorder=3, label=lab)

    ax.axhline(0, color=(0.55, 0.45, 0.35), lw=6.0, alpha=0.55, zorder=1)
    ax.text(1.92, 0.9, "ground x=0", fontsize=10, color="white",
            va="center", ha="right", zorder=4)
    ax.set_xlim(-0.05, T + 0.05)
    ax.set_ylim(-4, x0 + 3)
    ax.set_xlabel("time t (s)", fontsize=12.5)
    ax.set_ylabel("height x (m)", fontsize=12.5)
    ax.set_title("Free fall: continuous curve vs discrete timesteps (explicit Euler)",
                 fontsize=13, weight="bold")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=10.5, loc="upper right", framealpha=0.95)
    fig.savefig(os.path.join(OUT, "fig-01-freefall-continuous-vs-discrete.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-02
# 一个时间步内的递推示意(半隐式欧拉): 先用加速度更新速度, 再用新速度更新位置
def fig_recurrence_scheme():
    fig, ax = plt.subplots(figsize=(12.5, 4.6))
    # 时间轴
    ax.annotate("", xy=(12.2, 2.5), xytext=(0.2, 2.5),
                arrowprops=dict(arrowstyle="-|>", color=INK, lw=1.8))
    # 离散时刻
    ts = [1.5, 4.5, 7.5, 10.5]
    for i, tt in enumerate(ts):
        ax.plot([tt, tt], [2.2, 2.8], color=INK, lw=1.8)
        ax.text(tt, 3.05, f"t={i}", ha="center", fontsize=12, color=INK)
        ax.text(tt, 1.95, f"(state x{i}, v{i})", ha="center", fontsize=10.5,
                color=GREY if i < 3 else INK)
    # 高亮一个 dt 步
    ax.add_patch(FancyBboxPatch((4.5, 3.45), 3.0, 0.62,
                 boxstyle="round,pad=0.02,rounding_size=0.06",
                 facecolor=(0.90, 0.95, 1.0), edgecolor=BLUE, linewidth=1.6))
    ax.text(6.0, 3.76, "one step dt", ha="center", fontsize=11.5,
            color=BLUE, weight="bold")

    # 下方: 这一步内做了什么(半隐式欧拉)
    box(ax, 0.6, 0.15, 3.3, 1.15,
        "1. compute acceleration\n  a = F/m  (= -g for gravity)", fc="white")
    box(ax, 4.25, 0.15, 3.3, 1.15,
        "2. update velocity\n  $v_{n+1}=v_n + a\\cdot dt$", fc=(0.92, 0.96, 0.92))
    box(ax, 7.9, 0.15, 3.3, 1.15,
        "3. update position\n  $x_{n+1}=x_n + v_{n+1}\\cdot dt$", fc=(0.92, 0.96, 0.92))
    arrow(ax, (3.9, 0.72), (4.25, 0.72), color=INK, lw=1.8)
    arrow(ax, (7.55, 0.72), (7.9, 0.72), color=INK, lw=1.8)
    # 标注: 用新速度(symmetric Euler 的关键)
    ax.text(9.55, 1.45, "uses NEW velocity\n(symplectic / semi-implicit)",
            fontsize=9.5, color=GREEN, ha="center", style="italic")

    ax.set_xlim(0, 12.5); ax.set_ylim(-0.2, 4.2)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("One timestep: turn the ODE into a recurrence "
                 "(velocity then position)", fontsize=12.5, weight="bold")
    fig.savefig(os.path.join(OUT, "fig-02-recurrence-scheme.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-03
# dt -> 0 时离散趋近连续(收敛); dt 太大时偏离真实
# 左图: 不同 dt 下, t=T 时位置的误差 |x_discrete(T) - x_analytic(T)|
# 右图: dt=0.5(粗)与 dt=0.05(细)的速度轨迹对比, 看粗步长如何偏离
def fig_dt_convergence():
    g = 9.8
    x0 = 20.0; v0 = 0.0
    T = 1.5

    # 解析解在 T 处的值
    x_true_T = x0 + v0 * T - 0.5 * g * T ** 2

    # 左图: 误差 vs dt (log-log)
    dts = np.array([0.5, 0.25, 0.1, 0.05, 0.025, 0.01, 0.005])
    err_explicit = []   # 显式欧拉 x_{n+1}=x_n+v_n dt
    err_semi = []       # 半隐式 x_{n+1}=x_n+v_{n+1} dt
    for dt in dts:
        n = int(round(T / dt))
        # explicit Euler
        x = x0; v = v0
        for _ in range(n):
            v_new = v - g * dt
            x = x + v * dt
            v = v_new
        err_explicit.append(abs(x - x_true_T))
        # semi-implicit Euler
        x = x0; v = v0
        for _ in range(n):
            v = v - g * dt
            x = x + v * dt
        err_semi.append(abs(x - x_true_T))

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(13.5, 5.4))

    axL.loglog(dts, err_explicit, "o-", color=RED, lw=2.0, ms=8,
               label="explicit Euler  error")
    axL.loglog(dts, err_semi, "s-", color=GREEN, lw=2.0, ms=8,
               label="semi-implicit Euler  error")
    # 一阶参考线 O(dt)
    ref = err_semi[3] * (dts / dts[3])
    axL.loglog(dts, ref, ":", color=GREY, lw=1.6, label="O(dt)  reference (1st order)")
    axL.set_xlabel("timestep dt", fontsize=12)
    axL.set_ylabel("error at t=T:  |x_discrete - x_exact|", fontsize=11.5)
    axL.set_title("As dt -> 0, discrete converges to continuous (1st order)",
                  fontsize=11.5, weight="bold")
    axL.invert_xaxis()
    axL.grid(True, which="both", alpha=0.3)
    axL.legend(fontsize=10, loc="lower right")

    # 右图: 速度轨迹对比(dt=0.5 粗 vs dt=0.05 细 vs 解析)
    t_cont = np.linspace(0, T, 300)
    v_cont = v0 - g * t_cont
    axR.plot(t_cont, v_cont, color=BLUE, lw=2.4, label="exact v(t) = v0 - gt")

    for dt, c, lab, mk in [(0.5, RED, "dt=0.50 (coarse)", "o"),
                            (0.05, GREEN, "dt=0.05 (fine)", "^")]:
        n = int(round(T / dt)) + 1
        t = np.array([i * dt for i in range(n)])
        v = np.zeros(n); v[0] = v0
        for i in range(n - 1):
            v[i + 1] = v[i] - g * dt
        axR.plot(t, v, color=c, lw=0, marker=mk, ms=8,
                 markerfacecolor=c, markeredgecolor="white", mew=1.1,
                 label=lab)
    axR.axhline(0, color=GREY, lw=0.8)
    axR.set_xlabel("time t (s)", fontsize=12)
    axR.set_ylabel("velocity v (m/s)", fontsize=12)
    axR.set_title("Coarse dt drifts from exact; fine dt rides the curve",
                  fontsize=11.5, weight="bold")
    axR.grid(True, alpha=0.3)
    axR.legend(fontsize=10, loc="lower left")

    fig.savefig(os.path.join(OUT, "fig-03-dt-convergence.png"))
    plt.close(fig)


# ===============================================================
# P1-04 figures (suffix _p104)
# ===============================================================

# -------------------------------- fig-p104-01
# 检测 vs 响应二分全景图(全书骨架图, 比 P0 fig-01 更细):
# 左 Detection: 宽相 AABB 粗筛 -> 窄相 SAT/GJK + 接触流形
# 右 Response:  动力学积分(symplectic Euler) -> Sequential Impulse(PGS) -> 位置积分
def fig_detect_respond_panorama_p104():
    fig, ax = plt.subplots(figsize=(13.5, 8.0))

    # ===== left: Detection =====
    ax.add_patch(FancyBboxPatch((0.2, 0.6), 6.1, 7.1,
                 boxstyle="round,pad=0.05,rounding_size=0.12",
                 facecolor=BSOFT, edgecolor=BLUE, linewidth=2.2))
    ax.text(3.25, 7.35, "DETECTION", ha="center", fontsize=16,
            weight="bold", color=BLUE)
    ax.text(3.25, 6.92, "who collides, where, how deep", ha="center",
            fontsize=11, color=INK, style="italic")

    box(ax, 0.6, 5.55, 5.3, 1.0,
        "Broad phase  (coarse filter)\nAABB proxies + spatial structure (dynamic tree)",
        fc="white", ec=BLUE, fs=11.5, weight="bold")
    box(ax, 0.6, 4.15, 5.3, 1.0,
        "Narrow phase  (exact test)\nSAT (separating axis) / GJK (Minkowski diff)",
        fc="white", ec=BLUE, fs=11.5, weight="bold")
    box(ax, 0.6, 2.75, 5.3, 1.0,
        "Contact manifold\nnormal n, penetration depth d, contact points",
        fc=BSOFT, ec=BLUE, fs=11.5, weight="bold")
    arrow(ax, (3.25, 5.55), (3.25, 5.20), color=BLUE, lw=2.0)
    arrow(ax, (3.25, 4.15), (3.25, 3.80), color=BLUE, lw=2.0)
    ax.text(3.25, 1.05, "=> candidate pairs only\n=> exact contacts feed the solver",
            ha="center", fontsize=11, color=BLUE, weight="bold")
    ax.text(3.25, 1.95, "feeds Response ->", ha="center", fontsize=10.5,
            color=INK, style="italic")

    # ===== right: Response =====
    ax.add_patch(FancyBboxPatch((7.0, 0.6), 6.1, 7.1,
                 boxstyle="round,pad=0.05,rounding_size=0.12",
                 facecolor=GSOFT, edgecolor=GREEN, linewidth=2.2))
    ax.text(10.05, 7.35, "RESPONSE", ha="center", fontsize=16,
            weight="bold", color=GREEN)
    ax.text(10.05, 6.92, "how things move after collision", ha="center",
            fontsize=11, color=INK, style="italic")

    box(ax, 7.4, 5.55, 5.3, 1.0,
        "Dynamics integration\napply forces -> v += h*a  (symplectic Euler)",
        fc="white", ec=GREEN, fs=11.5, weight="bold")
    box(ax, 7.4, 4.15, 5.3, 1.0,
        "Constraint solving\nSequential Impulse / PGS  (iterate -> no overlap)",
        fc="white", ec=GREEN, fs=11.5, weight="bold")
    box(ax, 7.4, 2.75, 5.3, 1.0,
        "Position integration\nx += h*v  (advance motion, no tunnel)",
        fc=GSOFT, ec=GREEN, fs=11.5, weight="bold")
    arrow(ax, (10.05, 5.55), (10.05, 5.20), color=GREEN, lw=2.0)
    arrow(ax, (10.05, 4.15), (10.05, 3.80), color=GREEN, lw=2.0)
    ax.text(10.05, 1.05, "=> resolve, bounce, stack stable",
            ha="center", fontsize=11, color=GREEN, weight="bold")
    ax.text(10.05, 1.95, "<- consumes contacts", ha="center", fontsize=10.5,
            color=INK, style="italic")

    # center bridge: detection feeds response
    arrow(ax, (5.95, 3.25), (7.15, 4.65), color=AMBER, lw=2.4, rad=-0.15)
    ax.text(6.55, 4.15, "contact\nmanifold", ha="center", va="center",
            fontsize=9.5, color=AMBER, weight="bold")

    ax.text(6.65, 0.30, "one timestep:  integrate -> DETECT -> SOLVE -> next dt",
            ha="center", fontsize=12, color=INK, weight="bold")

    ax.set_xlim(0, 13.3); ax.set_ylim(0, 8.1)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("The two halves of a physics engine: Detection vs Response",
                 fontsize=14, weight="bold", pad=14)
    fig.savefig(os.path.join(OUT, "fig-p104-01-detect-respond-panorama.png"))
    plt.close(fig)


# -------------------------------- fig-p104-02
# 宽相分层: 暴力两两 O(n^2) vs 宽相 AABB 粗筛近 O(n)
def fig_broadphase_layering_p104():
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(13.5, 6.2))

    rng = np.random.default_rng(7)
    pts = rng.uniform([0.4, 0.4], [5.6, 4.6], size=(8, 2))

    # ---- left: brute force O(n^2) ----
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            axL.plot([pts[i, 0], pts[j, 0]], [pts[i, 1], pts[j, 1]],
                     color=RED, lw=0.7, alpha=0.35, zorder=1)
    for (x, y) in pts:
        axL.add_patch(Circle((x, y), 0.16, facecolor=BLUE, edgecolor=INK, zorder=3))
    axL.text(3.0, 5.05, "Brute force: test EVERY pair", ha="center",
             fontsize=12.5, weight="bold", color=RED)
    axL.text(3.0, 0.15, r"8 bodies -> 28 pairs,  n bodies -> O(n$^2$) pairs",
             ha="center", fontsize=11.5, color=INK)
    axL.set_xlim(0, 6); axL.set_ylim(-0.1, 5.4)
    axL.set_aspect("equal"); clean_ax(axL)

    # ---- right: broad phase AABB coarse filter ----
    half = 0.42
    aabbs = [(x - half, y - half, x + half, y + half) for (x, y) in pts]

    def aabb_overlap(a, b):
        return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])

    for (x0, y0, x1, y1) in aabbs:
        axR.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0,
                                facecolor="none", edgecolor=GREY, lw=1.1, zorder=1))
    for i in range(len(aabbs)):
        for j in range(i + 1, len(aabbs)):
            if aabb_overlap(aabbs[i], aabbs[j]):
                axR.plot([pts[i, 0], pts[j, 0]], [pts[i, 1], pts[j, 1]],
                         color=AMBER, lw=1.8, alpha=0.9, zorder=2)
    for (x, y) in pts:
        axR.add_patch(Circle((x, y), 0.16, facecolor=BLUE, edgecolor=INK, zorder=3))
    axR.text(3.0, 5.05, "Broad phase: only AABB-overlapping pairs",
             ha="center", fontsize=12.5, weight="bold", color=AMBER)
    axR.text(3.0, 0.15,
             "few candidate pairs -> near O(n),  expensive narrow phase spared",
             ha="center", fontsize=11, color=INK)
    axR.set_xlim(0, 6); axR.set_ylim(-0.1, 5.4)
    axR.set_aspect("equal"); clean_ax(axR)

    fig.suptitle("Why detection splits in two: cheap filter protects expensive exact test",
                 fontsize=13.5, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p104-02-broadphase-layering.png"))
    plt.close(fig)


# -------------------------------- fig-p104-03
# 一个时间步真实阶段顺序(Box2D v3.2 b2Solve stages), 标注 检测/响应/横切
def fig_timestep_stages_p104():
    fig, ax = plt.subplots(figsize=(14.5, 4.8))

    stages = [
        ("Broad phase\npairs", "D", BSOFT, BLUE),
        ("Narrow phase\n+ manifold", "D", BSOFT, BLUE),
        ("Prepare\ncontacts", "setup", SOFT, GREY),
        ("Integrate\nvelocities", "R", GSOFT, GREEN),
        ("Warm\nstart", "R", GSOFT, GREEN),
        ("Solve\n(SI loop)", "R", GSOFT, GREEN),
        ("Integrate\npositions", "R", GSOFT, GREEN),
        ("Relax /\nrestitution", "R", GSOFT, GREEN),
    ]
    n = len(stages)
    bw, bh = 1.5, 1.5
    gap = 0.18
    x0 = 0.3
    y = 1.2
    for i, (name, tag, fc, ec) in enumerate(stages):
        x = x0 + i * (bw + gap)
        box(ax, x, y, bw, bh, name, fc=fc, ec=ec, fs=10.5, weight="bold")
        ax.text(x + bw / 2, y + bh + 0.18, tag, ha="center",
                fontsize=9.5, color=ec, weight="bold")
        if i < n - 1:
            arrow(ax, (x + bw, y + bh / 2), (x + bw + gap, y + bh / 2),
                  color=INK, lw=1.5)
    # loop-back under Solve stage
    sx = x0 + 5 * (bw + gap)
    ax.add_artist(FancyArrowPatch((sx + bw * 0.7, y), (sx + bw * 0.7, y - 0.4),
                                  arrowstyle="-|>", color=GREEN, lw=1.6,
                                  connectionstyle="arc3,rad=0.0", mutation_scale=14))
    ax.add_artist(FancyArrowPatch((sx + bw * 0.3, y - 0.4), (sx + bw * 0.3, y),
                                  arrowstyle="-|>", color=GREEN, lw=1.6,
                                  connectionstyle="arc3,rad=0.0", mutation_scale=14))
    ax.text(sx + bw / 2, y - 0.65, "iterate", ha="center", fontsize=9.5,
            color=GREEN, style="italic")

    # legend
    ax.add_patch(Rectangle((0.3, 0.15), 0.28, 0.22, facecolor=BSOFT, edgecolor=BLUE))
    ax.text(0.66, 0.26, "Detection", fontsize=10, va="center", color=BLUE)
    ax.add_patch(Rectangle((2.1, 0.15), 0.28, 0.22, facecolor=GSOFT, edgecolor=GREEN))
    ax.text(2.46, 0.26, "Response", fontsize=10, va="center", color=GREEN)
    ax.add_patch(Rectangle((3.9, 0.15), 0.28, 0.22, facecolor=SOFT, edgecolor=GREY))
    ax.text(4.26, 0.26, "setup / cross-cutting", fontsize=10, va="center", color=INK)

    # next-dt loop
    lastx = x0 + (n - 1) * (bw + gap)
    arrow(ax, (lastx + bw / 2, y + bh), (x0 + bw / 2, y + bh + 0.7),
          color=INK, lw=1.4, rad=0.18)
    ax.text((lastx + x0) / 2 + bw / 2, y + bh + 0.95, "next timestep  (dt)",
            ha="center", fontsize=11, color=INK, weight="bold")

    ax.set_xlim(0, x0 + n * (bw + gap))
    ax.set_ylim(-0.2, y + bh + 1.4)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("Real stage order inside one b2World_Step (Box2D v3.2)",
                 fontsize=13.5, weight="bold", pad=10)
    fig.savefig(os.path.join(OUT, "fig-p104-03-timestep-stages.png"))
    plt.close(fig)


# ===============================================================
# P1-03 figures (suffix _p103, files prefixed fig-p103-)
# ===============================================================

# -------------------------------- fig-p103-01
# 有解析解的简单系统(谐振子, 光滑正弦) vs 真实引擎的复杂系统(两球碰撞, 分段折线)
def fig_analytic_vs_engine_p103():
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(13.5, 5.4))

    # ---------- 左: 谐振子 ----------
    t = np.linspace(0, 6 * np.pi, 600)
    x = np.cos(t)
    axL.plot(t, x, color=BLUE, lw=2.4)
    axL.fill_between(t, x, 0, where=(x > 0), color=BLUE, alpha=0.10)
    axL.fill_between(t, x, 0, where=(x < 0), color=BLUE, alpha=0.10)
    axL.axhline(0, color=GREY, lw=0.8)
    axL.set_title("Harmonic oscillator   x(t) = cos(t)\n"
                  "closed-form solution, smooth forever",
                  fontsize=12.5, weight="bold", color=BLUE)
    axL.set_xlabel("time t", fontsize=11)
    axL.set_ylabel("position x", fontsize=11)
    axL.set_xlim(0, 6 * np.pi); axL.set_ylim(-1.6, 1.6)
    axL.set_xticks([]); axL.set_yticks([-1, 0, 1])
    axL.text(0.4, 1.30, "F = -k x   (linear, single body, no contact)",
             fontsize=10.5, color=INK)
    axL.text(9.5, -1.45, "=> analytical formula", fontsize=10.5,
             color=BLUE, ha="right", weight="bold")
    axL.grid(True, alpha=0.25)

    # ---------- 右: 两球碰撞 ----------
    # 两球沿 x 轴接近, t=t_hit 相撞, 速度突变(分段), 之后分离
    tt = np.linspace(0, 6, 600)
    t_hit = 3.0
    xA = np.where(tt <= t_hit, -2.0 + 1.0 * tt,
                  (-2.0 + 1.0 * t_hit) - 0.6 * (tt - t_hit))
    xB = np.where(tt <= t_hit, 2.0 - 1.0 * tt,
                  (2.0 - 1.0 * t_hit) + 0.6 * (tt - t_hit))
    axR.plot(tt, xA, color=RED, lw=2.4, label="ball A")
    axR.plot(tt, xB, color=GREEN, lw=2.4, label="ball B")
    axR.axvline(t_hit, color=AMBER, lw=1.6, linestyle="--")
    axR.scatter([t_hit], [0.0], s=80, color=AMBER, zorder=5, edgecolor=INK)
    axR.annotate("impact: velocity jumps\n(piecewise, no single formula)",
                 xy=(t_hit, 0.0), xytext=(3.6, 1.5),
                 fontsize=10.5, color=AMBER,
                 arrowprops=dict(arrowstyle="->", color=AMBER, lw=1.5))
    axR.set_title("Two balls collide (with restitution)\n"
                  "no closed-form ODE solution, piecewise + constrained",
                  fontsize=12.5, weight="bold", color=RED)
    axR.set_xlabel("time t", fontsize=11)
    axR.set_ylabel("position x", fontsize=11)
    axR.set_xlim(0, 6); axR.set_ylim(-2.4, 2.4)
    axR.axhline(0, color=GREY, lw=0.8)
    axR.legend(fontsize=10.5, loc="upper left")
    axR.grid(True, alpha=0.25)

    fig.suptitle("Why physics needs numerical integration: closed-form (rare) vs real engines (usual)",
                 fontsize=13.5, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p103-01-analytic-vs-engine.png"))
    plt.close(fig)


# -------------------------------- fig-p103-02
# 大角度单摆 —— 加一点非线性, 解析解就没了
def fig_pendulum_nonlinear_p103():
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(13.5, 5.4))

    g = 1.0
    L = 1.0
    dt = 0.002
    tmax = 12.0
    n = int(tmax / dt)
    tt = np.arange(n) * dt

    def integrate(theta0, use_linear):
        """半隐式欧拉积分单摆. use_linear=True 把 sin(theta) 换成 theta."""
        th = theta0
        om = 0.0
        out = np.empty(n)
        for i in range(n):
            out[i] = th
            acc = -(g / L) * (th if use_linear else np.sin(th))
            om += acc * dt
            th += om * dt
        return out

    # 左: 小角度 (theta0 = 0.1 rad) 线性化
    th_lin_small = integrate(0.1, use_linear=True)
    th_analytic = 0.1 * np.cos(np.sqrt(g / L) * tt)
    axL.plot(tt, th_lin_small, color=BLUE, lw=2.2,
             label="numerical (linearized ODE)")
    axL.plot(tt, th_analytic, color=GREEN, lw=1.8, linestyle="--",
             label="closed-form: 0.1 cos(sqrt(g/L) t)")
    axL.set_title("Small-angle pendulum\n"
                  "sin(theta) ~ theta  =>  linear ODE, closed form exists",
                  fontsize=12.5, weight="bold", color=BLUE)
    axL.set_xlabel("time t", fontsize=11)
    axL.set_ylabel("angle theta", fontsize=11)
    axL.legend(fontsize=10.5, loc="upper right")
    axL.grid(True, alpha=0.25)
    axL.axhline(0, color=GREY, lw=0.8)
    axL.text(0.2, 0.085, "smooth sinusoid", fontsize=10.5, color=INK)

    # 右: 大角度 (theta0 = 2.5 rad ~ 143 deg) 真实非线性
    th_nonlin = integrate(2.5, use_linear=False)
    th_lin_big = integrate(2.5, use_linear=True)
    axR.plot(tt, th_nonlin, color=RED, lw=2.4,
             label="real nonlinear ODE  theta'' + (g/L) sin theta = 0")
    axR.plot(tt, th_lin_big, color=GREY, lw=1.6, linestyle=":",
             label="linearized (wrong here, sin != theta)")
    axR.set_title("Large-angle pendulum\n"
                  "sin(theta) keeps nonlinearity  =>  NO closed form",
                  fontsize=12.5, weight="bold", color=RED)
    axR.set_xlabel("time t", fontsize=11)
    axR.set_ylabel("angle theta", fontsize=11)
    axR.legend(fontsize=10.5, loc="upper right")
    axR.grid(True, alpha=0.25)
    axR.axhline(0, color=GREY, lw=0.8)
    axR.text(0.2, 2.2, "must integrate numerically", fontsize=10.5,
             color=RED, weight="bold")

    fig.suptitle("A touch of nonlinearity, and the closed-form solution is gone",
                 fontsize=13.5, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p103-02-pendulum-nonlinear.png"))
    plt.close(fig)


# -------------------------------- fig-p103-03
# 解析路径(大多走不通) vs 数值路径(总能走)
def fig_paths_p103():
    BSOFT = (0.90, 0.94, 1.0)
    GSOFT = (0.90, 0.95, 0.90)
    RSOFT = (0.97, 0.90, 0.90)
    fig, ax = plt.subplots(figsize=(12.5, 6.0))

    box(ax, 0.3, 2.6, 2.6, 0.9,
        "equation of motion\nF = m x''   (ODE)", fc=SOFT, weight="bold")

    box(ax, 4.0, 4.5, 3.6, 0.9,
        "try closed-form\n(solve the ODE symbolically)", fc=BSOFT, ec=BLUE)
    box(ax, 8.7, 5.2, 3.4, 0.8,
        "rare win:\nfree fall, harmonic osc.",
        fc=GSOFT, ec=GREEN, fs=11)
    box(ax, 8.7, 4.1, 3.4, 0.8,
        "X  fails for:\ncollision / constraint / nonlinear / N-body",
        fc=RSOFT, ec=RED, fs=10.5)

    box(ax, 4.0, 1.0, 3.6, 0.9,
        "numerical integration\n(discretize, step by step)",
        fc=GSOFT, ec=GREEN, weight="bold")
    box(ax, 8.7, 1.0, 3.4, 0.9,
        "approximate solution\n(always works, choose dt & integrator)",
        fc=GSOFT, ec=GREEN, fs=11)

    arrow(ax, (2.9, 3.2), (4.0, 4.9), color=BLUE, lw=1.8)
    arrow(ax, (2.9, 2.9), (4.0, 1.45), color=GREEN, lw=2.0)
    arrow(ax, (7.6, 4.95), (8.7, 5.6), color=GREEN, lw=1.6)
    arrow(ax, (7.6, 4.7), (8.7, 4.5), color=RED, lw=1.6)
    arrow(ax, (10.4, 4.1), (10.4, 1.9), color=RED, lw=1.8, rad=-0.3)
    ax.text(10.95, 3.0, "fall back to\nnumerical",
            fontsize=10, color=RED, va="center")

    ax.text(0.3, 0.3, "physics engines live entirely on the numerical path",
            fontsize=12, weight="bold", color=GREEN)

    ax.set_xlim(0, 12.5); ax.set_ylim(0, 6.2)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.set_title("Closed-form path (mostly blocked) vs numerical path (always open)",
                 fontsize=13.5, weight="bold")
    fig.savefig(os.path.join(OUT, "fig-p103-03-analytic-vs-numeric-paths.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_freefall_continuous_vs_discrete()
    fig_recurrence_scheme()
    fig_dt_convergence()
    fig_detect_respond_panorama_p104()
    fig_broadphase_layering_p104()
    fig_timestep_stages_p104()
    fig_analytic_vs_engine_p103()
    fig_pendulum_nonlinear_p103()
    fig_paths_p103()
    print("done:", sorted(f for f in os.listdir(OUT) if f.endswith(".png")))
