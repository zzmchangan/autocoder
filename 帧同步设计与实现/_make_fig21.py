"""fig-21-replay-free.png, fig-21-iron-triangle.png, fig-21-wiring-bug.png
Three figures for Chapter 21 (Replay System).
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch

BASE = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/"

# ============================================================
# Fig 1: fig-21-replay-free.png
#   Replay "free" principle: Lockstep records inputs (few KB)
#   -> deterministic machine -> perfect reproduction;
#   State Sync records full state (hundreds of MB) -> per-frame draw,
#   breaks when logic changes.
# ============================================================

def fig_replay_free():
    OUT = BASE + "fig-21-replay-free.png"
    fig, ax = plt.subplots(figsize=(14, 9.5), dpi=150)
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 10)
    ax.axis("off")

    ax.text(7, 9.65, "Replay \"Free\" Principle: Lockstep Replays Inputs vs State Sync Replays States",
            ha="center", va="top", fontsize=15, fontweight="bold", color="#212121")

    # Column divider
    ax.plot([7, 7], [0.7, 9.0], color="#BDBDBD", linewidth=1.0, linestyle="--")

    # ---------- LEFT: Lockstep ----------
    LXC = 3.5  # center x of left column
    # Header
    ax.add_patch(FancyBboxPatch((1.0, 8.25), 5.0, 0.55,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor="#1565C0", edgecolor="#0D47A1", linewidth=1.2))
    ax.text(LXC, 8.52, "LOCKSTEP  (record INPUTS  =  causes)",
            ha="center", va="center", fontsize=11.5, fontweight="bold", color="white")

    # Box 1: input stream (small)
    ax.add_patch(FancyBboxPatch((1.6, 7.15), 3.8, 0.85,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor="#E3F2FD", edgecolor="#1565C0", linewidth=1.4))
    ax.text(LXC, 7.72, "Input Stream File   ~ a few KB",
            ha="center", va="center", fontsize=10.5, fontweight="bold", color="#0D47A1")
    ax.text(LXC, 7.40, "initial state (once) + per-frame inputs (~tens of bytes)",
            ha="center", va="center", fontsize=8.8, color="#1565C0")

    # arrow down
    ax.add_patch(FancyArrowPatch((LXC, 7.10), (LXC, 6.75),
                                 arrowstyle="-|>", mutation_scale=20,
                                 color="#1565C0", linewidth=2.2))

    # Box 2: deterministic machine
    ax.add_patch(FancyBboxPatch((1.2, 5.45), 4.6, 1.25,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor="#1B5E20", edgecolor="#0B3D14", linewidth=1.4))
    ax.text(LXC, 6.40, "Deterministic Machine",
            ha="center", va="center", fontsize=11, fontweight="bold", color="white")
    ax.text(LXC, 6.05, "same initial state + same inputs",
            ha="center", va="center", fontsize=9, color="white")
    ax.text(LXC, 5.78, "  =>  bit-identical output",
            ha="center", va="center", fontsize=9, color="#FFEB3B", fontweight="bold")

    # arrow down
    ax.add_patch(FancyArrowPatch((LXC, 5.40), (LXC, 5.05),
                                 arrowstyle="-|>", mutation_scale=20,
                                 color="#1565C0", linewidth=2.2))

    # Box 3: perfect reproduction
    ax.add_patch(FancyBboxPatch((1.4, 3.95), 4.2, 1.05,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor="#E8F5E9", edgecolor="#2E7D32", linewidth=1.4))
    ax.text(LXC, 4.65, "Perfect Reproduction",
            ha="center", va="center", fontsize=10.5, fontweight="bold", color="#1B5E20")
    ax.text(LXC, 4.30, "re-run simulation.Tick(frame) from t=0",
            ha="center", va="center", fontsize=8.8, color="#2E7D32")

    # Annotations (left bottom)
    ax.add_patch(Rectangle((1.0, 2.10), 5.0, 1.55,
                           facecolor="#F1F8E9", edgecolor="#2E7D32", linewidth=0.8))
    ax.text(1.2, 3.45, "Records causes (inputs):",
            ha="left", va="top", fontsize=9.5, fontweight="bold", color="#1B5E20")
    ax.text(1.2, 3.12, u"  •  file tiny  (~ a few KB for a 30-min match)",
            ha="left", va="top", fontsize=9, color="#212121")
    ax.text(1.2, 2.82, u"  •  bit-identical every time  (incl. RNG calls)",
            ha="left", va="top", fontsize=9, color="#212121")
    ax.text(1.2, 2.52, u"  •  survives logic evolution  (replay old inputs w/ new logic)",
            ha="left", va="top", fontsize=9, color="#212121")
    ax.text(1.2, 2.22, u"  •  why e-sport replays / spectating sit on lockstep",
            ha="left", va="top", fontsize=9, color="#212121", fontstyle="italic")

    # ---------- RIGHT: State Sync ----------
    RXC = 10.5
    # Header
    ax.add_patch(FancyBboxPatch((8.0, 8.25), 5.0, 0.55,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor="#C62828", edgecolor="#8E0000", linewidth=1.2))
    ax.text(RXC, 8.52, "STATE SYNC  (record STATES  =  results)",
            ha="center", va="center", fontsize=11.5, fontweight="bold", color="white")

    # Box 1: full state file (BIG - drawn taller/wider with stacked layers)
    # stacked frames to imply huge
    for i, dy in enumerate([0.0, 0.10, 0.20, 0.30]):
        ax.add_patch(Rectangle((8.5 - dy*0.6, 6.55 + dy), 4.0 + dy*1.2, 0.55,
                               facecolor="#FFCDD2" if i < 3 else "#EF9A9A",
                               edgecolor="#B71C1C", linewidth=1.0))
    ax.text(RXC, 7.45, "Full State File   ~ hundreds of MB",
            ha="center", va="center", fontsize=10.5, fontweight="bold", color="#8E0000")
    ax.text(RXC, 6.78, "every frame: positions + HP + ... of all entities",
            ha="center", va="center", fontsize=8.8, color="#B71C1C")

    # arrow down
    ax.add_patch(FancyArrowPatch((RXC, 6.50), (RXC, 6.15),
                                 arrowstyle="-|>", mutation_scale=20,
                                 color="#C62828", linewidth=2.2))

    # Box 2: per-frame draw (no causal rebuild)
    ax.add_patch(FancyBboxPatch((8.4, 4.85), 4.2, 1.25,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor="#6A1B9A", edgecolor="#3A0066", linewidth=1.4))
    ax.text(RXC, 5.80, "Per-frame Read & Draw",
            ha="center", va="center", fontsize=11, fontweight="bold", color="white")
    ax.text(RXC, 5.45, "read frame N's stored state, render it",
            ha="center", va="center", fontsize=9, color="white")
    ax.text(RXC, 5.15, "NO causal chain to rebuild",
            ha="center", va="center", fontsize=9, color="#FFEB3B", fontweight="bold")

    # arrow down
    ax.add_patch(FancyArrowPatch((RXC, 4.80), (RXC, 4.45),
                                 arrowstyle="-|>", mutation_scale=20,
                                 color="#C62828", linewidth=2.2))

    # Box 3: brittle reproduction
    ax.add_patch(FancyBboxPatch((8.6, 3.95), 3.8, 0.45,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor="#FCE4EC", edgecolor="#AD1457", linewidth=1.4))
    ax.text(RXC, 4.18, "Brittle \"Reproduction\"",
            ha="center", va="center", fontsize=10.5, fontweight="bold", color="#8E0000")

    # Annotations (right bottom)
    ax.add_patch(Rectangle((8.0, 2.10), 5.0, 1.55,
                           facecolor="#FFEBEE", edgecolor="#C62828", linewidth=0.8))
    ax.text(8.2, 3.45, "Records results (states):",
            ha="left", va="top", fontsize=9.5, fontweight="bold", color="#8E0000")
    ax.text(8.2, 3.12, u"  •  file huge  (hundreds of MB for a 30-min match)",
            ha="left", va="top", fontsize=9, color="#212121")
    ax.text(8.2, 2.82, u"  •  one corrupted frame breaks the rest",
            ha="left", va="top", fontsize=9, color="#212121")
    ax.text(8.2, 2.52, u"  •  logic changes break old replays  (missing fields)",
            ha="left", va="top", fontsize=9, color="#212121")
    ax.text(8.2, 2.22, u"  •  no perfect fidelity  (no causal re-derivation)",
            ha="left", va="top", fontsize=9, color="#212121")

    # ---------- Bottom caption bar ----------
    ax.add_patch(Rectangle((1.0, 0.85), 12.0, 0.80,
                           facecolor="#FFF8E1", edgecolor="#F57F17", linewidth=1.0))
    ax.text(7, 1.40, "File size delta:  a few KB  (lockstep)   vs   hundreds of MB  (state sync)   =>   up to ~1000x smaller.",
            ha="center", va="center", fontsize=10, fontweight="bold", color="#5D4037")
    ax.text(7, 1.05, "Lockstep replays inputs  =>  deterministic machine re-derives states.   State Sync replays states directly.",
            ha="center", va="center", fontsize=9.5, color="#5D4037", fontstyle="italic")

    # tiny footnote
    ax.text(7, 0.45, "Same initial state  +  same input sequence  +  deterministic ops  =  same result.   (the contract from Ch.0)",
            ha="center", va="center", fontsize=8.8, color="#616161")

    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.15)
    plt.close(fig)
    print("saved:", OUT)


# ============================================================
# Fig 2: fig-21-iron-triangle.png
#   Iron-triangle file format: Magic "LSRP" (0x5052534C) +
#   version range [MinCompatible, Current] + trailing CRC32.
# ============================================================

def fig_iron_triangle():
    OUT = BASE + "fig-21-iron-triangle.png"
    fig, ax = plt.subplots(figsize=(15, 9), dpi=150)
    ax.set_xlim(0, 15)
    ax.set_ylim(0, 9)
    ax.axis("off")

    ax.text(7.5, 8.65, "Replay File Iron-Triangle Format  (.lrp)",
            ha="center", va="top", fontsize=15, fontweight="bold", color="#212121")
    ax.text(7.5, 8.28, "Magic identifies format   •   Version range gates compatibility   •   CRC32 guards integrity",
            ha="center", va="top", fontsize=10.5, color="#37474F", fontstyle="italic")

    # Three horizontal byte-layout segments stacked to read like a file
    # We'll draw ONE long byte strip in 4 logical zones, with colored bands.
    strip_y = 5.7
    strip_h = 1.5

    # zone boundaries (x left, width)
    z_magic  = (0.8, 1.6)   # Magic 4B
    z_head   = (2.4, 6.2)   # Header + Frames (variable)
    z_crc    = (8.6, 1.6)   # CRC32 4B
    # (gaps left for threat labels)

    # ---- Zone 1: Magic (blue) ----
    ax.add_patch(FancyBboxPatch((z_magic[0], strip_y), z_magic[1], strip_h,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#1565C0", edgecolor="#0D47A1", linewidth=1.6))
    ax.text(z_magic[0]+z_magic[1]/2, strip_y+strip_h-0.28, "Magic  (4 B)",
            ha="center", va="center", fontsize=10, fontweight="bold", color="white")
    ax.text(z_magic[0]+z_magic[1]/2, strip_y+strip_h-0.70, "LSRP",
            ha="center", va="center", fontsize=14, fontweight="bold", color="#FFEB3B")
    ax.text(z_magic[0]+z_magic[1]/2, strip_y+0.30, "0x5052534C",
            ha="center", va="center", fontsize=8.8, color="white", family="monospace")

    # ---- Zone 2: Header + Frames (green) ----
    ax.add_patch(FancyBboxPatch((z_head[0], strip_y), z_head[1], strip_h,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#E8F5E9", edgecolor="#2E7D32", linewidth=1.6))
    ax.text(z_head[0]+z_head[1]/2, strip_y+strip_h-0.22, "Header  +  Frames  (variable length)",
            ha="center", va="center", fontsize=10, fontweight="bold", color="#1B5E20")
    # sub-fields inside header
    fields = [
        "Version: 4 B   [MinCompatible=2, Current=2]",
        "RecordTime (8) • TotalFrames (4) • Duration (8)",
        "PlayerCount (4) • RandomSeed (4)   MUST record seed!",
        "InitialState [len:i32][bytes]   MUST record init state",
        "FrameCount (4) • Frames: FrameData...",
    ]
    fy = strip_y + strip_h - 0.55
    for f in fields:
        ax.text(z_head[0]+0.15, fy, f, ha="left", va="center", fontsize=8.6,
                color="#1B5E20", family="monospace")
        fy -= 0.24

    # ---- Zone 3: CRC32 (orange) ----
    ax.add_patch(FancyBboxPatch((z_crc[0], strip_y), z_crc[1], strip_h,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#EF6C00", edgecolor="#8E3D00", linewidth=1.6))
    ax.text(z_crc[0]+z_crc[1]/2, strip_y+strip_h-0.28, "CRC32  (4 B)",
            ha="center", va="center", fontsize=10, fontweight="bold", color="white")
    ax.text(z_crc[0]+z_crc[1]/2, strip_y+strip_h-0.70, "tail",
            ha="center", va="center", fontsize=12, fontweight="bold", color="#FFEB3B")
    ax.text(z_crc[0]+z_crc[1]/2, strip_y+0.30, "IEEE 802.3",
            ha="center", va="center", fontsize=8.5, color="white")

    # CRC coverage brace arrow: from end of Header+Frames zone down to CRC
    cov_x_start = z_head[0]
    cov_x_end = z_crc[0] + z_crc[1]
    ax.annotate("", xy=(cov_x_end, strip_y-0.10), xytext=(cov_x_start, strip_y-0.10),
                arrowprops=dict(arrowstyle="-", color="#EF6C00", lw=1.2))
    ax.plot([cov_x_start, cov_x_start], [strip_y-0.10, strip_y-0.22], color="#EF6C00", lw=1.2)
    ax.plot([cov_x_end, cov_x_end], [strip_y-0.10, strip_y-0.22], color="#EF6C00", lw=1.2)
    ax.text((cov_x_start+cov_x_end)/2, strip_y-0.40,
            "CRC32 covers  Version -> end of Frames   (excludes Magic & CRC bytes itself)",
            ha="center", va="center", fontsize=8.8, color="#8E3D00", fontstyle="italic")

    # ---- Threat labels below each zone (red boxes) ----
    threat_y = 3.95
    threat_h = 1.05

    # T1 under Magic
    ax.add_patch(Rectangle((z_magic[0]-0.45, threat_y), z_magic[1]+0.9, threat_h,
                           facecolor="#FFEBEE", edgecolor="#B71C1C", linewidth=1.2))
    ax.text(z_magic[0]+z_magic[1]/2, threat_y+threat_h-0.20, "THREAT 1",
            ha="center", va="center", fontsize=9, fontweight="bold", color="#B71C1C")
    ax.text(z_magic[0]+z_magic[1]/2, threat_y+0.62, "Format",
            ha="center", va="center", fontsize=9.5, fontweight="bold", color="#212121")
    ax.text(z_magic[0]+z_magic[1]/2, threat_y+0.40, "identification",
            ha="center", va="center", fontsize=9.5, fontweight="bold", color="#212121")
    ax.text(z_magic[0]+z_magic[1]/2, threat_y+0.16, "(wrong file type)",
            ha="center", va="center", fontsize=8.2, color="#616161", fontstyle="italic")

    # T2 under Header+Frames
    ax.add_patch(Rectangle((z_head[0]+0.3, threat_y), z_head[1]-0.6, threat_h,
                           facecolor="#FFEBEE", edgecolor="#B71C1C", linewidth=1.2))
    ax.text(z_head[0]+z_head[1]/2, threat_y+threat_h-0.20, "THREAT 2",
            ha="center", va="center", fontsize=9, fontweight="bold", color="#B71C1C")
    ax.text(z_head[0]+z_head[1]/2, threat_y+0.62, "Version",
            ha="center", va="center", fontsize=9.5, fontweight="bold", color="#212121")
    ax.text(z_head[0]+z_head[1]/2, threat_y+0.40, "evolution",
            ha="center", va="center", fontsize=9.5, fontweight="bold", color="#212121")
    ax.text(z_head[0]+z_head[1]/2, threat_y+0.16, "(new<->old playback)",
            ha="center", va="center", fontsize=8.2, color="#616161", fontstyle="italic")

    # T3 under CRC
    ax.add_patch(Rectangle((z_crc[0]-0.45, threat_y), z_crc[1]+0.9, threat_h,
                           facecolor="#FFEBEE", edgecolor="#B71C1C", linewidth=1.2))
    ax.text(z_crc[0]+z_crc[1]/2, threat_y+threat_h-0.20, "THREAT 3",
            ha="center", va="center", fontsize=9, fontweight="bold", color="#B71C1C")
    ax.text(z_crc[0]+z_crc[1]/2, threat_y+0.62, "Data",
            ha="center", va="center", fontsize=9.5, fontweight="bold", color="#212121")
    ax.text(z_crc[0]+z_crc[1]/2, threat_y+0.40, "corruption",
            ha="center", va="center", fontsize=9.5, fontweight="bold", color="#212121")
    ax.text(z_crc[0]+z_crc[1]/2, threat_y+0.16, "(disk/transfer bit-flip)",
            ha="center", va="center", fontsize=8.2, color="#616161", fontstyle="italic")

    # arrows zone -> threat
    for zx, zw in [z_magic, z_head, z_crc]:
        ax.add_patch(FancyArrowPatch((zx+zw/2, strip_y-0.05), (zx+zw/2, threat_y+threat_h+0.05),
                                     arrowstyle="-|>", mutation_scale=14,
                                     color="#9E9E9E", linewidth=1.2))

    # ---- Mechanism detail panel (bottom) ----
    mech_y = 2.4
    mech_h = 1.30
    # three columns mirroring
    colw = 4.3
    gap = 0.25
    x0 = 0.8
    details = [
        ("Magic  —  format ID",
         ["first 4 bytes = 0x5052534C  (\"LSRP\")",
          "like ZIP \"PK\", PNG \"\\x89PNG\", class 0xCAFEBABE",
          "reject instantly on mismatch",
          "prevents parsing .txt / wrong files as replay"],
         "#1565C0", "#E3F2FD"),
        ("Version range  —  compatibility",
         ["IsVersionCompatible(v):  Min <= v <= Current",
          "MinCompatible=2, Current=2  (V2 adds CRC32)",
          "range, NOT exact match  =>  room to evolve",
          "three version tracks independent (proto/snap/replay)"],
         "#2E7D32", "#E8F5E9"),
        ("CRC32  —  integrity",
         ["IEEE 802.3 poly  0xEDB88320, 8 rounds/byte",
          "re-compute on load, compare to stored tail",
          "1 byte flipped => fingerprint fully changes",
          "burst-error miss rate ~ 2^-32"],
         "#EF6C00", "#FFF3E0"),
    ]
    for i, (title, lines, ec, fc) in enumerate(details):
        cx = x0 + i*(colw+gap)
        ax.add_patch(FancyBboxPatch((cx, mech_y), colw, mech_h,
                                    boxstyle="round,pad=0.02,rounding_size=0.05",
                                    facecolor=fc, edgecolor=ec, linewidth=1.4))
        ax.text(cx+colw/2, mech_y+mech_h-0.18, title,
                ha="center", va="center", fontsize=10, fontweight="bold", color=ec)
        ly = mech_y+mech_h-0.45
        for ln in lines:
            ax.text(cx+0.15, ly, ln, ha="left", va="center", fontsize=8.4,
                    color="#212121", family="monospace")
            ly -= 0.22

    # footer
    ax.text(7.5, 1.85, "Three mechanisms gate three distinct threats  =>  none can be dropped:",
            ha="center", va="center", fontsize=10, fontweight="bold", color="#37474F")
    ax.text(7.5, 1.55, "Magic only:   new/old versions misalign on hard play, no corruption check.",
            ha="center", va="center", fontsize=9, color="#616161")
    ax.text(7.5, 1.30, "Version only:   wrong-format files parsed as garbage, no corruption check.",
            ha="center", va="center", fontsize=9, color="#616161")
    ax.text(7.5, 1.05, "CRC only:   cannot identify non-replay files, cannot gate compatibility.",
            ha="center", va="center", fontsize=9, color="#616161")
    ax.text(7.5, 0.70, "All three together guard format-ID + version-compat + data-integrity.",
            ha="center", va="center", fontsize=9.5, fontweight="bold", color="#212121")

    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.15)
    plt.close(fig)
    print("saved:", OUT)


# ============================================================
# Fig 3: fig-21-wiring-bug.png
#   "Designed but wired wrong" before/after comparison.
#   BEFORE: LoadWithValidation (full check) vs Deserialize
#           (reads savedCrc, discards) vs ReplayPlayer.Load (exact !=);
#           main path LoadFromFile -> Deserialize (wrong entry).
#   AFTER:  IsVersionCompatible single source of truth; Deserialize
#           self-verifies CRC; LoadFromFile -> LoadWithValidation.
# ============================================================

def fig_wiring_bug():
    OUT = BASE + "fig-21-wiring-bug.png"
    fig, ax = plt.subplots(figsize=(16, 10.5), dpi=150)
    ax.set_xlim(0, 16)
    ax.set_ylim(0, 10.5)
    ax.axis("off")

    ax.text(8, 10.15, "Wired Wrong:  Mechanism Exists but the Main Path Bypasses It",
            ha="center", va="top", fontsize=15, fontweight="bold", color="#212121")
    ax.text(8, 9.78, "P0-3/4/5 replay correctness bug  —  now fixed  (depicts the historical state)",
            ha="center", va="top", fontsize=10.5, color="#37474F", fontstyle="italic")

    # vertical divider
    ax.plot([8, 8], [0.6, 9.5], color="#BDBDBD", linewidth=1.0, linestyle="--")

    # ============ LEFT: BEFORE ============
    LXC = 4.0
    ax.add_patch(FancyBboxPatch((1.0, 8.85), 6.0, 0.55,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor="#B71C1C", edgecolor="#7F0000", linewidth=1.2))
    ax.text(LXC, 9.12, "BEFORE  (audit snapshot)",
            ha="center", va="center", fontsize=12, fontweight="bold", color="white")

    # three entry boxes side by side is too wide; stack them vertically on left
    # Entry A: LoadWithValidation (green)
    ax.add_patch(FancyBboxPatch((1.3, 7.55), 5.4, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#E8F5E9", edgecolor="#2E7D32", linewidth=1.5))
    ax.text(1.55, 8.30, "A.  LoadWithValidation(data)",
            ha="left", va="center", fontsize=9.8, fontweight="bold", color="#1B5E20", family="monospace")
    ax.text(1.55, 8.02, "verify Magic  +  verify CRC  +  call Deserialize",
            ha="left", va="center", fontsize=8.6, color="#2E7D32")
    ax.text(6.5, 8.30, "OK", ha="center", va="center", fontsize=9.5, fontweight="bold", color="#2E7D32")

    # Entry B: Deserialize (yellow)
    ax.add_patch(FancyBboxPatch((1.3, 6.30), 5.4, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#FFF8E1", edgecolor="#F57F17", linewidth=1.5))
    ax.text(1.55, 7.05, "B.  Deserialize(reader)",
            ha="left", va="center", fontsize=9.8, fontweight="bold", color="#E65100", family="monospace")
    ax.text(1.55, 6.77, "read Magic  +  read savedCrc then DISCARD (_ = savedCrc)",
            ha="left", va="center", fontsize=8.6, color="#E65100")
    ax.text(1.55, 6.50, "version checked by RANGE  (Min<=v<=Current)",
            ha="left", va="center", fontsize=8.6, color="#E65100")
    ax.text(6.5, 6.77, "FAKE", ha="center", va="center", fontsize=9.5, fontweight="bold", color="#E65100")

    # Entry C: ReplayPlayer.Load (yellow)
    ax.add_patch(FancyBboxPatch((1.3, 5.05), 5.4, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#FFF8E1", edgecolor="#F57F17", linewidth=1.5))
    ax.text(1.55, 5.80, "C.  ReplayPlayer.Load",
            ha="left", va="center", fontsize=9.8, fontweight="bold", color="#E65100", family="monospace")
    ax.text(1.55, 5.52, "version checked by EXACT mismatch (v != Current)",
            ha="left", va="center", fontsize=8.6, color="#E65100")
    ax.text(1.55, 5.25, "=> version policy DUPLICATED & inconsistent",
            ha="left", va="center", fontsize=8.6, color="#E65100")
    ax.text(6.5, 5.52, "DUP", ha="center", va="center", fontsize=9.5, fontweight="bold", color="#E65100")

    # ReadHeader (yellow)
    ax.add_patch(FancyBboxPatch((1.3, 3.80), 5.4, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#FFF8E1", edgecolor="#F57F17", linewidth=1.5))
    ax.text(1.55, 4.55, "D.  ReplayManager.ReadHeader",
            ha="left", va="center", fontsize=9.8, fontweight="bold", color="#E65100", family="monospace")
    ax.text(1.55, 4.27, "hard-coded read 64 B  (real header = 36 B)",
            ha="left", va="center", fontsize=8.6, color="#E65100")
    ax.text(1.55, 4.00, "bypasses version-range check entirely",
            ha="left", va="center", fontsize=8.6, color="#E65100")
    ax.text(6.5, 4.27, "BYPASS", ha="center", va="center", fontsize=8.8, fontweight="bold", color="#E65100")

    # Main path box (RED) -> arrow into B
    ax.add_patch(FancyBboxPatch((1.3, 2.40), 5.4, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#FFCDD2", edgecolor="#B71C1C", linewidth=2.0))
    ax.text(1.55, 3.15, "MAIN PATH:  ReplayManager.LoadFromFile",
            ha="left", va="center", fontsize=9.8, fontweight="bold", color="#B71C1C", family="monospace")
    ax.text(1.55, 2.87, "File.ReadAllBytes -> calls Deserialize  (entry B, NOT A)",
            ha="left", va="center", fontsize=8.6, color="#B71C1C")
    ax.text(1.55, 2.60, "=> real client load has CRC check effectively DISABLED",
            ha="left", va="center", fontsize=8.6, color="#B71C1C", fontweight="bold")

    # big red arrow main -> B
    ax.add_patch(FancyArrowPatch((4.0, 3.40), (4.0, 6.25),
                                 arrowstyle="-|>", mutation_scale=26,
                                 color="#B71C1C", linewidth=3.0,
                                 connectionstyle="arc3,rad=0.0"))
    ax.text(4.5, 4.85, "wrong entry!", ha="left", va="center",
            fontsize=10, fontweight="bold", color="#B71C1C", rotation=90)

    # bottom red banner
    ax.add_patch(Rectangle((1.0, 1.30), 6.0, 0.75,
                           facecolor="#B71C1C", edgecolor="#7F0000", linewidth=1.0))
    ax.text(LXC, 1.85, "All mechanisms present, but NOT WIRED",
            ha="center", va="center", fontsize=11, fontweight="bold", color="white")
    ax.text(LXC, 1.50, "CRC code exists in Deserialize yet never verified on the real path.",
            ha="center", va="center", fontsize=8.8, color="#FFEB3B", fontstyle="italic")

    # ============ RIGHT: AFTER ============
    RXC = 12.0
    ax.add_patch(FancyBboxPatch((9.0, 8.85), 6.0, 0.55,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor="#1B5E20", edgecolor="#003300", linewidth=1.2))
    ax.text(RXC, 9.12, "AFTER  (current source)",
            ha="center", va="center", fontsize=12, fontweight="bold", color="white")

    # Single source of truth box at top center of right column
    ax.add_patch(FancyBboxPatch((10.0, 7.65), 4.0, 0.85,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#1B5E20", edgecolor="#003300", linewidth=1.5))
    ax.text(RXC, 8.25, "Single Source of Truth",
            ha="center", va="center", fontsize=9.8, fontweight="bold", color="white")
    ax.text(RXC, 7.92, "ReplayFile.IsVersionCompatible(v)",
            ha="center", va="center", fontsize=9.2, color="#FFEB3B", family="monospace")

    # Deserialize now self-verifies CRC (green)
    ax.add_patch(FancyBboxPatch((9.3, 6.30), 5.4, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#E8F5E9", edgecolor="#2E7D32", linewidth=1.5))
    ax.text(9.55, 7.05, "B'.  Deserialize(reader)",
            ha="left", va="center", fontsize=9.8, fontweight="bold", color="#1B5E20", family="monospace")
    ax.text(9.55, 6.77, "computes CRC32, compares -> throws",
            ha="left", va="center", fontsize=8.6, color="#2E7D32")
    ax.text(9.55, 6.50, "ReplayCorruptedException on mismatch   (double insurance)",
            ha="left", va="center", fontsize=8.4, color="#2E7D32")
    ax.text(14.5, 6.77, "FIX", ha="center", va="center", fontsize=9.5, fontweight="bold", color="#2E7D32")

    # ReplayPlayer.Load delegates (green)
    ax.add_patch(FancyBboxPatch((9.3, 5.05), 5.4, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#E8F5E9", edgecolor="#2E7D32", linewidth=1.5))
    ax.text(9.55, 5.80, "C'.  ReplayPlayer.Load",
            ha="left", va="center", fontsize=9.8, fontweight="bold", color="#1B5E20", family="monospace")
    ax.text(9.55, 5.52, "delegates to IsVersionCompatible  (range policy)",
            ha="left", va="center", fontsize=8.6, color="#2E7D32")
    ax.text(9.55, 5.25, "throws typed ReplayVersionMismatchException",
            ha="left", va="center", fontsize=8.6, color="#2E7D32")
    ax.text(14.5, 5.52, "FIX", ha="center", va="center", fontsize=9.5, fontweight="bold", color="#2E7D32")

    # ReadHeader no longer bypasses (green)
    ax.add_patch(FancyBboxPatch((9.3, 3.80), 5.4, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#E8F5E9", edgecolor="#2E7D32", linewidth=1.5))
    ax.text(9.55, 4.55, "D'.  ReplayManager.ReadHeader",
            ha="left", va="center", fontsize=9.8, fontweight="bold", color="#1B5E20", family="monospace")
    ax.text(9.55, 4.27, "reads 36 B, uses IsVersionCompatible",
            ha="left", va="center", fontsize=8.6, color="#2E7D32")
    ax.text(9.55, 4.00, "no longer bypasses version-range check",
            ha="left", va="center", fontsize=8.6, color="#2E7D32")
    ax.text(14.5, 4.27, "FIX", ha="center", va="center", fontsize=9.5, fontweight="bold", color="#2E7D32")

    # Main path now -> LoadWithValidation (green)
    ax.add_patch(FancyBboxPatch((9.3, 2.40), 5.4, 0.95,
                                boxstyle="round,pad=0.02,rounding_size=0.05",
                                facecolor="#E8F5E9", edgecolor="#2E7D32", linewidth=2.0))
    ax.text(9.55, 3.15, "MAIN PATH:  ReplayManager.LoadFromFile",
            ha="left", va="center", fontsize=9.8, fontweight="bold", color="#1B5E20", family="monospace")
    ax.text(9.55, 2.87, "File.ReadAllBytes -> LoadWithValidation  (entry A)",
            ha="left", va="center", fontsize=8.6, color="#2E7D32")
    ax.text(9.55, 2.60, "=> real client load now goes through full validation",
            ha="left", va="center", fontsize=8.6, color="#1B5E20", fontweight="bold")

    # green arrow main -> entry A (which we place implied at top). Draw arrow up to a small "A" tag.
    # Place a small A' box near top-right to receive the arrow
    ax.add_patch(FancyBboxPatch((13.7, 7.05), 1.0, 0.45,
                                boxstyle="round,pad=0.02,rounding_size=0.04",
                                facecolor="#A5D6A7", edgecolor="#2E7D32", linewidth=1.4))
    ax.text(14.2, 7.27, "A'. LoadWith\nValidation", ha="center", va="center",
            fontsize=7.6, fontweight="bold", color="#1B5E20")
    ax.add_patch(FancyArrowPatch((12.0, 3.40), (14.0, 7.00),
                                 arrowstyle="-|>", mutation_scale=22,
                                 color="#2E7D32", linewidth=2.6,
                                 connectionstyle="arc3,rad=-0.15"))
    ax.text(13.6, 5.20, "right entry", ha="left", va="center",
            fontsize=9.5, fontweight="bold", color="#2E7D32", rotation=55)

    # delegation dashed arrows from B'/C'/D' to single source
    for sy in [6.78, 5.52, 4.27]:
        ax.add_patch(FancyArrowPatch((10.0, sy), (10.5, 7.85),
                                     arrowstyle="-|>", mutation_scale=10,
                                     color="#9E9E9E", linewidth=1.0,
                                     linestyle=(0, (3, 3)),
                                     connectionstyle="arc3,rad=0.15"))
    ax.text(9.95, 6.05, "delegate", ha="right", va="center",
            fontsize=7.8, color="#616161", fontstyle="italic", rotation=80)

    # bottom green banner
    ax.add_patch(Rectangle((9.0, 1.30), 6.0, 0.75,
                           facecolor="#1B5E20", edgecolor="#003300", linewidth=1.0))
    ax.text(RXC, 1.85, "Unified entry  +  delegation",
            ha="center", va="center", fontsize=11, fontweight="bold", color="white")
    ax.text(RXC, 1.50, "One IsVersionCompatible; Deserialize self-verifies; main path forced through A.",
            ha="center", va="center", fontsize=8.6, color="#FFEB3B", fontstyle="italic")

    # ============ Bottom takeaway bar ============
    ax.add_patch(Rectangle((1.0, 0.45), 14.0, 0.65,
                           facecolor="#FFF8E1", edgecolor="#F57F17", linewidth=1.0))
    ax.text(8, 0.92, "Lesson:   having a mechanism  ≠  having protection.",
            ha="center", va="center", fontsize=10.5, fontweight="bold", color="#5D4037")
    ax.text(8, 0.62, "Don't let callers pick the wrong door. Converge validation into ONE entry and delegate everywhere.",
            ha="center", va="center", fontsize=9.2, color="#5D4037", fontstyle="italic")

    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.15)
    plt.close(fig)
    print("saved:", OUT)


if __name__ == "__main__":
    fig_replay_free()
    fig_iron_triangle()
    fig_wiring_bug()
    print("ALL DONE")
