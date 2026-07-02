# -*- coding: utf-8 -*-
"""为《游戏引擎设计与实现深入浅出》附录 A (源码阅读路线图) 生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_appendix_a_figures.py

Figures:
  fig-appA_01-entt-source-map.png   EnTT 源码模块地图: src/entt/entity/ 各文件 +
                                    依赖层次(entity -> sparse_set -> storage ->
                                    registry -> view/group -> organizer)
  fig-appA_02-bevy-source-map.png   Bevy 源码模块地图: crates/ 各 crate 分层 +
                                    依赖(bevy_app 主循环 -> bevy_ecs 世界/查询 ->
                                    bevy_time/hierarchy/asset/render/input)
  fig-appA_03-reading-flow.png      推荐阅读顺序流程: 先 EnTT entity->sparse_set->
                                    storage->registry->view->group->organizer,
                                    再 Bevy app->ecs->time->relationship->
                                    asset->render->input
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch

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

# 固定配色 (与全系列一致)
RED    = (0.86, 0.18, 0.18)
GREEN  = (0.20, 0.70, 0.32)
BLUE   = (0.20, 0.45, 0.88)
AMBER  = (0.95, 0.70, 0.18)
GREY   = (0.78, 0.78, 0.80)
DGREY  = (0.55, 0.55, 0.60)
INK    = (0.10, 0.10, 0.10)
SOFT   = (0.96, 0.96, 0.97)
LBLUE  = (0.90, 0.94, 1.0)
LGREEN = (0.88, 0.94, 0.88)
LAMBER = (1.0, 0.94, 0.82)
LRED   = (1.0, 0.90, 0.90)
PURP   = (0.55, 0.36, 0.74)
LPURP  = (0.94, 0.88, 0.98)
TEAL   = (0.13, 0.58, 0.60)
LTEAL  = (0.86, 0.95, 0.95)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


# ============================================================
# Fig 1: EnTT 源码模块地图
#   把 src/entt/entity/ 的关键文件按"依赖层次"从底到上排:
#     L0 fwd.hpp / entity.hpp        (Entity = ID + entt_traits 位分配)
#     L1 sparse_set.hpp              (pool 基类: sparse[] -> packed[])
#     L2 storage.hpp                 (Component 存储: 继承 sparse_set, 模板特化)
#     L3 registry.hpp                (世界: create/emplace/view, 管所有 storage)
#     L4 view.hpp / group.hpp        (Query: view 多 sparse_set 交集; group 排序)
#     L5 organizer.hpp               (依赖图: 把 System 拓扑排序, 多线程调度)
#   横向: 还有 handle / helper / snapshot / runtime_view / mixin / ranges / component
# ============================================================
def fig1_entt_source_map():
    fig, ax = plt.subplots(figsize=(15.5, 9.6))
    clean_ax(ax)
    ax.set_xlim(0, 31); ax.set_ylim(0, 19)
    ax.set_title("EnTT source map:  src/entt/entity/  --  dependency layers (bottom-up)",
                 fontsize=14, weight="bold", color=INK, loc="left", pad=12)

    # 左侧层级标尺
    layers = [
        ("L5", "organizer.hpp",          "dependency graph: topological sort of systems, multi-thread scheduling", PURP, LPURP),
        ("L4", "view.hpp / group.hpp",   "Query:  view = intersection of sparse sets;  group = sorted, faster",      GREEN, LGREEN),
        ("L3", "registry.hpp",           "World:  create / emplace / view / group;  owns all storage pools",         BLUE, LBLUE),
        ("L2", "storage.hpp",            "Component pool:  extends sparse_set, templated <Entity, Component, Type>",  AMBER, LAMBER),
        ("L1", "sparse_set.hpp",         "Pool base:  sparse[entity] -> index into packed[];  O(1) add/remove",       RED, LRED),
        ("L0", "entity.hpp / fwd.hpp",   "Entity = id;  entt_traits splits bits into entity_mask + version_mask",     TEAL, LTEAL),
    ]

    # 主干方块 (居中一列, 从下到上)
    col_x = 4.5
    box_w = 14.5
    box_h = 2.05
    gap = 0.45
    bottom_y = 1.0

    centers = {}
    for i, (lv, fname, desc, col, lcol) in enumerate(layers):
        y = bottom_y + (len(layers) - 1 - i) * (box_h + gap)
        # box
        ax.add_patch(FancyBboxPatch((col_x, y), box_w, box_h,
                                    boxstyle="round,pad=0.02,rounding_size=0.10",
                                    facecolor=lcol, edgecolor=col, linewidth=2.0))
        # layer tag
        ax.add_patch(Rectangle((col_x + 0.18, y + 0.18), 1.3, box_h - 0.36,
                               facecolor=col, edgecolor=col))
        ax.text(col_x + 0.83, y + box_h / 2, lv,
                ha="center", va="center", fontsize=12, weight="bold", color="white")
        # filename
        ax.text(col_x + 1.7, y + box_h - 0.55, fname,
                ha="left", va="center", fontsize=12.5, weight="bold", color=INK)
        # description
        ax.text(col_x + 1.7, y + 0.55, desc,
                ha="left", va="center", fontsize=9.8, color=DGREY)
        centers[lv] = (col_x, col_x + box_w, y + box_h / 2)

    # 依赖箭头: L0 -> L1 -> L2 -> L3 -> L4 -> L5 (中间竖线)
    for a, b in [("L0", "L1"), ("L1", "L2"), ("L2", "L3"), ("L3", "L4"), ("L4", "L5")]:
        _, _, ya = centers[a]
        _, _, yb = centers[b]
        ax.annotate("", xy=(col_x + box_w / 2, yb - box_h / 2 - 0.05),
                    xytext=(col_x + box_w / 2, ya + box_h / 2 + 0.05),
                    arrowprops=dict(arrowstyle="-|>", color=DGREY, lw=1.6))

    # 右侧: 横向卫星文件 (registry 同时是 view/group/organizer 的依赖, 也连 handle/helper/snapshot 等)
    sat_x = 20.8
    sat_w = 9.6
    sat_h = 1.5
    satellites = [
        ("handle.hpp",        "Entity + Registry reference pair (operate on one entity)", GREY,  16.0),
        ("helper.hpp",        "Convenience tools: snapshot helpers / dependencies",      GREY,  14.2),
        ("snapshot.hpp",      "Serialization: dump/load world's entity + component data", PURP,  12.4),
        ("runtime_view.hpp",  "Runtime Query: type-erased view (lookup components at runtime)", GREEN, 10.6),
        ("mixin.hpp",         "storage mixin: layer extra behavior onto a pool (polymorphic)", AMBER, 8.8),
        ("component.hpp",     "Component base utilities / type traits",                  TEAL,  7.0),
        ("ranges.hpp",        "Expose view/group as ranges (range-v3 style)",            BLUE,  5.2),
    ]
    for fname, desc, col, y in satellites:
        ax.add_patch(FancyBboxPatch((sat_x, y), sat_w, sat_h,
                                    boxstyle="round,pad=0.02,rounding_size=0.08",
                                    facecolor=SOFT, edgecolor=col, linewidth=1.2))
        ax.text(sat_x + 0.25, y + sat_h - 0.4, fname,
                ha="left", va="center", fontsize=10.5, weight="bold", color=col)
        ax.text(sat_x + 0.25, y + 0.35, desc,
                ha="left", va="center", fontsize=8.8, color=DGREY)

    # registry -> 卫星 (虚线, 表示 registry 是它们的协作中枢)
    _, reg_x2, reg_y = centers["L3"]
    for _, _, _, sy in satellites:
        ax.add_artist(FancyArrowPatch((reg_x2 + 0.1, reg_y), (sat_x - 0.05, sy + sat_h / 2),
                                      arrowstyle="-|>", color=DGREY, lw=0.9,
                                      connectionstyle="arc3,rad=-0.10",
                                      alpha=0.55, linestyle=":"))

    # 左下: 阅读顺序提示
    ax.add_patch(FancyBboxPatch((0.5, 0.3), 3.6, 17.0,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=LBLUE, edgecolor=BLUE, linewidth=1.2))
    ax.text(2.3, 16.8, "READ", ha="center", fontsize=11, weight="bold", color=BLUE)
    ax.text(2.3, 16.2, "ORDER", ha="center", fontsize=11, weight="bold", color=BLUE)
    ax.text(2.3, 15.4, "(bottom\n-> top)", ha="center", fontsize=9, color=DGREY, style="italic")
    order = ["1 entity", "2 sparse_set", "3 storage", "4 registry", "5 view/group", "6 organizer"]
    oy = 13.5
    for i, s in enumerate(order):
        ax.text(2.3, oy - i * 1.4, s, ha="center", fontsize=10, color=INK, weight="bold")

    # 底部说明
    ax.text(15.5, 0.15,
            "header-only C++ ECS.  Read bottom-up: each layer is built ON the one below it.  "
            "(Minecraft, Roblox-use EnTT under the hood)",
            ha="center", va="center", fontsize=9.5, color=DGREY, style="italic")

    fig.savefig(os.path.join(OUT, "fig-appA_01-entt-source-map.png"))
    plt.close(fig)
    print("  saved fig-appA_01-entt-source-map.png")


# ============================================================
# Fig 2: Bevy 源码模块地图
#   crates/ 按"从内核到外层"分四层:
#     L0 bevy_ecs / bevy_ptr / bevy_utils    ECS 内核 (archetype/table/world/query)
#     L1 bevy_app                            主循环 + schedule + plugin (调度层)
#     L2 bevy_time / bevy_transform /        横切: 时间/变换/资产/层级关系
#        bevy_asset / relationship(in ecs)
#     L3 bevy_render / bevy_input /          子系统层 (承渲染管线/输入)
#        bevy_audio / bevy_scene ...
# ============================================================
def fig2_bevy_source_map():
    fig, ax = plt.subplots(figsize=(15.5, 9.8))
    clean_ax(ax)
    ax.set_xlim(0, 31); ax.set_ylim(0, 19.5)
    ax.set_title("Bevy source map:  crates/  --  layered (ECS core inside, subsystems outside)",
                 fontsize=14, weight="bold", color=INK, loc="left", pad=12)

    # ---- L0: ECS core (最里, 居中底) ----
    l0_y = 1.2
    l0_h = 2.4
    ax.add_patch(FancyBboxPatch((1.0, l0_y), 29.0, l0_h,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LBLUE, edgecolor=BLUE, linewidth=2.0))
    ax.text(1.4, l0_y + l0_h - 0.45, "L0  ECS Core",
            ha="left", va="center", fontsize=11.5, weight="bold", color=BLUE)

    l0_crates = [
        ("bevy_ecs",   "World / Entity / Component / Query / Schedule / Archetype / Table / relationship/"),
        ("bevy_ptr",   "Ptr / PtrMut -- safe thin pointer wrappers (low-overhead pointers used inside ECS)"),
        ("bevy_utils", "Hash / FixedBitset / general utilities (reused by every crate)"),
    ]
    for i, (name, desc) in enumerate(l0_crates):
        cx = 1.4 + i * 9.7
        ax.add_patch(Rectangle((cx, l0_y + 0.25), 9.3, 1.2,
                               facecolor="white", edgecolor=BLUE, linewidth=1.0))
        ax.text(cx + 4.65, l0_y + 1.15, name,
                ha="center", va="center", fontsize=10.5, weight="bold", color=BLUE)
        ax.text(cx + 4.65, l0_y + 0.55, desc,
                ha="center", va="center", fontsize=8.3, color=DGREY)

    # ---- L1: App framework (调度层) ----
    l1_y = 4.3
    l1_h = 2.4
    ax.add_patch(FancyBboxPatch((1.0, l1_y), 29.0, l1_h,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LGREEN, edgecolor=GREEN, linewidth=2.0))
    ax.text(1.4, l1_y + l1_h - 0.45, "L1  App Framework  (the main loop lives here)",
            ha="left", va="center", fontsize=11.5, weight="bold", color=GREEN)

    l1_crates = [
        ("bevy_app", "App / Main schedule (First->PreUpdate->RunFixedMainLoop->Update->SpawnScene->PostUpdate->Last) / Plugin / SubApp"),
        ("bevy_tasks", "task pool: async-executor-based multi-threaded job system (data parallel)"),
        ("bevy_state", "state machine: State / StateTransition schedule (menu / in-game switching)"),
    ]
    for i, (name, desc) in enumerate(l1_crates):
        cx = 1.4 + i * 9.7
        ax.add_patch(Rectangle((cx, l1_y + 0.25), 9.3, 1.2,
                               facecolor="white", edgecolor=GREEN, linewidth=1.0))
        ax.text(cx + 4.65, l1_y + 1.15, name,
                ha="center", va="center", fontsize=10.5, weight="bold", color=GREEN)
        ax.text(cx + 4.65, l1_y + 0.55, desc,
                ha="center", va="center", fontsize=8.3, color=DGREY)

    # ---- L2: 横切资源/时间/变换 ----
    l2_y = 7.4
    l2_h = 2.4
    ax.add_patch(FancyBboxPatch((1.0, l2_y), 29.0, l2_h,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LAMBER, edgecolor=AMBER, linewidth=2.0))
    ax.text(1.4, l2_y + l2_h - 0.45, "L2  Cross-cutting:  time / transform / asset / relationship",
            ha="left", va="center", fontsize=11.5, weight="bold", color=(0.75, 0.50, 0.05))

    l2_crates = [
        ("bevy_time", "Time<Virtual> / Time<Fixed> -- fixed.rs accumulator (FixedUpdate 64Hz default)"),
        ("bevy_transform", "Transform / GlobalTransform -- parent->child matrix propagation (PostUpdate)"),
        ("bevy_asset", "Handle / AssetServer -- async load + refcount (does not block main loop)"),
    ]
    for i, (name, desc) in enumerate(l2_crates):
        cx = 1.4 + i * 9.7
        ax.add_patch(Rectangle((cx, l2_y + 0.25), 9.3, 1.2,
                               facecolor="white", edgecolor=AMBER, linewidth=1.0))
        ax.text(cx + 4.65, l2_y + 1.15, name,
                ha="center", va="center", fontsize=10.5, weight="bold", color=(0.75, 0.50, 0.05))
        ax.text(cx + 4.65, l2_y + 0.55, desc,
                ha="center", va="center", fontsize=8.3, color=DGREY)

    # 一个内嵌小框: ChildOf 关系其实住在 bevy_ecs
    ax.add_patch(FancyBboxPatch((20.5, l2_y - 0.95), 9.0, 0.85,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=LTEAL, edgecolor=TEAL, linewidth=1.2))
    ax.text(25.0, l2_y - 0.52,
            "hierarchy = ChildOf relationship in bevy_ecs/src/relationship/  (no separate crate)",
            ha="center", va="center", fontsize=8.6, color=TEAL, weight="bold")

    # ---- L3: 子系统 ----
    l3_y = 10.5
    l3_h = 7.8
    ax.add_patch(FancyBboxPatch((1.0, l3_y), 29.0, l3_h,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LPURP, edgecolor=PURP, linewidth=2.0))
    ax.text(1.4, l3_y + l3_h - 0.45, "L3  Subsystems  (built on top of ECS + App)",
            ha="left", va="center", fontsize=11.5, weight="bold", color=PURP)

    # subsystem grid (3 cols x 3 rows-ish)
    subs = [
        ("bevy_render",       "render graph / extract -> prepare -> queue -> render\n(separate SubApp, pipelined rendering)", RED),
        ("bevy_input",        "Keyboard / Mouse / Gamepad -- read OS events into a\nMessages resource (PreUpdate)",            TEAL),
        ("bevy_core_pipeline","camera / clear / viewport and other basic render passes",                                       GREY),
        ("bevy_pbr",          "PBR materials / lighting / shadows (physically based)",                                          AMBER),
        ("bevy_sprite",       "2D sprites / tilemaps / animation",                                                               GREEN),
        ("bevy_audio",        "audio playback (audio assets + audio source components)",                                       BLUE),
        ("bevy_scene",        "scene serialization / dynamic spawn (SpawnScene schedule)",                                      PURP),
        ("bevy_winit",        "window + OS event loop (winit backend)",                                                          DGREY),
        ("bevy_diagnostic",   "frame rate / performance counter diagnostics",                                                    RED),
    ]
    col_n = 3
    cell_w = 9.3
    cell_h = 2.05
    sx0 = 1.4
    sy0 = l3_y + l3_h - 0.7 - cell_h
    for i, (name, desc, col) in enumerate(subs):
        r = i // col_n
        c = i % col_n
        cx = sx0 + c * (cell_w + 0.18)
        cy = sy0 - r * (cell_h + 0.18)
        ax.add_patch(Rectangle((cx, cy), cell_w, cell_h,
                               facecolor="white", edgecolor=col, linewidth=1.2))
        ax.text(cx + cell_w / 2, cy + cell_h - 0.4, name,
                ha="center", va="center", fontsize=10.3, weight="bold", color=col)
        ax.text(cx + cell_w / 2, cy + 0.7, desc,
                ha="center", va="center", fontsize=8.4, color=INK)

    # 层与层之间的依赖箭头 (L0 -> L1 -> L2 -> L3, 中间一条粗箭头)
    for (ya, yb) in [(l0_y + l0_h, l1_y), (l1_y + l1_h, l2_y), (l2_y + l2_h, l3_y)]:
        ax.annotate("", xy=(15.5, yb + 0.05),
                    xytext=(15.5, ya - 0.05),
                    arrowprops=dict(arrowstyle="-|>", color=DGREY, lw=2.0))

    # 右上角: 阅读顺序提示
    ax.add_patch(FancyBboxPatch((1.0, 18.5), 29.0, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=SOFT, edgecolor=DGREY, linewidth=1.0))
    ax.text(15.5, 18.95,
            "Read INSIDE-OUT:  L0 bevy_ecs (archetype/table/query)  ->  L1 bevy_app (main_schedule.rs)  "
            "->  L2 time/asset  ->  L3 subsystem you care about",
            ha="center", va="center", fontsize=10.2, color=INK, weight="bold")

    fig.savefig(os.path.join(OUT, "fig-appA_02-bevy-source-map.png"))
    plt.close(fig)
    print("  saved fig-appA_02-bevy-source-map.png")


# ============================================================
# Fig 3: 推荐阅读顺序流程
#   左半 EnTT (自下而上), 右半 Bevy (自内而外), 中间一个桥接
#   读 EnTT 解决"数据布局怎么落地", 读 Bevy 解决"整体架构怎么搭"
# ============================================================
def fig3_reading_flow():
    fig, ax = plt.subplots(figsize=(15.5, 9.2))
    clean_ax(ax)
    ax.set_xlim(0, 31); ax.set_ylim(0, 18)
    ax.set_title("Recommended reading order:  EnTT first (data layout),  then Bevy (whole architecture)",
                 fontsize=13.5, weight="bold", color=INK, loc="left", pad=12)

    # ----- 左半: EnTT 流程 (自下而上 6 步) -----
    ax.add_patch(FancyBboxPatch((0.5, 0.5), 14.2, 17.0,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LBLUE, edgecolor=BLUE, linewidth=1.6))
    ax.text(7.6, 16.9, "EnTT (C++ header-only)", ha="center",
            fontsize=12.5, weight="bold", color=BLUE)
    ax.text(7.6, 16.35, "focus:  how data is LAID OUT", ha="center",
            fontsize=10, color=DGREY, style="italic")

    entt_steps = [
        ("1. entity.hpp",        "Entity = id;  entt_traits splits bits\n(entity_mask + version_mask)", TEAL),
        ("2. sparse_set.hpp",    "Pool base: sparse[id] -> packed[]\nO(1) add/remove, iterate packed",   RED),
        ("3. storage.hpp",       "Component pool extends sparse_set\ntemplated <Entity, Component>",     AMBER),
        ("4. registry.hpp",      "World: create / emplace / view\ndispatches to per-type storage",       BLUE),
        ("5. view.hpp + group.hpp", "Query: view = intersection\n(group = sorted, archetype-like fast path)", GREEN),
        ("6. organizer.hpp",     "Dependency graph: topological sort\nsystems -> multi-thread schedule", PURP),
    ]
    bx = 1.2; bw = 12.8; bh = 2.05; bgap = 0.35
    by0 = 13.4
    prev_cx = None
    for i, (title, desc, col) in enumerate(entt_steps):
        y = by0 - i * (bh + bgap)
        ax.add_patch(FancyBboxPatch((bx, y), bw, bh,
                                    boxstyle="round,pad=0.02,rounding_size=0.08",
                                    facecolor="white", edgecolor=col, linewidth=1.6))
        ax.text(bx + 0.3, y + bh - 0.45, title,
                ha="left", va="center", fontsize=10.5, weight="bold", color=col)
        ax.text(bx + 0.3, y + 0.55, desc,
                ha="left", va="center", fontsize=9.0, color=INK)
        cx = bx + bw / 2
        if prev_cx is not None:
            ax.annotate("", xy=(cx, y + bh + 0.02),
                        xytext=(cx, prev_cx - 0.02),
                        arrowprops=dict(arrowstyle="-|>", color=BLUE, lw=1.4))
        prev_cx = y

    # ----- 中间: 桥接 -----
    ax.add_patch(FancyBboxPatch((14.9, 7.6), 1.4, 3.2,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LAMBER, edgecolor=AMBER, linewidth=1.6))
    ax.text(15.6, 9.2, "then\nbridge", ha="center", va="center",
            fontsize=9.5, color=(0.75,0.50,0.05), weight="bold")
    # 大箭头从 EnTT 顶 -> Bevy 底
    ax.annotate("", xy=(16.4, 13.0), xytext=(14.7, 13.0),
                arrowprops=dict(arrowstyle="-|>", color=AMBER, lw=2.2))

    # ----- 右半: Bevy 流程 (自内而外 7 步) -----
    ax.add_patch(FancyBboxPatch((16.5, 0.5), 14.0, 17.0,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LGREEN, edgecolor=GREEN, linewidth=1.6))
    ax.text(23.5, 16.9, "Bevy (Rust)", ha="center",
            fontsize=12.5, weight="bold", color=GREEN)
    ax.text(23.5, 16.35, "focus:  how the WHOLE engine is wired", ha="center",
            fontsize=10, color=DGREY, style="italic")

    bevy_steps = [
        ("1. bevy_app/main_schedule.rs", "Main schedule = First -> PreUpdate ->\nRunFixedMainLoop -> Update -> PostUpdate -> Last", GREEN),
        ("2. bevy_app/schedule.rs",      "Schedule = DAG (hierarchy + dependency)\nexecutor: multi/single-threaded",                 BLUE),
        ("3. bevy_ecs/archetype.rs + table", "Archetype (metadata) + Table (column data)\nEdges cache = archetype graph",            TEAL),
        ("4. bevy_ecs/world/ + query/",  "World owns entities/components\nQuery matches archetype via ComponentIndex",                PURP),
        ("5. bevy_time/fixed.rs",        "Time<Fixed> accumulator ->\nFixedUpdate runs 0..n times per frame",                         AMBER),
        ("6. bevy_ecs/relationship/ + bevy_transform", "ChildOf relationship ->\nGlobalTransform propagation",                        RED),
        ("7. bevy_asset + bevy_render + bevy_input", "subsystem layer:\npick the one you care about",                                 DGREY),
    ]
    bx2 = 17.1; bw2 = 12.8; bh2 = 1.75; bgap2 = 0.30
    by2 = 14.5
    prev_y = None
    for i, (title, desc, col) in enumerate(bevy_steps):
        y = by2 - i * (bh2 + bgap2)
        ax.add_patch(FancyBboxPatch((bx2, y), bw2, bh2,
                                    boxstyle="round,pad=0.02,rounding_size=0.08",
                                    facecolor="white", edgecolor=col, linewidth=1.6))
        ax.text(bx2 + 0.3, y + bh2 - 0.42, title,
                ha="left", va="center", fontsize=10.0, weight="bold", color=col)
        ax.text(bx2 + 0.3, y + 0.45, desc,
                ha="left", va="center", fontsize=8.8, color=INK)
        cx = bx2 + bw2 / 2
        if prev_y is not None:
            ax.annotate("", xy=(cx, y + bh2 + 0.02),
                        xytext=(cx, prev_y - 0.02),
                        arrowprops=dict(arrowstyle="-|>", color=GREEN, lw=1.4))
        prev_y = y

    # 底部 takeaway
    ax.add_patch(FancyBboxPatch((0.5, -0.6), 30.0, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=SOFT, edgecolor=DGREY, linewidth=1.0))
    ax.text(15.5, -0.12,
            "EnTT teaches the storage primitive (sparse_set / archetype) in a small header-only scope.  "
            "Bevy shows how a full engine wires those primitives into a schedule, subsystems, and rendering.",
            ha="center", va="center", fontsize=9.8, color=INK, weight="bold")

    fig.savefig(os.path.join(OUT, "fig-appA_03-reading-flow.png"))
    plt.close(fig)
    print("  saved fig-appA_03-reading-flow.png")


if __name__ == "__main__":
    print("Generating Appendix A figures ...")
    fig1_entt_source_map()
    fig2_bevy_source_map()
    fig3_reading_flow()
    print("Done.")
