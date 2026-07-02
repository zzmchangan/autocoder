"""fig-22-01 / 02 / 03 for chapter 22 (Performance Benchmark & Observability).

fig-22-01-benchmark-artifact.png : stacked bar, three code paths GC breakdown,
                                   highlighting the ToArray() artifact not on hot path.
fig-22-02-int128-tax.png          : before/after split + throughput bar showing the
                                   21x gap filled (add 236M / mul 11M -> 684M = 62x).
fig-22-03-desync-drilldown.png    : three-tier funnel: total hash -> per-type ->
                                   per-entity -> per-field, narrowing each tier.
fig-22-04 (mermaid) is skipped per task.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Polygon, Rectangle
import numpy as np

DPI = 150

# =============================================================================
# fig-22-01 : Benchmark artifact vs production path (stacked bar)
# =============================================================================
def fig_22_01():
    OUT = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-22-01-benchmark-artifact.png"

    fig, ax = plt.subplots(figsize=(13, 8.6), dpi=DPI)
    fig.patch.set_facecolor("white")

    # Three paths, GC sources as stacked segments (B/frame)
    # Path A pure Tick : 475  = foreach boxing 475
    # Path B production: 635  = foreach boxing 475 + serialization layer 160
    # Path C byte[]    : 2090 = foreach boxing 475 + serialization layer 160 +
    #                           Snapshot object/closure 0 (kept 0 here, attribution
    #                           merged into serialization for clarity) +
    #                           ToArray() artifact 1455
    # NOTE: text says Path B = 475 + 160 (snapshot/closure folded into serialization
    # layer for the figure to keep 5 legend categories legible). ToArray artifact
    # 1455 only present in Path C.
    paths = ["A. Pure Tick()\n(min logic frame,\nno snapshot)",
             "B. Production\nSaveState(BitWriter)\n+ pool  [HOT PATH]",
             "C. Convenience\nSaveState()  -> byte[]\n[BENCHMARKED, not on hot path]"]

    seg_boxing   = np.array([475, 475, 475])
    seg_serial   = np.array([  0, 160, 160])
    seg_toarray  = np.array([  0,   0, 1455])
    totals       = seg_boxing + seg_serial + seg_toarray   # 475 / 635 / 2090

    x = np.arange(len(paths))
    width = 0.55

    # color palette
    C_BOX   = "#90CAF9"   # foreach boxing (light blue)
    C_SER   = "#FFB74D"   # serialization layer (amber)
    C_ART   = "#EF5350"   # ToArray artifact (red - the warning color)

    b1 = ax.bar(x, seg_boxing, width, label="foreach boxing (List<T>.Enumerator via IReadOnlyList)",
                color=C_BOX, edgecolor="#1565C0", linewidth=0.8)
    b2 = ax.bar(x, seg_serial, width, bottom=seg_boxing,
                label="serialization layer (Snapshot obj + closure delegate ~160 B)",
                color=C_SER, edgecolor="#E65100", linewidth=0.8)
    b3 = ax.bar(x, seg_toarray, width, bottom=seg_boxing + seg_serial,
                label="ToArray() artifact  (~1455 B, NOT on production hot path)",
                color=C_ART, edgecolor="#B71C1C", linewidth=0.8, hatch="//")

    # total labels on top
    for xi, tot in zip(x, totals):
        ax.text(xi, tot + 45, f"{tot} B/frame",
                ha="center", va="bottom", fontsize=12.5, fontweight="bold", color="#212121")

    # segment value labels inside bars
    for xi, v in zip(x, seg_boxing):
        if v > 0:
            ax.text(xi, v / 2, f"{v}", ha="center", va="center",
                    fontsize=10, color="#0D47A1", fontweight="bold")
    for xi, v, base in zip(x, seg_serial, seg_boxing):
        if v > 0:
            ax.text(xi, base + v / 2, f"{v}", ha="center", va="center",
                    fontsize=10, color="#BF360C", fontweight="bold")
    for xi, v, base in zip(x, seg_toarray, seg_boxing + seg_serial):
        if v > 0:
            ax.text(xi, base + v / 2, f"{v}\n(artifact)", ha="center", va="center",
                    fontsize=11, color="white", fontweight="bold")

    # highlight the production hot path with a bracket
    ax.annotate("", xy=(1 - width/2 - 0.04, 635), xytext=(1 + width/2 + 0.04, 635),
                arrowprops=dict(arrowstyle="-[", color="#1B5E20", lw=2.0, shrinkA=0, shrinkB=0))
    ax.text(1, 760, "real target\n635 B/frame",
            ha="center", va="bottom", fontsize=10.5, color="#1B5E20", fontweight="bold")

    # dashed line at 635 showing "production ceiling"
    ax.axhline(635, xmin=0.04, xmax=0.96, ls="--", lw=1.1, color="#1B5E20", alpha=0.55)
    ax.text(2.42, 635, "production\nceiling 635 B",
            ha="left", va="center", fontsize=8.8, color="#1B5E20", fontstyle="italic")

    # BENCHMARK.md false claim annotation on path C
    ax.annotate("BENCHMARK.md reported 2251 B/frame\nas the GC target -> optimizing a phantom",
                xy=(2, 2090), xytext=(1.35, 2380),
                fontsize=9.8, color="#B71C1C", fontweight="bold", ha="center",
                arrowprops=dict(arrowstyle="->", color="#B71C1C", lw=1.6))

    ax.set_xticks(x)
    ax.set_xticklabels(paths, fontsize=10.2)
    ax.set_ylabel("GC allocation  (bytes / frame)", fontsize=12, color="#263238")
    ax.set_ylim(0, 2580)
    ax.set_title("Benchmark Artifact vs Production Path: 2251 B/frame is a Phantom\n"
                 "the ToArray() copy is real in the benchmark but production never calls this overload",
                 fontsize=13.5, fontweight="bold", color="#212121", pad=14)
    ax.tick_params(axis="y", labelsize=10)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", ls=":", alpha=0.35)

    leg = ax.legend(loc="upper left", fontsize=9.3, framealpha=0.95,
                    edgecolor="#90A4AE", title="GC source segments")
    leg.get_title().set_fontweight("bold")

    # bottom takeaway band
    fig.text(0.5, -0.005,
             "Takeaway: always measure THREE paths and label each. The 1455 B segment exists in the "
             "benchmark but production (controller main loop, rollback, reconnect) all use\n"
             "SaveState(BitWriter) writing into a pooled buffer -- no byte[] is ever allocated. "
             "Optimizing the 2251 B phantom = optimizing a strawman.",
             ha="center", va="top", fontsize=9.6, color="#37474F",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#F5F5F5", edgecolor="#90A4AE"))

    plt.tight_layout(rect=[0, 0.07, 1, 1])
    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.2)
    plt.close(fig)
    print("saved:", OUT)


# =============================================================================
# fig-22-02 : Int128 tax elimination (before/after + throughput timeline)
# =============================================================================
def fig_22_02():
    OUT = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-22-02-int128-tax.png"

    fig = plt.figure(figsize=(14, 9.2), dpi=DPI)
    fig.patch.set_facecolor("white")

    # ---- top half: left = before, right = after (control-flow boxes) ----
    ax_top = fig.add_axes([0.04, 0.42, 0.92, 0.54])
    ax_top.set_xlim(0, 1); ax_top.set_ylim(0, 1); ax_top.axis("off")

    ax_top.text(0.5, 0.965, "Int128 Software Tax Elimination: long Fast Path + Int128 Fallback",
                ha="center", va="top", fontsize=14.5, fontweight="bold", color="#212121")
    ax_top.text(0.5, 0.915,
                ".NET 8 Int128 has NO 128-bit hardware instruction -> JIT synthesizes multiple "
                "imul/mul/carry-add. Game coords |a|,|b| < 2^31 -> long holds the product.",
                ha="center", va="top", fontsize=10, color="#546E7A", fontstyle="italic")

    # ---- LEFT panel: BEFORE ----
    LX, LW = 0.03, 0.42
    ax_top.add_patch(FancyBboxPatch((LX, 0.10), LW, 0.74,
                    boxstyle="round,pad=0.008,rounding_size=0.012",
                    facecolor="#FFEBEE", edgecolor="#C62828", linewidth=2.0))
    ax_top.add_patch(FancyBboxPatch((LX, 0.785), LW, 0.055,
                    boxstyle="round,pad=0.008,rounding_size=0.012",
                    facecolor="#B71C1C", edgecolor="none"))
    ax_top.text(LX + LW/2, 0.812, "BEFORE  (stage-0 baseline)",
                ha="center", va="center", fontsize=12, fontweight="bold", color="white")

    # single path: all ops go Int128
    ax_top.text(LX + LW/2, 0.72, "ALL LVector2 hot ops (SqrMagnitude/Dot/Cross/Rotate)",
                ha="center", va="center", fontsize=9.6, color="#212121")
    # box: Int128 software
    ax_top.add_patch(FancyBboxPatch((LX + 0.06, 0.46), LW - 0.12, 0.20,
                    boxstyle="round,pad=0.006,rounding_size=0.010",
                    facecolor="white", edgecolor="#C62828", linewidth=1.6))
    ax_top.text(LX + LW/2, 0.62, "(long)((Int128)a * b >> 16)",
                ha="center", va="center", fontsize=10.5, family="monospace", color="#B71C1C")
    ax_top.text(LX + LW/2, 0.535,
                "JIT software synthesis:\n  imul x2 + adc + shrd + sar\n(~10+ uops, ~1 order of magnitude slower)",
                ha="center", va="center", fontsize=9, color="#37474F", linespacing=1.45)
    # arrow down to result
    ax_top.annotate("", xy=(LX + LW/2, 0.34), xytext=(LX + LW/2, 0.455),
                    arrowprops=dict(arrowstyle="->", color="#C62828", lw=2.2))
    # throughput result box
    ax_top.add_patch(FancyBboxPatch((LX + 0.06, 0.18), LW - 0.12, 0.16,
                    boxstyle="round,pad=0.006,rounding_size=0.010",
                    facecolor="#FFCDD2", edgecolor="#C62828", linewidth=1.4))
    ax_top.text(LX + LW/2, 0.295, "LVector2.mul   11.0 M ops/s",
                ha="center", va="center", fontsize=11, fontweight="bold", color="#B71C1C")
    ax_top.text(LX + LW/2, 0.225, "21x slower than .add (236 M ops/s)",
                ha="center", va="center", fontsize=9.2, color="#B71C1C", fontstyle="italic")
    ax_top.text(LX + LW/2, 0.135, "no fast path  -  100% of ops pay the 128-bit tax",
                ha="center", va="center", fontsize=8.8, color="#546E7A")

    # ---- RIGHT panel: AFTER ----
    RX, RW = 0.55, 0.42
    ax_top.add_patch(FancyBboxPatch((RX, 0.10), RW, 0.74,
                    boxstyle="round,pad=0.008,rounding_size=0.012",
                    facecolor="#E8F5E9", edgecolor="#2E7D32", linewidth=2.0))
    ax_top.add_patch(FancyBboxPatch((RX, 0.785), RW, 0.055,
                    boxstyle="round,pad=0.008,rounding_size=0.012",
                    facecolor="#1B5E20", edgecolor="none"))
    ax_top.text(RX + RW/2, 0.812, "AFTER  (stage-2 fast path)",
                ha="center", va="center", fontsize=12, fontweight="bold", color="white")

    ax_top.text(RX + RW/2, 0.72, "LVector2 hot ops now branch on |a|,|b| < 2^31",
                ha="center", va="center", fontsize=9.6, color="#212121")

    # diamond decision
    dia = Polygon([(RX + RW/2, 0.685), (RX + RW - 0.04, 0.62),
                   (RX + RW/2, 0.555), (RX + 0.04, 0.62)],
                  closed=True, facecolor="white", edgecolor="#2E7D32", linewidth=1.6)
    ax_top.add_patch(dia)
    ax_top.text(RX + RW/2, 0.62, "|a|,|b| < 2^31 ?",
                ha="center", va="center", fontsize=9.8, fontweight="bold", color="#1B5E20")

    # YES branch -> long fast path
    ax_top.text(RX + 0.075, 0.515, "YES  (>99%)", ha="left", va="center",
                fontsize=9.2, color="#1B5E20", fontweight="bold")
    ax_top.annotate("", xy=(RX + 0.135, 0.475), xytext=(RX + 0.085, 0.555),
                    arrowprops=dict(arrowstyle="->", color="#2E7D32", lw=1.8))
    ax_top.add_patch(FancyBboxPatch((RX + 0.045, 0.40), 0.225, 0.085,
                    boxstyle="round,pad=0.004,rounding_size=0.008",
                    facecolor="#C8E6C9", edgecolor="#2E7D32", linewidth=1.3))
    ax_top.text(RX + 0.157, 0.455, "long fast path",
                ha="center", va="center", fontsize=9.5, fontweight="bold", color="#1B5E20")
    ax_top.text(RX + 0.157, 0.425, "(a.RawValue * b) >> 16\n1 imul, 1 sar",
                ha="center", va="center", fontsize=8.2, family="monospace", color="#1B5E20", linespacing=1.3)

    # NO branch -> Int128 fallback
    ax_top.text(RX + RW - 0.095, 0.515, "NO  (<1%)", ha="right", va="center",
                fontsize=9.2, color="#C62828", fontweight="bold")
    ax_top.annotate("", xy=(RX + RW - 0.145, 0.475), xytext=(RX + RW - 0.085, 0.555),
                    arrowprops=dict(arrowstyle="->", color="#C62828", lw=1.8))
    ax_top.add_patch(FancyBboxPatch((RX + RW - 0.275, 0.40), 0.235, 0.085,
                    boxstyle="round,pad=0.004,rounding_size=0.008",
                    facecolor="#FFCDD2", edgecolor="#C62828", linewidth=1.3))
    ax_top.text(RX + RW - 0.157, 0.455, "Int128 fallback",
                ha="center", va="center", fontsize=9.5, fontweight="bold", color="#B71C1C")
    ax_top.text(RX + RW - 0.157, 0.425, "software 128-bit\n(only when overflow possible)",
                ha="center", va="center", fontsize=8, color="#B71C1C", linespacing=1.3)

    # merge arrow down
    ax_top.annotate("", xy=(RX + RW/2, 0.34), xytext=(RX + RW/2, 0.395),
                    arrowprops=dict(arrowstyle="->", color="#2E7D32", lw=2.2))
    ax_top.add_patch(FancyBboxPatch((RX + 0.06, 0.18), RW - 0.12, 0.16,
                    boxstyle="round,pad=0.006,rounding_size=0.010",
                    facecolor="#A5D6A7", edgecolor="#2E7D32", linewidth=1.4))
    ax_top.text(RX + RW/2, 0.295, "LVector2.mul   684 M ops/s",
                ha="center", va="center", fontsize=11, fontweight="bold", color="#1B5E20")
    ax_top.text(RX + RW/2, 0.225, "62x speedup  -  golden hash UNCHANGED",
                ha="center", va="center", fontsize=9.2, color="#1B5E20", fontstyle="italic")
    ax_top.text(RX + RW/2, 0.135, "bit-level equivalent (fast product < 2^62 = no overflow)",
                ha="center", va="center", fontsize=8.8, color="#546E7A")

    # center 62x badge
    ax_top.annotate("", xy=(RX + 0.02, 0.29), xytext=(LX + LW - 0.02, 0.29),
                    arrowprops=dict(arrowstyle="-|>", color="#F9A825", lw=3.0,
                                    connectionstyle="arc3,rad=-0.25"))
    ax_top.add_patch(FancyBboxPatch((0.475, 0.265), 0.05, 0.07,
                    boxstyle="round,pad=0.004,rounding_size=0.010",
                    facecolor="#F9A825", edgecolor="#F57F17", linewidth=1.5))
    ax_top.text(0.50, 0.30, "62x", ha="center", va="center",
                fontsize=12, fontweight="bold", color="white")

    # ---- bottom half: throughput timeline bar ----
    ax_bot = fig.add_axes([0.10, 0.10, 0.84, 0.20])
    cats = ["LVector2.add\n(baseline, no Int128)",
            "LVector2.mul\nBEFORE (all Int128)",
            "LVector2.mul\nAFTER (fast path)",
            "LVector2.dot\nAFTER"]
    vals = [236, 11, 684, 595]
    cols = ["#90CAF9", "#EF5350", "#66BB6A", "#A5D6A7"]
    bars = ax_bot.bar(cats, vals, color=cols, edgecolor=["#1565C0", "#B71C1C", "#1B5E20", "#2E7D32"],
                      linewidth=1.0, width=0.62)
    for b, v in zip(bars, vals):
        ax_bot.text(b.get_x() + b.get_width()/2, v + 12, f"{v} M ops/s",
                    ha="center", va="bottom", fontsize=10.5, fontweight="bold",
                    color="#212121")
    # 21x bracket (add vs mul-before)
    ax_bot.annotate("", xy=(1, 11), xytext=(0, 236),
                    arrowprops=dict(arrowstyle="<->", color="#C62828", lw=1.4,
                                    connectionstyle="arc3,rad=0.25"))
    ax_bot.text(0.62, 150, "21x\ngap", ha="center", va="center",
                fontsize=9, color="#C62828", fontweight="bold")
    # 62x bracket (mul before vs after)
    ax_bot.annotate("", xy=(2, 684), xytext=(1, 11),
                    arrowprops=dict(arrowstyle="<->", color="#1B5E20", lw=1.4,
                                    connectionstyle="arc3,rad=-0.25"))
    ax_bot.text(1.5, 360, "62x\nfilled", ha="center", va="center",
                fontsize=9, color="#1B5E20", fontweight="bold")

    ax_bot.set_ylabel("throughput (M ops/s)", fontsize=10.5, color="#263238")
    ax_bot.set_ylim(0, 760)
    ax_bot.set_title("Throughput timeline: the 21x gap between add and mul is filled by the fast path "
                     "(mul now 2.9x faster than add because add was not vectorized)",
                     fontsize=10, color="#37474F", pad=8)
    ax_bot.tick_params(axis="x", labelsize=9)
    ax_bot.tick_params(axis="y", labelsize=9)
    ax_bot.spines["top"].set_visible(False); ax_bot.spines["right"].set_visible(False)
    ax_bot.grid(axis="y", ls=":", alpha=0.35)

    # safety-net footnote
    fig.text(0.5, 0.015,
             "Safety net: MathEquivalenceTests (random + boundary inputs: 0, max, +/-2^31, negatives) "
             "assert fast-path == fallback RawValue bit-for-bit. 9-sample golden hash unchanged across stage 2.",
             ha="center", va="bottom", fontsize=9.2, color="#37474F",
             bbox=dict(boxstyle="round,pad=0.45", facecolor="#F5F5F5", edgecolor="#90A4AE"))

    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.2)
    plt.close(fig)
    print("saved:", OUT)


# =============================================================================
# fig-22-03 : desync three-tier drilldown funnel
# =============================================================================
def fig_22_03():
    OUT = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-22-03-desync-drilldown.png"

    fig, ax = plt.subplots(figsize=(13.5, 9.0), dpi=DPI)
    fig.patch.set_facecolor("white")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

    ax.text(0.5, 0.965, "desync Three-Tier Drilldown: from a 32-bit hash to a specific field",
            ha="center", va="top", fontsize=14.5, fontweight="bold", color="#212121")
    ax.text(0.5, 0.925,
            "A single uint can only say \"different\". Three narrowing tiers locate the exact field.",
            ha="center", va="top", fontsize=10.5, color="#546E7A", fontstyle="italic")

    # four funnel tiers, narrowing width
    tiers = [
        # (label, api, scope, info, color, y_top, y_bot, half_width)
        ("Tier 0",  "ComputeHash()",
         "Total Hash Mismatch",
         "this.hash = 0xABCD1234\nother.hash = 0xABCD1235\n=> only know \"DIFFERENT\"",
         "#ECEFF1", 0.86, 0.71, 0.42, "#455A64"),
        ("Tier 1",  "GetPerTypeHashes()",
         "Per-Type  (component type)",
         "TankGame.Health   this=0x55667788\nTankGame.Health   other=0x55667789\n=> divergent TYPE = Health",
         "#E3F2FD", 0.68, 0.53, 0.345, "#1565C0"),
        ("Tier 2",  "Diff(World other)",
         "Per-Entity  (component instance)",
         "[COMPONENT] Entity 7 Health:\n  this=0x55667788  other=0x55667789\n=> divergent ENTITY = #7",
         "#FFF3E0", 0.50, 0.35, 0.275, "#E65100"),
        ("Tier 3",  "GetDebugString(entityId)",
         "Per-Field  (raw value text)",
         "{ HP: this=87  other=86\n  MaxHP: 100 = 100\n  LastAttacker: 3 = 3 }\n=> HP differs by 1",
         "#E8F5E9", 0.32, 0.17, 0.205, "#2E7D32"),
    ]

    cx = 0.42   # funnel center
    for i, (tier, api, scope, info, fill, yt, yb, hw, edge) in enumerate(tiers):
        # trapezoid (funnel segment)
        if i < len(tiers) - 1:
            next_hw = tiers[i+1][6]
        else:
            next_hw = hw - 0.03
        poly = Polygon([(cx - hw, yt), (cx + hw, yt),
                        (cx + next_hw, yb), (cx - next_hw, yb)],
                       closed=True, facecolor=fill, edgecolor=edge, linewidth=1.8)
        ax.add_patch(poly)
        # tier tag (left of segment)
        ax.text(cx - hw - 0.025, (yt + yb) / 2 + 0.018, tier,
                ha="right", va="center", fontsize=10.5, fontweight="bold", color=edge)
        ax.text(cx - hw - 0.025, (yt + yb) / 2 - 0.022, scope,
                ha="right", va="center", fontsize=9.2, color=edge, fontstyle="italic")
        # API name centered in segment
        ax.text(cx, (yt + yb) / 2 + 0.035, api,
                ha="center", va="center", fontsize=11.2, fontweight="bold",
                color=edge, family="monospace")
        # narrowing result (info) inside segment
        ax.text(cx, (yt + yb) / 2 - 0.032, info,
                ha="center", va="center", fontsize=8.6, color="#212121", linespacing=1.4)

    # down-arrows between tiers (already implied by trapezoid edges, add a center chevron)
    for i in range(len(tiers) - 1):
        yt = tiers[i][5]; yb = tiers[i][6]
        ax.annotate("", xy=(cx, yb - 0.005), xytext=(cx, yb + 0.018),
                    arrowprops=dict(arrowstyle="->", color="#37474F", lw=1.4))

    # right-side: example narrowing chain + API location
    RX = 0.78
    ax.add_patch(FancyBboxPatch((RX - 0.005, 0.17), 0.215, 0.69,
                 boxstyle="round,pad=0.006,rounding_size=0.010",
                 facecolor="#FAFAFA", edgecolor="#90A4AE", linewidth=1.2))
    ax.text(RX + 0.10, 0.84, "API  (all on hot path,\nbut NOT auto-wired yet)",
            ha="center", va="top", fontsize=9.8, fontweight="bold", color="#263238", linespacing=1.4)

    api_rows = [
        ("World.cs:868",          "ComputeHash()",          "#455A64"),
        ("World.cs:1334",         "GetPerTypeHashes()",     "#1565C0"),
        ("World.cs:1351",         "Diff(World)",            "#E65100"),
        ("World.cs:115",          "GetDebugString(int)",    "#2E7D32"),
    ]
    yy = 0.74
    for loc, api, col in api_rows:
        ax.add_patch(FancyBboxPatch((RX + 0.01, yy - 0.04), 0.185, 0.055,
                     boxstyle="round,pad=0.004,rounding_size=0.006",
                     facecolor="white", edgecolor=col, linewidth=1.0))
        ax.text(RX + 0.025, yy + 0.005, loc,
                ha="left", va="center", fontsize=8.6, color=col, family="monospace")
        ax.text(RX + 0.025, yy - 0.022, api,
                ha="left", va="center", fontsize=9, fontweight="bold", color="#212121",
                family="monospace")
        yy -= 0.13

    ax.text(RX + 0.10, 0.215,
            "Status: capability IN PLACE.\nAuto-wiring (Diff called\nautomatically on OnHashDrift)\nis a TODO at World.cs:1165.",
            ha="center", va="center", fontsize=8.6, color="#37474F", linespacing=1.4,
            bbox=dict(boxstyle="round,pad=0.4", facecolor="#FFF8E1", edgecolor="#F9A825"))

    # bottom takeaway
    fig.text(0.5, 0.045,
             "Each tier narrows the suspect set by ~1 order of magnitude. Tier 0 = whole World. "
             "Tier 1 = one component type. Tier 2 = one entity. Tier 3 = one field value.\n"
             "Drilling from a 32-bit hash to \"Entity 7 Health.HP this=87 other=86\" is what makes "
             "field-level desync localization possible.",
             ha="center", va="center", fontsize=9.6, color="#37474F",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#F5F5F5", edgecolor="#90A4AE"))

    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.2)
    plt.close(fig)
    print("saved:", OUT)


if __name__ == "__main__":
    fig_22_01()
    fig_22_02()
    fig_22_03()
    print("all done")
