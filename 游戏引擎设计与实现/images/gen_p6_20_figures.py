# -*- coding: utf-8 -*-
"""Generate PNG figures for chapter P6-20 (book finale: data-oriented big loop).
English labels, Chinese text in the chapter body. Run: python gen_p6_20_figures.py

Figures:
  fig-p6_20_01-knowledge-map.png       Whole-book knowledge map: Organize (ECS) vs Drive (main loop) as skeleton, chapters in place, cross-cutting supports both
  fig-p6_20_02-one-frame-timeline.png  One frame's full timeline: main loop tick -> input -> RunFixedMainLoop (physics fixed step) -> Update -> ApplyDeferred sync -> Extract -> render thread Queue/PhaseSort/Prepare+Batch/Render -> GPU pipeline -> next frame (pipeline)
  fig-p6_20_03-universality.png        Universality: game engine / browser / physics sim / realtime dashboard are all "big loop + data-oriented"
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch, Circle, Polygon
import numpy as np

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
GREY  = (0.78, 0.78, 0.80)
DGREY = (0.55, 0.55, 0.60)
INK   = (0.10, 0.10, 0.10)
SOFT  = (0.96, 0.96, 0.97)
LBLUE = (0.90, 0.94, 1.0)
LGREEN = (0.88, 0.94, 0.88)
LAMBER = (1.0, 0.94, 0.82)
LRED  = (1.0, 0.90, 0.90)
PURP  = (0.55, 0.36, 0.74)
LPURP = (0.94, 0.88, 0.98)
TEAL  = (0.13, 0.58, 0.60)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def node_box(ax, cx, cy, w, h, label, sub, color, txt_color="white", ec=INK, fs=10.5, sub_fs=8.2):
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=color, edgecolor=ec, linewidth=1.2, alpha=0.96))
    ax.text(cx, cy + h * 0.18, label, fontsize=fs, ha="center", va="center",
            color=txt_color, weight="bold")
    if sub:
        ax.text(cx, cy - h * 0.22, sub, fontsize=sub_fs, ha="center", va="center",
                color=txt_color)


def arrow(ax, x1, y1, x2, y2, color=INK, lw=1.5, style="->", ls="-"):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2),
                                 arrowstyle=style, mutation_scale=14,
                                 color=color, linewidth=lw, linestyle=ls,
                                 shrinkA=2, shrinkB=2))


# =====================================================================
# Figure 1: Knowledge Map (Organize vs Drive, chapters in place)
# =====================================================================
def fig1_knowledge_map():
    fig, ax = plt.subplots(figsize=(14.5, 9.2))
    clean_ax(ax)
    ax.set_xlim(0, 14.5)
    ax.set_ylim(0, 9.2)

    # Title
    ax.text(7.25, 8.75, "Game Engine = Data-Oriented Big Loop",
            fontsize=15.5, ha="center", va="center", weight="bold", color=INK)
    ax.text(7.25, 8.30, "Whole-book knowledge map: Organize (ECS) vs Drive (main loop) as two halves, cross-cutting supports both",
            fontsize=10.2, ha="center", va="center", color=DGREY, style="italic")

    # Top center: the one-line thesis
    node_box(ax, 7.25, 7.55, 6.8, 0.62, "while(true){ update world; render frame; }  x60/s",
             "data laid out by how Systems traverse it", INK, "white", fs=10.5, sub_fs=8.5)

    # Left column: ORGANIZE (blue)
    ax.add_patch(FancyBboxPatch((0.4, 1.6), 4.4, 5.5,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LBLUE, edgecolor=BLUE, linewidth=1.6, alpha=0.5))
    ax.text(2.6, 6.80, "ORGANIZE  (ECS: how data lies)", fontsize=12.2, ha="center",
            va="center", weight="bold", color=BLUE)
    ax.text(2.6, 6.45, "for cache + parallelism", fontsize=9.0, ha="center", va="center",
            color=BLUE, style="italic")

    org_steps = [
        ("P1-04  OOP wall",          "deep inheritance / data scattered"),
        ("P2-05  Trinity",           "Entity=ID  Component=data  System=behavior"),
        ("P2-06  SoA vs AoS",        "same field contiguous  (alloc book)"),
        ("P2-07  SIMD + parallel",   "data parallel inside one System"),
        ("P2-08  Archetype",         "same combo -> same table, no skip"),
        ("P2-09  Query bitmask",     "O(1) match archetype to query"),
    ]
    y = 5.95
    for label, sub in org_steps:
        node_box(ax, 2.6, y, 4.0, 0.56, label, sub, BLUE, "white", fs=9.6, sub_fs=7.6)
        if y < 5.95:
            arrow(ax, 2.6, y + 0.30, 2.6, y + 0.55 - 0.30 + 0.30, color=BLUE, lw=1.4)
        y -= 0.66

    # arrow chain inside organize
    for i in range(len(org_steps) - 1):
        y_top = 5.95 - i * 0.66 - 0.30
        y_bot = 5.95 - (i + 1) * 0.66 + 0.30
        arrow(ax, 2.6, y_top, 2.6, y_bot, color=BLUE, lw=1.4)

    # Right column: DRIVE (green)
    ax.add_patch(FancyBboxPatch((9.7, 1.6), 4.4, 5.5,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LGREEN, edgecolor=GREEN, linewidth=1.6, alpha=0.5))
    ax.text(11.9, 6.80, "DRIVE  (main loop: how it runs)", fontsize=12.2, ha="center",
            va="center", weight="bold", color=GREEN)
    ax.text(11.9, 6.45, "for real-time / frame rate", fontsize=9.0, ha="center", va="center",
            color=GREEN, style="italic")

    drv_steps = [
        ("P1-02  while 3-phase",     "input -> update -> render, 16ms"),
        ("P3-10  fixed vs render",   "accumulator, 64Hz, max_delta"),
        ("P3-11  delta time",        "frame-rate independent, 3 clocks"),
        ("P3-12  scene + cull",      "frustum cull: 90% gone"),
        ("P5-17  job DAG",           "static conflict bits, sync point"),
        ("P5-18  render submit",     "cull + sort + batch + draw call"),
    ]
    y = 5.95
    for label, sub in drv_steps:
        node_box(ax, 11.9, y, 4.0, 0.56, label, sub, GREEN, "white", fs=9.6, sub_fs=7.6)
        y -= 0.66
    for i in range(len(drv_steps) - 1):
        y_top = 5.95 - i * 0.66 - 0.30
        y_bot = 5.95 - (i + 1) * 0.66 + 0.30
        arrow(ax, 11.9, y_top, 11.9, y_bot, color=GREEN, lw=1.4)

    # Middle column: CROSS-CUTTING (purple)
    ax.add_patch(FancyBboxPatch((5.55, 2.6), 3.4, 3.5,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LPURP, edgecolor=PURP, linewidth=1.6, alpha=0.5))
    ax.text(7.25, 5.80, "CROSS-CUTTING", fontsize=11.2, ha="center", va="center",
            weight="bold", color=PURP)
    ax.text(7.25, 5.50, "supports both halves", fontsize=8.8, ha="center", va="center",
            color=PURP, style="italic")
    cross = [
        ("P4-13  assets",     "handle + async + refcount"),
        ("P4-14  Lua",        "hot-reload scripts"),
        ("P4-15  C# IL2CPP",  "Unity cross-platform"),
        ("P4-16  serialize",  "ECS-friendly scene save"),
    ]
    y = 4.95
    for label, sub in cross:
        node_box(ax, 7.25, y, 3.0, 0.50, label, sub, PURP, "white", fs=9.0, sub_fs=7.2)
        y -= 0.58

    # Two-way arrows: thesis <-> each half
    arrow(ax, 4.30, 7.30, 6.85, 7.30, color=INK, lw=1.6, style="<->")
    arrow(ax, 7.65, 7.30, 10.20, 7.30, color=INK, lw=1.6, style="<->")

    # Cross-cutting <-> organize and drive
    arrow(ax, 5.55, 4.30, 4.80, 4.30, color=PURP, lw=1.2, style="<-", ls="--")
    arrow(ax, 8.95, 4.30, 9.70, 4.30, color=PURP, lw=1.2, style="->", ls="--")

    # Bottom: cross-links to other books
    ax.add_patch(FancyBboxPatch((0.4, 0.35), 13.7, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=SOFT, edgecolor=DGREY, linewidth=1.0))
    ax.text(0.75, 1.02, "Cross-book links:", fontsize=9.8, ha="left", va="center",
            weight="bold", color=INK)
    ax.text(0.75, 0.62,
            "Graphics Pipeline (render line)  |  Allocator (SoA/SIMD root)  |  Lua VM (script)  |  JVM (C# IL2CPP)\n"
            "Linux Sync  +  Tokio (job DAG locks & work-stealing)  ->  next: Physics Engine (update segment heaviest box)",
            fontsize=8.6, ha="left", va="center", color=DGREY)

    # Bottom label tying the two halves
    ax.text(2.6, 1.35, "how data lies\n-> how fast each loop runs",
            fontsize=8.4, ha="center", va="center", color=BLUE, style="italic")
    ax.text(11.9, 1.35, "how loop runs\n-> how data should lie",
            fontsize=8.4, ha="center", va="center", color=GREEN, style="italic")
    arrow(ax, 4.0, 1.55, 10.5, 1.55, color=INK, lw=1.2, style="<->", ls=":")
    ax.text(7.25, 1.78, "two faces of one coin", fontsize=8.8, ha="center", va="center",
            color=INK, weight="bold")

    plt.savefig(os.path.join(OUT, "fig-p6_20_01-knowledge-map.png"))
    plt.close(fig)
    print("wrote fig-p6_20_01-knowledge-map.png")


# =====================================================================
# Figure 2: One Frame Timeline (the book's climax)
# =====================================================================
def fig2_one_frame_timeline():
    fig, ax = plt.subplots(figsize=(14.5, 8.6))
    clean_ax(ax)
    ax.set_xlim(0, 14.5)
    ax.set_ylim(0, 8.6)

    ax.text(7.25, 8.20, "One Frame, End to End",
            fontsize=15.5, ha="center", va="center", weight="bold", color=INK)
    ax.text(7.25, 7.85, "Organize (ECS layout) and Drive (loop scheduling) interlock inside every 16ms",
            fontsize=10.0, ha="center", va="center", color=DGREY, style="italic")

    # Two swim lanes: main thread (top), render thread (bottom)
    MAIN_Y = 5.6
    REND_Y = 2.2

    # lane backgrounds
    ax.add_patch(Rectangle((0.5, MAIN_Y - 0.85), 13.5, 1.7, facecolor=LBLUE, edgecolor=BLUE,
                           linewidth=1.2, alpha=0.45))
    ax.add_patch(Rectangle((0.5, REND_Y - 0.85), 13.5, 1.7, facecolor=LGREEN, edgecolor=GREEN,
                           linewidth=1.2, alpha=0.45))
    ax.text(0.7, MAIN_Y + 0.62, "MAIN THREAD  (simulation)", fontsize=10.2, ha="left", va="center",
            weight="bold", color=BLUE)
    ax.text(0.7, MAIN_Y + 0.32, "Main World", fontsize=8.4, ha="left", va="center", color=BLUE,
            style="italic")
    ax.text(0.7, REND_Y + 0.62, "RENDER THREAD  (parallel)", fontsize=10.2, ha="left", va="center",
            weight="bold", color=GREEN)
    ax.text(0.7, REND_Y + 0.32, "Render World", fontsize=8.4, ha="left", va="center", color=GREEN,
            style="italic")

    # ---- Main thread segments (frame N) ----
    # 1. tick + input
    seg_x = 2.0
    seg_w = 1.5
    node_box(ax, seg_x + seg_w/2, MAIN_Y, seg_w, 0.62, "1  tick + input",
             "delta clamp", INK, "white", fs=9.0, sub_fs=7.2)
    # 2. RunFixedMainLoop (physics)
    seg_x2 = seg_x + seg_w + 0.15
    seg_w2 = 2.3
    node_box(ax, seg_x2 + seg_w2/2, MAIN_Y, seg_w2, 0.62, "2  RunFixedMainLoop",
             "physics fixed step  64Hz", BLUE, "white", fs=9.0, sub_fs=7.2)
    # 3. Update (variable)
    seg_x3 = seg_x2 + seg_w2 + 0.15
    seg_w3 = 2.0
    node_box(ax, seg_x3 + seg_w3/2, MAIN_Y, seg_w3, 0.62, "3  Update",
             "AI / camera / UI  variable dt", BLUE, "white", fs=9.0, sub_fs=7.2)
    # 4. ApplyDeferred sync
    seg_x4 = seg_x3 + seg_w3 + 0.15
    seg_w4 = 1.8
    node_box(ax, seg_x4 + seg_w4/2, MAIN_Y, seg_w4, 0.62, "4  ApplyDeferred",
             "exclusive  &mut World", RED, "white", fs=9.0, sub_fs=7.2)
    # 5. Extract
    seg_x5 = seg_x4 + seg_w4 + 0.15
    seg_w5 = 1.7
    node_box(ax, seg_x5 + seg_w5/2, MAIN_Y, seg_w5, 0.62, "5  Extract",
             "Main -> Render World", AMBER, "white", fs=9.0, sub_fs=7.2)

    # arrows between main segments
    for a, b in [(seg_x + seg_w, seg_x2),
                 (seg_x2 + seg_w2, seg_x3),
                 (seg_x3 + seg_w3, seg_x4),
                 (seg_x4 + seg_w4, seg_x5)]:
        arrow(ax, a, MAIN_Y, b, MAIN_Y, color=INK, lw=1.4)

    # ---- Then frame N+1 simulation begins on main thread ----
    nx_x = seg_x5 + seg_w5 + 0.3
    node_box(ax, nx_x + 1.0, MAIN_Y, 2.0, 0.62, "frame N+1 sim",
             "main thread continues", DGREY, "white", fs=9.0, sub_fs=7.2)
    arrow(ax, seg_x5 + seg_w5, MAIN_Y, nx_x, MAIN_Y, color=INK, lw=1.4, ls="--")

    # ---- channel transfer Extract -> render thread ----
    # channel box
    chan_x = seg_x5 + seg_w5/2
    chan_y = (MAIN_Y + REND_Y) / 2
    node_box(ax, chan_x, chan_y, 2.2, 0.50, "bounded channel  cap=1",
             "borrow Render World", PURP, "white", fs=8.6, sub_fs=7.0)
    arrow(ax, seg_x5 + seg_w5/2, MAIN_Y - 0.32, chan_x, chan_y + 0.26, color=PURP, lw=1.6)
    arrow(ax, chan_x, chan_y - 0.26, chan_x, REND_Y + 0.32, color=PURP, lw=1.6)

    # ---- Render thread segments (rendering frame N while main does N+1) ----
    r1_x = 2.0
    r1_w = 2.0
    node_box(ax, r1_x + r1_w/2, REND_Y, r1_w, 0.62, "6  Queue",
             "cull + gen PhaseItem", GREEN, "white", fs=9.0, sub_fs=7.2)
    r2_x = r1_x + r1_w + 0.15
    r2_w = 1.8
    node_box(ax, r2_x + r2_w/2, REND_Y, r2_w, 0.62, "7  PhaseSort",
             "bin / depth sort", GREEN, "white", fs=9.0, sub_fs=7.2)
    r3_x = r2_x + r2_w + 0.15
    r3_w = 2.0
    node_box(ax, r3_x + r3_w/2, REND_Y, r3_w, 0.62, "8  Prepare+Batch",
             "instanced instance buffer", GREEN, "white", fs=9.0, sub_fs=7.2)
    r4_x = r3_x + r3_w + 0.15
    r4_w = 1.5
    node_box(ax, r4_x + r4_w/2, REND_Y, r4_w, 0.62, "9  Render",
             "draw call / batch", GREEN, "white", fs=9.0, sub_fs=7.2)
    r5_x = r4_x + r4_w + 0.15
    r5_w = 2.4
    node_box(ax, r5_x + r5_w/2, REND_Y, r5_w, 0.62, "10  GPU pipeline",
             "vertex / raster / depth / shade", TEAL, "white", fs=9.0, sub_fs=7.2)

    for a, b in [(r1_x + r1_w, r2_x),
                 (r2_x + r2_w, r3_x),
                 (r3_x + r3_w, r4_x),
                 (r4_x + r4_w, r5_x)]:
        arrow(ax, a, REND_Y, b, REND_Y, color=INK, lw=1.4)

    # return channel: render -> main (give Render World back)
    ret_x = r5_x + r5_w/2
    node_box(ax, ret_x, chan_y, 2.0, 0.50, "return channel",
             "Render World back", PURP, "white", fs=8.6, sub_fs=7.0)
    arrow(ax, ret_x, REND_Y + 0.32, ret_x, chan_y - 0.26, color=PURP, lw=1.4, ls="--")
    arrow(ax, ret_x, chan_y + 0.26, nx_x + 1.0, MAIN_Y - 0.32, color=PURP, lw=1.4, ls="--")

    # vertical "parallel" indicator: frame N render || frame N+1 sim
    ax.annotate("", xy=(nx_x + 1.0, REND_Y + 0.5), xytext=(nx_x + 1.0, MAIN_Y - 0.5),
                arrowprops=dict(arrowstyle="<->", color=AMBER, lw=1.6, linestyle=":"))
    ax.text(nx_x + 1.6, (MAIN_Y + REND_Y)/2, "parallel\nframe N render\n|| frame N+1 sim",
            fontsize=8.4, ha="left", va="center", color=AMBER, weight="bold")

    # Bottom annotations: which ECS face each segment uses
    ax.text(7.25, 0.95, "ECS layout serves each step:",
            fontsize=10.0, ha="center", va="center", weight="bold", color=INK)
    ax.text(7.25, 0.55,
            "physics 2  &  update 3  ->  archetype tables contiguous, SIMD/parallel   |   "
            "queue 6  ->  bitmask O(1) match archetype   |   batch 8  ->  same Material merged into one instanced draw call",
            fontsize=8.4, ha="center", va="center", color=DGREY)
    ax.text(7.25, 0.20,
            "GPU pipeline (step 10) is the entire Graphics Pipeline book  ->  cross-link",
            fontsize=8.2, ha="center", va="center", color=TEAL, style="italic")

    plt.savefig(os.path.join(OUT, "fig-p6_20_02-one-frame-timeline.png"))
    plt.close(fig)
    print("wrote fig-p6_20_02-one-frame-timeline.png")


# =====================================================================
# Figure 3: Universality (game / browser / sim / dashboard all same model)
# =====================================================================
def fig3_universality():
    fig, ax = plt.subplots(figsize=(14.0, 8.4))
    clean_ax(ax)
    ax.set_xlim(0, 14.0)
    ax.set_ylim(0, 8.4)

    ax.text(7.0, 8.00, "Big Loop + Data-Oriented = Universal Solution",
            fontsize=15.5, ha="center", va="center", weight="bold", color=INK)
    ax.text(7.0, 7.62, "Game engine is just the most complete instance of this pattern",
            fontsize=10.2, ha="center", va="center", color=DGREY, style="italic")

    # Central node: the pattern
    node_box(ax, 7.0, 6.45, 5.2, 0.95, "while(true){ update data; output; }",
             "data laid out by access pattern", INK, "white", fs=11.5, sub_fs=9.0)

    # Four domains, each with: domain name + what is "world" + loop stages
    domains = [
        # (x_center, color, light, name, world, stages)
        (2.3, BLUE, LBLUE, "Game engine",
         "world = entities + components",
         ["tick -> input", "update (ECS / physics)", "render submit -> GPU"]),
        (5.9, GREEN, LGREEN, "Browser render",
         "world = DOM tree",
         ["event loop -> JS", "layout (batch)", "paint -> composite"]),
        (9.5, AMBER, LAMBER, "Physics sim",
         "world = particles / bodies",
         ["integrate positions", "solve constraints", "GPU / SIMD advance"]),
        (12.7, PURP, LPURP, "Realtime dashboard",
         "world = metrics time series",
         ["ingest events", "aggregate (columnar)", "render charts"]),
    ]

    box_y_top = 4.95
    box_h = 2.7
    for cx, col, light, name, world, stages in domains:
        # domain container
        ax.add_patch(FancyBboxPatch((cx - 1.55, box_y_top - box_h), 3.1, box_h,
                                    boxstyle="round,pad=0.02,rounding_size=0.10",
                                    facecolor=light, edgecolor=col, linewidth=1.6, alpha=0.55))
        ax.text(cx, box_y_top - 0.30, name, fontsize=11.2, ha="center", va="center",
                weight="bold", color=col)
        ax.text(cx, box_y_top - 0.62, world, fontsize=8.2, ha="center", va="center",
                color=col, style="italic")
        # stages
        for i, st in enumerate(stages):
            sy = box_y_top - 1.05 - i * 0.50
            node_box(ax, cx, sy, 2.7, 0.40, st, "", col, "white", fs=8.6, sub_fs=7.0)
            if i < len(stages) - 1:
                arrow(ax, cx, sy - 0.22, cx, sy - 0.50 + 0.22, color=col, lw=1.2)
        # arrow from central pattern to this domain
        arrow(ax, 7.0, 6.45 - 0.48, cx, box_y_top - 0.05, color=DGREY, lw=1.2, style="->", ls="--")

    # Bottom: shared insights
    ax.add_patch(FancyBboxPatch((0.6, 0.30), 12.8, 1.45,
                                boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=SOFT, edgecolor=DGREY, linewidth=1.0))
    ax.text(7.0, 1.52, "Three shared insights across all four domains",
            fontsize=10.4, ha="center", va="center", weight="bold", color=INK)
    ax.text(7.0, 1.10,
            "1.  data contiguous (cache hit + prefetch)        "
            "2.  same op over many items (SIMD / batch)        "
            "3.  items independent (multi-core / GPU parallel)",
            fontsize=9.2, ha="center", va="center", color=INK)
    ax.text(7.0, 0.62,
            "React Fiber = job DAG    |    columnar TSDB = SoA    |    GPU compute = data parallelism",
            fontsize=8.6, ha="center", va="center", color=DGREY, style="italic")
    ax.text(7.0, 0.40,
            "Allocator book root:  data layout determines performance  ->  ECS is its ultimate game-engine instance",
            fontsize=8.4, ha="center", va="center", color=TEAL, style="italic")

    plt.savefig(os.path.join(OUT, "fig-p6_20_03-universality.png"))
    plt.close(fig)
    print("wrote fig-p6_20_03-universality.png")


if __name__ == "__main__":
    fig1_knowledge_map()
    fig2_one_frame_timeline()
    fig3_universality()
    print("done.")
