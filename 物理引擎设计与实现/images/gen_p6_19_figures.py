# -*- coding: utf-8 -*-
"""为《物理引擎》P6-19 (全书收束章) 生成 PNG 示意图。运行: python gen_p6_19_figures.py

本脚本独占生成 fig-p6_19-* PNG:
  1. 全书最终全景图:一个时间步的完整流程 + 每步对应哪一章
  2. 普适性对照:物理引擎 vs 机器人碰撞回避 vs 布料/流体 vs CAD 装配干涉
  3. v3.2 全书源码地图:b2World_Step -> b2UpdateBroadPhasePairs + b2Solve 的 9 阶段
风格沿用 gen_p0_figures.py:DejaVu Sans / clean_ax / box() / arrow() / English labels.
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch, FancyArrowPatch, Circle, FancyArrow

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 12.0,
    "axes.linewidth": 1.2,
    "figure.dpi": 150,
    "savefig.dpi": 150,
    "savefig.bbox": "tight",
    "savefig.facecolor": "white",
    "savefig.pad_inches": 0.18,
})

OUT = os.path.dirname(os.path.abspath(__file__))

# ---- palette (consistent with gen_p0_figures.py) ----
RED   = (0.86, 0.18, 0.18)
GREEN = (0.20, 0.70, 0.32)
BLUE  = (0.20, 0.45, 0.88)
AMBER = (0.95, 0.70, 0.18)
PURPLE= (0.55, 0.32, 0.78)
TEAL  = (0.13, 0.60, 0.62)
GREY  = (0.78, 0.78, 0.80)
DGREY = (0.45, 0.45, 0.48)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)
BSOFT = (0.90, 0.94, 1.0)   # detect side soft blue
GSOFT = (0.90, 0.95, 0.90)  # response side soft green
XSOFT = (0.95, 0.90, 0.96)  # cross-cut soft purple


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=11, weight="normal", tc=INK, lw=1.4, ls="-"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=fc, edgecolor=ec, linewidth=lw, linestyle=ls))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.8, rad=0.0, ms=16):
    cs = f"arc3,rad={rad}"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=ms))


# ============================================================ fig 1
def fig_timestep_panorama():
    """全书最终全景图:一个时间步完整流程,每块标注对应章节 + 二分法归属(D/R/X)。"""
    fig, ax = plt.subplots(figsize=(13.5, 7.4))

    # 左中右三大色带:横切 -> 检测 -> 响应(响应被检测切成两段)
    # 顶部 banner: 一个 timestep 的循环
    ax.text(6.75, 7.05,
            "ONE TIMESTEP  (dt = 1/60 s):  the whole book in one loop",
            ha="center", fontsize=13.5, weight="bold", color=INK)
    ax.text(6.75, 6.66,
            "apply forces -> integrate velocity -> broad phase -> narrow phase + manifold "
            "-> integrate position -> solve constraints -> next dt",
            ha="center", fontsize=10.5, color=DGREY)

    # 横切: fixed timestep (P2-08) + sleeping/CCD (P5-18)  —— 包裹全图的护栏
    ax.add_patch(FancyBboxPatch((0.35, 0.45), 12.8, 5.85, boxstyle="round,pad=0.04,rounding_size=0.10",
                                facecolor=XSOFT, edgecolor=PURPLE, linewidth=1.6, linestyle=(0, (5, 3))))
    ax.text(0.7, 6.0, "cross-cut scaffolding",
            fontsize=10.5, color=PURPLE, weight="bold", rotation=90, va="center")
    ax.text(0.95, 5.6, "fixed dt\n(P2-08)", fontsize=9.2, color=PURPLE, va="center")
    ax.text(0.95, 4.7, "sleeping\n(P5-18)", fontsize=9.2, color=PURPLE, va="center")
    ax.text(0.95, 3.8, "CCD\n(P5-18)", fontsize=9.2, color=PURPLE, va="center")

    # 主流程:7 个块,从左到右
    # 1. apply forces + integrate velocity (响应, P2-05/06/07)
    box(ax, 1.9, 3.55, 1.95, 1.15,
        "1. integrate velocity\nv += h*(F/m + g)\nsymplectic Euler\n(P2-05/06/07)",
        fc="white", ec=GREEN, fs=9.5, weight="bold", tc=GREEN)
    # 2. broad phase (检测, P3-09/10/11)
    box(ax, 4.05, 3.55, 1.95, 1.15,
        "2. broad phase\nAABB + dynamic tree\ncoarse filter O(n^2)->O(n)\n(P3-09/10/11)",
        fc="white", ec=BLUE, fs=9.5, weight="bold", tc=BLUE)
    # 3. narrow phase (检测, P4-12/13/14)
    box(ax, 6.2, 3.55, 1.95, 1.15,
        "3. narrow phase\nSAT / GJK + manifold\nnormal, depth, points\n(P4-12/13/14)",
        fc="white", ec=BLUE, fs=9.5, weight="bold", tc=BLUE)
    # 4. warm start + solve (响应, P5-15/16/17)  —— 核心最难
    box(ax, 8.35, 3.55, 2.15, 1.15,
        "4. constraint solve\nSequential Impulse\nwarm / soft / speculative\n(P5-15/16/17)",
        fc="white", ec=GREEN, fs=9.5, weight="bold", tc=GREEN)
    # 5. integrate position (响应, P2-07)
    box(ax, 10.7, 3.55, 2.2, 1.15,
        "5. integrate position\nx += h*v_new\n(uses post-solve v)\n(P2-07)",
        fc="white", ec=GREEN, fs=9.5, weight="bold", tc=GREEN)

    # 箭头连接
    for (x1, x2) in [(3.85, 4.05), (6.0, 6.2), (8.15, 8.35), (10.5, 10.7)]:
        arrow(ax, (x1, 4.12), (x2, 4.12), color=INK, lw=1.8, ms=14)
    # 回环到下一个 dt
    arrow(ax, (11.8, 3.55), (11.8, 2.75), color=AMBER, lw=2.0, rad=-0.35, ms=16)
    arrow(ax, (11.8, 2.75), (2.85, 2.75), color=AMBER, lw=2.0, rad=-0.2, ms=16)
    arrow(ax, (2.85, 2.75), (2.85, 3.55), color=AMBER, lw=2.0, rad=-0.35, ms=16)
    ax.text(7.3, 2.45, "next dt  ->  loop", ha="center", fontsize=10.5,
            color=AMBER, weight="bold")

    # 上下注释带:每步对应二分法 + 章号
    # 上: detect vs respond 标签
    ax.text(5.15, 4.95, "[ DETECT ]", ha="center", fontsize=10.5, color=BLUE, weight="bold")
    ax.text(9.4, 4.95, "[ RESPONSE ]", ha="center", fontsize=10.5, color=GREEN, weight="bold")

    # 下:三句话总结
    ax.text(6.75, 1.7,
            "Detection answers: who collides, where, how deep.",
            ha="center", fontsize=11, color=BLUE)
    ax.text(6.75, 1.3,
            "Response answers: how things move, and never inter-penetrate.",
            ha="center", fontsize=11, color=GREEN)
    ax.text(6.75, 0.85,
            "Both halves ride on numerics: integration (symplectic) + "
            "geometry (SAT/GJK) + iterative solve (PGS on LCP).",
            ha="center", fontsize=10, color=DGREY, style="italic")

    ax.set_xlim(0, 13.5); ax.set_ylim(0, 7.4)
    ax.set_aspect("equal"); clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p6_19-01-timestep-panorama.png"))
    plt.close(fig)


# ============================================================ fig 2
def fig_universality():
    """普适性对照:物理引擎 vs 机器人碰撞回避 vs 布料/流体 vs CAD 装配干涉。
    本质都是 "geometry detection + physical response"。"""
    fig, ax = plt.subplots(figsize=(13.0, 7.6))

    ax.text(6.5, 7.25,
            "The same two halves everywhere:  geometry DETECTION  +  physical RESPONSE",
            ha="center", fontsize=13.5, weight="bold", color=INK)

    # 四列:物理引擎 / 机器人 / 布料流体 / CAD
    cols = [
        # (x, title, color, detect_sub, detect_algo, respond_sub, respond_algo, note)
        (0.6, "Game physics engine\n(Box2D)", TEAL,
         "rigid body collision",
         "AABB tree + SAT / GJK",
         "no penetration +\nbounce + joints",
         "symplectic Euler +\nSequential Impulse",
         "boxes stack, balls bounce"),
        (3.75, "Robot collision\navoidance", PURPLE,
         "self + obstacle\ngeometry",
         "bounding volume\nhierarchy / GJK",
         "plan a path that\nstays collision-free",
         "potential field /\noptimization",
         "autonomous driving,\narm motion planning"),
        (6.9, "Cloth / fluid sim", AMBER,
         "particle vs particle\n/ mesh proximity",
         "spatial hash / BVH",
         "distance / volume\nconstraints",
         "Verlet + PBD\n(position projection)",
         "flags, ropes, water"),
        (10.05, "CAD assembly\ninterference", RED,
         "part vs part\nsolid intersection",
         "OBB tree / boolean\nops on B-rep",
         "report interference\n(no motion response)",
         "detection-only,\nstatic check",
         "does part A fit\ninside assembly B?"),
    ]

    for (x, title, c, ds, da, rs, ra, note) in cols:
        # column container
        ax.add_patch(FancyBboxPatch((x, 0.5), 2.85, 6.35, boxstyle="round,pad=0.04,rounding_size=0.10",
                                    facecolor=(c[0]*0.92+0.08, c[1]*0.92+0.08, c[2]*0.92+0.08),
                                    edgecolor=c, linewidth=1.8, alpha=0.18))
        # title
        ax.text(x + 1.42, 6.55, title, ha="center", fontsize=11.5,
                weight="bold", color=c)
        # DETECT half
        box(ax, x + 0.2, 4.35, 2.45, 1.55,
            f"DETECT\n{ds}\n--\n{da}",
            fc="white", ec=c, fs=9.3, weight="bold", tc=c)
        # RESPOND half
        box(ax, x + 0.2, 2.55, 2.45, 1.55,
            f"RESPOND\n{rs}\n--\n{ra}",
            fc="white", ec=c, fs=9.3, weight="bold", tc=c)
        # example note
        ax.text(x + 1.42, 1.9, note, ha="center", fontsize=9.0,
                color=DGREY, style="italic")
        # local arrow detect -> respond
        arrow(ax, (x + 1.42, 4.35), (x + 1.42, 4.1), color=c, lw=1.6, ms=12)

    # 中间分隔 + 一个统一的 takeaway
    ax.text(6.5, 0.95,
            "Same skeleton:  geometry tells you WHAT touches,  "
            "numerics decide HOW it responds.",
            ha="center", fontsize=11.5, weight="bold", color=INK)

    ax.set_xlim(0, 13.0); ax.set_ylim(0, 7.6)
    ax.set_aspect("equal"); clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p6_19-02-universality.png"))
    plt.close(fig)


# ============================================================ fig 3
def fig_source_map():
    """全书源码地图:b2World_Step -> b2UpdateBroadPhasePairs (检测) + b2Solve (响应, 9 阶段)。
    对应 anchor 第 1/2 节,把全书源码锚点串成一张图。"""
    fig, ax = plt.subplots(figsize=(13.5, 8.2))

    ax.text(6.75, 7.85,
            "Box2D v3.2 source map: b2World_Step -> broad+narrow (detect) + b2Solve (respond)",
            ha="center", fontsize=13, weight="bold", color=INK)
    ax.text(6.75, 7.5,
            "C handle API; commit 56edae79; one timestep = 9-stage pipeline",
            ha="center", fontsize=10, color=DGREY, style="italic")

    # 顶部入口 b2World_Step
    box(ax, 4.7, 6.55, 4.1, 0.85,
        "b2World_Step(worldId, timeStep, subStepCount)\nsrc/physics_world.c:828",
        fc=SOFT, ec=INK, fs=10.5, weight="bold")
    # 派发: 切子步 h = timeStep/subStepCount
    box(ax, 9.3, 6.55, 3.9, 0.85,
        "context.h = timeStep / subStepCount\nphysics_world.c:898  (sub-stepping)",
        fc=XSOFT, ec=PURPLE, fs=9.8, tc=PURPLE)
    arrow(ax, (8.8, 6.97), (9.3, 6.97), color=INK, lw=1.8, ms=14)

    # 左路: 检测 b2UpdateBroadPhasePairs (一次)
    box(ax, 0.5, 4.9, 5.4, 1.3,
        "b2UpdateBroadPhasePairs(world)   [ DETECT, once ]\n"
        "physics_world.c:886 -> broad_phase.c:412\n"
        "dynamic AABB tree (per body type) -> b2CollideXxx",
        fc=BSOFT, ec=BLUE, fs=9.5, weight="bold", tc=BLUE)
    arrow(ax, (5.0, 6.55), (3.2, 6.2), color=BLUE, lw=1.8, rad=0.15, ms=14)

    # 右路: b2Solve (9 阶段)
    box(ax, 6.3, 4.9, 6.7, 1.3,
        "b2Solve(world, &context)   [ RESPOND, 9 stages ]\nsolver.c:1272",
        fc=GSOFT, ec=GREEN, fs=10.5, weight="bold", tc=GREEN)
    arrow(ax, (7.5, 6.55), (9.0, 6.2), color=GREEN, lw=1.8, rad=-0.15, ms=14)

    # 9 阶段竖排 (右半)
    stages = [
        ("0 PrepareJoints",      GREY,   "joint effective mass"),
        ("1 PrepareContacts",    GREY,   "contact eff. mass / bias / softness"),
        ("2 IntegrateVelocities",TEAL,   "v += h*a  (symplectic, P2-07)  solver.c:100"),
        ("3 WarmStart",          AMBER,  "last-step impulse as init (P5-16)"),
        ("4 Solve x colors x iter", GREEN,"Sequential Impulse / PGS (P5-16)"),
        ("5 IntegratePositions", TEAL,   "x += h*v_new  solver.c:157"),
        ("6 Relax",              AMBER,  "TGS soft tail (useBias=false)"),
        ("7 Restitution",        RED,    "bounce post-process (P5-15/18)"),
        ("8 StoreImpulses",      GREY,   "save for next warm start"),
    ]
    y0 = 4.45
    for i, (name, c, sub) in enumerate(stages):
        yy = y0 - i * 0.45
        # stage box
        ax.add_patch(FancyBboxPatch((6.4, yy - 0.18), 6.55, 0.36,
                                    boxstyle="round,pad=0.01,rounding_size=0.04",
                                    facecolor=(c[0]*0.85+0.15, c[1]*0.85+0.15, c[2]*0.85+0.15),
                                    edgecolor=c, linewidth=1.2, alpha=0.35))
        ax.text(6.55, yy, name, fontsize=9.0, color=INK, weight="bold", va="center")
        ax.text(8.55, yy, sub, fontsize=8.6, color=DGREY, va="center")
    # 阶段之间小箭头 (示意顺序, 不逐条画避免杂乱)
    ax.text(9.7, 0.35, "... stages run in order, embedded in subStep x color x iteration loops",
            fontsize=8.8, color=DGREY, style="italic", ha="center")

    # 左半: 检测侧内部展开 (宽相 -> 窄相 -> 接触流形)
    box(ax, 0.6, 4.0, 5.2, 0.65,
        "broad phase:  dynamic tree pairs  (P3-09/10/11)",
        fc="white", ec=BLUE, fs=9.0, tc=BLUE)
    box(ax, 0.6, 3.2, 5.2, 0.65,
        "narrow phase:  SAT (b2FindMaxSeparation) / GJK  (P4-12/13)",
        fc="white", ec=BLUE, fs=9.0, tc=BLUE)
    box(ax, 0.6, 2.4, 5.2, 0.65,
        "contact manifold:  normal, depth, points  (P4-14)",
        fc="white", ec=BLUE, fs=9.0, tc=BLUE)
    box(ax, 0.6, 1.6, 5.2, 0.65,
        "feeds contacts -> stage 1 + stage 4",
        fc=SOFT, ec=INK, fs=9.0)
    for yy in [4.0, 3.2, 2.4]:
        arrow(ax, (3.2, yy), (3.2, yy - 0.15), color=BLUE, lw=1.4, ms=11)
    arrow(ax, (5.8, 4.55), (6.3, 4.55), color=INK, lw=1.6, rad=0.0, ms=12)

    # 底部一句: 概念主线 vs v3.2 工程实现
    ax.text(6.75, 0.9,
            "concept line:  integrate -> detect -> solve.   "
            "v3.2 reality:  9-stage pipeline + constraint-graph coloring (parallel SI).",
            ha="center", fontsize=10.5, color=INK, weight="bold")
    ax.text(6.75, 0.5,
            "SI / PGS is the algorithm;  v3.2 is its high-performance, warm-started, "
            "soft-constraint, speculative, parallel engineering.",
            ha="center", fontsize=9.8, color=DGREY, style="italic")

    ax.set_xlim(0, 13.5); ax.set_ylim(0, 8.2)
    ax.set_aspect("equal"); clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p6_19-03-source-map.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_timestep_panorama()
    fig_universality()
    fig_source_map()
    print("done:", sorted(f for f in os.listdir(OUT) if f.startswith("fig-p6_19")))
