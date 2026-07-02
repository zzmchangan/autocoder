"""fig-24-01, fig-24-02, fig-24-03 for Chapter 24 (Determinism Red Lines).

fig-24-01: Twelve Commandments master table with attribution + safeguard coverage
fig-24-02: Attribution split -- 5 lockstep-required vs 7 rollback-imposed (Venn + bars)
fig-24-03: SystemStateValidator defence-in-depth coverage boundary (3 layers, float gap)
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Circle, FancyArrowPatch, Rectangle
import numpy as np

OUT_DIR = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/"

# ---------- shared palette ----------
C_LS    = "#1565C0"   # locksync (blue)
C_LS_L  = "#BBDEFB"
C_RB    = "#E65100"   # rollback (orange)
C_RB_L  = "#FFE0B2"
C_BOTH  = "#6A1B9A"   # both (purple)
C_BOTH_L= "#E1BEE7"
C_OK    = "#2E7D32"   # green: validator catches
C_DES   = "#F9A825"   # yellow: design-layer
C_GAP   = "#C62828"   # red: not caught (gap)
C_INK   = "#212121"
C_GREY  = "#546E7A"


# =====================================================================
# FIG 24-01 : Twelve Commandments master table
# =====================================================================
def fig_01():
    OUT = OUT_DIR + "fig-24-01-twelve-commandments.png"
    # Each row: (num, name_short, attribution, safeguard)
    # attribution: LS / RB / BOTH
    # safeguard: OK / DES / GAP
    rows = [
        ("I",     "No float / double",                       "LS",   "GAP"),
        ("II",    "No Dictionary / HashSet iteration",       "LS",   "OK"),
        ("III",   "No swap-and-pop removal",                 "RB",   "DES"),
        ("IV",    "No System.Random",                        "LS",   "OK"),
        ("V",     "No system time as logic input",           "LS",   "OK"),  # Stopwatch only
        ("VI",    "LINQ sort must be stable (ThenBy Id)",    "LS",   "GAP"),
        ("VII",   "String compare: Ordinal",                 "LS",   "GAP"),
        ("VIII",  "Event / delegate order deterministic",    "RB",   "OK"),
        ("IX",    "First-frame state identical everywhere",  "BOTH", "GAP"),
        ("X",     "Create/destroy via CommandBuffer",        "RB",   "DES"),
        ("XI",    "No cross-thread World access",            "BOTH", "DES"),  # DEBUG assert
        ("XII",   "RNG state enters snapshot",               "RB",   "OK"),  # World built-in
    ]

    fig, ax = plt.subplots(figsize=(15, 11), dpi=150)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

    # ---- title ----
    ax.text(0.5, 0.975, "Twelve Commandments of Determinism  —  Master Table with Attribution",
            ha="center", va="top", fontsize=16, fontweight="bold", color=C_INK)
    ax.text(0.5, 0.945,
            "Each red line tagged: WHO demands it (locksync vs rollback)  +  HOW WELL the framework catches it",
            ha="center", va="top", fontsize=10.5, color=C_GREY, fontstyle="italic")

    # ---- column header bar ----
    cols = [("#", 0.030), ("Red Line", 0.055), ("Attribution", 0.450), ("Safeguard Coverage", 0.620), ("Safeguard Detail", 0.760)]
    hdr_top = 0.915
    hdr_h   = 0.040
    ax.add_patch(FancyBboxPatch((0.020, hdr_top - hdr_h), 0.960, hdr_h,
                                boxstyle="round,pad=0.004,rounding_size=0.006",
                                facecolor="#263238", edgecolor="#263238"))
    for label, x in cols:
        ax.text(x, hdr_top - hdr_h/2, label, ha="left", va="center",
                fontsize=10.5, fontweight="bold", color="white")

    # ---- 12 rows ----
    row_h = 0.059
    y_top = hdr_top - hdr_h
    ATTR_LABEL = {"LS": "LOCKSYNC", "RB": "ROLLBACK", "BOTH": "BOTH"}
    ATTR_FILL  = {"LS": C_LS_L, "RB": C_RB_L, "BOTH": C_BOTH_L}
    ATTR_EDGE  = {"LS": C_LS,   "RB": C_RB,   "BOTH": C_BOTH}
    ATTR_DESC  = {"LS": "multi-machine\nagreement",
                  "RB": "rewindable\nreplay",
                  "BOTH": "both layers\ndemand it"}
    SAFE_FILL  = {"OK": "#E8F5E9", "DES": "#FFF8E1", "GAP": "#FFEBEE"}
    SAFE_EDGE  = {"OK": C_OK,     "DES": C_DES,     "GAP": C_GAP}
    SAFE_TAG   = {"OK": "VALIDATOR", "DES": "DESIGN-LAYER", "GAP": "NOT CAUGHT"}
    SAFE_DESC  = {
        "OK":  "ISystem.cs:169-225\nreflection scan,\nthrows in DEBUG",
        "DES": "ComponentPool /\nCheckThreadAffinity /\nbuilt into SaveState",
        "GAP": "no static safeguard\n-> manual review +\ncross-TFM equiv test"
    }
    # special-case descriptions
    safe_detail_per_row = {
        "IV":   "Validator catches\nRandom field\n(ISystem.cs:188)",
        "V":    "Stopwatch field caught\nDateTime.Now call NOT\n(only field scan)",
        "XI":   "DEBUG assert at runtime\n(not compile-time)",
        "XII":  "World.SaveState\nauto-serializes\n_state0,_state1",
        "III":  "SafeECS BinarySearch\nordered list;\nUnsafeECS not snapshotted",
        "X":    "ComponentPool pending\nremoval queue + Validator\ncatches cached refs",
    }

    for i, (num, name, attr, safe) in enumerate(rows):
        yy = y_top - i * row_h
        # zebra stripe
        if i % 2 == 0:
            ax.add_patch(Rectangle((0.020, yy - row_h + 0.004), 0.960, row_h - 0.004,
                                   facecolor="#FAFAFA", edgecolor="none", zorder=0))
        # number badge
        ax.add_patch(Circle((0.045, yy - row_h/2 + 0.002), 0.018,
                            facecolor=ATTR_EDGE[attr], edgecolor="white", linewidth=1.5, zorder=3))
        ax.text(0.045, yy - row_h/2 + 0.002, num, ha="center", va="center",
                fontsize=9.5, fontweight="bold", color="white", zorder=4)
        # name
        ax.text(0.080, yy - row_h/2 + 0.006, name, ha="left", va="center",
                fontsize=10.3, fontweight="bold", color=C_INK)
        # attribution chip
        ax.add_patch(FancyBboxPatch((0.455, yy - row_h/2 - 0.016), 0.150, 0.034,
                                    boxstyle="round,pad=0.003,rounding_size=0.010",
                                    facecolor=ATTR_FILL[attr], edgecolor=ATTR_EDGE[attr], linewidth=1.3))
        ax.text(0.530, yy - row_h/2 + 0.006, ATTR_LABEL[attr], ha="center", va="center",
                fontsize=9.0, fontweight="bold", color=ATTR_EDGE[attr])
        ax.text(0.530, yy - row_h/2 - 0.013, ATTR_DESC[attr], ha="center", va="center",
                fontsize=7.0, color=ATTR_EDGE[attr], linespacing=1.1)
        # safeguard tag chip
        ax.add_patch(FancyBboxPatch((0.625, yy - row_h/2 - 0.013), 0.125, 0.028,
                                    boxstyle="round,pad=0.003,rounding_size=0.010",
                                    facecolor=SAFE_FILL[safe], edgecolor=SAFE_EDGE[safe], linewidth=1.3))
        ax.text(0.6875, yy - row_h/2 + 0.001, SAFE_TAG[safe], ha="center", va="center",
                fontsize=7.8, fontweight="bold", color=SAFE_EDGE[safe])
        # safeguard detail
        detail = safe_detail_per_row.get(num, SAFE_DESC[safe])
        ax.text(0.765, yy - row_h/2 + 0.006, detail, ha="left", va="center",
                fontsize=7.6, color="#37474F", linespacing=1.25)

    # ---- legend bar (bottom) ----
    leg_y = y_top - 12 * row_h - 0.005
    ax.add_patch(FancyBboxPatch((0.020, leg_y - 0.075), 0.960, 0.078,
                                boxstyle="round,pad=0.005,rounding_size=0.008",
                                facecolor="#F5F5F5", edgecolor="#455A64", linewidth=1.4))
    ax.text(0.035, leg_y - 0.013, "Legend", ha="left", va="top",
            fontsize=10, fontweight="bold", color="#263238")
    # attribution legend
    ax.text(0.035, leg_y - 0.035, "Attribution:", ha="left", va="top",
            fontsize=9, fontweight="bold", color=C_GREY)
    x = 0.115
    for k, lab in [("LS","Locksync-required (multi-machine agreement)"),
                   ("RB","Rollback-imposed (rewindable replay)"),
                   ("BOTH","Both layers demand it")]:
        ax.add_patch(FancyBboxPatch((x, leg_y - 0.042), 0.018, 0.018,
                                    boxstyle="round,pad=0.002,rounding_size=0.005",
                                    facecolor=ATTR_FILL[k], edgecolor=ATTR_EDGE[k], linewidth=1.2))
        ax.text(x + 0.024, leg_y - 0.033, lab, ha="left", va="center",
                fontsize=8.4, color=C_INK)
        x += 0.275
    # safeguard legend
    ax.text(0.035, leg_y - 0.058, "Safeguard:", ha="left", va="top",
            fontsize=9, fontweight="bold", color=C_GREY)
    x = 0.115
    for k, lab in [("OK","Validator catches (DEBUG throw)"),
                   ("DES","Design-layer / runtime assert"),
                   ("GAP","NOT caught -- manual review")]:
        ax.add_patch(FancyBboxPatch((x, leg_y - 0.065), 0.018, 0.018,
                                    boxstyle="round,pad=0.002,rounding_size=0.005",
                                    facecolor=SAFE_FILL[k], edgecolor=SAFE_EDGE[k], linewidth=1.2))
        ax.text(x + 0.024, leg_y - 0.056, lab, ha="left", va="center",
                fontsize=8.4, color=C_INK)
        x += 0.275

    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.18)
    plt.close(fig)
    print("saved:", OUT)


# =====================================================================
# FIG 24-02 : Attribution split -- 5 locksync vs 7 rollback (Venn + stacked bar)
# =====================================================================
def fig_02():
    OUT = OUT_DIR + "fig-24-02-commandments-attribution.png"
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(15, 8.5), dpi=150,
                                   gridspec_kw={"width_ratios": [1.05, 1.0]})
    fig.subplots_adjust(top=0.88, bottom=0.10, left=0.04, right=0.97, wspace=0.22)

    fig.suptitle("Twelve Commandments: Most are Rollback-Imposed, not Locksync-Required",
                 fontsize=15.5, fontweight="bold", color=C_INK, y=0.965)
    fig.text(0.5, 0.915,
             "Pure lockstep (no prediction/rollback) only demands FIVE core rules;  prediction-rollback adds SEVEN more",
             ha="center", fontsize=10.5, color=C_GREY, fontstyle="italic")

    # ---------- LEFT: Venn diagram ----------
    axL.set_xlim(0, 1); axL.set_ylim(0, 1); axL.axis("off")
    axL.set_title("Commandment Attribution  (who demands each red line)",
                  fontsize=12, fontweight="bold", color="#263238", pad=12)

    # two big circles
    R = 0.28
    c_ls = Circle((0.385, 0.55), R, facecolor=C_LS,  alpha=0.32, edgecolor=C_LS,  linewidth=2.4)
    c_rb = Circle((0.615, 0.55), R, facecolor=C_RB,  alpha=0.32, edgecolor=C_RB,  linewidth=2.4)
    axL.add_patch(c_ls); axL.add_patch(c_rb)
    # labels
    axL.text(0.245, 0.83, "LOCKSYNC-REQUIRED",
             ha="center", fontsize=11, fontweight="bold", color=C_LS)
    axL.text(0.245, 0.805, "(multi-machine agreement)",
             ha="center", fontsize=8.2, color=C_LS, fontstyle="italic")
    axL.text(0.755, 0.83, "ROLLBACK-IMPOSED",
             ha="center", fontsize=11, fontweight="bold", color=C_RB)
    axL.text(0.755, 0.805, "(rewindable replay)",
             ha="center", fontsize=8.2, color=C_RB, fontstyle="italic")

    # LS-only zone (left): 6 items listed; book counts VI/VII as extensions of II/IV -> "5 core"
    ls_only = [
        "I     No float/double",
        "II    No Dict/HashSet iterate",
        "IV    No System.Random",
        "V     No system time (logic)",
        "VI    Stable LINQ sort  (ext. of II)",
        "VII   String Ordinal    (ext. of IV)",
    ]
    axL.text(0.235, 0.69, "\n".join(ls_only), ha="center", va="top",
             fontsize=8.0, color=C_LS, fontweight="bold", linespacing=1.5)
    axL.text(0.235, 0.40, "6 listed\n(= 5 core:\nVI/VII are\nhalf-rules)",
             ha="center", va="center", fontsize=8.0, fontweight="bold",
             color=C_LS, linespacing=1.35)

    # overlap zone (BOTH)
    both = ["IX  First-frame identical",
            "XI  No cross-thread World"]
    axL.text(0.5, 0.66, "\n".join(both), ha="center", va="top",
             fontsize=8.3, color=C_BOTH, fontweight="bold", linespacing=1.55)
    axL.text(0.5, 0.52, "(2 rules\nclaimed by both)",
             ha="center", va="center", fontsize=8, color=C_BOTH, fontstyle="italic", linespacing=1.3)

    # RB-only zone (right): 4 items in table; book's "7 rollback" = 4 pure + 2 both + 1 ch.9-discipline-merge
    rb_only = [
        "III   No swap-and-pop",
        "VIII  Event order fixed",
        "X     CommandBuffer",
        "XII   RNG state in snapshot",
    ]
    axL.text(0.765, 0.69, "\n".join(rb_only), ha="center", va="top",
             fontsize=8.3, color=C_RB, fontweight="bold", linespacing=1.5)
    axL.text(0.765, 0.46, "4 listed\n(pure RB;\nsee bar ->)",
             ha="center", va="center", fontsize=8.0, fontweight="bold",
             color=C_RB, linespacing=1.35)

    # ---------- RIGHT: stacked bar by essence ----------
    axR.set_xlim(0, 1); axR.set_ylim(0, 1); axR.axis("off")
    axR.set_title("Counted by Essence  (5 vs 7  ->  7 > 5, so MOST are rollback)",
                  fontsize=12, fontweight="bold", color="#263238", pad=12)

    bar_x = 0.10
    bar_w = 0.20
    # bar 1: pure locksync -- book counts 5 core (VI/VII merged as one half-rule line)
    h_ls = 5 * 0.082
    axR.add_patch(FancyBboxPatch((bar_x, 0.14), bar_w, h_ls,
                                 boxstyle="round,pad=0.002,rounding_size=0.008",
                                 facecolor=C_LS, edgecolor=C_LS, linewidth=1.5))
    axR.text(bar_x + bar_w/2, 0.14 + h_ls + 0.03, "5",
             ha="center", fontsize=22, fontweight="bold", color=C_LS)
    axR.text(bar_x + bar_w/2, 0.14 + h_ls + 0.005, "LOCKSYNC\nrequired",
             ha="center", va="bottom", fontsize=9, fontweight="bold", color=C_LS)
    # ticks (5 core)
    ls_ticks = ["I   No float","II  No Dict iter","IV  No Random",
                "V   No sys-time","VI+VII  stable+Ordinal\n(half-rules, 1 slot)"]
    for i in range(5):
        axR.text(bar_x + bar_w + 0.012, 0.14 + (i+0.5)*0.082,
                 ls_ticks[i], ha="left", va="center", fontsize=7.3, color=C_LS, linespacing=1.2)

    # bar 2: rollback -- 7 = 4 pure RB table rules + IX/XI (both) + 1 ch.9 discipline-merge
    # (book sec 3.2 lists 4 table rules + 3 ch.9 = 7; we visualize as 4 pure + 2 shared + 1 ch.9)
    bar2_x = bar_x + bar_w + 0.40
    seg_h = 0.082
    # segment colors: 4 pure RB, then 2 BOTH-shaded, then 1 ch.9
    segs = [
        ("III   No swap-pop",          C_RB),
        ("VIII  Event order",          C_RB),
        ("X     CommandBuffer",        C_RB),
        ("XII   RNG snapshot",         C_RB),
        ("IX    First-frame  (both)",  C_BOTH),
        ("XI    No cross-thr (both)",  C_BOTH),
        ("ch.9  serialize/side-ef/reset", "#8D6E63"),
    ]
    h_rb = 7 * seg_h
    cur_y = 0.14
    for label, col in segs:
        axR.add_patch(FancyBboxPatch((bar2_x, cur_y), bar_w, seg_h - 0.004,
                                     boxstyle="round,pad=0.002,rounding_size=0.005",
                                     facecolor=col, edgecolor="white", linewidth=1.0))
        cur_y += seg_h
    axR.text(bar2_x + bar_w/2, 0.14 + h_rb + 0.03, "7",
             ha="center", fontsize=22, fontweight="bold", color=C_RB)
    axR.text(bar2_x + bar_w/2, 0.14 + h_rb + 0.005, "ROLLBACK\nimposed",
             ha="center", va="bottom", fontsize=9, fontweight="bold", color=C_RB)
    # ticks
    for i in range(7):
        axR.text(bar2_x + bar_w + 0.012, 0.14 + (i+0.5)*seg_h,
                 segs[i][0], ha="left", va="center", fontsize=7.3, color=segs[i][1], linespacing=1.2)

    # comparison arrow
    axR.annotate("", xy=(bar2_x + bar_w/2, 0.14 + h_rb + 0.10),
                    xytext=(bar_x + bar_w/2, 0.14 + h_ls + 0.10),
                 arrowprops=dict(arrowstyle="->", color=C_GAP, lw=2.0,
                                 connectionstyle="arc3,rad=-0.25"))
    axR.text((bar_x + bar_w/2 + bar2_x + bar_w/2)/2, 0.14 + h_rb + 0.135,
             "7 > 5  ->  MOST red lines exist\nBECAUSE rollback exists",
             ha="center", va="bottom", fontsize=10, fontweight="bold",
             color=C_GAP, linespacing=1.3)

    # essence captions
    axR.text(bar_x + bar_w/2, 0.07,
             "Essence:\n\"Two machines, same input\n-> same output\"",
             ha="center", va="top", fontsize=8.6, color=C_LS, fontstyle="italic", linespacing=1.4)
    axR.text(bar2_x + bar_w/2, 0.07,
             "Essence:\n\"Logic must be pure,\nfully snapshot-able\"",
             ha="center", va="top", fontsize=8.6, color=C_RB, fontstyle="italic", linespacing=1.4)

    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.18)
    plt.close(fig)
    print("saved:", OUT)


# =====================================================================
# FIG 24-03 : Defence-in-depth coverage boundary (3 layers, float gap)
# =====================================================================
def fig_03():
    OUT = OUT_DIR + "fig-24-03-defence-coverage.png"
    fig, ax = plt.subplots(figsize=(15.5, 10), dpi=150)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

    ax.text(0.5, 0.975, "Defence-in-Depth Coverage Boundary  —  3 Safeguard Layers",
            ha="center", va="top", fontsize=16, fontweight="bold", color=C_INK)
    ax.text(0.5, 0.945,
            "Honest annotation: which red lines each layer catches, and the float gap none of them cover",
            ha="center", va="top", fontsize=10.5, color=C_GREY, fontstyle="italic")

    # ---------- 3 layer columns ----------
    layers = [
        {
            "x": 0.025, "w": 0.30,
            "head": "Layer 1", "sub": "SystemStateValidator",
            "when": "DEBUG / load-time\n(reflection scan on Initialize)",
            "color": "#1565C0", "fill": "#E3F2FD", "head_c": "#0D47A1",
            "catches": [
                "World / Entity (cached ref)",
                "Dictionary<,> / HashSet<>",
                "Hashtable",
                "System.Random",
                "Guid / Stopwatch",
                "CancellationToken(Src)",
                "Task / ValueTask / Thread / Timer",
                "static fields (non-const)",
                "delegate / event fields",
                "dangerous name patterns",
            ],
            "misses": [
                "method-body locals",
                "DateTime.Now calls",
                "LINQ stability analysis",
                "IComponent fields (DG-4)",
            ],
        },
        {
            "x": 0.355, "w": 0.30,
            "head": "Layer 2", "sub": "[AutoSerialize] SourceGen",
            "when": "compile-time\n(diagnostics at build)",
            "color": "#6A1B9A", "fill": "#F3E5F5", "head_c": "#4A148E",
            "catches": [
                "unsupported field silently dropped (LSGEN002)",
                "reference-type field on component (LSGEN003)",
                "missing Serialize/Deserialize code",
            ],
            "misses": [
                "field type is deterministic or not",
                "cross-platform value equality",
                "LSGEN003 still Warning (not Error)",
            ],
        },
        {
            "x": 0.685, "w": 0.30,
            "head": "Layer 3", "sub": "DEBUG round-trip check",
            "when": "runtime (DEBUG)\n(serialize -> deserialize -> diff)",
            "color": "#00838F", "fill": "#E0F7FA", "head_c": "#006064",
            "catches": [
                "serialization asymmetry",
                "hand-written logic mismatching\n   generated code",
                "round-trip dropped field",
            ],
            "misses": [
                "cross-platform value drift",
                "lowest-bit float divergence",
                "release-mode (DEBUG only)",
            ],
        },
    ]

    top_y = 0.91
    col_h = 0.66
    bot_y = top_y - col_h

    for L in layers:
        # outer panel
        ax.add_patch(FancyBboxPatch((L["x"], bot_y), L["w"], col_h,
                                    boxstyle="round,pad=0.006,rounding_size=0.010",
                                    facecolor=L["fill"], edgecolor=L["color"], linewidth=2.0))
        # header bar
        hh = 0.075
        ax.add_patch(FancyBboxPatch((L["x"], top_y - hh), L["w"], hh,
                                    boxstyle="round,pad=0.006,rounding_size=0.010",
                                    facecolor=L["head_c"], edgecolor=L["head_c"]))
        ax.text(L["x"] + L["w"]/2, top_y - 0.022, L["head"],
                ha="center", va="center", fontsize=12.5, fontweight="bold", color="white")
        ax.text(L["x"] + L["w"]/2, top_y - 0.043, L["sub"],
                ha="center", va="center", fontsize=10, color="white")
        ax.text(L["x"] + L["w"]/2, top_y - 0.062, L["when"],
                ha="center", va="center", fontsize=7.8, color="#ECEFF1", fontstyle="italic", linespacing=1.3)

        # CATCHES section
        cy = top_y - hh - 0.025
        ax.text(L["x"] + 0.012, cy, "CATCHES",
                ha="left", va="top", fontsize=8.6, fontweight="bold", color=C_OK)
        # divider
        ax.plot([L["x"]+0.012, L["x"]+L["w"]-0.012], [cy-0.012, cy-0.012],
                color=C_OK, linewidth=1.0)
        ty = cy - 0.022
        for c in L["catches"]:
            ax.text(L["x"] + 0.018, ty, "•", ha="left", va="top", fontsize=8.5, color=C_OK)
            ax.text(L["x"] + 0.030, ty, c, ha="left", va="top", fontsize=7.7,
                    color="#1B5E20", linespacing=1.25)
            ty -= 0.022 + 0.012 * (c.count("\n"))

        # MISSES section
        my = bot_y + 0.155
        ax.add_patch(FancyBboxPatch((L["x"]+0.008, bot_y+0.005), L["w"]-0.016, my - bot_y - 0.010,
                                    boxstyle="round,pad=0.004,rounding_size=0.006",
                                    facecolor="#FFF3E0", edgecolor="#EF6C00", linewidth=1.0))
        ax.text(L["x"] + L["w"]/2, my - 0.012, "DOES NOT CATCH",
                ha="center", va="top", fontsize=8.6, fontweight="bold", color="#EF6C00")
        ty2 = my - 0.030
        for m in L["misses"]:
            ax.text(L["x"] + 0.018, ty2, "✗", ha="left", va="top", fontsize=8, color="#EF6C00")
            ax.text(L["x"] + 0.030, ty2, m, ha="left", va="top", fontsize=7.5,
                    color="#BF360C", linespacing=1.2)
            ty2 -= 0.024 + 0.011 * (m.count("\n"))

    # ---------- bottom: the float gap banner ----------
    gap_y = 0.21
    ax.add_patch(FancyBboxPatch((0.025, 0.025), 0.955, gap_y - 0.025,
                                boxstyle="round,pad=0.006,rounding_size=0.010",
                                facecolor="#FFEBEE", edgecolor=C_GAP, linewidth=2.2))
    ax.add_patch(FancyBboxPatch((0.025, gap_y - 0.040), 0.955, 0.040,
                                boxstyle="round,pad=0.006,rounding_size=0.010",
                                facecolor=C_GAP, edgecolor=C_GAP))
    ax.text(0.5, gap_y - 0.020,
            "THE GAP NONE OF THE THREE LAYERS COVER:  float / double fields  (Red Line I, the most insidious)",
            ha="center", va="center", fontsize=11.5, fontweight="bold", color="white")

    # gap body
    ax.text(0.045, gap_y - 0.058,
            "WHY NOT CAUGHT:",
            ha="left", va="top", fontsize=9.5, fontweight="bold", color=C_GAP)
    reasons = [
        "Layer 1:  float/double NOT in any of the 4 dangerous-type HashSets (ISystem.cs:169-225).  Validator's design intent = catch *rollback*-relevant fields (cached refs, static state, delegates).",
        "Layer 2:  Source Generator only checks the field is serializable;  it does NOT reason about cross-platform determinism of the field's arithmetic.",
        "Layer 3:  Round-trip only checks serialization symmetry on ONE machine;  cross-machine lowest-bit drift is invisible to it.",
    ]
    yy = gap_y - 0.075
    for r in reasons:
        ax.text(0.055, yy, "•", ha="left", va="top", fontsize=8.5, color=C_GAP)
        ax.text(0.068, yy, r, ha="left", va="top", fontsize=8.0, color="#B71C1C", linespacing=1.35)
        yy -= 0.030

    ax.text(0.045, yy - 0.005,
            "CURRENT MITIGATION (honest):  manual code review + cross-TFM equivalence test (P0-1 workflow).  DG-4 (component-layer scan) and DG-7 (runtime leak detection) not implemented.",
            ha="left", va="top", fontsize=8.4, color=C_GAP, fontweight="bold", fontstyle="italic")

    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.18)
    plt.close(fig)
    print("saved:", OUT)


if __name__ == "__main__":
    fig_01()
    fig_02()
    fig_03()
    print("all done")
