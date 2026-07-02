# -*- coding: utf-8 -*-
"""为《游戏引擎》P2-07 (System 的遍历: 缓存友好与并行) 生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_p2_07_figures.py

Figures:
  fig-p2_07_01-traversal-pointer-chasing.png   连续遍历 vs 指针追逐 (聚焦遍历步进, 缓存行命中)
  fig-p2_07_02-simd-batch.png                  SIMD 批处理: 一条指令同时处理 8 个实体的 pos+=vel
  fig-p2_07_03-data-parallel.png               数据并行: 实体数组切成 N 块给 N 核 + 加速比曲线
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
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


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


# ============================================================
# Fig 1: 连续遍历 vs 指针追逐  (聚焦遍历步进)
# ============================================================
def fig1_traversal():
    fig, axes = plt.subplots(2, 1, figsize=(13.8, 8.6))

    # ---- top: SoA contiguous traversal ----
    ax = axes[0]
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 7.0)
    ax.set_title("SoA contiguous traversal:  stride = 1,  prefetcher ahead,  cache lines full",
                 fontsize=13.0, weight="bold", color=INK, loc="left", pad=10)

    # one row: 16 contiguous cells representing x[0..15] of Position array
    n_cells = 16
    cell_w = 1.2
    cell_h = 1.0
    y0 = 3.6
    x0 = 1.2
    for i in range(n_cells):
        cx = x0 + i * cell_w
        ax.add_patch(Rectangle((cx, y0), cell_w - 0.08, cell_h, facecolor=BLUE,
                               edgecolor=INK, linewidth=1.0))
        ax.text(cx + (cell_w - 0.08) / 2, y0 + cell_h / 2, f"x{i}",
                ha="center", va="center", fontsize=9.2, color="white", weight="bold")

    # cache line brackets: 64B = 16 floats, every 16 cells = one cache line
    for k in range(1):
        cl_x0 = x0
        cl_w = 16 * cell_w - 0.08
        cl_y = y0 - 0.55
        ax.add_patch(Rectangle((cl_x0, cl_y), cl_w, 0.16, facecolor=GREEN, edgecolor=GREEN))
        ax.annotate("", xy=(cl_x0, cl_y - 0.05), xytext=(cl_x0 + cl_w, cl_y - 0.05),
                    arrowprops=dict(arrowstyle="<->", color=GREEN, lw=1.8))
        ax.text(cl_x0 + cl_w / 2, cl_y - 0.45,
                "ONE 64B cache line  =  16 positions  (loaded once, all hit)",
                ha="center", va="center", fontsize=10.5, color=GREEN, weight="bold")

    # traversal arrows: sequential rightward
    ax.annotate("", xy=(x0 + 14 * cell_w + cell_w, y0 + cell_h + 0.55),
                xytext=(x0, y0 + cell_h + 0.55),
                arrowprops=dict(arrowstyle="-|>", color=RED, lw=2.4,
                                connectionstyle="arc3,rad=-0.05"))
    ax.text(x0 + 7.5 * cell_w, y0 + cell_h + 1.1,
            "PC walks forward in lockstep:  next element already in L1",
            ha="center", va="center", fontsize=11.0, color=RED, weight="bold")

    # time markers below
    ty = y0 - 1.7
    for i, label in enumerate(["t0: load x[0..15] (1 miss)", "t1..t15: free (L1 hit)",
                                "t16: prefetch x[16..31]", "t17..: still hit"]):
        ax.text(x0, ty - i * 0.62, label, fontsize=10.3, color=INK, ha="left", va="center")

    ax.text(x0 + 14.5 * cell_w, ty - 1.5,
            "misses / 16 elements  =  1\namortized ~1 miss / element",
            ha="left", va="center", fontsize=10.5, color=GREEN, weight="bold",
            bbox=dict(boxstyle="round,pad=0.35", facecolor=LGREEN, edgecolor=GREEN))

    # ---- bottom: pointer chasing ----
    ax = axes[1]
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 7.0)
    ax.set_title("Pointer chasing  (AoS scattered on heap):  stride unpredictable,  prefetcher dead,  ~1 miss / element",
                 fontsize=13.0, weight="bold", color=INK, loc="left", pad=10)

    # 8 boxes scattered at predetermined positions, each = one Ball object
    # (deterministic so the loop terminates; mimics heap scatter)
    pts = [
        (1.5, 4.6), (5.2, 3.0), (9.0, 4.2), (13.0, 3.2),
        (17.0, 4.5), (21.0, 3.0), (25.0, 4.3), (4.0, 5.0),
    ]
    n_obj = len(pts)
    # traversal order: shuffled to mimic pointer-chase
    order = [2, 6, 0, 5, 3, 7, 1, 4]

    for idx, (px, py) in enumerate(pts):
        ax.add_patch(FancyBboxPatch((px, py), 2.6, 1.4,
                                    boxstyle="round,pad=0.02,rounding_size=0.10",
                                    facecolor=AMBER, edgecolor=INK, linewidth=1.1, alpha=0.95))
        ax.text(px + 1.3, py + 1.05, f"Ball_{idx}", fontsize=9.2, ha="center", va="center",
                color="white", weight="bold")
        # show fields x|y|vx|vy|... inside
        sub = ["x", "y", "vx", "vy", "r"]
        for fi, fl in enumerate(sub):
            fx = px + 0.15 + fi * 0.48
            ax.add_patch(Rectangle((fx, py + 0.15), 0.42, 0.42,
                                   facecolor=BLUE if fi < 4 else GREY,
                                   edgecolor="white", linewidth=0.6))
            ax.text(fx + 0.21, py + 0.36, fl, fontsize=7, ha="center", va="center",
                    color="white", weight="bold")

    # arrows: traversal in shuffled order
    for k in range(len(order) - 1):
        a = pts[order[k]]
        b = pts[order[k + 1]]
        ax.add_artist(FancyArrowPatch((a[0] + 1.3, a[1] + 0.7), (b[0] + 1.3, b[1] + 0.7),
                                      arrowstyle="-|>", color=RED, lw=2.0,
                                      connectionstyle="arc3,rad=0.18", alpha=0.9))
        ax.text((a[0] + b[0]) / 2 + 0.6, (a[1] + b[1]) / 2 + 0.9, f"miss",
                fontsize=8.5, color=RED, weight="bold", ha="center", va="center")

    # cache-line bursts (each object's read = at least 1 miss; some objects span 2 lines)
    ax.text(0.6, 6.3, "Traversal jumps across heap:  every hop = cache miss",
            fontsize=11.5, color=RED, weight="bold", ha="left", va="center")

    ty = 0.5
    for i, label in enumerate(["each Ball straddles / is alone in its cache line",
                                "prefetcher cannot predict random jumps",
                                "amortized ~1 miss / element  (16x worse than SoA)"]):
        ax.text(0.8, ty - i * 0.0 + 0.0, label, fontsize=10.0, color=INK, ha="left", va="bottom")

    plt.subplots_adjust(hspace=0.55)
    fig.savefig(os.path.join(OUT, "fig-p2_07_01-traversal-pointer-chasing.png"))
    plt.close(fig)
    print("  saved fig-p2_07_01-traversal-pointer-chasing.png")


# ============================================================
# Fig 2: SIMD 批处理 - 一条指令处理 8 个实体的 pos+=vel
# ============================================================
def fig2_simd():
    fig, ax = plt.subplots(figsize=(13.8, 8.2))
    clean_ax(ax)
    ax.set_xlim(0, 28); ax.set_ylim(0, 15.5)
    ax.set_title("SIMD batch:  ONE instruction  pos[0..7] += vel[0..7]   (8 entities / 256-bit AVX)",
                 fontsize=13.5, weight="bold", color=INK, loc="left", pad=12)

    # --- scalar lane (top) ---
    scalar_y = 12.6
    ax.text(0.4, scalar_y + 1.4, "Scalar (1 entity at a time):", fontsize=11.5,
            color=INK, weight="bold", ha="left", va="center")
    n = 8
    cell_w = 1.25
    x0 = 3.0
    # pos[]
    for i in range(n):
        cx = x0 + i * cell_w
        ax.add_patch(Rectangle((cx, scalar_y), cell_w - 0.08, 0.9, facecolor=BLUE,
                               edgecolor=INK, linewidth=1.0))
        ax.text(cx + (cell_w - 0.08) / 2, scalar_y + 0.45, f"p{i}", ha="center", va="center",
                fontsize=9, color="white", weight="bold")
    # 8 separate add arrows
    for i in range(n):
        cx = x0 + i * cell_w + (cell_w - 0.08) / 2
        ax.annotate("", xy=(cx, scalar_y - 0.05), xytext=(cx, scalar_y - 1.0),
                    arrowprops=dict(arrowstyle="-|>", color=AMBER, lw=1.2))
    ax.text(x0 + n * cell_w / 2, scalar_y - 1.45,
            "8 separate ADD instructions   (loop trip count = 8)",
            fontsize=10.5, color=AMBER, weight="bold", ha="center", va="center")

    # --- SIMD lane (bottom) ---
    simd_y = 7.4
    ax.text(0.4, simd_y + 4.6, "SIMD (8 entities at once):", fontsize=11.5,
            color=INK, weight="bold", ha="left", va="center")

    # pos[0..7] row
    ax.text(0.4, simd_y + 3.7, "pos[]", fontsize=11, color=BLUE, weight="bold",
            ha="left", va="center")
    for i in range(n):
        cx = x0 + i * cell_w
        ax.add_patch(Rectangle((cx, simd_y + 3.1), cell_w - 0.08, 0.9, facecolor=BLUE,
                               edgecolor=INK, linewidth=1.0))
        ax.text(cx + (cell_w - 0.08) / 2, simd_y + 3.55, f"p{i}", ha="center", va="center",
                fontsize=9, color="white", weight="bold")
    # one big bracket showing 8 lanes loaded together
    bx0 = x0; bw = n * cell_w - 0.08
    ax.annotate("", xy=(bx0, simd_y + 2.85), xytext=(bx0 + bw, simd_y + 2.85),
                arrowprops=dict(arrowstyle="<->", color=GREEN, lw=1.8))
    ax.text(bx0 + bw / 2, simd_y + 2.5, "load 8 floats in one SIMD load",
            fontsize=10, color=GREEN, weight="bold", ha="center", va="center")

    # vel[0..7] row
    ax.text(0.4, simd_y + 1.9, "vel[]", fontsize=11, color=GREEN, weight="bold",
            ha="left", va="center")
    for i in range(n):
        cx = x0 + i * cell_w
        ax.add_patch(Rectangle((cx, simd_y + 1.3), cell_w - 0.08, 0.9, facecolor=GREEN,
                               edgecolor=INK, linewidth=1.0))
        ax.text(cx + (cell_w - 0.08) / 2, simd_y + 1.75, f"v{i}", ha="center", va="center",
                fontsize=9, color="white", weight="bold")
    ax.annotate("", xy=(bx0, simd_y + 1.05), xytext=(bx0 + bw, simd_y + 1.05),
                arrowprops=dict(arrowstyle="<->", color=GREEN, lw=1.8))
    ax.text(bx0 + bw / 2, simd_y + 0.7, "load 8 floats in one SIMD load",
            fontsize=10, color=GREEN, weight="bold", ha="center", va="center")

    # single SIMD add instruction
    ax.add_patch(FancyBboxPatch((x0 + 2.0, simd_y - 0.55), bw - 4.0, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=RED, edgecolor=RED, linewidth=1.2))
    ax.text(x0 + bw / 2, simd_y - 0.075,
            "ONE SIMD instruction:  _mm256_add_ps(pos, vel)",
            fontsize=11.5, color="white", weight="bold", ha="center", va="center")

    # result row
    ax.text(0.4, simd_y - 1.7, "res[]", fontsize=11, color=PURP, weight="bold",
            ha="left", va="center")
    for i in range(n):
        cx = x0 + i * cell_w
        ax.add_patch(Rectangle((cx, simd_y - 2.3), cell_w - 0.08, 0.9, facecolor=PURP,
                               edgecolor=INK, linewidth=1.0))
        ax.text(cx + (cell_w - 0.08) / 2, simd_y - 1.85, f"p{i}+v{i}", ha="center", va="center",
                fontsize=7.6, color="white", weight="bold")
    ax.annotate("", xy=(bx0, simd_y - 1.45), xytext=(bx0 + bw, simd_y - 1.45),
                arrowprops=dict(arrowstyle="<->", color=GREEN, lw=1.8))
    ax.text(bx0 + bw / 2, simd_y - 2.95, "store 8 results in one SIMD store",
            fontsize=10, color=GREEN, weight="bold", ha="center", va="center")

    # bottom annotations: why SoA feeds this
    ax.text(x0, 1.0,
            "Why this works:  SoA lays pos[] and vel[] CONTIGUOUSLY.\n"
            "A SIMD load grabs 8 (AVX2) or 16 (AVX-512) adjacent floats in one shot.\n"
            "AoS objects (pos, vel, color, radius mixed per object) cannot be loaded this way.",
            fontsize=10.8, color=INK, ha="left", va="center",
            bbox=dict(boxstyle="round,pad=0.45", facecolor=SOFT, edgecolor=DGREY))

    fig.savefig(os.path.join(OUT, "fig-p2_07_02-simd-batch.png"))
    plt.close(fig)
    print("  saved fig-p2_07_02-simd-batch.png")


# ============================================================
# Fig 3: 数据并行多核切分 + 加速比曲线
# ============================================================
def fig3_parallel():
    fig, axes = plt.subplots(1, 2, figsize=(14.5, 6.6),
                             gridspec_kw={"width_ratios": [1.35, 1.0]})

    # ---- left: chunk split to N cores ----
    ax = axes[0]
    clean_ax(ax)
    ax.set_xlim(0, 20); ax.set_ylim(0, 12)
    ax.set_title("Data parallel:  split entity array into N chunks  ->  N cores",
                 fontsize=12.8, weight="bold", color=INK, loc="left", pad=10)

    # entity array (top), 16 cells, split into 4 chunks of 4
    n = 16
    cell_w = 0.95
    x0 = 1.0
    y0 = 9.5
    chunk_colors = [BLUE, GREEN, AMBER, PURP]
    chunk_n = 4
    per = n // chunk_n
    for i in range(n):
        cx = x0 + i * cell_w
        ci = i // per
        ax.add_patch(Rectangle((cx, y0), cell_w - 0.06, 0.9, facecolor=chunk_colors[ci],
                               edgecolor=INK, linewidth=0.8))
        ax.text(cx + (cell_w - 0.06) / 2, y0 + 0.45, f"e{i}", ha="center", va="center",
                fontsize=8, color="white", weight="bold")
    ax.text(x0, y0 + 1.4, "Entity array  (one System, e.g. MovementSystem)",
            fontsize=11, color=INK, weight="bold", ha="left", va="center")

    # chunk dividers
    for k in range(1, chunk_n):
        dx = x0 + k * per * cell_w - 0.03
        ax.plot([dx, dx], [y0 - 0.15, y0 + 1.05], color=RED, lw=2.0, linestyle="--")

    # arrows down to each core
    core_y = 5.0
    core_w = 3.5
    for k in range(chunk_n):
        cx_center = x0 + (k * per + per / 2) * cell_w
        ax.annotate("", xy=(cx_center, core_y + 1.5), xytext=(cx_center, y0 - 0.05),
                    arrowprops=dict(arrowstyle="-|>", color=chunk_colors[k], lw=1.8))
        # core box
        bx = cx_center - core_w / 2
        ax.add_patch(FancyBboxPatch((bx, core_y), core_w, 1.4,
                                    boxstyle="round,pad=0.02,rounding_size=0.10",
                                    facecolor=chunk_colors[k], edgecolor=INK, linewidth=1.2,
                                    alpha=0.95))
        ax.text(bx + core_w / 2, core_y + 1.05, f"Core {k}", fontsize=10.5,
                ha="center", va="center", color="white", weight="bold")
        ax.text(bx + core_w / 2, core_y + 0.5, f"pos += vel * dt\non e[{k*per}..{k*per+per-1}]",
                fontsize=8.6, ha="center", va="center", color="white")

    # note: same operation, disjoint data
    ax.text(10, 3.6, "Each core runs the SAME System body on a DISJOINT slice.\n"
                     "No locks:  chunk i only writes pos[i*per .. i*per+per).",
            fontsize=10.3, color=INK, ha="center", va="center",
            bbox=dict(boxstyle="round,pad=0.4", facecolor=SOFT, edgecolor=DGREY))

    ax.text(10, 1.6, "RESULT:  4 chunks run concurrently  ->  ~4x throughput",
            fontsize=11.5, color=GREEN, weight="bold", ha="center", va="center")

    # ---- right: speedup curve vs core count ----
    ax = axes[1]
    cores = np.array([1, 2, 4, 8, 16, 32])
    # Amdahl-ish: parallel fraction p. The "work" (the pos+=vel loop) is fully parallel,
    # but there is per-chunk dispatch overhead and a serial setup/query phase.
    # ideal: speedup = cores
    ideal = cores.astype(float)
    # simulated real: s = 1 / ((1-p)/1 + p/cores) + overhead grows slightly
    p = 0.95
    s_real = 1.0 / ((1 - p) + p / cores)
    # Bevy par_iter batching has small per-batch overhead, so deviates more at high core count
    s_bevy = s_real * (1.0 - 0.01 * cores / 8.0)

    ax.plot(cores, ideal, "--", color=GREY, lw=1.8, label="ideal (linear)")
    ax.plot(cores, s_real, "-o", color=GREEN, lw=2.4, ms=8, label="data-parallel (simulated)")
    ax.plot(cores, s_bevy, "-^", color=BLUE, lw=2.2, ms=8, label="with batch overhead")
    # OOP scattered baseline: cannot be parallelized cleanly (pointer chase, shared vtable)
    ax.axhline(1.0, color=RED, lw=1.4, linestyle=":", alpha=0.7)
    ax.text(1.2, 1.15, "OOP pointer-chase: hard to split, no clean speedup",
            fontsize=9.5, color=RED, style="italic")

    ax.set_xlabel("core count", fontsize=11.5)
    ax.set_ylabel("speedup vs single-core", fontsize=11.5)
    ax.set_title("Speedup vs core count", fontsize=12.5, weight="bold")
    ax.set_xscale("log", base=2); ax.set_xticks(cores); ax.set_xticklabels(cores)
    ax.set_yscale("log", base=2); ax.set_yticks([1, 2, 4, 8, 16, 32])
    ax.set_yticklabels([1, 2, 4, 8, 16, 32])
    ax.grid(True, which="both", ls=":", alpha=0.5)
    ax.legend(fontsize=10, loc="upper left", framealpha=0.95)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    fig.suptitle("Data parallelism:  same operation on disjoint slices,  multi-core concurrent",
                 fontsize=13, weight="bold", color=INK, y=1.02)
    plt.tight_layout()
    fig.savefig(os.path.join(OUT, "fig-p2_07_03-data-parallel.png"))
    plt.close(fig)
    print("  saved fig-p2_07_03-data-parallel.png")


if __name__ == "__main__":
    print("Generating P2-07 figures ...")
    fig1_traversal()
    fig2_simd()
    fig3_parallel()
    print("Done.")
