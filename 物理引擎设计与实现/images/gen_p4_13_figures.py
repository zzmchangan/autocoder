# -*- coding: utf-8 -*-
"""为《物理引擎》P4-13 · GJK 与闵可夫斯基差 生成一组 PNG 示意图。

运行:  python gen_p4_13_figures.py

图清单:
  fig-p4_13-01-minkowski-difference-overlap.png   闵可夫斯基差 A-B, 相交(原点在差内)
  fig-p4_13-02-minkowski-difference-separate.png  闵可夫斯基差 A-B, 分离(原点在差外)+最近距离
  fig-p4_13-03-support-function.png               支撑函数 support: 沿方向取最远点
  fig-p4_13-04-gjk-simplex-iteration.png          GJK 单纯形迭代(点->线->三角)朝原点收敛
  fig-p4_13-05-gjk-terminate-origin.png           三角形包含原点 = 相交
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, Circle, FancyArrowPatch, Rectangle
from matplotlib.collections import PatchCollection

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 12.5,
    "axes.linewidth": 1.2,
    "figure.dpi": 150,
    "savefig.dpi": 150,
    "savefig.bbox": "tight",
    "savefig.facecolor": "white",
    "savefig.pad_inches": 0.18,
})

OUT = os.path.dirname(os.path.abspath(__file__))

RED   = (0.86, 0.18, 0.18)
GREEN = (0.20, 0.70, 0.32)
BLUE  = (0.20, 0.45, 0.88)
AMBER = (0.95, 0.70, 0.18)
PURPLE= (0.55, 0.30, 0.78)
GREY  = (0.78, 0.78, 0.80)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)
RED_SOFT  = (0.96, 0.82, 0.82)
BLUE_SOFT = (0.82, 0.88, 0.96)
GREEN_SOFT= (0.82, 0.92, 0.82)
AMBER_SOFT= (0.98, 0.90, 0.72)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def arrow(ax, p1, p2, color=INK, lw=1.6, rad=0.0, ms=16):
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=f"arc3,rad={rad}",
                                  mutation_scale=ms))


def polygon_verts(cx, cy, r, n, rot=0.0):
    """Regular polygon vertices centered at (cx,cy)."""
    ang = rot + np.array([2 * np.pi * i / n for i in range(n)])
    return np.column_stack([cx + r * np.cos(ang), cy + r * np.sin(ang)])


def minkowski_sum(P, Q):
    """Minkowski sum of two convex polygons (vertex arrays)."""
    s = []
    for p in P:
        for q in Q:
            s.append((p[0] + q[0], p[1] + q[1]))
    s = np.array(s)
    return convex_hull(s)


def convex_hull(points):
    """Andrew's monotone chain convex hull."""
    pts = sorted(map(tuple, points))
    if len(pts) <= 1:
        return np.array(pts)
    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1])


def minkowski_difference(A, B):
    """A - B = A + (-B)."""
    negB = -B
    return minkowski_sum(A, negB)


def closest_point_on_convex_boundary(poly, point):
    """最近点 on the boundary of a convex polygon (vertex array, CCW or CW) to a point.

    Returns the closest point on the polygon boundary. For each edge, project the
    point onto the edge segment (clamped to endpoints) and keep the closest.
    """
    point = np.asarray(point, dtype=float)
    n = len(poly)
    best_pt = None
    best_d2 = np.inf
    for i in range(n):
        a = np.asarray(poly[i], dtype=float)
        b = np.asarray(poly[(i + 1) % n], dtype=float)
        seg = b - a
        L2 = float(np.dot(seg, seg))
        if L2 < 1e-18:
            proj = a
        else:
            t = float(np.dot(point - a, seg) / L2)
            t = max(0.0, min(1.0, t))
            proj = a + t * seg
        d2 = float(np.dot(point - proj, point - proj))
        if d2 < best_d2:
            best_d2 = d2
            best_pt = proj
    return float(best_pt[0]), float(best_pt[1])


