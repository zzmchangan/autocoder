# -*- coding: utf-8 -*-
"""Generate PNG figures for P4-15 (Script Binding & Cross-Language: C# and IL2CPP).

Four figures:
  fig-p4_15_01-il2cpp-pipeline.png   C# -> IL -> IL2CPP -> C++ -> native machine code
  fig-p4_15_02-il2cpp-vs-jvm.png     IL2CPP (AOT via C++) vs JVM (JIT) vs NativeAOT
  fig-p4_15_03-gc-boundary.png       GC managed heap vs native memory across P/Invoke
  fig-p4_15_04-codegen-boilerplate.png  Raw C# vs the C++ IL2CPP emits (boilerplate)

Style follows the series conventions: English labels, fixed palette,
bbox='tight', top/right spines removed.
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch, Polygon, Circle

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

# Fixed palette (series-wide, same as P4-14)
RED   = (0.86, 0.18, 0.18)
GREEN = (0.20, 0.70, 0.32)
BLUE  = (0.20, 0.45, 0.88)
AMBER = (0.95, 0.70, 0.18)
GREY  = (0.78, 0.78, 0.80)
DGREY = (0.62, 0.62, 0.66)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)
PURP  = (0.55, 0.36, 0.74)
LBLUE = (0.92, 0.95, 1.0)
LGREEN = (0.90, 0.95, 0.90)
LAMBER = (1.0, 0.94, 0.84)
LRED  = (1.0, 0.92, 0.92)
LPURP = (0.95, 0.90, 0.99)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=12, weight="normal", tc=INK):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=fc, edgecolor=ec, linewidth=1.4))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.6, style="-|>", rad=0.0, ms=14, ls="-"):
    cs = f"arc3,rad={rad}" if rad else "arc3,rad=0"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle=style, color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=ms,
                                  linestyle=ls))


# ============================================================ fig 01
# IL2CPP pipeline: C# -> IL -> IL2CPP -> C++ -> platform compiler -> native binary.
# Shows the AOT chain and where it diverges from JIT (Mono).
def fig_il2cpp_pipeline():
    fig, ax = plt.subplots(figsize=(14.2, 8.8))
    clean_ax(ax)
    ax.set_xlim(0, 14.2)
    ax.set_ylim(0, 8.8)

    ax.text(7.1, 8.4,
            "IL2CPP pipeline: C# to IL to C++ to native (all at build time)",
            ha="center", fontsize=15, weight="bold", color=INK)
    ax.text(7.1, 7.96,
            "The whole chain runs AOT — no runtime code generation, so it ships on iOS / Switch",
            ha="center", fontsize=11.5, color=DGREY, style="italic")

    # ---- Stage row (5 boxes left to right)
    stages = [
        ("C# source\n(Player.cs)", LBLUE, BLUE),
        ("C# compiler\n(Roslyn)\n-> managed DLL\n(IL bytecode)", LBLUE, BLUE),
        ("IL2CPP translator\n(il2cpp.exe)\nIL -> C++ source", LAMBER, AMBER),
        ("Platform C++ compiler\n(MSVC / Clang / GCC)\n+ libil2cpp runtime", LGREEN, GREEN),
        ("Native binary\n(GameAssembly.dll\n/ libil2cpp.so)", LRED, RED),
    ]
    x = 0.4
    w = 2.55
    gap = 0.18
    centers = []
    y_stage = 5.0
    h_stage = 1.95
    for text, fc, ec in stages:
        box(ax, x, y_stage, w, h_stage, text, fc=fc, ec=ec, fs=10.8, weight="bold")
        centers.append((x, x + w))
        x += w + gap
    # arrows between stages
    for i in range(len(stages) - 1):
        arrow(ax, (centers[i][1], y_stage + h_stage / 2),
              (centers[i + 1][0], y_stage + h_stage / 2),
              color=INK, lw=1.9)

    # ---- When each stage runs (build vs runtime)
    ax.add_patch(FancyBboxPatch((0.4, 3.45), 12.95, 1.0,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=SOFT, edgecolor=GREY, linewidth=1.2,
                                linestyle=":"))
    ax.text(6.9, 4.2,
            "All 4 arrows happen at BUILD time on the developer's machine  ->  pure AOT",
            ha="center", fontsize=11.5, weight="bold", color=INK)
    ax.text(6.9, 3.75,
            "(contrast: Mono JIT does stage 2->machine code INSIDE the running game)",
            ha="center", fontsize=10.5, color=DGREY, style="italic")

    # ---- Two side artifacts
    # global-metadata.dat
    box(ax, 5.05, 1.7, 4.1, 1.35,
        "global-metadata.dat\n(type defs, method sigs, string\nliterals — for reflection)",
        fc=LPURP, ec=PURP, fs=10.3, weight="bold")
    arrow(ax, (3.0, 5.0), (6.0, 3.05), color=PURP, lw=1.5, rad=-0.2, ls="--")
    ax.text(3.7, 3.9, "IL2CPP also emits\nmetadata blob", fontsize=9.5,
            color=PURP, style="italic", ha="center")

    # libil2cpp runtime (GC, type system)
    box(ax, 9.4, 1.7, 4.1, 1.35,
        "libil2cpp runtime\n(GC = Boehm, type system,\nthreading, reflection)",
        fc=LGREEN, ec=GREEN, fs=10.3, weight="bold")
    arrow(ax, (11.0, 5.0), (11.4, 3.05), color=GREEN, lw=1.5, rad=0.15, ls="--")
    ax.text(12.3, 3.9, "statically linked\ninto the binary", fontsize=9.5,
            color=GREEN, style="italic", ha="center")

    # ---- Bottom: the core insight
    ax.add_patch(FancyBboxPatch((0.4, 0.3), 13.1, 1.1,
                                boxstyle="round,pad=0.03,rounding_size=0.08",
                                facecolor=LAMBER, edgecolor=INK, linewidth=1.2))
    ax.text(0.75, 1.08, "Why C++ as the intermediate?", fontsize=11.5,
            weight="bold", color=INK)
    ax.text(0.75, 0.7,
            "Every platform already has a C++ compiler (MSVC / Clang / GCC / console toolchains). "
            "Emit C++, lean on it — IL2CPP gets portability, debuggability and optimizer work for free.",
            fontsize=10.6, color=INK)

    plt.savefig(os.path.join(OUT, "fig-p4_15_01-il2cpp-pipeline.png"))
    plt.close(fig)


# ============================================================ fig 02
# IL2CPP (AOT via C++) vs JVM (JIT) vs .NET NativeAOT — three ways to run IL/bytecode.
# Rebuilt for clarity: one stage per row, all three columns aligned, generous spacing.
def fig_il2cpp_vs_jvm():
    fig, ax = plt.subplots(figsize=(14.0, 9.6))
    clean_ax(ax)
    ax.set_xlim(0, 14.0)
    ax.set_ylim(0, 9.6)

    ax.text(7.0, 9.25,
            "Three ways to run IL / bytecode: IL2CPP vs JVM vs NativeAOT",
            ha="center", fontsize=14.5, weight="bold", color=INK)
    ax.text(7.0, 8.82,
            "Same family of problem (compiled IL + GC + type system), three deployment strategies",
            ha="center", fontsize=11.5, color=DGREY, style="italic")

    # ---- Three columns, generous width
    col_w = 4.35
    col_gap = 0.35
    col_x = [0.4, 0.4 + col_w + col_gap, 0.4 + 2 * (col_w + col_gap)]

    # ---- Column headers
    headers = [
        ("Unity IL2CPP\n(AOT via C++)", LAMBER, AMBER),
        ("JVM / HotSpot\n(JIT at runtime)", LPURP, PURP),
        (".NET NativeAOT\n(direct IL -> native)", LGREEN, GREEN),
    ]
    for i, (t, fc, ec) in enumerate(headers):
        box(ax, col_x[i], 7.55, col_w, 1.0, t, fc=fc, ec=ec, fs=11.5, weight="bold")

    # ---- 3 stage rows, each row = one logical phase, all 3 columns side by side
    # Row layout: compile | ship/run | platform constraint
    row_specs = [
        # (y, label, [text_per_column], fc, ec)
        (6.05, "STEP 1  compile",
         ["C# -> IL\n(Roslyn)",
          "Java -> bytecode\n(javac)",
          "C# -> IL\n(Roslyn)"],
         LBLUE, BLUE),
        (4.55, "STEP 2  translate / ship",
         ["IL -> C++ -> native\n(il2cpp + platform C++\ncompiler, BUILD time)",
          "bytecode shipped as-is\n(no translation)",
          "IL -> native directly\n(RyuJIT backend,\nBUILD time)"],
         LAMBER, AMBER),
        (2.75, "STEP 3  how it runs",
         ["native code + libil2cpp\nruntime, NO codegen\nat runtime",
          "JIT compiles hot methods\n-> machine code\nAT RUNTIME",
          "native code + trimmed\nCoreCLR runtime,\nNO codegen at runtime"],
         LGREEN, GREEN),
    ]
    for y, label, texts, fc, ec in row_specs:
        # row label on far left margin is inside header text instead; draw boxes
        for i, t in enumerate(texts):
            box(ax, col_x[i], y, col_w, 1.25, t, fc=fc, ec=ec, fs=10.3)
        # row label spanning under
        ax.text(7.0, y - 0.18, label, ha="center", fontsize=10.2,
                color=DGREY, style="italic", weight="bold")

    # ---- Platform row (red, the key constraint)
    plat_y = 1.45
    plat_texts = [
        "Ships on iOS / Switch /\nPS5 / Xbox (no JIT)",
        "Cannot ship on iOS /\nSwitch (W^X forbids JIT)",
        "Can ship on iOS / Switch\n(newer, less mature)",
    ]
    for i, t in enumerate(plat_texts):
        box(ax, col_x[i], plat_y, col_w, 0.9, t, fc=LRED, ec=RED, fs=9.8)
    ax.text(7.0, 1.2, "platform fit (the deciding factor)",
            ha="center", fontsize=10.2, color=DGREY, style="italic",
            weight="bold")

    # ---- GC one-liner at bottom
    ax.text(7.0, 0.55,
            "GC: IL2CPP = Boehm (conservative, non-moving)   |   "
            "JVM = G1/ZGC (moving, generational)   |   NativeAOT = CoreCLR GC (moving)",
            ha="center", fontsize=10.0, color=INK)

    plt.savefig(os.path.join(OUT, "fig-p4_15_02-il2cpp-vs-jvm.png"))
    plt.close(fig)


# ============================================================ fig 03
# GC managed heap vs native memory, with the P/Invoke boundary in the middle.
# Shows: C# objects on managed heap (Boehm scans), C++ objects in native heap
# (manual), and the marshalling cost when crossing.
def fig_gc_boundary():
    fig, ax = plt.subplots(figsize=(14.0, 9.4))
    clean_ax(ax)
    ax.set_xlim(0, 14.0)
    ax.set_ylim(0, 9.4)

    ax.text(7.0, 9.05,
            "GC boundary: managed heap vs native memory across P/Invoke",
            ha="center", fontsize=14.5, weight="bold", color=INK)
    ax.text(7.0, 8.62,
            "Two worlds, two lifetimes. Crossing the boundary has a price — keep it blittable, batch it",
            ha="center", fontsize=11.5, color=DGREY, style="italic")

    # ---- Left: C# managed heap (Boehm GC)
    ax.text(3.1, 8.05, "C# managed heap", ha="center", fontsize=13,
            weight="bold", color=BLUE)
    ax.add_patch(FancyBboxPatch((0.5, 4.2), 5.2, 3.5,
                                boxstyle="round,pad=0.04,rounding_size=0.08",
                                facecolor=LBLUE, edgecolor=BLUE, linewidth=1.8))
    # some managed objects
    objs = [
        (0.9, 6.55, "Vector3[1024]\n(managed array)"),
        (3.3, 6.55, "GameObject\nhandle"),
        (0.9, 5.35, "List<Entity>\n(managed)"),
        (3.3, 5.35, "string name"),
    ]
    for ox, oy, t in objs:
        ax.add_patch(FancyBboxPatch((ox, oy), 2.1, 0.95,
                                    boxstyle="round,pad=0.02,rounding_size=0.05",
                                    facecolor="white", edgecolor=BLUE, linewidth=1.2))
        ax.text(ox + 1.05, oy + 0.48, t, ha="center", va="center",
                fontsize=9.5, color=INK)
    ax.text(3.1, 4.55,
            "Boehm GC scans this region\n(conservative, non-moving)",
            ha="center", fontsize=10, color=BLUE, style="italic")

    # GC sweeper
    ax.add_patch(FancyBboxPatch((0.5, 3.1), 5.2, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=LRED, edgecolor=RED, linewidth=1.4))
    ax.text(3.1, 3.58,
            "Boehm GC: stops the world, scans, frees unreferenced",
            ha="center", fontsize=10.3, color=INK, weight="bold")

    # ---- Right: native heap (C++, manual / RAII)
    ax.text(10.9, 8.05, "Native memory (C++)", ha="center", fontsize=13,
            weight="bold", color=GREEN)
    ax.add_patch(FancyBboxPatch((8.3, 4.2), 5.2, 3.5,
                                boxstyle="round,pad=0.04,rounding_size=0.08",
                                facecolor=LGREEN, edgecolor=GREEN, linewidth=1.8))
    nobjs = [
        (8.7, 6.55, "Mesh vertex buffer\n(new[] / malloc)"),
        (11.1, 6.55, "Texture pixels"),
        (8.7, 5.35, "Entity ID pool\n(registry owns)"),
        (11.1, 5.35, "Physics world"),
    ]
    for ox, oy, t in nobjs:
        ax.add_patch(FancyBboxPatch((ox, oy), 2.1, 0.95,
                                    boxstyle="round,pad=0.02,rounding_size=0.05",
                                    facecolor="white", edgecolor=GREEN, linewidth=1.2))
        ax.text(ox + 1.05, oy + 0.48, t, ha="center", va="center",
                fontsize=9.5, color=INK)
    ax.text(10.9, 4.55,
            "Manual lifetime (RAII / smart ptr)\nor owned by engine subsystem",
            ha="center", fontsize=10, color=GREEN, style="italic")

    ax.add_patch(FancyBboxPatch((8.3, 3.1), 5.2, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=SOFT, edgecolor=GREEN, linewidth=1.4))
    ax.text(10.9, 3.58,
            "No GC touches this. Leaks -> leaks forever.",
            ha="center", fontsize=10.3, color=INK, weight="bold")

    # ---- Middle: P/Invoke boundary
    ax.add_patch(Rectangle((5.85, 3.05), 2.3, 4.95, facecolor=SOFT,
                           edgecolor=AMBER, linewidth=1.4, linestyle=":"))
    ax.text(7.0, 7.7, "P/Invoke", ha="center", fontsize=12,
            weight="bold", color=AMBER)
    ax.text(7.0, 7.35, "boundary", ha="center", fontsize=11,
            color=AMBER)
    # call arrows
    arrow(ax, (5.7, 6.0), (8.25, 6.0), color=AMBER, lw=1.9)
    ax.text(7.0, 6.25, "[DllImport] call", ha="center",
            fontsize=9.8, color=AMBER, weight="bold")
    arrow(ax, (8.25, 5.2), (5.7, 5.2), color=AMBER, lw=1.5, ls="--")
    ax.text(7.0, 4.95, "return", ha="center",
            fontsize=9.5, color=AMBER)

    # ---- Bottom: marshalling cost table
    ax.add_patch(FancyBboxPatch((0.4, 0.35), 13.2, 2.3,
                                boxstyle="round,pad=0.03,rounding_size=0.08",
                                facecolor=SOFT, edgecolor=INK, linewidth=1.2))
    ax.text(0.75, 2.35, "Crossing the boundary", fontsize=12, weight="bold",
            color=INK)
    rows = [
        ("blittable (int, float, struct of primitives, pointers)",
         "layouts match  ->  ~0 marshalling, fast"),
        ("non-blittable (string, bool, object, nested class)",
         "runtime allocates + copies + converts  ->  costly"),
        ("managed object passed by reference",
         "GC must PIN it so it does not move (Boehm is non-moving, so pin is free here)"),
        ("hot loop with many calls",
         "batch: one call over an array beats N calls over singletons"),
    ]
    for i, (a, b) in enumerate(rows):
        ax.text(0.75, 1.95 - i * 0.42, "- " + a, fontsize=10.3, color=INK,
                family="monospace")
        ax.text(8.7, 1.95 - i * 0.42, "-> " + b, fontsize=10.3, color=DGREY)

    plt.savefig(os.path.join(OUT, "fig-p4_15_03-gc-boundary.png"))
    plt.close(fig)


# ============================================================ fig 04
# IL2CPP codegen: what the C# `a + b` becomes vs what you might expect.
# Shows the boilerplate: initialize_method, il2cpp_codegen_add, Box, etc.
def fig_codegen_boilerplate():
    fig, ax = plt.subplots(figsize=(14.0, 9.0))
    clean_ax(ax)
    ax.set_xlim(0, 14.0)
    ax.set_ylim(0, 9.0)

    ax.text(7.0, 8.65,
            "IL2CPP codegen: a 3-line C# method becomes verbose C++",
            ha="center", fontsize=14.5, weight="bold", color=INK)
    ax.text(7.0, 8.22,
            "The translator walks the IL stack machine and emits straightforward C++ — the optimizer cleans up",
            ha="center", fontsize=11.3, color=DGREY, style="italic")

    # ---- Left: C# source
    ax.add_patch(FancyBboxPatch((0.4, 2.3), 6.2, 5.5,
                                boxstyle="round,pad=0.04,rounding_size=0.08",
                                facecolor=LBLUE, edgecolor=BLUE, linewidth=1.6))
    ax.text(3.5, 7.45, "C# source", ha="center", fontsize=12.5,
            weight="bold", color=BLUE)
    cs_lines = [
        "static void Main(string[] args) {",
        "    var a = 1;",
        "    var b = 2;",
        "    Console.WriteLine(",
        "        \"Hello: {0}\", a + b);",
        "}",
    ]
    for i, ln in enumerate(cs_lines):
        ax.text(0.75, 6.85 - i * 0.42, ln, fontsize=10.8, color=INK,
                family="monospace")
    ax.text(3.5, 1.7,
            "5 lines, reads like\nwhat you meant",
            ha="center", fontsize=10.5, color=BLUE, style="italic")

    # ---- Right: generated C++ (condensed from real IL2CPP output)
    ax.add_patch(FancyBboxPatch((7.2, 0.6), 6.4, 7.2,
                                boxstyle="round,pad=0.04,rounding_size=0.08",
                                facecolor=LAMBER, edgecolor=AMBER, linewidth=1.6))
    ax.text(10.4, 7.45, "IL2CPP-generated C++ (real, condensed)",
            ha="center", fontsize=11.8, weight="bold", color=AMBER)
    cpp_lines = [
        "void Program_Main_m...(String_t** args,",
        "                       const MethodInfo* m) {",
        "  static bool inited = false;",
        "  if (!inited) {",
        "    il2cpp_codegen_initialize_method(...);",
        "    inited = true;",
        "  }",
        "  int32_t V_0 = 0, V_1 = 0;",
        "  V_0 = 2;",
        "  V_1 = il2cpp_codegen_add(1, V_0);   // a + b",
        "  RuntimeObject* boxed =",
        "      Box(Int32_t..., &V_1);          // box for WriteLine",
        "  IL2CPP_RUNTIME_CLASS_INIT(Console_t...);",
        "  Console_WriteLine_m...(str, boxed, NULL);",
        "}",
    ]
    for i, ln in enumerate(cpp_lines):
        ax.text(7.5, 6.95 - i * 0.42, ln, fontsize=9.8, color=INK,
                family="monospace")

    # ---- Arrow between
    arrow(ax, (6.6, 5.0), (7.2, 5.0), color=INK, lw=2.2)
    ax.text(6.9, 5.3, "IL ->\nC++", ha="center", fontsize=10,
            color=INK, weight="bold")

    # ---- Bottom: the point
    ax.add_patch(FancyBboxPatch((0.4, 0.3), 6.2, 1.5,
                                boxstyle="round,pad=0.03,rounding_size=0.08",
                                facecolor=LRED, edgecolor=RED, linewidth=1.2))
    ax.text(0.7, 1.55, "Why so much gunk?", fontsize=11, weight="bold",
            color=INK)
    ax.text(0.7, 1.18,
            "IL is a stack machine. IL2CPP does a linear scan,", fontsize=10,
            color=INK)
    ax.text(0.7, 0.85,
            "materializing the stack into named locals (V_0, V_1).", fontsize=10,
            color=INK)
    ax.text(0.7, 0.52,
            "Add, box, class-init are explicit runtime calls.", fontsize=10,
            color=INK)

    plt.savefig(os.path.join(OUT, "fig-p4_15_04-codegen-boilerplate.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_il2cpp_pipeline()
    fig_il2cpp_vs_jvm()
    fig_gc_boundary()
    fig_codegen_boilerplate()
    print("Generated 4 figures for P4-15.")
