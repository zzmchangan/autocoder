# -*- coding: utf-8 -*-
"""为《游戏引擎》P2-08 (Archetype: 现代 ECS 的内存布局) 生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_p2_08_figures.py

Figures:
  fig-p2_08_01-archetype-grouping.png      Archetype 分组布局: 实体按"组件组合"分到不同 table,
                                           每个 table 列=组件类型(连续数组), 行=实体
  fig-p2_08_02-archetype-vs-sparse-set.png Archetype vs sparse set 对比: 同一个 (Pos+Vel) 查询在
                                           两种范式下的访问模式与代价
  fig-p2_08_03-archetype-migration.png     实体加组件导致 archetype 迁移: 数据从一张 table 搬到另一张
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

# 固定配色 (与 P2-06 系列一致)
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
LTEAL = (0.86, 0.95, 0.95)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


# ============================================================
# Fig 1: Archetype 分组布局
#   世界上的实体按"组件组合"分到不同 archetype table.
#   每个 table: 列 = 组件类型(一条连续数组), 行 = 实体.
#   MovementSystem 要 (Pos + Vel) -> 只扫 archetype B / C / D (含这两列), 跳过 A / E.
# ============================================================
def fig1_archetype_grouping():
    fig, ax = plt.subplots(figsize=(15.0, 9.6))
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 19)
    ax.set_title("Archetype grouping:  entities with the SAME component set share one table",
                 fontsize=14, weight="bold", color=INK, loc="left", pad=12)

    # 5 个 archetype, 每个是一张小表 (列 = 组件, 行 = 实体)
    # (name, components, n_rows, x, y, highlight_query_match)
    archetypes = [
        ("Archetype A",  ["Pos", "Col"],              3, 0.5,  9.5, False),
        ("Archetype B",  ["Pos", "Vel"],              4, 7.0,  9.5, True),   # matches Pos+Vel
        ("Archetype C",  ["Pos", "Vel", "HP"],        5, 0.5,  1.0, True),   # matches Pos+Vel
        ("Archetype D",  ["Pos", "Vel", "HP", "AI"],  2, 9.5,  1.0, True),   # matches Pos+Vel
        ("Archetype E",  ["Pos", "Cam"],              1, 17.5, 1.0, False),
    ]

    col_w = 1.55
    row_h = 0.95
    header_h = 1.15
    label_w = 2.7  # left label column for entity id

    # component -> color
    comp_color = {
        "Pos": BLUE,
        "Vel": GREEN,
        "HP":  AMBER,
        "AI":  RED,
        "Col": PURP,
        "Cam": TEAL,
    }

    for name, comps, nrows, x0, y0, match in archetypes:
        total_w = label_w + len(comps) * col_w
        total_h = header_h + nrows * row_h

        # table frame
        face = LGREEN if match else SOFT
        edge = GREEN if match else DGREY
        lw = 2.0 if match else 1.0
        ax.add_patch(FancyBboxPatch((x0, y0), total_w, total_h,
                                    boxstyle="round,pad=0.02,rounding_size=0.10",
                                    facecolor=face, edgecolor=edge, linewidth=lw))

        # title above the table
        title_col = GREEN if match else INK
        ax.text(x0 + total_w / 2, y0 + total_h + 0.45, name,
                ha="center", va="center", fontsize=12, weight="bold", color=title_col)
        ax.text(x0 + total_w / 2, y0 + total_h + 0.05,
                "{" + ", ".join(comps) + "}",
                ha="center", va="center", fontsize=9.5, color=DGREY, style="italic")

        if match:
            ax.text(x0 + total_w / 2, y0 - 0.45, "matches (Pos+Vel)",
                    ha="center", va="center", fontsize=10, weight="bold", color=GREEN)

        # header row: first the "entity" label, then each component
        hy = y0 + total_h - header_h
        ax.add_patch(Rectangle((x0 + 0.08, hy + 0.08), label_w - 0.16, header_h - 0.16,
                               facecolor="white", edgecolor=DGREY, linewidth=0.8))
        ax.text(x0 + label_w / 2, hy + header_h / 2, "entity",
                ha="center", va="center", fontsize=9.5, color=INK, weight="bold")
        for ci, c in enumerate(comps):
            cx = x0 + label_w + ci * col_w
            ax.add_patch(Rectangle((cx + 0.08, hy + 0.08), col_w - 0.16, header_h - 0.16,
                                   facecolor=comp_color[c], edgecolor=INK, linewidth=0.9))
            ax.text(cx + col_w / 2, hy + header_h / 2, c,
                    ha="center", va="center", fontsize=10, color="white", weight="bold")

        # data rows
        for ri in range(nrows):
            ry = hy - (ri + 1) * row_h
            # entity id cell
            ax.add_patch(Rectangle((x0 + 0.08, ry + 0.08), label_w - 0.16, row_h - 0.16,
                                   facecolor="white", edgecolor=DGREY, linewidth=0.7))
            ax.text(x0 + label_w / 2, ry + row_h / 2, f"row {ri}",
                    ha="center", va="center", fontsize=8.5, color=DGREY)
            # component cells (just colored blocks, imply continuous column)
            for ci, c in enumerate(comps):
                cx = x0 + label_w + ci * col_w
                ax.add_patch(Rectangle((cx + 0.08, ry + 0.08), col_w - 0.16, row_h - 0.16,
                                       facecolor=comp_color[c], edgecolor="white",
                                       linewidth=0.6, alpha=0.85))

    # legend on the right
    leg_x = 23.3; leg_y = 14.5
    ax.add_patch(FancyBboxPatch((leg_x, leg_y - 9.0), 6.3, 9.4,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=SOFT, edgecolor=DGREY, linewidth=1.0))
    ax.text(leg_x + 3.15, leg_y + 0.2, "How a System queries",
                                 ha="center", va="center", fontsize=11.5, weight="bold", color=INK)
    ax.text(leg_x + 0.3, leg_y - 0.55,
            "MovementSystem wants:\n   Position + Velocity\n\n"
            "Engine finds archetype tables\n   that have BOTH columns:\n"
            "   -> B {Pos,Vel}\n   -> C {Pos,Vel,HP}\n   -> D {Pos,Vel,HP,AI}\n\n"
            "Skips:\n"
            "   A {Pos,Col}   no Vel\n"
            "   E {Pos,Cam}   no Vel\n\n"
            "Inside each matched table:\n"
            "   Pos[] and Vel[] are contiguous\n"
            "   AND row-aligned (row i = same entity)\n"
            "   -> zero skip, SIMD-friendly",
            ha="left", va="top", fontsize=9.8, color=INK)

    # bottom: "row i aligned" highlight under archetype C (the best example)
    cx0 = 0.5; cy0 = 1.0
    ccomps = ["Pos", "Vel", "HP"]
    # highlight row 2 across the 3 columns
    ri = 2
    hy_c = cy0 + (header_h + 5 * row_h) - header_h
    ry = hy_c - (ri + 1) * row_h
    for ci in range(len(ccomps)):
        xx = cx0 + label_w + ci * col_w
        ax.add_patch(Rectangle((xx + 0.05, ry + 0.05), col_w - 0.10, row_h - 0.10,
                               facecolor="none", edgecolor=RED, linewidth=2.2))
    ax.annotate("row 2 in ALL columns = same entity\n(columns contiguous + row-aligned)",
                xy=(cx0 + label_w + 1.5 * col_w, ry + row_h / 2),
                xytext=(cx0 + 0.2, ry - 1.4),
                fontsize=10, color=RED, weight="bold", ha="left", va="center",
                arrowprops=dict(arrowstyle="-|>", color=RED, lw=1.6))

    fig.savefig(os.path.join(OUT, "fig-p2_08_01-archetype-grouping.png"))
    plt.close(fig)
    print("  saved fig-p2_08_01-archetype-grouping.png")


# ============================================================
# Fig 2: Archetype vs sparse set - 同一查询的访问模式对比
#   左: Archetype - 直接扫匹配的 table, Pos[]/Vel[] 行对齐, 连续无跳过
#   右: sparse set - 沿小 set 扫, 每个实体查另一个 set 的 sparse (分支 + 间接跳转)
# ============================================================
def fig2_archetype_vs_sparse_set():
    fig, axes = plt.subplots(2, 1, figsize=(15.0, 10.0),
                             gridspec_kw={"height_ratios": [1, 1]})

    # ============== Archetype (top) ==============
    ax = axes[0]
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 8.4)
    ax.set_title("Archetype:  scan matched table directly  ->  contiguous, row-aligned, SIMD-friendly",
                 fontsize=12.5, weight="bold", color=GREEN, loc="left", pad=8)

    col_w = 1.5; row_h = 0.85; header_h = 1.0; label_w = 2.4
    x0 = 0.6; y0 = 1.3
    comps = ["Pos", "Vel"]
    nrows = 8

    # frame
    total_w = label_w + len(comps) * col_w
    total_h = header_h + nrows * row_h
    ax.add_patch(FancyBboxPatch((x0, y0), total_w, total_h,
                                boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=LGREEN, edgecolor=GREEN, linewidth=1.8))
    ax.text(x0 + total_w / 2, y0 + total_h + 0.45,
            "Archetype C  {Pos, Vel, HP}  (showing Pos & Vel columns)",
            ha="center", va="center", fontsize=11.5, weight="bold", color=INK)

    # header
    hy = y0 + total_h - header_h
    ax.add_patch(Rectangle((x0 + 0.08, hy + 0.08), label_w - 0.16, header_h - 0.16,
                           facecolor="white", edgecolor=DGREY, linewidth=0.8))
    ax.text(x0 + label_w / 2, hy + header_h / 2, "row",
            ha="center", va="center", fontsize=9.5, weight="bold", color=INK)
    for ci, c in enumerate(comps):
        cx = x0 + label_w + ci * col_w
        col = BLUE if c == "Pos" else GREEN
        ax.add_patch(Rectangle((cx + 0.08, hy + 0.08), col_w - 0.16, header_h - 0.16,
                               facecolor=col, edgecolor=INK, linewidth=0.9))
        ax.text(cx + col_w / 2, hy + header_h / 2, c + "[]",
                ha="center", va="center", fontsize=10, color="white", weight="bold")

    # rows - all 8 are valid (this table only holds entities that have BOTH)
    for ri in range(nrows):
        ry = hy - (ri + 1) * row_h
        ax.add_patch(Rectangle((x0 + 0.08, ry + 0.08), label_w - 0.16, row_h - 0.16,
                               facecolor="white", edgecolor=DGREY, linewidth=0.7))
        ax.text(x0 + label_w / 2, ry + row_h / 2, str(ri),
                ha="center", va="center", fontsize=9, color=DGREY)
        for ci, c in enumerate(comps):
            cx = x0 + label_w + ci * col_w
            col = BLUE if c == "Pos" else GREEN
            ax.add_patch(Rectangle((cx + 0.08, ry + 0.08), col_w - 0.16, row_h - 0.16,
                                   facecolor=col, edgecolor="white", linewidth=0.6, alpha=0.9))

    # SIMD batch bracket: 8 rows processed together
    sb_y = hy - nrows * row_h - 0.35
    ax.add_patch(Rectangle((x0, sb_y), total_w, 0.16, facecolor=RED, edgecolor=RED))
    ax.annotate("", xy=(x0, sb_y - 0.05), xytext=(x0 + total_w, sb_y - 0.05),
                arrowprops=dict(arrowstyle="<->", color=RED, lw=1.6))
    ax.text(x0 + total_w / 2, sb_y - 0.55,
            "SIMD: 8x Pos += Vel  in ONE vector op  (no skip, no branch)",
            ha="center", va="center", fontsize=10.5, color=RED, weight="bold")

    # traversal arrow
    ax.annotate("", xy=(x0 + total_w + 0.2, y0 + total_h / 2),
                xytext=(x0 + label_w - 0.1, y0 + total_h / 2),
                arrowprops=dict(arrowstyle="-|>", color=RED, lw=2.0))
    ax.text(x0 + total_w + 0.3, y0 + total_h / 2,
            "  contiguous\n  read Pos[]\n  + Vel[]\n  row-aligned\n  -> all cache hits",
            fontsize=10, color=RED, weight="bold", ha="left", va="center")

    # right-side summary box
    sx = 9.0
    ax.add_patch(FancyBboxPatch((sx, 1.0), 20.5, 6.8,
                                boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=SOFT, edgecolor=DGREY, linewidth=1.0))
    ax.text(sx + 0.4, 7.4, "PROS", ha="left", va="top", fontsize=11, weight="bold", color=GREEN)
    ax.text(sx + 0.4, 6.9,
            "+  zero skip (every row in this table has BOTH components)\n"
            "+  both columns contiguous & row-aligned (row i = same entity)\n"
            "+  SIMD fully vectorized (no branch to break the batch)\n"
            "+  multi-core: split rows into chunks, each chunk contiguous",
            ha="left", va="top", fontsize=10, color=INK)
    ax.text(sx + 0.4, 3.7, "CONS", ha="left", va="top", fontsize=11, weight="bold", color=RED)
    ax.text(sx + 0.4, 3.2,
            "-  add/remove component -> migrate entity to another table\n"
            "    (copy column data, swap_remove old row, update location)\n"
            "-  heavy if components added/removed frequently (particles, buffs)",
            ha="left", va="top", fontsize=10, color=INK)

    # ============== Sparse set (bottom) ==============
    ax = axes[1]
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 8.4)
    ax.set_title("Sparse set:  iterate smaller set, probe the other via sparse[]  ->  branch + indirection",
                 fontsize=12.5, weight="bold", color=PURP, loc="left", pad=8)

    # two sparse sets side by side, mismatched
    # Vel.packed (smaller) - left; Pos.sparse (probed) - right
    # show iteration over Vel.packed with branch outcomes
    set_x = 0.6; set_y = 1.0
    cell_w = 1.35; cell_h = 0.85

    # Vel.packed header
    ax.text(set_x, set_y + 6.6, "Vel  sparse set",
            fontsize=11.5, weight="bold", color=GREEN, ha="left")
    ax.text(set_x, set_y + 6.1, "packed[] (contiguous):", fontsize=10, weight="bold", color=INK, ha="left")
    vel_packed = ["e3", "e7", "e11", "e15", "e19", "e23", "e27", "e31"]
    vel_have_pos = [True, False, True, True, False, True, True, False]  # does this entity also have Pos?
    for i, e in enumerate(vel_packed):
        cx = set_x + i * (cell_w + 0.05)
        ok = vel_have_pos[i]
        col = GREEN if ok else GREY
        ax.add_patch(Rectangle((cx, set_y + 4.8), cell_w, cell_h,
                               facecolor=col, edgecolor=INK, linewidth=1.0,
                               alpha=0.95 if ok else 0.5))
        ax.text(cx + cell_w / 2, set_y + 4.8 + cell_h / 2, e,
                ha="center", va="center", fontsize=9, color="white", weight="bold")
        ax.text(cx + cell_w / 2, set_y + 4.55, f"[{i}]",
                ha="center", va="center", fontsize=7.5, color=DGREY)

    # iteration arrow with branch labels
    ax.annotate("", xy=(set_x + 8 * (cell_w + 0.05) + 0.1, set_y + 5.2),
                xytext=(set_x - 0.1, set_y + 5.2),
                arrowprops=dict(arrowstyle="-|>", color=RED, lw=2.0))
    ax.text(set_x + 4 * (cell_w + 0.05), set_y + 3.8,
            "iterate Vel.packed[] in order",
            fontsize=10, color=RED, weight="bold", ha="center")

    # branch outcomes per element
    for i, ok in enumerate(vel_have_pos):
        cx = set_x + i * (cell_w + 0.05) + cell_w / 2
        if ok:
            ax.text(cx, set_y + 3.2, "OK", ha="center", va="center",
                    fontsize=9, color=GREEN, weight="bold")
        else:
            ax.text(cx, set_y + 3.2, "branch\n(skip)", ha="center", va="center",
                    fontsize=8, color=RED, weight="bold")

    # Pos sparse array (probed) - show as indexed slots, scattered positions
    px = 12.5; py = 1.0
    ax.text(px, py + 6.6, "Pos  sparse set",
            fontsize=11.5, weight="bold", color=BLUE, ha="left")
    ax.text(px, py + 6.1, "sparse[] indexed by entity id  (paged, mostly null):",
            fontsize=10, weight="bold", color=INK, ha="left")

    # show sparse slots: only entities in vel_packed matter
    slot_w = 1.0; slot_h = 0.85
    # display 12 slots with entity ids and whether present
    slots = [
        ("e3",  True,  "->Pos.packed[?]"),
        ("e7",  False, "null (no Pos)"),
        ("e11", True,  "->Pos.packed[?]"),
        ("e15", True,  "->Pos.packed[?]"),
        ("e19", False, "null"),
        ("e23", True,  "->Pos.packed[?]"),
        ("e27", True,  "->Pos.packed[?]"),
        ("e31", False, "null"),
    ]
    for i, (eid, has, note) in enumerate(slots):
        cx = px + i * (slot_w + 0.05)
        if has:
            ax.add_patch(Rectangle((cx, py + 4.4), slot_w, slot_h,
                                   facecolor=BLUE, edgecolor=INK, linewidth=0.9, alpha=0.85))
            ax.text(cx + slot_w / 2, py + 4.4 + slot_h / 2, eid,
                    ha="center", va="center", fontsize=8, color="white", weight="bold")
        else:
            ax.add_patch(Rectangle((cx, py + 4.4), slot_w, slot_h,
                                   facecolor=GREY, edgecolor=DGREY, linewidth=0.7, alpha=0.55))
            ax.text(cx + slot_w / 2, py + 4.4 + slot_h / 2, eid,
                    ha="center", va="center", fontsize=8, color="white", style="italic")

    # probe arrows: for each element in vel.packed, probe pos.sparse
    for i, (eid, ok, _) in enumerate(slots):
        if ok:
            sx_from = set_x + i * (cell_w + 0.05) + cell_w
            sx_to = px + i * (slot_w + 0.05)
            ax.add_artist(FancyArrowPatch((sx_from, set_y + 5.2), (sx_to, py + 4.8),
                                          arrowstyle="-|>", color=AMBER, lw=0.9,
                                          connectionstyle="arc3,rad=-0.15", alpha=0.8))
        else:
            sx_from = set_x + i * (cell_w + 0.05) + cell_w
            sx_to = px + i * (slot_w + 0.05)
            ax.add_artist(FancyArrowPatch((sx_from, set_y + 5.2), (sx_to, py + 4.8),
                                          arrowstyle="-|>", color=RED, lw=0.8,
                                          connectionstyle="arc3,rad=-0.15", alpha=0.55,
                                          linestyle=":"))

    ax.text(px + 4 * (slot_w + 0.05), py + 3.6,
            "each probe = O(1) but is an INDIRECTION\n"
            "(position in Pos.packed unrelated to position in Vel.packed)",
            fontsize=9.5, color=RED, ha="center", va="top", weight="bold")

    # summary
    sx2 = 0.6
    ax.add_patch(FancyBboxPatch((sx2, -0.5), 29.0, 1.3,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=LRED, edgecolor=RED, linewidth=1.0))
    ax.text(sx2 + 0.3, 0.15,
            "PROS:  + add/remove O(1), zero migration, stable entity id      |      "
            "CONS:  - cross-component query = gather (branch + indirection), SIMD broken by if(has)",
            ha="left", va="center", fontsize=10, color=INK, weight="bold")

    plt.subplots_adjust(hspace=0.45)
    fig.savefig(os.path.join(OUT, "fig-p2_08_02-archetype-vs-sparse-set.png"))
    plt.close(fig)
    print("  saved fig-p2_08_02-archetype-vs-sparse-set.png")


# ============================================================
# Fig 3: 实体加组件导致 archetype 迁移
#   enemy_0 在 Archetype C {Pos,Vel,HP}, 加 AI -> 搬到 Archetype D {Pos,Vel,HP,AI}
#   逐列拷贝共同组件, 新列写新值, 老 table swap_remove 收回空位
# ============================================================
def fig3_archetype_migration():
    fig, ax = plt.subplots(figsize=(15.0, 9.2))
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 17)
    ax.set_title("Archetype migration:  adding AI to enemy_0 moves it from table C to table D",
                 fontsize=13.5, weight="bold", color=INK, loc="left", pad=12)

    col_w = 1.6; row_h = 0.95; header_h = 1.15; label_w = 2.6

    # ---- BEFORE: Archetype C (left) ----
    cx0 = 0.6; cy0 = 8.5
    ccomps = ["Pos", "Vel", "HP"]
    crow_total = 4   # rows 0..3, enemy_0 is row 0 (will move)
    cw = label_w + len(ccomps) * col_w
    ch = header_h + crow_total * row_h
    ax.add_patch(FancyBboxPatch((cx0, cy0), cw, ch,
                                boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=SOFT, edgecolor=DGREY, linewidth=1.4))
    ax.text(cx0 + cw / 2, cy0 + ch + 0.5, "BEFORE",
            ha="center", fontsize=11, weight="bold", color=INK)
    ax.text(cx0 + cw / 2, cy0 + ch + 0.05, "Archetype C  {Pos, Vel, HP}",
            ha="center", fontsize=11, weight="bold", color=INK)

    hy_c = cy0 + ch - header_h
    ax.add_patch(Rectangle((cx0 + 0.08, hy_c + 0.08), label_w - 0.16, header_h - 0.16,
                           facecolor="white", edgecolor=DGREY, linewidth=0.8))
    ax.text(cx0 + label_w / 2, hy_c + header_h / 2, "row",
            ha="center", va="center", fontsize=9, weight="bold", color=INK)
    comp_color = {"Pos": BLUE, "Vel": GREEN, "HP": AMBER, "AI": RED}
    for ci, c in enumerate(ccomps):
        xx = cx0 + label_w + ci * col_w
        ax.add_patch(Rectangle((xx + 0.08, hy_c + 0.08), col_w - 0.16, header_h - 0.16,
                               facecolor=comp_color[c], edgecolor=INK, linewidth=0.9))
        ax.text(xx + col_w / 2, hy_c + header_h / 2, c,
                ha="center", va="center", fontsize=10, color="white", weight="bold")
    rows_c = ["enemy_0 (moving)", "enemy_1", "enemy_2", "enemy_3"]
    for ri in range(crow_total):
        ry = hy_c - (ri + 1) * row_h
        ax.add_patch(Rectangle((cx0 + 0.08, ry + 0.08), label_w - 0.16, row_h - 0.16,
                               facecolor=LRED if ri == 0 else "white",
                               edgecolor=RED if ri == 0 else DGREY,
                               linewidth=1.6 if ri == 0 else 0.7))
        ax.text(cx0 + label_w / 2, ry + row_h / 2, str(ri),
                ha="center", va="center", fontsize=9,
                color=RED if ri == 0 else DGREY, weight="bold")
        for ci, c in enumerate(ccomps):
            xx = cx0 + label_w + ci * col_w
            ax.add_patch(Rectangle((xx + 0.08, ry + 0.08), col_w - 0.16, row_h - 0.16,
                                   facecolor=comp_color[c], edgecolor="white",
                                   linewidth=0.6,
                                   alpha=0.45 if ri == 0 else 0.85))

    # arrow indicating the move
    ax.annotate("add AI component", xy=(cx0 + cw / 2, cy0 + ch / 2),
                xytext=(cx0 - 0.2, cy0 + ch + 1.6),
                fontsize=10, color=RED, weight="bold", ha="center",
                arrowprops=dict(arrowstyle="-|>", color=RED, lw=1.4))

    # ---- AFTER: Archetype D (right) ----
    dx0 = 11.0; dy0 = 8.5
    dcomps = ["Pos", "Vel", "HP", "AI"]
    drow_total = 3  # suppose D had enemy_4, enemy_5, then enemy_0 lands as new last row
    # Actually show D initially with 2 rows then enemy_0 lands as row 2
    dw = label_w + len(dcomps) * col_w
    dh = header_h + (drow_total + 1) * row_h   # +1 to show the incoming row
    ax.add_patch(FancyBboxPatch((dx0, dy0), dw, dh,
                                boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=LGREEN, edgecolor=GREEN, linewidth=1.8))
    ax.text(dx0 + dw / 2, dy0 + dh + 0.5, "AFTER",
            ha="center", fontsize=11, weight="bold", color=INK)
    ax.text(dx0 + dw / 2, dy0 + dh + 0.05, "Archetype D  {Pos, Vel, HP, AI}",
            ha="center", fontsize=11, weight="bold", color=GREEN)

    hy_d = dy0 + dh - header_h
    ax.add_patch(Rectangle((dx0 + 0.08, hy_d + 0.08), label_w - 0.16, header_h - 0.16,
                           facecolor="white", edgecolor=DGREY, linewidth=0.8))
    ax.text(dx0 + label_w / 2, hy_d + header_h / 2, "row",
            ha="center", va="center", fontsize=9, weight="bold", color=INK)
    for ci, c in enumerate(dcomps):
        xx = dx0 + label_w + ci * col_w
        ax.add_patch(Rectangle((xx + 0.08, hy_d + 0.08), col_w - 0.16, header_h - 0.16,
                               facecolor=comp_color[c], edgecolor=INK, linewidth=0.9))
        ax.text(xx + col_w / 2, hy_d + header_h / 2, c,
                ha="center", va="center", fontsize=10, color="white", weight="bold")
    rows_d = ["enemy_4", "enemy_5", "enemy_0 (NEW)"]
    for ri in range(len(rows_d)):
        ry = hy_d - (ri + 1) * row_h
        is_new = (ri == len(rows_d) - 1)
        ax.add_patch(Rectangle((dx0 + 0.08, ry + 0.08), label_w - 0.16, row_h - 0.16,
                               facecolor=LGREEN if is_new else "white",
                               edgecolor=GREEN if is_new else DGREY,
                               linewidth=1.6 if is_new else 0.7))
        ax.text(dx0 + label_w / 2, ry + row_h / 2, str(ri),
                ha="center", va="center", fontsize=9,
                color=GREEN if is_new else DGREY, weight="bold")
        for ci, c in enumerate(dcomps):
            xx = dx0 + label_w + ci * col_w
            # AI column = brand new (dashed), others = copied from C
            if c == "AI":
                ax.add_patch(Rectangle((xx + 0.08, ry + 0.08), col_w - 0.16, row_h - 0.16,
                                       facecolor=comp_color[c] if is_new else "white",
                                       edgecolor=RED, linewidth=1.2,
                                       alpha=0.9 if is_new else 0.3))
            else:
                ax.add_patch(Rectangle((xx + 0.08, ry + 0.08), col_w - 0.16, row_h - 0.16,
                                       facecolor=comp_color[c] if is_new else comp_color[c],
                                       edgecolor="white", linewidth=0.6,
                                       alpha=0.9 if is_new else 0.5))

    # ---- migration arrows: 3 columns copied from C row 0 -> D new row ----
    # source positions: row 0 of C
    src_y = hy_c - 1 * row_h + row_h / 2
    # dest positions: new row (last) of D
    dst_y = hy_d - len(rows_d) * row_h + row_h / 2
    for ci, c in enumerate(["Pos", "Vel", "HP"]):
        sx = cx0 + label_w + ci * col_w + col_w / 2
        # destination column index in D for c is same ci
        tx = dx0 + label_w + ci * col_w + col_w / 2
        ax.add_artist(FancyArrowPatch((sx, src_y), (tx, dst_y),
                                      arrowstyle="-|>", color=GREEN, lw=1.6,
                                      connectionstyle="arc3,rad=0.18", alpha=0.85))
    ax.text((cx0 + cw + dx0) / 2, src_y + 1.8,
            "copy column data\n(Pos, Vel, HP)",
            ha="center", va="center", fontsize=10, color=GREEN, weight="bold")

    # arrow for the new AI value
    ai_tx = dx0 + label_w + 3 * col_w + col_w / 2
    ax.add_artist(FancyArrowPatch((ai_tx, dst_y + 2.5), (ai_tx, dst_y + row_h),
                                  arrowstyle="-|>", color=RED, lw=1.6,
                                  mutation_scale=14))
    ax.text(ai_tx + 0.2, dst_y + 2.9, "write new AI value",
            fontsize=10, color=RED, weight="bold", ha="left")

    # ---- swap_remove note on C: row 0 now occupied by old last row (enemy_3) ----
    ax.annotate("", xy=(cx0 + cw / 2, hy_c - 1 * row_h),
                xytext=(cx0 + cw / 2, hy_c - crow_total * row_h),
                arrowprops=dict(arrowstyle="-|>", color=AMBER, lw=1.4,
                                connectionstyle="arc3,rad=0.0"))
    ax.text(cx0 - 0.2, hy_c - crow_total * row_h - 0.6,
            "swap_remove: last row (enemy_3) moves\ninto row 0 to keep table compact",
            fontsize=9.5, color=AMBER, weight="bold", ha="left", va="center")

    # ---- Edges cache box (bottom) ----
    ex = 0.6; ey = 0.3
    ax.add_patch(FancyBboxPatch((ex, ey), 28.8, 2.5,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=LPURP, edgecolor=PURP, linewidth=1.2))
    ax.text(ex + 0.3, ey + 2.05, "Edges cache  (crates/bevy_ecs/src/archetype.rs)",
            fontsize=11, weight="bold", color=PURP, ha="left")
    ax.text(ex + 0.3, ey + 1.45,
            "Archetype.edges.insert_bundle[BundleId] -> target ArchetypeId   "
            "(first time computed & cached; subsequent hits O(1))",
            fontsize=10, color=INK, ha="left")
    ax.text(ex + 0.3, ey + 0.65,
            "Without cache:  each add = set union + hash lookup of component combination.\n"
            "With cache:     each add (same archetype + same bundle) = one SparseArray lookup, O(1).",
            fontsize=9.8, color=INK, ha="left", va="center")

    fig.savefig(os.path.join(OUT, "fig-p2_08_03-archetype-migration.png"))
    plt.close(fig)
    print("  saved fig-p2_08_03-archetype-migration.png")


if __name__ == "__main__":
    print("Generating P2-08 figures ...")
    fig1_archetype_grouping()
    fig2_archetype_vs_sparse_set()
    fig3_archetype_migration()
    print("Done.")
