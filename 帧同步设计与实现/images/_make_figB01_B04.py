"""Generate fig-B-01 (three-level drilldown funnel) and fig-B-04 (four-round
convergence curve) for Appendix B of the lockstep book.

Style: English labels, matplotlib Agg, dpi 150.
B-01 is a real funnel (trapezoid stack) emphasising the *workflow* of how the
three tools are actually used together with BeyondCompare (deliberately a
different emphasis from fig-22-03 which shows code samples per tier).
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Polygon
import numpy as np
import os

OUT = os.path.dirname(os.path.abspath(__file__))

# consistent palette
C0 = "#5A6473"   # slate
C1 = "#2F6FB0"   # blue
C2 = "#D9822B"   # orange
C3 = "#2E8B57"   # green
CNEUTRAL = "#C0392B"  # red accent for diffs

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 10,
    "axes.unicode_minus": False,
})

# ---------------------------------------------------------------------------
# fig-B-01 : three-level drilldown funnel (workflow-oriented)
# ---------------------------------------------------------------------------
def make_funnel():
    fig, ax = plt.subplots(figsize=(11.5, 8.2))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 10)
    ax.axis("off")

    fig.suptitle(
        "desync Three-Level Drilldown: Narrowing the Suspect Set",
        fontsize=15, fontweight="bold", y=0.965)
    ax.text(6, 9.35,
            "From a whole-World hash mismatch, three tools each shrink the circle ~10x,\n"
            "ending at a single field value. BeyondCompare stays in the loop at every tier.",
            ha="center", va="top", fontsize=10.5, color="#444", style="italic")

    # --- 4 funnel tiers as shrinking trapezoids, centered on x=4.5 ---
    tiers = [
        # (label, sub, width_top, width_bot, y_bot, height, color, sample, count_text)
        ("Tier 0  Whole-World Hash", "ComputeHash()  [World.cs:868]",
         4.6, 4.0, 7.5, 0.95, C0,
         "this = 0xABCD1234   other = 0xABCD1235", "ONLY know: DRIFTED"),
        ("Tier 1  Per-Type Hash", "GetPerTypeHashes()  [World.cs:1334]",
         3.6, 3.0, 5.95, 0.95, C1,
         "Position: 0xA1B2C3D4 vs 0xE5F6A7B8", "divergent TYPE = Position"),
        ("Tier 2  Per-Entity Hash", "Diff(World other)  [World.cs:1351]",
         2.6, 2.0, 4.4, 0.95, C2,
         "[COMPONENT] Entity 7 Position:", "divergent ENTITY = #7"),
        ("Tier 3  Per-Field Dump", "GetDebugString(entityId)  [World.cs:115]",
         1.6, 1.0, 2.85, 0.95, C3,
         "X = LFloat(6553600) vs LFloat(6553601)", "divergent FIELD = X (1 LSB)"),
    ]
    cx = 4.5
    for i, (lab, sub, wt, wb, yb, h, col, sample, finding) in enumerate(tiers):
        # trapezoid
        poly = Polygon(
            [(cx - wt/2, yb + h), (cx + wt/2, yb + h),
             (cx + wb/2, yb),    (cx - wb/2, yb)],
            closed=True, facecolor=col, alpha=0.18,
            edgecolor=col, linewidth=2.0, zorder=2)
        ax.add_patch(poly)
        # label band
        ax.text(cx, yb + h - 0.18, lab, ha="center", va="top",
                fontsize=11.5, fontweight="bold", color=col, zorder=3)
        ax.text(cx, yb + h - 0.45, sub, ha="center", va="top",
                fontsize=8.8, color=col, style="italic", zorder=3)
        # sample inside
        ax.text(cx, yb + 0.42, sample, ha="center", va="center",
                fontsize=8.6, color="#222",
                family="DejaVu Sans Mono", zorder=3)
        # finding badge to the right of tier
        ax.text(cx + wt/2 + 0.25, yb + 0.45, finding,
                ha="left", va="center", fontsize=9, fontweight="bold",
                color=CNEUTRAL, zorder=3)
        # down arrow between tiers
        if i < 3:
            ax.annotate("", xy=(cx, yb - 0.06), xytext=(cx, yb + 0.0),
                        arrowprops=dict(arrowstyle="-|>", color="#555",
                                        lw=1.8, mutation_scale=18))

    # --- right-hand workflow column: how the three are actually used ---
    rx = 9.7
    ax.text(rx, 8.55, "HOW YOU ACTUALLY USE THEM",
            ha="center", va="center", fontsize=10.5, fontweight="bold",
            color="#333")
    steps = [
        ("1. Turn on FullValidation", "dev: DualTrackMode.FullValidation\non HashDrift -> dump both sides",
         "#6C3483"),
        ("2. ToDebugString() snapshots", "this / other -> world_x_frameN.txt",
         C0),
        ("3. BeyondCompare the two files", "first red line = drift tier\n(watch DG-2: F4 hides 1 LSB\n-> temporarily use ToStringRaw)",
         C1),
        ("4. Run the drilldown trio", "GetPerTypeHashes -> Diff -> GetDebugString\n(each tool usable standalone)",
         C2),
        ("5. Cross-check Red-Line list", "Ch.24 twelve commandments:\nwhich op produced this field?",
         C3),
    ]
    sy = 8.05
    sh = 0.95
    for lab, body, col in steps:
        box = FancyBboxPatch((rx - 1.75, sy - sh), 3.5, sh - 0.12,
                             boxstyle="round,pad=0.02,rounding_size=0.08",
                             facecolor=col, alpha=0.12,
                             edgecolor=col, linewidth=1.4)
        ax.add_patch(box)
        ax.text(rx - 1.6, sy - 0.18, lab, ha="left", va="top",
                fontsize=9.6, fontweight="bold", color=col)
        ax.text(rx - 1.6, sy - 0.42, body, ha="left", va="top",
                fontsize=8.3, color="#222")
        sy -= sh + 0.18

    # bottom caption strip
    ax.text(cx, 1.55,
            "Each tier cuts the suspect set by ~one order of magnitude:",
            ha="center", fontsize=10, fontweight="bold", color="#333")
    ax.text(cx, 1.15,
            "whole World  (N types)   ->   one type  (M entities)   ->   one entity  (K fields)   ->   one field value",
            ha="center", fontsize=9, color="#444", family="DejaVu Sans Mono")
    ax.text(cx, 0.6,
            "Note: BeyondCompare stays in the loop at every tier - human pattern-matching\n"
            "on aligned text complements the programmatic hash drilldown.",
            ha="center", fontsize=8.6, color="#666", style="italic")

    # left-side vs right-side legend mini
    ax.text(0.15, 5.5, "NARROWING\n(FUNNEL)", ha="left", va="center",
            fontsize=9, fontweight="bold", color="#777", rotation=90)

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    out = os.path.join(OUT, "fig-B-01-three-level-drilldown-funnel.png")
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("wrote", out)
    return out


# ---------------------------------------------------------------------------
# fig-B-04 : four-round convergence curve
# ---------------------------------------------------------------------------
def make_convergence():
    fig, ax = plt.subplots(figsize=(11.5, 7.0))

    # x stages
    stages = ["Initial\nreport", "R1\nverify", "R2\ndeep",
              "R3\nnew", "R3\nverify", "R4\nnew", "Final"]
    # candidate counts (per caption: 15->10->7->15->8->12->7)
    candidate = [15, 10, 7, 15, 8, 12, 7]
    # confirmed-true: monotone-ish descending to 7
    # initial: of 15, eventually 7 true. R1 trims, R2 lands 7 confirmed,
    # R3 new candidates add but most rejected (1 true: P0-2 demoted),
    # R4 new candidates mostly rejected. Final confirmed = 7.
    confirmed = [15, 11, 7, 8, 7, 9, 7]

    x = np.arange(len(stages))

    # shade verify vs new-findings phases
    # verify cols: index 1,2,4 ; new cols: 3,5
    for idx in [1, 2, 4]:
        ax.axvspan(idx - 0.45, idx + 0.45, color="#EAF2F8", alpha=0.6, zorder=0)
    for idx in [3, 5]:
        ax.axvspan(idx - 0.45, idx + 0.45, color="#FDEBD0", alpha=0.55, zorder=0)

    ax.plot(x, candidate, "-o", color="#2F6FB0", lw=2.4, ms=10,
            label="Candidate issues (reported)", zorder=3)
    ax.plot(x, confirmed, "-s", color="#C0392B", lw=2.4, ms=10,
            label="Confirmed TRUE problems", zorder=3)

    # value labels
    for xi, yi in zip(x, candidate):
        ax.annotate(str(yi), (xi, yi), textcoords="offset points",
                    xytext=(0, 10), ha="center", fontsize=10,
                    fontweight="bold", color="#2F6FB0")
    for xi, yi in zip(x, confirmed):
        ax.annotate(str(yi), (xi, yi), textcoords="offset points",
                    xytext=(0, -16), ha="center", fontsize=10,
                    fontweight="bold", color="#C0392B")

    # annotate key events
    ax.annotate("R2: 3 dropped\n(P1-001/002/003 all FALSE)\n4 severity revised",
                xy=(2, 7), xytext=(1.05, 13.2),
                fontsize=9, color="#444",
                arrowprops=dict(arrowstyle="->", color="#777", lw=1.2))
    ax.annotate("R3 NEW: P0-1 cross-TFM,\nP0-2 token-takeover\n(change of view: TFM + trust)",
                xy=(3, 15), xytext=(3.0, 16.6),
                fontsize=9, color="#7E5109",
                ha="center",
                arrowprops=dict(arrowstyle="->", color="#7E5109", lw=1.2))
    ax.annotate("R3 verify: P0-2\ndemoted P0->P1",
                xy=(4, 7), xytext=(4.55, 11.8),
                fontsize=9, color="#444",
                arrowprops=dict(arrowstyle="->", color="#777", lw=1.2))
    ax.annotate("R4 NEW: FrameData OOM,\nBitReader GC, ...",
                xy=(5, 12), xytext=(5.5, 15.5),
                fontsize=9, color="#7E5109",
                arrowprops=dict(arrowstyle="->", color="#7E5109", lw=1.2))
    ax.annotate("FINAL: 7 true, all fixed",
                xy=(6, 7), xytext=(5.4, 2.5),
                fontsize=10, fontweight="bold", color="#1E8449",
                arrowprops=dict(arrowstyle="->", color="#1E8449", lw=1.6))

    # legend for shading
    from matplotlib.patches import Patch
    legend_handles = [
        plt.Line2D([0], [0], color="#2F6FB0", marker="o", lw=2.4,
                   label="Candidate issues (reported)"),
        plt.Line2D([0], [0], color="#C0392B", marker="s", lw=2.4,
                   label="Confirmed TRUE problems"),
        Patch(facecolor="#EAF2F8", label="Verify phase (trim false positives)"),
        Patch(facecolor="#FDEBD0", label="New-view round (re-scan with fresh lens)"),
    ]
    ax.legend(handles=legend_handles, loc="upper right", fontsize=9.5,
              framealpha=0.95)

    ax.set_xticks(x)
    ax.set_xticklabels(stages, fontsize=10)
    ax.set_ylabel("Number of issues", fontsize=11)
    ax.set_xlabel("Audit round", fontsize=11)
    ax.set_ylim(0, 18)
    ax.set_xlim(-0.5, 6.5)
    ax.grid(axis="y", linestyle=":", alpha=0.45)
    ax.set_axisbelow(True)
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)

    fig.suptitle(
        "Four-Round Review Convergence: 15 reported -> 7 true",
        fontsize=14, fontweight="bold", y=0.98)
    ax.set_title(
        "Audit is convergent - but NOT monotonically decreasing.\n"
        "New views dig up new issues; verify phases trim false ones.",
        fontsize=10, color="#555", pad=14)

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    out = os.path.join(OUT, "fig-B-04-four-round-convergence-curve.png")
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("wrote", out)
    return out


if __name__ == "__main__":
    f1 = make_funnel()
    f4 = make_convergence()
    print("done:", f1, f4)