# ---------------------------------------------------------------- fig 01
def fig_minkowski_overlap():
    """相交情形: 原点在 A-B 内部."""
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 6.4))

    # ---- 左: A 和 B 两个形状 (相交)
    ax = axes[0]
    A = polygon_verts(0.2, 0.3, 1.5, 5, rot=0.2)         # 五边形 A
    B = polygon_verts(1.0, -0.4, 1.2, 4, rot=np.pi/4)    # 四边形 B, 与 A 相交
    ax.add_patch(Polygon(A, closed=True, facecolor=RED_SOFT, edgecolor=RED, lw=2.2, alpha=0.85))
    ax.add_patch(Polygon(B, closed=True, facecolor=BLUE_SOFT, edgecolor=BLUE, lw=2.2, alpha=0.7))
    ax.text(A[:,0].mean()-1.4, A[:,1].mean()+1.5, "A", color=RED, fontsize=18, weight="bold")
    ax.text(B[:,0].mean()+0.2, B[:,1].mean()-1.9, "B", color=BLUE, fontsize=18, weight="bold")
    # 标出相交区域(用一个小标记)
    ax.plot([0.6], [0.0], marker="*", color=AMBER, markersize=18, zorder=5)
    ax.text(0.6, 0.55, "overlap", color=AMBER, fontsize=11, ha="center", weight="bold")
    ax.set_title("Shapes A and B overlap\n(A and B intersect)", fontsize=12.5, weight="bold")
    ax.set_xlim(-2.6, 3.6); ax.set_ylim(-3.0, 3.0)
    ax.set_aspect("equal"); clean_ax(ax)

    # ---- 右: 闵可夫斯基差 A - B, 原点在内部
    ax = axes[1]
    diff = minkowski_difference(A, B)
    ax.add_patch(Polygon(diff, closed=True, facecolor=GREEN_SOFT, edgecolor=GREEN, lw=2.4, alpha=0.8))
    # 原点
    ax.plot([0], [0], marker="o", color=RED, markersize=11, zorder=6)
    ax.plot([0], [0], marker="o", fillstyle="none", color=RED, markersize=20, mew=1.5, zorder=5)
    ax.text(0.18, 0.18, "origin (0,0)", color=RED, fontsize=11.5, weight="bold")
    ax.text(diff[:,0].mean(), diff[:,1].max()+0.35, "A - B  (Minkowski difference)",
            color=GREEN, fontsize=13, weight="bold", ha="center")
    # 一个示例点: a - b
    a = A[1]; b = B[2]
    ax.plot([a[0]-b[0]], [a[1]-b[1]], marker="x", color=INK, markersize=10, mew=2, zorder=5)
    ax.text(a[0]-b[0]+0.12, a[1]-b[1]-0.05, "a - b", fontsize=9.5, color=INK)
    ax.set_title("Origin is INSIDE A - B\n=>  A and B intersect", fontsize=12.5, weight="bold", color=GREEN)
    ax.set_xlim(-4.2, 3.6); ax.set_ylim(-3.8, 3.4)
    ax.set_aspect("equal"); clean_ax(ax)
    # 轴线提示
    ax.axhline(0, color=GREY, lw=0.8, alpha=0.6, zorder=0)
    ax.axvline(0, color=GREY, lw=0.8, alpha=0.6, zorder=0)

    fig.suptitle("Minkowski difference:  A and B intersect  <=>  origin in (A - B)",
                 fontsize=13.5, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p4_13-01-minkowski-difference-overlap.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 02
def fig_minkowski_separate():
    """分离情形: 原点在 A-B 外部, 最近距离 = 原点到 A-B 边界的距离."""
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 6.4))

    # ---- 左: A 和 B 两个形状 (分离)
    ax = axes[0]
    A = polygon_verts(-1.5, 0.2, 1.3, 5, rot=0.2)        # 五边形 A (左)
    B = polygon_verts(2.2, 0.0, 1.1, 4, rot=np.pi/4)     # 四边形 B (右), 与 A 分离
    ax.add_patch(Polygon(A, closed=True, facecolor=RED_SOFT, edgecolor=RED, lw=2.2, alpha=0.85))
    ax.add_patch(Polygon(B, closed=True, facecolor=BLUE_SOFT, edgecolor=BLUE, lw=2.2, alpha=0.7))
    ax.text(A[:,0].mean(), A[:,1].mean()+1.7, "A", color=RED, fontsize=18, weight="bold", ha="center")
    ax.text(B[:,0].mean(), B[:,1].mean()+1.6, "B", color=BLUE, fontsize=18, weight="bold", ha="center")
    # 最近点对(示意)
    pa = np.array([A[:,0].max(), A[:,1].mean()])
    pb = np.array([B[:,0].min(), B[:,1].mean()])
    ax.plot([pa[0], pb[0]], [pa[1], pb[1]], "--", color=AMBER, lw=2.2, zorder=4)
    ax.plot([pa[0]], [pa[1]], "o", color=RED, markersize=9, zorder=6)
    ax.plot([pb[0]], [pb[1]], "o", color=BLUE, markersize=9, zorder=6)
    ax.text((pa[0]+pb[0])/2, pa[1]+0.28, "distance d", color=AMBER, fontsize=11.5, ha="center", weight="bold")
    ax.set_title("Shapes A and B separated\n(A and B do NOT intersect)", fontsize=12.5, weight="bold")
    ax.set_xlim(-3.6, 4.0); ax.set_ylim(-2.6, 2.6)
    ax.set_aspect("equal"); clean_ax(ax)

    # ---- 右: 闵可夫斯基差 A - B, 原点在外部
    ax = axes[1]
    diff = minkowski_difference(A, B)
    ax.add_patch(Polygon(diff, closed=True, facecolor=GREEN_SOFT, edgecolor=GREEN, lw=2.4, alpha=0.8))
    # 原点
    ax.plot([0], [0], marker="o", color=RED, markersize=11, zorder=6)
    ax.plot([0], [0], marker="o", fillstyle="none", color=RED, markersize=20, mew=1.5, zorder=5)
    ax.text(0.18, 0.05, "origin (0,0)", color=RED, fontsize=11.5, weight="bold")
    ax.text(diff[:,0].mean(), diff[:,1].max()+0.35, "A - B",
            color=GREEN, fontsize=13, weight="bold", ha="center")
    # 原点到差集(凸多边形)边界的最近点: 对每条边求投影, 取最近
    nx, ny = closest_point_on_convex_boundary(diff, (0.0, 0.0))
    ax.plot([0, nx], [0, ny], "-", color=AMBER, lw=2.4, zorder=4)
    ax.plot([nx], [ny], "o", color=AMBER, markersize=9, zorder=6)
    ax.text(nx-0.3, ny+0.25, "closest\npoint", color=AMBER, fontsize=10, ha="center", weight="bold")
    ax.text(0.15, -0.55, "d = dist(origin, A-B)\n= dist(A, B)", color=AMBER, fontsize=10.5, weight="bold")
    ax.set_title("Origin is OUTSIDE A - B\n=>  separated;  distance = |origin to A-B|",
                 fontsize=12.5, weight="bold", color=RED)
    ax.set_xlim(-5.0, 4.5); ax.set_ylim(-3.6, 3.4)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.axhline(0, color=GREY, lw=0.8, alpha=0.6, zorder=0)
    ax.axvline(0, color=GREY, lw=0.8, alpha=0.6, zorder=0)

    fig.suptitle("Minkowski difference:  A and B separated  <=>  origin outside (A - B)",
                 fontsize=13.5, weight="bold", y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p4_13-02-minkowski-difference-separate.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 03
def fig_support_function():
    """支撑函数 support(S, d) = S 上沿方向 d 最远的点."""
    fig, ax = plt.subplots(figsize=(9.0, 6.6))
    S = polygon_verts(0, 0, 2.0, 6, rot=0.1)
    ax.add_patch(Polygon(S, closed=True, facecolor=SOFT, edgecolor=INK, lw=2.2))
    ax.text(0, 0, "S", fontsize=20, ha="center", va="center", weight="bold", color=INK)

    # 三个方向, 各取最远点
    dirs = [np.array([1.0, 0.3]), np.array([-0.5, 1.0]), np.array([-0.8, -0.7])]
    colors = [RED, BLUE, PURPLE]
    labels = ["d1", "d2", "d3"]
    for d, c, lab in zip(dirs, colors, labels):
        dn = d / np.linalg.norm(d)
        # support = argmax <v, d>
        dots = S @ d
        idx = int(np.argmax(dots))
        sp = S[idx]
        # 画方向箭头(从中心出发)
        arrow(ax, (0, 0), (dn[0]*1.6, dn[1]*1.6), color=c, lw=1.8)
        ax.text(dn[0]*1.7+0.05, dn[1]*1.7+0.05, lab, color=c, fontsize=12, weight="bold")
        # 标出最远点
        ax.plot([sp[0]], [sp[1]], "o", color=c, markersize=11, zorder=6)
        ax.text(sp[0]*1.18, sp[1]*1.18, f"support(S,{lab})", color=c, fontsize=10, weight="bold",
                ha="center")
        # 画支撑线 (perpendicular to d, passing through sp): 即最远点处的切线方向
        perp = np.array([-dn[1], dn[0]])
        L = 1.0
        ax.plot([sp[0]-perp[0]*L, sp[0]+perp[0]*L],
                [sp[1]-perp[1]*L, sp[1]+perp[1]*L], "--", color=c, lw=1.6, alpha=0.7)

    ax.text(0, -3.0, "support(S, d) = the point of S farthest in direction d\n"
                     "= argmax over v in S of  <v, d>",
            ha="center", fontsize=11.5, color=INK)
    ax.set_title("Support function:  pick the farthest vertex along a direction",
                 fontsize=12.5, weight="bold")
    ax.set_xlim(-3.4, 3.4); ax.set_ylim(-3.4, 3.0)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.axhline(0, color=GREY, lw=0.7, alpha=0.5, zorder=0)
    ax.axvline(0, color=GREY, lw=0.7, alpha=0.5, zorder=0)
    fig.savefig(os.path.join(OUT, "fig-p4_13-03-support-function.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 04
def fig_gjk_simplex_iteration():
    """GJK 迭代: 点 -> 线段 -> 三角形, 朝原点逼近.

    手工模拟一遍 GJK 在 (A-B) 上的迭代(用真实凸包):
      iter1: d=(1,0), w1=support, simplex=1 点
      iter2: d=-w1, w2=support, simplex=2 点(线段), 求线段上最近原点的点, 得新 d
      iter3: d 向原点, w3=support, simplex=3 点(三角), 若原点在三角内则相交
    这里用 3 个子图展示 simplex 的演化(在 A-B 空间).
    """
    # 用 fig01 的 A,B 构造 A-B(相交, 原点在内部)
    A = polygon_verts(0.2, 0.3, 1.5, 5, rot=0.2)
    B = polygon_verts(1.0, -0.4, 1.2, 4, rot=np.pi/4)
    diff = minkowski_difference(A, B)

    def support(d):
        dots = diff @ d
        return diff[int(np.argmax(dots))]

    fig, axes = plt.subplots(1, 3, figsize=(15.5, 5.6))

    # ---- iter 1: 1 点
    ax = axes[0]
    ax.add_patch(Polygon(diff, closed=True, facecolor=GREEN_SOFT, edgecolor=GREEN, lw=1.8, alpha=0.45))
    ax.plot([0], [0], "o", color=RED, markersize=10, zorder=6)
    ax.plot([0], [0], "o", fillstyle="none", color=RED, markersize=18, mew=1.4, zorder=5)
    ax.text(0.12, 0.12, "O", color=RED, fontsize=12, weight="bold")
    d1 = np.array([1.0, 0.0])
    w1 = support(d1)
    ax.plot([w1[0]], [w1[1]], "o", color=AMBER, markersize=12, zorder=7)
    ax.text(w1[0]+0.1, w1[1]+0.1, "w1", color=AMBER, fontsize=12, weight="bold")
    arrow(ax, (0,0), tuple(d1*1.5), color=INK, lw=1.4)
    ax.text(d1[0]*1.5+0.1, d1[1]*1.5-0.25, "d", fontsize=11, color=INK)
    ax.set_title("Iteration 1\nsimplex = {w1} (point)\nnext d = -w1 (toward origin)",
                 fontsize=11.5, weight="bold")
    ax.set_xlim(-4.0, 4.0); ax.set_ylim(-3.6, 3.4)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.axhline(0, color=GREY, lw=0.7, alpha=0.5)
    ax.axvline(0, color=GREY, lw=0.7, alpha=0.5)

    # ---- iter 2: 2 点 (线段)
    ax = axes[1]
    ax.add_patch(Polygon(diff, closed=True, facecolor=GREEN_SOFT, edgecolor=GREEN, lw=1.8, alpha=0.45))
    ax.plot([0], [0], "o", color=RED, markersize=10, zorder=6)
    ax.plot([0], [0], "o", fillstyle="none", color=RED, markersize=18, mew=1.4, zorder=5)
    ax.text(0.12, 0.12, "O", color=RED, fontsize=12, weight="bold")
    d2 = -w1
    w2 = support(d2)
    # 线段 w1-w2
    ax.plot([w1[0], w2[0]], [w1[1], w2[1]], "-", color=AMBER, lw=3.0, zorder=6)
    ax.plot([w1[0]], [w1[1]], "o", color=AMBER, markersize=12, zorder=7)
    ax.plot([w2[0]], [w2[1]], "o", color=AMBER, markersize=12, zorder=7)
    ax.text(w1[0]+0.1, w1[1]+0.1, "w1", color=AMBER, fontsize=12, weight="bold")
    ax.text(w2[0]-0.45, w2[1]-0.1, "w2", color=AMBER, fontsize=12, weight="bold")
    # 线段上离原点最近的点
    seg = w2 - w1
    t = -np.dot(w1, seg) / np.dot(seg, seg)
    t = float(np.clip(t, 0.0, 1.0))
    cp = w1 + t * seg
    ax.plot([cp[0]], [cp[1]], "*", color=PURPLE, markersize=16, zorder=8)
    arrow(ax, (0,0), tuple(cp*0.95), color=PURPLE, lw=1.6)
    ax.set_title("Iteration 2\nsimplex = {w1,w2} (segment)\nnext d = toward origin\n"
                 "(closest point on segment to O)",
                 fontsize=11.5, weight="bold")
    ax.set_xlim(-4.0, 4.0); ax.set_ylim(-3.6, 3.4)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.axhline(0, color=GREY, lw=0.7, alpha=0.5)
    ax.axvline(0, color=GREY, lw=0.7, alpha=0.5)

    # ---- iter 3: 3 点 (三角形), 原点在内部 => 相交
    ax = axes[2]
    ax.add_patch(Polygon(diff, closed=True, facecolor=GREEN_SOFT, edgecolor=GREEN, lw=1.8, alpha=0.45))
    ax.plot([0], [0], "o", color=RED, markersize=10, zorder=6)
    ax.plot([0], [0], "o", fillstyle="none", color=RED, markersize=18, mew=1.4, zorder=5)
    ax.text(0.12, 0.12, "O", color=RED, fontsize=12, weight="bold")
    # 用线段最近点方向继续取 support
    d3 = -cp / (np.linalg.norm(cp) + 1e-9)
    w3 = support(d3)
    tri = np.array([w1, w2, w3])
    ax.add_patch(Polygon(tri, closed=True, facecolor=AMBER_SOFT, edgecolor=AMBER, lw=2.6, alpha=0.85, zorder=5))
    for lab, p in zip(["w1","w2","w3"], [w1,w2,w3]):
        ax.plot([p[0]], [p[1]], "o", color=AMBER, markersize=11, zorder=8)
        ax.text(p[0]+0.1, p[1]+0.1, lab, color=INK, fontsize=11.5, weight="bold")
    ax.set_title("Iteration 3\nsimplex = {w1,w2,w3} (triangle)\norigin INSIDE triangle\n"
                 "=>  A and B INTERSECT",
                 fontsize=11.5, weight="bold", color=GREEN)
    ax.set_xlim(-4.0, 4.0); ax.set_ylim(-3.6, 3.4)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.axhline(0, color=GREY, lw=0.7, alpha=0.5)
    ax.axvline(0, color=GREY, lw=0.7, alpha=0.5)

    fig.suptitle("GJK simplex iteration on (A - B):  point -> segment -> triangle, toward origin",
                 fontsize=13.5, weight="bold", y=1.03)
    fig.savefig(os.path.join(OUT, "fig-p4_13-04-gjk-simplex-iteration.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig 05
def fig_gjk_terminate_origin():
    """三角形包含原点 = 相交的判据(2D 用叉积判断原点在三角内的三个区域)."""
    fig, ax = plt.subplots(figsize=(9.5, 7.0))
    # 一个包含原点的三角形
    tri_in = np.array([[-2.2, -1.4], [2.6, -0.8], [0.4, 2.4]])
    ax.add_patch(Polygon(tri_in, closed=True, facecolor=GREEN_SOFT, edgecolor=GREEN, lw=2.6, alpha=0.8))
    ax.plot([0],[0], "o", color=RED, markersize=11, zorder=7)
    ax.plot([0],[0], "o", fillstyle="none", color=RED, markersize=20, mew=1.5, zorder=6)
    ax.text(0.12, 0.12, "origin", color=RED, fontsize=12, weight="bold")
    for lab, p in zip(["w1","w2","w3"], tri_in):
        ax.plot([p[0]],[p[1]], "o", color=AMBER, markersize=11, zorder=8)
        ax.text(p[0]+0.12, p[1]+0.12, lab, fontsize=12, weight="bold")
    ax.text(0, -0.6, "origin inside triangle\n=> A and B INTERSECT", ha="center",
            color=GREEN, fontsize=12.5, weight="bold")
    ax.set_title("Termination: 3-point simplex containing origin  =>  overlap",
                 fontsize=12.5, weight="bold")
    lim = 3.6
    ax.set_xlim(-lim, lim); ax.set_ylim(-lim, lim)
    ax.set_aspect("equal"); clean_ax(ax)
    ax.axhline(0, color=GREY, lw=0.7, alpha=0.5)
    ax.axvline(0, color=GREY, lw=0.7, alpha=0.5)
    fig.savefig(os.path.join(OUT, "fig-p4_13-05-gjk-terminate-origin.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_minkowski_overlap()
    fig_minkowski_separate()
    fig_support_function()
    fig_gjk_simplex_iteration()
    fig_gjk_terminate_origin()
    print("done:", sorted(f for f in os.listdir(OUT)
                          if f.startswith("fig-p4_13") and f.endswith(".png")))
