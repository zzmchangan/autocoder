"""P4-13 资源管理:加载/异步/引用计数 配图生成脚本.

三张图:
  fig-p4_13_01-handle-refcount.png  资产句柄 + 引用计数(System 持 Handle,缓存计数,归零释放)
  fig-p4_13_02-async-pipeline.png   异步加载流水线(请求队列->后台 IO 线程->主循环创 GPU 资源)
  fig-p4_13_03-dependency-graph.png 资产依赖图(模型->贴图/骨骼/动画,等待依赖)

风格:纯英文标注,固定配色,去上右边框,bbox='tight'.
"""

import os
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

# ---------- 固定 rcParams ----------
plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 11,
    "axes.titlesize": 13,
    "axes.labelsize": 11,
    "savefig.dpi": 150,
    "figure.dpi": 110,
})

# 固定配色
C_STRONG = "#2563eb"   # 蓝 strong handle / 主路径
C_WEAK = "#9ca3af"     # 灰 weak(uuid) / 不阻止释放
C_CACHE = "#16a34a"    # 绿 Assets cache / 命中
C_DROP = "#dc2626"     # 红 drop / 释放
C_IO = "#a21caf"       # 紫 后台 IO 线程
C_GPU = "#ea580c"      # 橙 GPU / 主循环
C_BG = "#f8fafc"       # 浅底
C_EDGE = "#334155"     # 边框 / 箭头


def _despine(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def _box(ax, x, y, w, h, text, fc, ec=C_EDGE, fontsize=10, fontweight="normal", tc="black"):
    box = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                         fc=fc, ec=ec, linewidth=1.4)
    ax.add_patch(box)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fontsize, fontweight=fontweight, color=tc, wrap=True)


def _arrow(ax, x1, y1, x2, y2, color=C_EDGE, style="->", lw=1.4, text=None, tc=C_EDGE, rad=0.0):
    cs = f"arc3,rad={rad}" if rad else "arc3"
    a = FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style,
                        mutation_scale=14, color=color, lw=lw,
                        connectionstyle=cs, shrinkA=2, shrinkB=2)
    ax.add_patch(a)
    if text:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        ax.text(mx, my, text, color=tc, fontsize=9, ha="center", va="center",
                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85))


# ===================================================================
# 图 1: 资产句柄 + 引用计数
# ===================================================================
def fig1_handle_refcount(path):
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax.set_xlim(0, 11)
    ax.set_ylim(0, 6.5)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Asset Handle + Reference Counting", pad=12, fontweight="bold")

    # 左侧:三个 System 各持一个 Handle, 都指向同一资产
    ax.text(1.7, 6.15, "Systems hold Handles, NOT asset data", ha="center",
            fontsize=10, style="italic", color="#475569")

    _box(ax, 0.4, 4.7, 2.6, 1.0, "Sprite System\nHandle::Strong\n(clone of h)",
         "#dbeafe", fontsize=9.5)
    _box(ax, 0.4, 3.3, 2.6, 1.0, "UI System\nHandle::Strong\n(clone of h)",
         "#dbeafe", fontsize=9.5)
    _box(ax, 0.4, 1.9, 2.6, 1.0, "Texture Cache\nHandle::Strong\n(clone of h)",
         "#dbeafe", fontsize=9.5)

    # 中间:Handle 是 Arc<StrongHandle>, Arc 引用计数 = 3
    _box(ax, 4.0, 3.2, 3.0, 1.6,
         "Handle::Strong\nArc<StrongHandle>\n\nstrong_count = 3\nindex = AssetIndex{...}",
         "#dcfce7", fontsize=9.5, fontweight="bold")

    # 三个箭头 from systems to handle
    for sy in (5.2, 3.8, 2.4):
        _arrow(ax, 3.0, sy, 4.0, 4.0, color=C_STRONG, lw=1.6)

    # 右侧:Assets<Image> 全局缓存
    _box(ax, 8.2, 2.6, 2.6, 2.8,
         "Assets<Image>\n(global cache)\n\n[0] Image { pixels }\n[1] Image { ... }\n[2] hero.png  <-\n[3] ...",
         "#fef3c7", fontsize=9, fontweight="bold")

    # handle -> cache index
    _arrow(ax, 7.0, 4.0, 8.2, 4.0, color=C_CACHE, lw=1.8,
           text="index lookup  O(1)", tc=C_CACHE)

    # 底部:释放流程说明
    ax.text(5.5, 1.2,
            "Each Handle::Strong clone  =>  strong_count += 1\n"
            "Each clone dropped         =>  strong_count -= 1\n"
            "strong_count hits 0        =>  DropEvent sent to channel\n"
            "track_assets system drains =>  asset freed + index recycled",
            ha="center", va="center", fontsize=9.2, color="#334155",
            bbox=dict(boxstyle="round,pad=0.5", fc=C_BG, ec=C_EDGE, lw=1.0))

    # 右下角:Handle::Uuid 对照(弱)
    _box(ax, 8.4, 0.25, 2.6, 0.8,
         "Handle::Uuid  (stable id)\ndrop does NOT free asset",
         "#f1f5f9", fontsize=8.5, tc="#475569")

    plt.tight_layout()
    plt.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ===================================================================
