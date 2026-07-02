# -*- coding: utf-8 -*-
"""为《游戏引擎》P2-06 (Component 的存储: SoA vs AoS) 生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_p2_06_figures.py

Figures:
  fig-p2_06_01-aos-vs-soa-cache.png   AoS vs SoA 内存布局, 画清缓存行如何覆盖字段
  fig-p2_06_02-entt-sparse-set.png    EnTT sparse_set: sparse 分页索引 + dense 连续数组
  fig-p2_06_03-aos-vs-soa-bench.png   数值模拟: 缓存 miss 次数 + 遍历耗时随实体数增长 (双对数)
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
# Fig 1: AoS vs SoA 内存布局 + 缓存行覆盖
# ============================================================
def fig1_aos_vs_soa():
    fig, axes = plt.subplots(2, 1, figsize=(13.5, 8.4))

    # ---- AoS ----
    ax = axes[0]
    clean_ax(ax)
    ax.set_xlim(0, 32); ax.set_ylim(0, 6.6)
    ax.set_title("AoS  (Array of Structures): each object is one block, fields mixed",
                 fontsize=13.5, weight="bold", color=INK, loc="left", pad=10)

    # field legend colors: x,y,vx,vy (used by MovementSystem) = shades of blue/green;
    # r,g,b,radius (unused) = amber/red/grey
    field_colors = [BLUE, BLUE, GREEN, GREEN, AMBER, AMBER, AMBER, GREY]
    field_labels = ["x", "y", "vx", "vy", "r", "g", "b", "rad"]
    used = [True, True, True, True, False, False, False, False]  # MovementSystem cares?

    cell_w = 1.0   # each float = 1 cell
    cell_h = 1.0
    n_obj = 4      # show 4 objects
    start_x = 0.5
    y0 = 3.2

    for obj in range(n_obj):
        ox = start_x + obj * (8 * cell_w + 0.4)
        # object label
        ax.text(ox + 4 * cell_w, y0 + cell_h + 0.55, f"Ball_{obj}",
                ha="center", va="center", fontsize=11.5, weight="bold", color=INK)
        # 8 cells
        for f in range(8):
            cx = ox + f * cell_w
            fc = field_colors[f]
            # lighten unused
            if used[f]:
                face = fc
                edge = INK
                lw = 1.3
                alpha = 1.0
            else:
                face = fc
                edge = DGREY
                lw = 0.8
                alpha = 0.55
            ax.add_patch(Rectangle((cx, y0), cell_w, cell_h, facecolor=face,
                                   edgecolor=edge, linewidth=lw, alpha=alpha))
            ax.text(cx + cell_w / 2, y0 + cell_h / 2, field_labels[f],
                    ha="center", va="center", fontsize=9.5, color="white", weight="bold")

    # cache line bracket over first 16 cells (= 2 balls = 64B), MovementSystem reads ball[0].x
    cl_y = y0 - 0.55
    cl_x0 = start_x
    cl_w = 16 * cell_w
    ax.add_patch(Rectangle((cl_x0, cl_y), cl_w, 0.18, facecolor=RED, edgecolor=RED))
    ax.annotate("", xy=(cl_x0, cl_y - 0.05), xytext=(cl_x0 + cl_w, cl_y - 0.05),
                arrowprops=dict(arrowstyle="<->", color=RED, lw=1.8))
    ax.text(cl_x0 + cl_w / 2, cl_y - 0.5, "64B cache line  (loaded when reading Ball_0.x)",
            ha="center", va="center", fontsize=11, color=RED, weight="bold")

    # utilization bar
    util_y = cl_y - 1.25
    ax.text(start_x, util_y + 0.55, "Useful to MovementSystem:", fontsize=10.5,
            color=INK, ha="left", va="center")
    # 4 useful (x,y,vx,vy of ball0) out of 16
    bar_x = start_x + 5.5
    for i in range(16):
        cx = bar_x + i * 0.7
        which_obj = i // 8
        which_field = i % 8
        if used[which_field]:
            fc = GREEN
        else:
            fc = GREY
        ax.add_patch(Rectangle((cx, util_y), 0.66, 0.45, facecolor=fc, edgecolor="white", lw=0.8))
    ax.text(bar_x + 16 * 0.7 + 0.3, util_y + 0.22, "8/16 = 50% per cache line\n(25% if 1 ball at a time)",
            fontsize=10.5, color=RED, ha="left", va="center", weight="bold")

    # ---- SoA ----
    ax = axes[1]
    clean_ax(ax)
    ax.set_xlim(0, 32); ax.set_ylim(0, 6.6)
    ax.set_title("SoA  (Structure of Arrays): each field is one contiguous array",
                 fontsize=13.5, weight="bold", color=INK, loc="left", pad=10)

    arrays = [
        ("x[]",      BLUE,  True),
        ("y[]",      BLUE,  True),
        ("vx[]",     GREEN, True),
        ("vy[]",     GREEN, True),
        ("r[]",      AMBER, False),
        ("g[]",      AMBER, False),
        ("b[]",      AMBER, False),
        ("rad[]",    GREY,  False),
    ]
    n_per_arr = 16   # show 16 elements per array
    arr_w_total = 16 * 0.7
    ax.set_ylim(0, 8 * 1.1 + 1.5)
    y_top = 8 * 1.1 + 0.4
    for ai, (name, col, is_used) in enumerate(arrays):
        ay = y_top - ai * 1.1
        ax.text(0.3, ay + 0.3, name, fontsize=11, weight="bold", color=INK, ha="left", va="center")
        for i in range(n_per_arr):
            cx = 2.0 + i * 0.7
            alpha = 1.0 if is_used else 0.5
            edge = INK if is_used else DGREY
            lw = 1.0 if is_used else 0.6
            ax.add_patch(Rectangle((cx, ay), 0.62, 0.55, facecolor=col, edgecolor=edge,
                                   linewidth=lw, alpha=alpha))
        # MovementSystem banner over used arrays only
        if is_used:
            ax.text(2.0 + arr_w_total + 0.3, ay + 0.28, "<-- MovementSystem reads",
                    fontsize=10, color=GREEN, weight="bold", ha="left", va="center")
        else:
            ax.text(2.0 + arr_w_total + 0.3, ay + 0.28, "(untouched)",
                    fontsize=9.5, color=DGREY, ha="left", va="center", style="italic")

    # cache line bracket over x[] first 16 elements
    cl_y_arr = y_top + 0.85
    cl_x0 = 2.0
    ax.add_patch(Rectangle((cl_x0, cl_y_arr), arr_w_total, 0.18, facecolor=RED, edgecolor=RED))
    ax.annotate("", xy=(cl_x0, cl_y_arr + 0.85), xytext=(cl_x0 + arr_w_total, cl_y_arr + 0.85),
                arrowprops=dict(arrowstyle="<->", color=RED, lw=1.8))
    ax.text(cl_x0 + arr_w_total / 2, cl_y_arr + 1.25,
            "64B cache line  =  16 objects' x   (100% useful!)",
            ha="center", va="center", fontsize=11, color=RED, weight="bold")

    plt.subplots_adjust(hspace=0.55)
    fig.savefig(os.path.join(OUT, "fig-p2_06_01-aos-vs-soa-cache.png"))
    plt.close(fig)
    print("  saved fig-p2_06_01-aos-vs-soa-cache.png")


# ============================================================
# Fig 2: EnTT sparse_set 结构
# ============================================================
def fig2_sparse_set():
    fig, ax = plt.subplots(figsize=(13.8, 8.2))
    clean_ax(ax)
    ax.set_xlim(0, 28); ax.set_ylim(0, 16)
    ax.set_title("EnTT sparse_set<T>:  paged sparse index  +  dense (packed) arrays",
                 fontsize=14, weight="bold", color=INK, loc="left", pad=12)

    # ---- sparse side (left) ----
    sp_x = 0.5
    sp_y = 2.5
    ax.text(sp_x + 5.5, sp_y + 11.2, "sparse  (paged, by Entity ID)",
            fontsize=12.5, weight="bold", color=PURP, ha="center")

    # show 2 pages
    for pi, page in enumerate(["page 0", "page 1"]):
        px = sp_x + pi * 6.0
        py = sp_y + 1.5
        ax.add_patch(FancyBboxPatch((px, py), 5.2, 9.0,
                                    boxstyle="round,pad=0.02,rounding_size=0.08",
                                    facecolor=LPURP, edgecolor=PURP, linewidth=1.4))
        ax.text(px + 2.6, py + 9.4, page, fontsize=11, weight="bold", color=PURP,
                ha="center", va="center")
        # slots page_size shown as 8 (abbreviated; real page_size ~ 4096)
        n_slots = 8
        ents_in_dense = [None, 0, None, 1, 2, None, None, 3] if pi == 0 else [4, None, 5, None, None, None, None, None]
        for si in range(n_slots):
            sy = py + 8.0 - si * 1.0
            # slot header (entity id)
            eid = pi * 8 + si
            ax.add_patch(Rectangle((px + 0.2, sy), 1.6, 0.78, facecolor="white",
                                   edgecolor=DGREY, linewidth=0.9))
            ax.text(px + 1.0, sy + 0.39, f"id {eid}", fontsize=8.5, ha="center", va="center", color=INK)
            # value: pointer to dense index or null
            val = ents_in_dense[si]
            vx = px + 2.0
            if val is None:
                ax.add_patch(Rectangle((vx, sy), 2.9, 0.78, facecolor=GREY, edgecolor=DGREY,
                                       linewidth=0.8, alpha=0.55))
                ax.text(vx + 1.45, sy + 0.39, "null", fontsize=8.5, ha="center", va="center",
                        color="white", style="italic")
            else:
                ax.add_patch(Rectangle((vx, sy), 2.9, 0.78, facecolor=GREEN, edgecolor=INK, linewidth=1.0))
                ax.text(vx + 1.45, sy + 0.39, f"-> dense[{val}]", fontsize=8.5, ha="center", va="center",
                        color="white", weight="bold")

    ax.text(sp_x + 5.5, sp_y + 0.6,
            "(each page = page_size slots; only used pages allocated)",
            fontsize=9.5, color=DGREY, ha="center", style="italic")

    # ---- dense side (right) ----
    de_x = 14.5
    de_y = 2.5
    ax.text(de_x + 6.5, de_y + 11.2, "dense  (packed, contiguous, cache-friendly)",
            fontsize=12.5, weight="bold", color=BLUE, ha="center")

    # packed entity array
    ax.text(de_x, de_y + 9.6, "packed  (entity ids, contiguous):",
            fontsize=11, weight="bold", color=INK, ha="left")
    ents = [1, 3, 4, 11, 13]   # the entities that have this component (matching sparse above)
    for i, e in enumerate(ents):
        cx = de_x + i * 1.35
        ax.add_patch(Rectangle((cx, de_y + 8.3), 1.2, 0.9, facecolor=BLUE, edgecolor=INK, linewidth=1.2))
        ax.text(cx + 0.6, de_y + 8.75, f"e{e}", fontsize=10, ha="center", va="center",
                color="white", weight="bold")
        ax.text(cx + 0.6, de_y + 8.05, f"[{i}]", fontsize=8, ha="center", va="center", color=DGREY)

    # payload (component data) array, parallel to packed
    ax.text(de_x, de_y + 6.6, "payload  (component data, contiguous, parallel):",
            fontsize=11, weight="bold", color=INK, ha="left")
    for i in range(len(ents)):
        cx = de_x + i * 1.35
        # each Position = {x, y}, draw 2 sub-cells
        ax.add_patch(Rectangle((cx, de_y + 5.0), 1.2, 1.4, facecolor=LGREEN, edgecolor=INK, linewidth=1.2))
        ax.add_patch(Rectangle((cx, de_y + 5.7), 1.2, 0.7, facecolor=GREEN, edgecolor=INK, linewidth=0.8))
        ax.add_patch(Rectangle((cx, de_y + 5.0), 1.2, 0.7, facecolor=GREEN, edgecolor=INK, linewidth=0.8))
        ax.text(cx + 0.6, de_y + 6.05, "x", fontsize=8, ha="center", va="center", color="white", weight="bold")
        ax.text(cx + 0.6, de_y + 5.35, "y", fontsize=8, ha="center", va="center", color="white", weight="bold")
        ax.text(cx + 0.6, de_y + 4.75, f"[{i}]", fontsize=8, ha="center", va="center", color=DGREY)

    # traversal arrow
    ax.annotate("", xy=(de_x + len(ents) * 1.35 + 0.1, de_y + 5.7),
                xytext=(de_x - 0.1, de_y + 5.7),
                arrowprops=dict(arrowstyle="-|>", color=RED, lw=2.2))
    ax.text(de_x + len(ents) * 1.35 / 2, de_y + 3.9,
            "System traverses dense in order  ->  cache hits, prefetcher ahead",
            fontsize=10.5, color=RED, weight="bold", ha="center", va="center")

    # mapping arrows: sparse -> dense (a few representative)
    for eid, didx in [(1, 0), (3, 1), (4, 2), (11, 3), (13, 4)]:
        # locate sparse slot position
        if eid < 8:
            pi = 0; si = eid
        else:
            pi = 1; si = eid - 8
        px = sp_x + pi * 6.0
        py = sp_y + 1.5
        sy = py + 8.0 - si * 1.0
        # dense target
        dx = de_x + didx * 1.35 + 0.6
        dy = de_y + 8.75
        ax.add_artist(FancyArrowPatch((px + 5.4, sy + 0.39), (dx, dy),
                                      arrowstyle="-|>", color=AMBER, lw=0.9,
                                      connectionstyle="arc3,rad=0.12", alpha=0.75))

    # legend
    ax.text(14.0, 1.2,
            "O(1) lookup:  entity e -> sparse[e] -> dense index -> payload[i]\n"
            "Traversal:   walk dense in order (entity + payload both contiguous)",
            fontsize=10.5, color=INK, ha="center", va="center",
            bbox=dict(boxstyle="round,pad=0.4", facecolor=SOFT, edgecolor=DGREY))

    fig.savefig(os.path.join(OUT, "fig-p2_06_02-entt-sparse-set.png"))
    plt.close(fig)
    print("  saved fig-p2_06_02-entt-sparse-set.png")


# ============================================================
# Fig 3: 数值模拟 - 缓存 miss + 遍历耗时随实体数增长
# ============================================================
def fig3_benchmark():
    fig, axes = plt.subplots(1, 2, figsize=(14, 6.0))

    n = np.array([1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000], dtype=float)
    cache_line = 64.0
    obj_bytes_aos = 32.0       # x,y,vx,vy,r,g,b,rad
    used_bytes_movement = 16.0 # x,y,vx,vy

    # --- cache miss estimates ---
    # AoS contiguous: every cache line touched once per traversal
    aos_contiguous_miss = (n * obj_bytes_aos) / cache_line
    # SoA: only 4 arrays (x,y,vx,vy), each n*4 bytes
    soa_miss = (4 * n * 4.0) / cache_line
    # AoS scattered (pointer chasing): ~ each object one miss (or more), prefetcher fails
    aos_scattered_miss = n * 1.5

    ax = axes[0]
    ax.loglog(n, aos_scattered_miss, "-o", color=RED, lw=2.2, ms=7,
              label="AoS scattered (pointer chasing)")
    ax.loglog(n, aos_contiguous_miss, "-s", color=AMBER, lw=2.2, ms=7,
              label="AoS contiguous")
    ax.loglog(n, soa_miss, "-^", color=GREEN, lw=2.4, ms=8,
              label="SoA (ECS pool)")
    ax.set_xlabel("number of entities", fontsize=12)
    ax.set_ylabel("cache line misses per traversal", fontsize=12)
    ax.set_title("Cache misses vs entity count  (log-log)", fontsize=12.5, weight="bold")
    ax.grid(True, which="both", ls=":", alpha=0.5)
    ax.legend(fontsize=10.5, loc="upper left", framealpha=0.95)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    # annotate SoA ~ half of contiguous AoS
    ax.annotate("SoA = ~1/2 of contiguous AoS\n(= ~1/4 of scattered)",
                xy=(50000, soa_miss[5]), xytext=(2200, 400),
                fontsize=10, color=GREEN, weight="bold",
                arrowprops=dict(arrowstyle="->", color=GREEN, lw=1.4))

    # --- traversal time simulation ---
    # simulate relative time per element (ns/element) reflecting miss cost
    # SoA: ~0.5 ns/elem (perfect prefetch, SIMD-ish)
    # AoS contiguous: ~1.2 ns/elem (50% useful bytes)
    # AoS scattered: grows super-linearly once cache saturates (~32KB L1, ~256KB L2)
    rng = np.random.default_rng(7)
    base_soa = 0.5
    base_aos_c = 1.2
    # scattered: high constant + miss penalty kicks in as n exceeds cache
    def scattered_time(nn):
        # bytes working set
        ws = nn * obj_bytes_aos
        # L1 ~32KB, L2 ~256KB, L3 ~8MB -> step penalties
        t = 2.0 + 0.0 * nn
        t = np.where(ws > 32e3, t + 1.5, t)
        t = np.where(ws > 256e3, t + 3.0, t)
        t = np.where(ws > 8e6, t + 8.0, t)
        return t * (1.0 + 0.05 * rng.random(len(nn)))  # small jitter
    t_soa = base_soa * n * (1.0 + 0.03 * rng.random(len(n)))
    t_aos_c = base_aos_c * n * (1.0 + 0.03 * rng.random(len(n)))
    t_aos_s = scattered_time(n) * n / 4.0 * (1.0 + 0.03 * rng.random(len(n)))

    ax = axes[1]
    ax.loglog(n, t_aos_s, "-o", color=RED, lw=2.2, ms=7, label="AoS scattered (pointer chasing)")
    ax.loglog(n, t_aos_c, "-s", color=AMBER, lw=2.2, ms=7, label="AoS contiguous")
    ax.loglog(n, t_soa, "-^", color=GREEN, lw=2.4, ms=8, label="SoA (ECS pool)")
    ax.set_xlabel("number of entities", fontsize=12)
    ax.set_ylabel("traversal time  (relative, log)", fontsize=12)
    ax.set_title("Traversal time vs entity count  (log-log, simulated)", fontsize=12.5, weight="bold")
    ax.grid(True, which="both", ls=":", alpha=0.5)
    ax.legend(fontsize=10.5, loc="upper left", framealpha=0.95)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.annotate("scattered AoS super-linear\n(cache saturation, prefetcher dead)",
                xy=(100000, t_aos_s[6]), xytext=(1500, t_aos_s[6] * 0.4),
                fontsize=10, color=RED, weight="bold",
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.4))

    fig.suptitle("AoS vs SoA:  quantitative impact of data layout on MovementSystem traversal",
                 fontsize=13, weight="bold", color=INK, y=1.02)
    plt.tight_layout()
    fig.savefig(os.path.join(OUT, "fig-p2_06_03-aos-vs-soa-bench.png"))
    plt.close(fig)
    print("  saved fig-p2_06_03-aos-vs-soa-bench.png")


if __name__ == "__main__":
    print("Generating P2-06 figures ...")
    fig1_aos_vs_soa()
    fig2_sparse_set()
    fig3_benchmark()
    print("Done.")
