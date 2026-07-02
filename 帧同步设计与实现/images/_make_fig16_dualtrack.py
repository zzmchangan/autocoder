"""fig-16-dual-track-version.png
Two-track version number comparison: ProtocolVersion (wire handshake, Major-only)
vs SerializationVersion (snapshot format, exact match). Two independent
compatibility boundaries, side-by-side comparison panels.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

OUT = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-16-dual-track-version.png"

fig, ax = plt.subplots(figsize=(14, 9.5), dpi=150)
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.axis("off")

# Title
ax.text(0.5, 0.965, "Dual-Track Version Numbers: Two Independent Compatibility Boundaries",
        ha="center", va="top", fontsize=15, fontweight="bold", color="#212121")
ax.text(0.5, 0.925, "Wire handshake vs snapshot format  —  each track upgrades on its own trigger, never coupled",
        ha="center", va="top", fontsize=10.5, color="#546E7A", fontstyle="italic")

# Two panel rectangles
left_x, right_x = 0.025, 0.515
panel_w = 0.46
panel_top = 0.89
panel_bot = 0.18

# ---------- LEFT PANEL: ProtocolVersion ----------
PV_FILL = "#E3F2FD"
PV_EDGE = "#1565C0"
PV_HEAD = "#0D47A1"
ax.add_patch(FancyBboxPatch((left_x, panel_bot), panel_w, panel_top - panel_bot,
                            boxstyle="round,pad=0.008,rounding_size=0.012",
                            facecolor=PV_FILL, edgecolor=PV_EDGE, linewidth=2.2))

# panel header bar
ax.add_patch(FancyBboxPatch((left_x, panel_top - 0.075), panel_w, 0.075,
                            boxstyle="round,pad=0.008,rounding_size=0.012",
                            facecolor=PV_HEAD, edgecolor=PV_HEAD, linewidth=0))
ax.text(left_x + panel_w / 2, panel_top - 0.037,
        "Track 1: ProtocolVersion",
        ha="center", va="center", fontsize=14, fontweight="bold", color="white")
ax.text(left_x + panel_w / 2, panel_top - 0.062,
        "Wire Handshake / Network Messages",
        ha="center", va="center", fontsize=9.5, color="#BBDEFB", fontstyle="italic")

# 5 attribute rows: label : value
pv_rows = [
    ("Current value",       "1.1   (Major=1, Minor=1)"),
    ("Encoded int",         "(1 << 16) | 1  =  65537"),
    ("Guards",              "Can the client connect to the server?\n(wire protocol: JoinRequest / Input / HashReport ...)"),
    ("Check strategy",      "Major-only  (LENIENT)\nreturn otherMajor == Major;   // Minor ignored"),
    ("On mismatch",         "JoinResponse.Success = false\nhandshake rejected, client cannot join"),
]
y = panel_top - 0.105
row_h = 0.115
for i, (lab, val) in enumerate(pv_rows):
    yy = y - i * row_h
    # label cell
    ax.add_patch(FancyBboxPatch((left_x + 0.012, yy - row_h + 0.018), 0.135, row_h - 0.025,
                                boxstyle="round,pad=0.004,rounding_size=0.006",
                                facecolor="#BBDEFB", edgecolor=PV_EDGE, linewidth=1.0))
    ax.text(left_x + 0.012 + 0.0675, yy - row_h / 2 + 0.006, lab,
            ha="center", va="center", fontsize=9.5, fontweight="bold", color=PV_HEAD)
    # value cell
    ax.add_patch(FancyBboxPatch((left_x + 0.155, yy - row_h + 0.018), panel_w - 0.167, row_h - 0.025,
                                boxstyle="round,pad=0.004,rounding_size=0.006",
                                facecolor="white", edgecolor=PV_EDGE, linewidth=1.0))
    ax.text(left_x + 0.163, yy - row_h / 2 + 0.006, val,
            ha="left", va="center", fontsize=9.3, color="#212121", linespacing=1.4)

# upgrade-trigger mini note
ax.text(left_x + panel_w / 2, panel_bot + 0.035,
        "Upgrades: add a message type / append a field -> bump Minor\nbreaking field-type / semantic change -> bump Major",
        ha="center", va="center", fontsize=8.6, color=PV_HEAD, fontstyle="italic", linespacing=1.4)

# ---------- RIGHT PANEL: SerializationVersion ----------
SV_FILL = "#FFF3E0"
SV_EDGE = "#E65100"
SV_HEAD = "#BF360C"
ax.add_patch(FancyBboxPatch((right_x, panel_bot), panel_w, panel_top - panel_bot,
                            boxstyle="round,pad=0.008,rounding_size=0.012",
                            facecolor=SV_FILL, edgecolor=SV_EDGE, linewidth=2.2))
ax.add_patch(FancyBboxPatch((right_x, panel_top - 0.075), panel_w, 0.075,
                            boxstyle="round,pad=0.008,rounding_size=0.012",
                            facecolor=SV_HEAD, edgecolor=SV_HEAD, linewidth=0))
ax.text(right_x + panel_w / 2, panel_top - 0.037,
        "Track 2: SerializationVersion",
        ha="center", va="center", fontsize=14, fontweight="bold", color="white")
ax.text(right_x + panel_w / 2, panel_top - 0.062,
        "Snapshot Format / World SaveState",
        ha="center", va="center", fontsize=9.5, color="#FFE0B2", fontstyle="italic")

sv_rows = [
    ("Current value",       "2   (a single integer)"),
    ("Defined at",          "ECS/World.cs:823   (private const int)"),
    ("Guards",              "Can the snapshot be loaded back?\n(World.SaveState byte layout: all component pools, contiguous)"),
    ("Check strategy",      "Exact match  (STRICT)\nif (version != SerializationVersion) throw;"),
    ("On mismatch",         "LoadState throws InvalidOperationException\n\"Incompatible snapshot version: expected V2, got V{n}\""),
]
for i, (lab, val) in enumerate(sv_rows):
    yy = y - i * row_h
    ax.add_patch(FancyBboxPatch((right_x + 0.012, yy - row_h + 0.018), 0.135, row_h - 0.025,
                                boxstyle="round,pad=0.004,rounding_size=0.006",
                                facecolor="#FFE0B2", edgecolor=SV_EDGE, linewidth=1.0))
    ax.text(right_x + 0.012 + 0.0675, yy - row_h / 2 + 0.006, lab,
            ha="center", va="center", fontsize=9.5, fontweight="bold", color=SV_HEAD)
    ax.add_patch(FancyBboxPatch((right_x + 0.155, yy - row_h + 0.018), panel_w - 0.167, row_h - 0.025,
                                boxstyle="round,pad=0.004,rounding_size=0.006",
                                facecolor="white", edgecolor=SV_EDGE, linewidth=1.0))
    ax.text(right_x + 0.163, yy - row_h / 2 + 0.006, val,
            ha="left", va="center", fontsize=9.3, color="#212121", linespacing=1.4)

ax.text(right_x + panel_w / 2, panel_bot + 0.035,
        "Upgrades: add a component field / change pool serialization order\n-> must bump (old snapshots become unreadable, no built-in migrator yet)",
        ha="center", va="center", fontsize=8.6, color=SV_HEAD, fontstyle="italic", linespacing=1.4)

# ---------- STRATEGY CONTRAST BANNER (between panels, top) ----------
banner_y = panel_top - 0.105 - 1.5 * row_h + 0.06
ax.annotate("", xy=(left_x + panel_w - 0.005, banner_y),
            xytext=(left_x + panel_w + 0.045, banner_y),
            arrowprops=dict(arrowstyle="<->", color="#37474F", lw=1.6))
ax.text(left_x + panel_w + 0.020, banner_y + 0.020,
        "different\ncheck\nstrategies",
        ha="center", va="bottom", fontsize=8, color="#37474F", fontstyle="italic", linespacing=1.2)

# ---------- BOTTOM: relationship summary ----------
rel_top = 0.135
ax.add_patch(FancyBboxPatch((0.025, 0.015), 0.95, rel_top - 0.015,
                            boxstyle="round,pad=0.008,rounding_size=0.010",
                            facecolor="#F5F5F5", edgecolor="#455A64", linewidth=1.6))
ax.text(0.05, rel_top - 0.018, "Relationship between the two tracks:",
        ha="left", va="top", fontsize=11, fontweight="bold", color="#263238")

ax.text(0.05, rel_top - 0.045,
        "1.  Numeric values are UNRELATED.   ProtocolVersion may stay 1.1 while SerializationVersion climbs to 5 (frequent component edits), and vice-versa.",
        ha="left", va="top", fontsize=9.3, color="#37474F")
ax.text(0.05, rel_top - 0.068,
        "2.  Upgrade triggers DIFFER.        Wire/message change bumps ProtocolVersion; component/snapshot-layout change bumps SerializationVersion.",
        ha="left", va="top", fontsize=9.3, color="#37474F")
ax.text(0.05, rel_top - 0.091,
        "3.  NOT coupled in effect.          A snapshot-format bump does NOT force online players to reconnect (wire protocol untouched); a Minor wire bump does NOT invalidate old snapshots.",
        ha="left", va="top", fontsize=9.3, color="#37474F")
ax.text(0.05, rel_top - 0.114,
        "Why split:   decouple slow-changing wire protocol from fast-changing component schema, so component iteration never forces full-server reconnect.",
        ha="left", va="top", fontsize=9.3, color="#BF360C", fontweight="bold", fontstyle="italic")

plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.18)
print("saved:", OUT)