# 图 2: 异步加载流水线
# ===================================================================
def fig2_async_pipeline(path):
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 6)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Async Asset Load Pipeline (never blocks the 16 ms frame)", pad=12, fontweight="bold")

    # 上半部:主循环那一帧(16ms budget) 不被阻塞
    _box(ax, 0.3, 4.8, 3.0, 0.9,
         "Main Loop frame\ninput -> update -> render\n(budget: 16 ms)", C_GPU, fontsize=9.5,
         fontweight="bold", tc="white")

    # system 调 asset_server.load -> 立即返回 Handle
    _box(ax, 4.2, 4.8, 3.4, 0.9,
         "System calls\nasset_server.load(\"hero.png\")\n=> returns Handle IMMEDIATELY",
         "#dbeafe", fontsize=9)
    _arrow(ax, 3.3, 5.25, 4.2, 5.25, color=C_GPU, lw=1.6)

    # 主循环继续, 不等
    _box(ax, 8.4, 4.8, 3.3, 0.9,
         "Main loop keeps running\nframe N, N+1, N+2 ...  (no stall)",
         "#fed7aa", fontsize=9)
    _arrow(ax, 7.6, 5.25, 8.4, 5.25, color=C_EDGE, lw=1.6)

    # 下半部:后台 IO 线程的流水线
    ax.text(6, 3.95, "Background (IoTaskPool, separate thread)  ----  runs across many frames",
            ha="center", fontsize=9.3, style="italic", color=C_IO, fontweight="bold")

    # 三段流水线
    _box(ax, 0.4, 2.4, 3.0, 1.2,
         "1. AssetReader::read(path)\nread raw bytes from disk\n(File / Embedded / Web)",
         "#f3e8ff", fontsize=9)
    _box(ax, 4.0, 2.4, 3.0, 1.2,
         "2. AssetLoader::load(bytes)\ndecode (PNG -> pixels,\ngltf -> meshes ...)",
         "#f3e8ff", fontsize=9)
    _box(ax, 7.6, 2.4, 3.6, 1.2,
         "3. Assets::insert(id, asset)\nstore value, emit\nAssetEvent::Added",
         "#dcfce7", fontsize=9)

    _arrow(ax, 3.4, 3.0, 4.0, 3.0, color=C_IO, lw=1.6)
    _arrow(ax, 7.0, 3.0, 7.6, 3.0, color=C_IO, lw=1.6)

    # load 请求 -> 进入后台队列
    _arrow(ax, 5.9, 4.8, 5.9, 3.65, color=C_IO, lw=1.4, style="->")
    ax.text(6.05, 4.2, "enqueue", fontsize=8.5, color=C_IO, ha="left")

    # 完成通知:AssetEvent 回到主循环
    _box(ax, 4.0, 0.7, 4.0, 1.1,
         "AssetEvent::Added / LoadedWithDependencies\n=> main-loop systems can now use it",
         "#fef3c7", fontsize=9)
    _arrow(ax, 9.4, 2.4, 7.0, 1.8, color=C_CACHE, lw=1.5, rad=-0.15)

    # 左下注解:GPU 资源创建仍在主线程/渲染线程
    _box(ax, 0.2, 0.5, 3.5, 1.3,
         "GPU resource (texture)\ncreation happens on the\nrender thread, NOT here\n(vulkan/metal requires it)",
         "#fee2e2", fontsize=8.8, tc=C_DROP)

    plt.tight_layout()
    plt.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ===================================================================
