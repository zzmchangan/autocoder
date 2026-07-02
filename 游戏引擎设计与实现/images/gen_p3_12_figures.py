# -*- coding: utf-8 -*-
"""为《游戏引擎》P3-12《场景图与空间划分》生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_p3_12_figures.py
配色与 rcParams 与 gen_p0/gen_p1_02/gen_p3_11 保持一致。
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import (Rectangle, FancyArrowPatch, FancyBboxPatch,
                                Circle, Polygon, PathPatch)
from matplotlib.path import Path

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
DGREY = (0.62, 0.62, 0.66)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)
PURP  = (0.55, 0.30, 0.78)
TEAL  = (0.13, 0.62, 0.62)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=12, weight="normal", tc=INK):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=fc, edgecolor=ec, linewidth=1.4))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.6, style="-|>", rad=0.0):
    cs = f"arc3,rad={rad}" if rad else "arc3,rad=0"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle=style, color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=16))


# ---------------------------------------------------------------- fig-01
# 场景图父子变换树: 角色 -> (躯干 -> 手 -> 剑) 三个层级, 标注 local/global Transform
def fig_scene_graph_tree():
    fig, ax = plt.subplots(figsize=(13, 7.4))
    ax.set_xlim(0, 13); ax.set_ylim(0, 7.4)
    clean_ax(ax)
    ax.set_title("Scene Graph: Parent-Child Transform Propagation",
                 fontsize=15, weight="bold", pad=14)

    # root: Character
    box(ax, 5.0, 6.0, 3.0, 0.95,
        "Character (root)\nTransform: T(10,0,0) R(0)", fc=BLUE, ec=INK,
        fs=11.5, weight="bold", tc="white")

    # child: Torso
    box(ax, 5.0, 4.4, 3.0, 0.95,
        "Torso\nTransform: T(0,1,0)  (local)", fc=SOFT, ec=INK, fs=11)
    # grandchild: Hand
    box(ax, 1.2, 2.7, 3.0, 0.95,
        "Hand\nTransform: T(0.6, -0.4, 0)\n(local to Torso)", fc=SOFT, ec=INK, fs=10.5)
    # grandchild: Sword (child of Hand)
    box(ax, 1.2, 0.9, 3.0, 0.95,
        "Sword\nTransform: T(0.1, -0.9, 0)\n(local to Hand)", fc=AMBER, ec=INK,
        fs=10.5, weight="bold")
    # sibling of Torso: Head
    box(ax, 9.0, 4.4, 3.0, 0.95,
        "Head\nTransform: T(0, 0.8, 0)\n(local to Character)", fc=SOFT, ec=INK, fs=10.5)

    # hierarchy edges (ChildOf points up: child -> parent)
    arrow(ax, (6.5, 5.35), (6.5, 5.98), color=DGREY, lw=2.0)
    arrow(ax, (6.3, 3.65), (6.3, 4.38), color=DGREY, lw=2.0)   # Hand -> Torso
    arrow(ax, (2.7, 1.85), (2.7, 2.68), color=DGREY, lw=2.0)   # Sword -> Hand
    arrow(ax, (10.5, 5.35), (7.4, 5.98), color=DGREY, lw=2.0, rad=-0.15)  # Head -> Character
    # Hand placement relative to Torso: a curved link from Torso down-left to Hand node region
    arrow(ax, (5.0, 4.55), (4.2, 3.4), color=DGREY, lw=2.0, rad=0.18)  # Torso -> Hand region

    ax.text(0.4, 6.55, "depth 0", fontsize=10, color=DGREY, rotation=90, va="center")
    ax.text(0.4, 4.85, "depth 1", fontsize=10, color=DGREY, rotation=90, va="center")
    ax.text(0.4, 3.15, "depth 2", fontsize=10, color=DGREY, rotation=90, va="center")
    ax.text(0.4, 1.35, "depth 3", fontsize=10, color=DGREY, rotation=90, va="center")

    # Right-side annotation: world transform chain
    ann = ("GlobalTransform (world) = chain of local\n"
           "  Character:  G = T(10,0,0)\n"
           "  Torso:      G = T(10,0,0) * T(0,1,0)\n"
           "  Hand:       G = T(10,0,0) * T(0,1,0) * T(0.6,-0.4,0)\n"
           "  Sword:      G = ... * T(0.1,-0.9,0)\n\n"
           "Move Character 1 unit right ->\n"
           "all descendants auto-shift (one matrix)")
    ax.text(9.05, 3.05, ann, fontsize=10.2, color=INK,
            family="monospace",
            bbox=dict(boxstyle="round,pad=0.4", fc="#fdf6e3", ec=AMBER, lw=1.3))

    fig.savefig(os.path.join(OUT, "fig-p3_12_01-scene-graph-tree.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-02
# 四叉树空间划分: 递归四分空间, 对象分到叶子节点
def fig_quadtree():
    fig, ax = plt.subplots(figsize=(11.5, 7.4))
    ax.set_xlim(-0.3, 11.5); ax.set_ylim(-0.3, 7.4)
    clean_ax(ax)
    ax.set_title("Quadtree: Recursively Subdivide Space (2D, 4 children per node)",
                 fontsize=14.5, weight="bold", pad=12)

    # main region: a square subdivided
    def draw_cell(x, y, s, depth, maxdepth=2):
        ax.add_patch(Rectangle((x, y), s, s, fill=False, ec=INK, lw=1.4))
        if depth < maxdepth:
            half = s / 2
            # only subdivide if "has enough objects" - we draw all 4 for illustration
            for dx, dy in [(0, 0), (half, 0), (0, half), (half, half)]:
                draw_cell(x + dx, y + dy, half, depth + 1, maxdepth)

    base_x, base_y, base_s = 0.5, 0.8, 6.0
    draw_cell(base_x, base_y, base_s, 0, maxdepth=2)

    # scatter objects (dots), each falling into a leaf cell
    rng = np.random.default_rng(7)
    pts = rng.uniform([base_x + 0.3, base_y + 0.3],
                      [base_x + base_s - 0.3, base_y + base_s - 0.3], size=(34, 2))
    ax.scatter(pts[:, 0], pts[:, 1], s=42, c=PURP, edgecolors=INK,
               linewidths=0.6, zorder=5)

    # a query window (e.g. frustum) -- only visit cells it overlaps
    qx, qy, qw, qh = 0.7, 0.9, 2.6, 2.6
    ax.add_patch(Rectangle((qx, qy), qw, qh, fill=True, facecolor=TEAL,
                           alpha=0.16, ec=TEAL, lw=2.0, zorder=4))
    ax.text(qx + qw / 2, qy + qh + 0.18, "query region",
            ha="center", fontsize=10.5, color=TEAL, weight="bold")

    # annotation
    ann = ("Quadtree (2D):\n"
           "  node = aabb + up to CAP objects\n"
           "  CAP exceeded -> split into 4 children\n"
           "  object lives in the leaf that contains it\n\n"
           "Query: descend only into cells the query\n"
           "  overlaps -> skip the empty / far cells\n\n"
           "Octree (3D) = same idea, 8 children per node")
    ax.text(7.0, 4.3, ann, fontsize=10.6, color=INK, family="monospace",
            va="center",
            bbox=dict(boxstyle="round,pad=0.45", fc=SOFT, ec=INK, lw=1.2))

    fig.savefig(os.path.join(OUT, "fig-p3_12_02-quadtree.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-03
# 视锥剔除: 摄像机视锥 vs 对象 AABB
def fig_frustum_culling():
    fig, ax = plt.subplots(figsize=(12.5, 7.2))
    ax.set_xlim(-0.5, 12.5); ax.set_ylim(-0.5, 7.2)
    clean_ax(ax)
    ax.set_title("Frustum Culling: Skip Objects Outside the Camera View",
                 fontsize=14.5, weight="bold", pad=12)

    # camera at left
    cam = (1.0, 3.5)
    ax.add_patch(Circle(cam, 0.18, fc=INK, ec=INK, zorder=6))
    ax.text(cam[0], cam[1] - 0.55, "camera", ha="center", fontsize=11, weight="bold")

    # frustum (trapezoid) opening to the right
    near_l, near_r = 1.6, 2.2
    far_l, far_r = 9.5, 11.5
    fy_top_n, fy_bot_n = 4.4, 2.6
    fy_top_f, fy_bot_f = 6.4, 0.6
    frust = Polygon([(near_l, fy_bot_n), (near_r, fy_top_n),
                     (far_r, fy_top_f), (far_l, fy_bot_f)],
                    closed=True, facecolor=BLUE, alpha=0.10,
                    edgecolor=BLUE, lw=1.8)
    ax.add_patch(frust)
    ax.text(6.0, 5.7, "camera frustum (view volume)", fontsize=11.5,
            color=BLUE, weight="bold")

    # AABB inside (visible - green)
    def aabb(x, y, w, h, color, label, ec=None, tc=INK):
        ax.add_patch(Rectangle((x, y), w, h, fill=True, facecolor=color,
                               alpha=0.30, edgecolor=ec or color, lw=1.8, zorder=3))
        ax.text(x + w / 2, y + h + 0.18, label, ha="center",
                fontsize=10.2, color=tc, weight="bold")

    aabb(4.0, 3.0, 1.1, 1.0, GREEN, "inside: draw", tc=GREEN)
    aabb(6.2, 2.2, 1.4, 1.6, GREEN, "inside: draw", tc=GREEN)

    # AABB fully outside (red - culled)
    aabb(2.0, 5.6, 1.0, 0.8, RED, "outside: cull", tc=RED)
    aabb(2.4, 0.2, 0.9, 0.7, RED, "outside: cull", tc=RED)
    aabb(10.6, 4.6, 1.2, 1.2, RED, "outside: cull", tc=RED)

    # AABB straddling (edge) -> intersect test says visible
    aabb(8.6, 3.1, 1.6, 1.4, AMBER, "intersects: draw", tc=AMBER)

    # decision flow (right side)
    ann = ("Per-object test (naive, O(n)):\n"
           "  for each entity:\n"
           "    if frustum.intersects(aabb): draw\n"
           "    else: cull\n\n"
           "n = 50000  ->  too slow per frame\n\n"
           "With spatial index (BVH / octree):\n"
           "  traverse only nodes overlapping\n"
           "  the frustum -> visit a handful of\n"
           "  leaves, test their handful of objs")
    ax.text(0.0, 6.7, ann, fontsize=10.0, color=INK, family="monospace",
            va="top",
            bbox=dict(boxstyle="round,pad=0.4", fc="#fdf6e3", ec=AMBER, lw=1.2))

    fig.savefig(os.path.join(OUT, "fig-p3_12_03-frustum-culling.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-04
# BVH 层次包围盒: 内部节点 AABB 包住子树, 树形加速剔除
def fig_bvh():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.5, 6.6),
                                    gridspec_kw={"width_ratios": [1.0, 1.05]})
    fig.suptitle("Bounding Volume Hierarchy (BVH): Nest AABBs as a Tree",
                 fontsize=14.5, weight="bold", y=0.99)

    # ---- left: spatial view
    ax1.set_xlim(0, 6.6); ax1.set_ylim(0, 6.6)
    clean_ax(ax1)
    ax1.set_title("space: nested AABBs", fontsize=12)

    # root AABB (whole scene) - thick
    ax1.add_patch(Rectangle((0.25, 0.25), 6.0, 6.0, fill=False,
                            ec=INK, lw=2.4, ls="--"))
    ax1.text(0.32, 6.05, "root", fontsize=10.5, color=INK, weight="bold")

    # two child AABBs
    ax1.add_patch(Rectangle((0.5, 0.6), 2.9, 3.0, fill=False, ec=BLUE, lw=2.0))
    ax1.text(0.55, 3.45, "L", fontsize=11, color=BLUE, weight="bold")
    ax1.add_patch(Rectangle((3.5, 0.7), 2.6, 4.6, fill=False, ec=PURP, lw=2.0))
    ax1.text(3.55, 5.15, "R", fontsize=11, color=PURP, weight="bold")

    # leaf objects (small AABBs / dots)
    leaves_L = [(0.9, 1.2), (1.4, 2.4), (2.3, 1.0), (2.0, 2.9)]
    for (x, y) in leaves_L:
        ax1.add_patch(Rectangle((x, y), 0.55, 0.5, fc=GREEN, alpha=0.45,
                                ec=GREEN, lw=1.3))
    leaves_R = [(3.9, 1.4), (4.7, 2.6), (5.2, 3.8), (4.2, 4.2), (5.4, 1.0)]
    for (x, y) in leaves_R:
        ax1.add_patch(Rectangle((x, y), 0.55, 0.5, fc=AMBER, alpha=0.45,
                                ec=AMBER, lw=1.3))

    # frustum overlay (a tall thin window on the left side)
    ax1.add_patch(Rectangle((0.4, 0.5), 1.4, 5.7, fill=True,
                            facecolor=TEAL, alpha=0.12, ec=TEAL, lw=2.0, zorder=2))
    ax1.text(1.1, 6.35, "frustum", ha="center", color=TEAL, fontsize=10, weight="bold")

    # ---- right: the tree
    ax2.set_xlim(0, 7.0); ax2.set_ylim(0, 6.6)
    clean_ax(ax2)
    ax2.set_title("tree: descend only visited branches", fontsize=12)

    # root node
    box(ax2, 2.8, 5.5, 1.6, 0.7, "root\nAABB", fc=SOFT, ec=INK, fs=10.5, weight="bold")
    # children
    box(ax2, 0.8, 3.7, 1.6, 0.7, "L (visit)\nfrustum\noverlaps", fc=BLUE, ec=INK,
        fs=10, tc="white", weight="bold")
    box(ax2, 4.6, 3.7, 1.6, 0.7, "R (SKIP)\nfrustum\nmisses AABB",
        fc=GREY, ec=INK, fs=10, tc=INK)
    # leaves under L
    for i, (lx, ly) in enumerate([(0.2, 1.8), (1.7, 1.8), (0.2, 0.6), (1.7, 0.6)]):
        box(ax2, lx, ly, 1.3, 0.65, f"obj {i+1}", fc=GREEN, ec=INK, fs=9.5)
    # leaves under R shown greyed (skipped subtree)
    for i, (lx, ly) in enumerate([(4.4, 1.8), (5.6, 1.8), (4.4, 0.6), (5.6, 0.6)]):
        box(ax2, lx, ly, 1.0, 0.55, f"obj", fc=GREY, ec=DGREY, fs=9.0, tc=DGREY)
    ax2.text(5.1, 0.05, "(whole subtree pruned)", ha="center",
             color=DGREY, fontsize=9.5, style="italic")

    # edges
    arrow(ax2, (3.6, 5.45), (1.6, 4.45), color=DGREY, lw=1.7)
    arrow(ax2, (3.6, 5.45), (5.4, 4.45), color=DGREY, lw=1.7)
    for (lx, ly) in [(0.85, 1.8), (2.35, 1.8), (0.85, 0.6), (2.35, 0.6)]:
        arrow(ax2, (1.6, 3.65), (lx + 0.65, ly + 0.7), color=DGREY, lw=1.2)

    fig.savefig(os.path.join(OUT, "fig-p3_12_04-bvh.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_scene_graph_tree()
    fig_quadtree()
    fig_frustum_culling()
    fig_bvh()
    print("OK: generated 4 figures for P3-12")
