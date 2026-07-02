# -*- coding: utf-8 -*-
"""Generate PNG figures for chapter P5-18 (Render submission: how the engine drives the pipeline).
English labels, Chinese text in the chapter body. Run: python gen_p5_18_figures.py

Figures:
  fig-p5_18_01-drawcall-vs-batch.png      1000 objects: naive (1000 draw calls) vs batched (a few draw calls),
                                           annotate state-switch (pipeline/texture) cost.
  fig-p5_18_02-render-pipeline.png        The Extract -> Prepare -> Queue -> PhaseSort -> Prepare -> Render flow,
                                           two-world (main world / render world) + pipelined (frame N render || frame N+1 sim).
  fig-p5_18_03-material-sort.png          Unsorted draw call list (many state switches) vs material-sorted list (few switches).
  fig-p5_18_04-binned-vs-sorted.png       BinnedRenderPhase (opaque, no sort, bin by material+mesh) vs
                                           SortedRenderPhase (transparent, back-to-front), and the batch unit.
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch
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
        ax.text(cx, cy - h * 0.24, sub, fontsize=sub_fs, ha="center", va="center",
                color=txt_color)


def dep_arrow(ax, a, b, color=DGREY, lw=1.6, rad=0.0, style="-|>"):
    ax.add_artist(FancyArrowPatch(a, b, arrowstyle=style, color=color, lw=lw,
                                  connectionstyle=f"arc3,rad={rad}", alpha=0.92))


# ============================================================
# Fig 1: draw call vs batching
# ============================================================
def fig1_drawcall_vs_batch():
    fig, ax = plt.subplots(figsize=(14.2, 8.6))
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 11.5)
    ax.set_title("1000 objects:  naive (1000 draw calls, ~1000 state switches)  vs.  batched by material (a few draw calls)",
                 fontsize=13.0, weight="bold", color=INK, loc="left", pad=10)

    # ---- naive side (top) ----
    ax.text(0.2, 10.7, "NAIVE  (one draw call per object)", fontsize=12, weight="bold", color=RED)
    ax.text(0.2, 10.25, "every object: CPU->GPU round trip + pipeline/texture state switch",
            fontsize=9.5, color=DGREY, style="italic")

    # 18 mini-cubes (representing 1000), each its own draw call with a switch marker
    rng = np.random.default_rng(7)
    palette = [BLUE, GREEN, AMBER, PURP, TEAL, RED]
    mat_ids = rng.integers(0, 6, 18)
    for i in range(18):
        cx = 0.6 + i * 1.05
        ax.add_patch(Rectangle((cx, 8.6), 0.8, 0.9, facecolor=palette[mat_ids[i]], alpha=0.85,
                               edgecolor=INK, linewidth=0.8))
        # arrow down = a draw call
        ax.annotate("", xy=(cx + 0.4, 8.0), xytext=(cx + 0.4, 8.55),
                    arrowprops=dict(arrowstyle="-|>", color=DGREY, lw=1.0))
        # state switch mark (lightning) between consecutive different materials
        if i > 0 and mat_ids[i] != mat_ids[i-1]:
            ax.text(cx + 0.02, 9.7, "*", fontsize=16, ha="center", color=RED, weight="bold")
    ax.text(1.4, 9.7, "... = state switch", fontsize=8.5, color=RED, style="italic")
    ax.text(9.4, 7.55, "...  (1000 cubes, each a separate draw call)",
            fontsize=9.5, color=DGREY, style="italic")

    # GPU block
    ax.add_patch(FancyBboxPatch((0.6, 6.4), 18.6, 1.1,
                                boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=SOFT, edgecolor=INK, linewidth=1.2))
    ax.text(0.9, 6.95, "GPU", fontsize=10.5, weight="bold", color=INK)
    ax.text(2.3, 6.95, "spends most time re-binding shaders/textures  ->  GPU starved",
            fontsize=9.5, color=RED, style="italic")
    ax.text(0.6, 5.95, "CPU cost:  ~1000 x (driver call + state validation)  =  CPU-bound",
            fontsize=10, color=RED, weight="bold")

    # divider
    ax.plot([0.2, 28.0], [5.2, 5.2], linestyle="--", color=GREY, lw=1.2)

    # ---- batched side (bottom) ----
    ax.text(0.2, 4.7, "BATCHED  (group by material, merge into few draw calls)", fontsize=12,
            weight="bold", color=GREEN)
    ax.text(0.2, 4.3, "same material = same shader + same textures = one bind, many instances",
            fontsize=9.5, color=DGREY, style="italic")

    # 6 material bins
    counts = [4, 3, 3, 4, 2, 2]   # represent the 18 cubes grouped
    x = 0.6
    bin_starts = []
    for i, c in enumerate(counts):
        bin_starts.append(x)
        for j in range(c):
            ax.add_patch(Rectangle((x + j * 0.95, 2.7), 0.8, 0.9, facecolor=palette[i], alpha=0.85,
                                   edgecolor=INK, linewidth=0.8))
        # one bracket / arrow down = ONE batched draw call for the whole bin
        bx = x + (c * 0.95) / 2 - 0.4
        ax.annotate("", xy=(bx + 0.4, 2.2), xytext=(bx + 0.4, 2.65),
                    arrowprops=dict(arrowstyle="-|>", color=GREEN, lw=1.6))
        ax.text(x + c * 0.95 / 2 - 0.4, 1.95, f"1 draw call\n({c} instances)",
                fontsize=7.8, ha="center", color=GREEN, weight="bold")
        x += c * 0.95 + 0.7

    ax.text(0.6, 1.25, "GPU", fontsize=10.5, weight="bold", color=INK)
    ax.text(1.9, 1.25, "few state switches  ->  GPU busy drawing, not rebinding",
            fontsize=9.5, color=GREEN, style="italic")
    ax.text(0.6, 0.75, "CPU cost:  6 draw calls (one per material)  =  CPU free to do other work",
            fontsize=10, color=GREEN, weight="bold")

    ax.text(15.5, 0.75, "Note: objects still must share the same mesh\nto merge into one call (instancing); else per-mesh batch.",
            fontsize=8.2, color=DGREY, style="italic", va="center",
            bbox=dict(boxstyle="round,pad=0.3", facecolor=LAMBER, edgecolor=AMBER, lw=0.8))

    fig.savefig(os.path.join(OUT, "fig-p5_18_01-drawcall-vs-batch.png"))
    plt.close(fig)


# ============================================================
# Fig 2: the render submission pipeline (two worlds + pipelined)
# ============================================================
def fig2_render_pipeline():
    fig, ax = plt.subplots(figsize=(15.0, 8.6))
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 10.5)
    ax.set_title("Render submission pipeline:  Main World --Extract--> Render World --(Prepare/Queue/Sort/Batch/Render)--> GPU",
                 fontsize=12.5, weight="bold", color=INK, loc="left", pad=10)

    # Two world containers
    ax.add_patch(FancyBboxPatch((0.3, 0.5), 7.2, 9.3,
                                boxstyle="round,pad=0.04,rounding_size=0.15",
                                facecolor=LBLUE, edgecolor=BLUE, linewidth=1.6, alpha=0.35))
    ax.text(3.9, 9.4, "MAIN WORLD\n(simulation thread)", fontsize=11, weight="bold",
            color=BLUE, ha="center")
    ax.text(3.9, 8.85, "the ECS world you mutate in Update", fontsize=8.6,
            color=DGREY, ha="center", style="italic")

    ax.add_patch(FancyBboxPatch((8.4, 0.5), 18.3, 9.3,
                                boxstyle="round,pad=0.04,rounding_size=0.15",
                                facecolor=LPURP, edgecolor=PURP, linewidth=1.6, alpha=0.35))
    ax.text(17.55, 9.4, "RENDER WORLD  (render thread / SubApp)", fontsize=11, weight="bold",
            color=PURP, ha="center")
    ax.text(17.55, 8.85, "a separate ECS world; only render-relevant data", fontsize=8.6,
            color=DGREY, ha="center", style="italic")

    # Main world contents
    node_box(ax, 3.9, 7.4, 5.4, 1.2, "ECS entities", "Mesh / Material / Transform / Visibility",
             BLUE, fs=10)
    node_box(ax, 3.9, 5.6, 5.4, 1.2, "Camera + Frustum", "view proj, view pos", BLUE, fs=10)

    # Main schedule box (Update etc.)
    ax.add_patch(FancyBboxPatch((1.0, 2.0), 5.8, 2.0,
                                boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor="white", edgecolor=BLUE, linewidth=1.2))
    ax.text(3.9, 3.55, "Main Schedule (Update...)", fontsize=9.5, weight="bold", color=BLUE, ha="center")
    ax.text(3.9, 3.05, "mutates the main world", fontsize=8.4, color=DGREY, ha="center", style="italic")
    ax.text(3.9, 2.5, "frame N simulation", fontsize=8.4, color=DGREY, ha="center", style="italic")

    # Extract step (the bridge)
    node_box(ax, 3.9, 0.95, 5.0, 0.85, "EXTRACT", "copy data OUT (the only bridge)", AMBER,
             txt_color=INK, fs=10.5)

    # Render world stages (the schedule chain)
    stages = [
        ("ExtractCommands", "apply spawn/despawn", 9.7, 7.4, TEAL),
        ("PrepareMeshes /\nPrepareAssets", "upload GPU buffers / textures", 13.6, 7.4, GREEN),
        ("Queue", "for each visible entity ->\ncreate PhaseItem, pick Draw fn", 17.7, 7.4, BLUE),
        ("PhaseSort", "sort Opaque by bin key\nsort Transparent back-to-front", 21.9, 7.4, PURP),
        ("Prepare /\nBatch", "merge same-material+mesh\ninto one batch; build bind groups", 25.9, 7.4, AMBER),
    ]
    for (lab, sub, cx, cy, col) in stages:
        node_box(ax, cx, cy, 3.5, 1.4, lab, sub, col, fs=9.0, sub_fs=7.6)

    # chain arrows between stages
    for i in range(len(stages) - 1):
        a = (stages[i][2] + 1.78, 7.4)
        b = (stages[i+1][2] - 1.78, 7.4)
        dep_arrow(ax, a, b, color=INK, lw=1.4)

    # Extract -> ExtractCommands arrow (the bridge across worlds)
    dep_arrow(ax, (3.9 + 2.5, 0.95), (9.7 - 1.85, 0.95), color=AMBER, lw=2.0)
    ax.text(6.9, 1.25, "bounded\nasync channel", fontsize=7.8, color=AMBER, ha="center",
            weight="bold", style="italic")
    # lift up into ExtractCommands
    dep_arrow(ax, (9.7, 1.4), (9.7, 6.7), color=DGREY, lw=1.2, rad=-0.1)

    # Render -> GPU
    node_box(ax, 25.9, 5.0, 3.6, 1.3, "RENDER", "TrackedRenderPass:\nissue draw calls", RED, fs=9.5)
    dep_arrow(ax, (25.9, 6.7), (25.9, 5.65), color=INK, lw=1.4)
    node_box(ax, 25.9, 3.2, 3.6, 1.3, "GPU", "execute pipeline\n(vertex/raster/depth/shade)", GREEN, fs=9.5)
    dep_arrow(ax, (25.9, 4.35), (25.9, 3.85), color=RED, lw=1.8)
    ax.text(25.9, 2.35, "(pipeline internals -> see\n Graphics Rendering Pipeline)",
            fontsize=8.0, color=DGREY, ha="center", style="italic")

    # ---- pipelined timeline (bottom) ----
    ax.text(0.3, 0.0, "Pipelined:  Frame N render runs IN PARALLEL with Frame N+1 simulation (bounded channel of size 1)",
            fontsize=9.8, weight="bold", color=PURP)

    fig.savefig(os.path.join(OUT, "fig-p5_18_02-render-pipeline.png"))
    plt.close(fig)


# ============================================================
# Fig 3: material sort -> fewer state switches
# ============================================================
def fig3_material_sort():
    fig, ax = plt.subplots(figsize=(14.5, 8.0))
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 11)
    ax.set_title("Sorting draw calls by material minimizes expensive state switches (shader / texture rebinds)",
                 fontsize=12.5, weight="bold", color=INK, loc="left", pad=10)

    palette = {"MatA": BLUE, "MatB": GREEN, "MatC": AMBER, "MatD": PURP, "MatE": TEAL, "MatF": RED}
    # ---- unsorted (top) ----
    ax.text(0.2, 10.3, "UNSORTED  (submission order = arbitrary)", fontsize=11.5, weight="bold", color=RED)
    order_u = ["MatA","MatB","MatA","MatC","MatB","MatD","MatA","MatC","MatE","MatB","MatA","MatD","MatC","MatA","MatB","MatE"]
    switches_u = sum(1 for i in range(1, len(order_u)) if order_u[i] != order_u[i-1]) + 1
    x = 0.6
    for i, m in enumerate(order_u):
        ax.add_patch(Rectangle((x, 8.6), 1.5, 1.0, facecolor=palette[m], alpha=0.88,
                               edgecolor=INK, linewidth=0.7))
        ax.text(x + 0.75, 9.1, m, fontsize=8.0, ha="center", color="white", weight="bold")
        if i == 0 or order_u[i] != order_u[i-1]:
            ax.annotate("", xy=(x + 0.75, 8.3), xytext=(x + 0.75, 8.55),
                        arrowprops=dict(arrowstyle="-|>", color=RED, lw=1.2))
            ax.text(x + 0.75, 7.95, "switch", fontsize=7.0, ha="center", color=RED, weight="bold")
        x += 1.62
    ax.text(0.6, 7.4, f"{len(order_u)} draw calls,  {switches_u} state switches (pipeline/texture rebinds)",
            fontsize=10, color=RED, weight="bold")
    ax.text(0.6, 6.95, "GPU keeps rebinding the same shaders back and forth  ->  wasted bandwidth",
            fontsize=9.0, color=DGREY, style="italic")

    ax.plot([0.2, 28.5], [6.4, 6.4], linestyle="--", color=GREY, lw=1.2)

    # ---- sorted (bottom) ----
    ax.text(0.2, 5.85, "SORTED  (group same material together)", fontsize=11.5, weight="bold", color=GREEN)
    order_s = sorted(order_u, key=lambda m: m)   # stable group
    switches_s = len(set(order_s))
    x = 0.6
    for i, m in enumerate(order_s):
        ax.add_patch(Rectangle((x, 4.1), 1.5, 1.0, facecolor=palette[m], alpha=0.88,
                               edgecolor=INK, linewidth=0.7))
        ax.text(x + 0.75, 4.6, m, fontsize=8.0, ha="center", color="white", weight="bold")
        if i == 0 or order_s[i] != order_s[i-1]:
            ax.annotate("", xy=(x + 0.75, 3.8), xytext=(x + 0.75, 4.05),
                        arrowprops=dict(arrowstyle="-|>", color=GREEN, lw=1.4))
            ax.text(x + 0.75, 3.45, "switch", fontsize=7.0, ha="center", color=GREEN, weight="bold")
        x += 1.62
    ax.text(0.6, 2.9, f"{len(order_s)} draw calls,  only {switches_s} state switches (one per material)",
            fontsize=10, color=GREEN, weight="bold")
    ax.text(0.6, 2.45, "each shader/texture bound once, then used for many consecutive draws",
            fontsize=9.0, color=DGREY, style="italic")

    # ---- note ----
    ax.add_patch(FancyBboxPatch((0.6, 0.6), 27.6, 1.55,
                                boxstyle="round,pad=0.03,rounding_size=0.08",
                                facecolor=LAMBER, edgecolor=AMBER, linewidth=1.0))
    ax.text(1.0, 1.75, "Two sorting policies in Bevy:", fontsize=9.5, weight="bold", color=INK)
    ax.text(1.0, 1.25, "Opaque (BinnedRenderPhase): bin by (pipeline, mesh, material) -> consecutive bins share state; no global sort needed.",
            fontsize=9.0, color=INK)
    ax.text(1.0, 0.82, "Transparent (SortedRenderPhase): MUST sort back-to-front for correctness; binning would break alpha blending.",
            fontsize=9.0, color=INK)

    fig.savefig(os.path.join(OUT, "fig-p5_18_03-material-sort.png"))
    plt.close(fig)


# ============================================================
# Fig 4: Binned vs Sorted render phase, and the batch unit
# ============================================================
def fig4_binned_vs_sorted():
    fig, ax = plt.subplots(figsize=(14.5, 8.4))
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 11.5)
    ax.set_title("Binned render phase (opaque, fast)  vs.  Sorted render phase (transparent, correct)",
                 fontsize=12.5, weight="bold", color=INK, loc="left", pad=10)

    # ===== Binned (opaque) =====
    ax.add_patch(FancyBboxPatch((0.3, 6.0), 28.6, 5.0,
                                boxstyle="round,pad=0.03,rounding_size=0.10",
                                facecolor=LGREEN, edgecolor=GREEN, linewidth=1.2, alpha=0.35))
    ax.text(0.6, 10.55, "BinnedRenderPhase  (e.g. Opaque3d)  --  ordering not critical, fastest",
            fontsize=11, weight="bold", color=GREEN)
    ax.text(0.6, 10.1, "BinKey = (pipeline id, draw fn, mesh asset id, material bind group id)",
            fontsize=8.6, color=DGREY, style="italic")

    # three bins: MatA+Cube, MatA+Sphere, MatB+Cube
    bins = [
        ("Bin 1\nMatA + Cube",  [f"e{i}" for i in range(6)], BLUE),
        ("Bin 2\nMatA + Sphere",[f"e{i}" for i in range(6, 9)], TEAL),
        ("Bin 3\nMatB + Cube",  [f"e{i}" for i in range(9, 13)], PURP),
    ]
    bx = 0.8
    for (lab, ents, col) in bins:
        ax.add_patch(FancyBboxPatch((bx, 6.7), 4.4, 2.7,
                                    boxstyle="round,pad=0.02,rounding_size=0.08",
                                    facecolor="white", edgecolor=col, linewidth=1.4))
        ax.text(bx + 2.2, 9.05, lab, fontsize=8.6, ha="center", color=col, weight="bold")
        # entity dots
        for i, e in enumerate(ents):
            ex = bx + 0.35 + (i % 4) * 0.95
            ey = 8.3 - (i // 4) * 0.7
            ax.add_patch(plt.Circle((ex, ey), 0.22, facecolor=col, alpha=0.85, edgecolor=INK, lw=0.6))
        ax.text(bx + 2.2, 6.95, f"{len(ents)} instances", fontsize=7.6, ha="center", color=DGREY, style="italic")
        # one batch arrow
        ax.annotate("", xy=(bx + 2.2, 6.5), xytext=(bx + 2.2, 6.7),
                    arrowprops=dict(arrowstyle="-|>", color=GREEN, lw=1.6))
        ax.text(bx + 2.2, 6.25, "1 draw call\n(instanced)", fontsize=7.4, ha="center", color=GREEN, weight="bold")
        bx += 4.9

    ax.text(0.6, 6.4, "Same BinKey -> merge into one instanced draw call.  No global sort: bins iterated in BinKey order.",
            fontsize=9.2, color=INK)
    ax.text(0.6, 6.05, "(order within a bin doesn't matter; opaque pixels fail depth test -> early-z handles overlap)",
            fontsize=8.2, color=DGREY, style="italic")

    # ===== Sorted (transparent) =====
    ax.add_patch(FancyBboxPatch((0.3, 0.4), 28.6, 5.1,
                                boxstyle="round,pad=0.03,rounding_size=0.10",
                                facecolor=LRED, edgecolor=RED, linewidth=1.2, alpha=0.35))
    ax.text(0.6, 5.05, "SortedRenderPhase  (e.g. Transparent3d)  --  MUST sort back-to-front",
            fontsize=11, weight="bold", color=RED)
    ax.text(0.6, 4.6, "SortKey = view-space distance (far first).  Needed for correct alpha blending.",
            fontsize=8.6, color=DGREY, style="italic")

    # a camera and a depth line
    ax.add_patch(plt.Polygon([[1.2, 3.4], [1.6, 3.7], [1.6, 3.1]], closed=True, facecolor=INK))
    ax.text(1.4, 2.6, "camera", fontsize=7.8, ha="center", color=INK)

    # transparent quads at varying depth (squares), unsorted then sorted
    ax.text(2.6, 4.05, "before sort:", fontsize=8.6, color=RED, weight="bold")
    depths_u = [3, 8, 5, 1, 6]   # view-space Z, arbitrary order
    cols = [BLUE, TEAL, PURP, AMBER, GREEN]
    for i, (d, c) in enumerate(zip(depths_u, cols)):
        xx = 4.0 + i * 4.5
        yy = 2.0 + (10 - d) * 0.15
        ax.add_patch(Rectangle((xx, yy), 1.2, 1.2, facecolor=c, alpha=0.45, edgecolor=c, lw=1.0))
        ax.text(xx + 0.6, yy - 0.35, f"z={d}", fontsize=7.2, ha="center", color=INK)
    ax.annotate("", xy=(27.0, 1.8), xytext=(4.0, 1.8),
                arrowprops=dict(arrowstyle="->", color=DGREY, lw=1.0))
    ax.text(15.5, 1.45, "wrong blend order -> visual artifacts", fontsize=8.0, ha="center", color=RED, style="italic")

    ax.text(2.6, 1.1, "after sort (sort_unstable_by_key):", fontsize=8.6, color=GREEN, weight="bold")
    depths_s = sorted(depths_u, reverse=True)
    for i, (d, c) in enumerate(zip(depths_s, [c for _, c in sorted(zip(depths_u, cols), key=lambda t: -t[0])])):
        xx = 4.0 + i * 4.5
        yy = 0.55 + (10 - d) * 0.0
        ax.add_patch(Rectangle((xx, yy + 0.05), 1.0, 1.0, facecolor=c, alpha=0.45, edgecolor=c, lw=1.0))
        ax.text(xx + 0.5, yy - 0.2, f"z={d}", fontsize=7.0, ha="center", color=INK)
    ax.annotate("", xy=(27.0, 0.55), xytext=(4.0, 0.55),
                arrowprops=dict(arrowstyle="->", color=DGREY, lw=1.0))
    ax.text(15.5, 0.2, "far -> near: each pixel blended over the one behind it",
            fontsize=8.0, ha="center", color=GREEN, style="italic")

    fig.savefig(os.path.join(OUT, "fig-p5_18_04-binned-vs-sorted.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig1_drawcall_vs_batch()
    fig2_render_pipeline()
    fig3_material_sort()
    fig4_binned_vs_sorted()
    print("Generated 4 figures in:", OUT)