# 图 3: 资产依赖图
# ===================================================================
def fig3_dependency_graph(path):
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax.set_xlim(0, 11)
    ax.set_ylim(0, 6.5)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Asset Dependency Graph: model waits for its dependencies", pad=12, fontweight="bold")

    # 顶层:模型资产(还在加载,等依赖)
    _box(ax, 3.8, 5.0, 3.4, 1.1,
         "Gltf model:  hero.gltf\nstatus: LoadingWithDependencies\n(not usable until ALL deps ready)",
         "#dbeafe", fontsize=9.3, fontweight="bold")

    # 中层:GltfLoader 解析时发现的依赖
    deps = [
        (0.3, 3.0, "Mesh data\n(hero.mesh)"),
        (2.6, 3.0, "Skeleton\n(hero.skel)"),
        (4.9, 3.0, "Anim clip\n(idle.anim)"),
        (7.2, 3.0, "Anim clip\n(run.anim)"),
        (9.5, 3.0, "Material\n(hero.mat)"),
    ]
    for x, y, t in deps:
        _box(ax, x, y, 1.7, 1.1, t, "#f3e8ff", fontsize=8.8)

    # model -> deps 箭头(等待)
    for x, _, _ in deps:
        _arrow(ax, 5.5, 5.0, x + 0.85, 4.1, color=C_IO, lw=1.2, rad=0.05)

    ax.text(5.5, 4.55, "depends on (loads first)", ha="center", fontsize=9,
            color=C_IO, style="italic",
            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85))

    # 底层:Material 又依赖贴图(二级依赖)
    tex_deps = [
        (6.0, 1.0, "albedo.png\n(4K, ~30 MB)"),
        (8.2, 1.0, "normal.png\n(4K, ~30 MB)"),
    ]
    for x, y, t in tex_deps:
        _box(ax, x, y, 1.9, 1.0, t, "#fef3c7", fontsize=8.6)

    # material -> textures
    for x, _, _ in tex_deps:
        _arrow(ax, 10.0, 3.0, x + 0.95, 2.0, color=C_CACHE, lw=1.1, rad=-0.1)

    # 右侧说明:LoadedWithDependencies
    _box(ax, 0.2, 0.8, 5.3, 1.8,
         "How 'loaded' is decided:\n"
         "  AssetEvent::Added           = bytes parsed\n"
         "  AssetEvent::LoadedWithDeps  = this asset AND every\n"
         "                                recursive dep is ready\n"
         "Systems gate on the latter to avoid half-loaded assets.",
         "#f8fafc", fontsize=8.8, ec=C_EDGE)

    plt.tight_layout()
    plt.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    out_dir = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(out_dir)
    target = os.path.join(parent, "images")
    os.makedirs(target, exist_ok=True)
    names = [
        "fig-p4_13_01-handle-refcount.png",
        "fig-p4_13_02-async-pipeline.png",
        "fig-p4_13_03-dependency-graph.png",
    ]
    fig1_handle_refcount(os.path.join(target, names[0]))
    fig2_async_pipeline(os.path.join(target, names[1]))
    fig3_dependency_graph(os.path.join(target, names[2]))
    print("generated:")
    for n in names:
        print("  ", os.path.join(target, n))
