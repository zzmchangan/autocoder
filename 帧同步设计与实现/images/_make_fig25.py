"""fig-25-01 .. fig-25-04 for Chapter 25 (Bug Localization -- from symptom to root cause).

fig-25-01: Three-step localization methodology flow (Symptom -> DivergenceFrame -> DivergenceField -> RootCause -> RegressionTest)
fig-25-02: Process-level reconnect five-step root cause chain (right-to-left 5 Whys, ROOT CAUSE marker)
fig-25-03: Defensive code masks bug -- correct vs wrong handling of hash drift (side-by-side comparison)
fig-25-04: Fake bug identification decision tree (three groups: language spec / determinism boundary / design intent)
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Circle, FancyArrowPatch, Rectangle, Polygon
import numpy as np

OUT_DIR = r"c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/"

# ---------- shared palette ----------
C_INK   = "#212121"
C_GREY  = "#546E7A"
C_BLUE  = "#1565C0"
C_BLUE_L= "#BBDEFB"
C_ORANGE= "#E65100"
C_ORG_L = "#FFE0B2"
C_GREEN = "#2E7D32"
C_GRN_L = "#E8F5E9"
C_RED   = "#C62828"
C_RED_L = "#FFEBEE"
C_PURPLE= "#6A1B9A"
C_PUR_L = "#E1BEE7"
C_TEAL  = "#00838F"
C_TEAL_L= "#E0F7FA"
C_AMBER = "#F9A825"
C_AMB_L = "#FFF8E1"


# =====================================================================
# FIG 25-01 : Three-step localization methodology flow
# =====================================================================
def fig_01():
    OUT = OUT_DIR + "fig-25-01-bug-localization-method.png"
    fig, ax = plt.subplots(figsize=(16, 9), dpi=150)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

    ax.text(0.5, 0.975, "Desync Bug Localization:  Three-Step Method + Regression Loop",
            ha="center", va="top", fontsize=16, fontweight="bold", color=C_INK)
    ax.text(0.5, 0.945,
            "Each step shrinks the search space by an order of magnitude  --  from a million-field world down to ONE field, ONE operation",
            ha="center", va="top", fontsize=10.5, color=C_GREY, fontstyle="italic")

    # ---- 5 stage boxes along a horizontal axis (top row) ----
    # Symptom -> DivergenceFrame -> DivergenceField -> RootCause -> RegressionTest
    stages = [
        # (label, sub, weapon, search_space, color, fill)
        ("1. SYMPTOM",
         "HashReport mismatch\n(OnHashDrift fires)",
         "Trigger / Entry",
         "search space:\nENTIRE world\n(millions of fields)",
         C_GREY, "#ECEFF1"),
        ("2. DIVERGENCE FRAME",
         "Which TICK first differs",
         "WEAPON: hash binary-search\n(snapshot + input replay,\nlog(N) over blind zone)",
         "search space:\n~60 frames\n(SnapshotInterval)",
         C_BLUE, C_BLUE_L),
        ("3. DIVERGENCE FIELD",
         "Which COMPONENT.FIELD\nfirst differs at that tick",
         "WEAPON: World.Diff(other)\n+ GetPerTypeHashes()\n+ GetDebugString(id)",
         "search space:\none entity,\none component",
         C_PURPLE, C_PUR_L),
        ("4. ROOT CAUSE",
         "Which OPERATION made\nthat field diverge",
         "WEAPON: minimal repro (.lsr)\n+ write-log diff\n+ code-change bisect",
         "search space:\nONE field,\nONE operation",
         C_ORANGE, C_ORG_L),
        ("5. REGRESSION TEST",
         "Pin the fix, prevent\nfuture regression",
         "WEAPON: golden test\ncross-TFM equivalence\n(2 TFMs run same expected)",
         "feedback loop:\nfix verified,\nbug closed",
         C_GREEN, C_GRN_L),
    ]

    n = len(stages)
    box_w = 0.175
    box_h = 0.30
    gap = (1.0 - 0.04 * 2 - n * box_w) / (n - 1)
    x0 = 0.04
    y_box = 0.45

    centers = []
    for i, (lab, sub, weapon, sp, col, fill) in enumerate(stages):
        x = x0 + i * (box_w + gap)
        cx = x + box_w / 2
        centers.append(cx)
        # main box
        ax.add_patch(FancyBboxPatch((x, y_box), box_w, box_h,
                                    boxstyle="round,pad=0.005,rounding_size=0.012",
                                    facecolor=fill, edgecolor=col, linewidth=2.0))
        # header strip
        hh = 0.052
        ax.add_patch(FancyBboxPatch((x, y_box + box_h - hh), box_w, hh,
                                    boxstyle="round,pad=0.005,rounding_size=0.012",
                                    facecolor=col, edgecolor=col))
        ax.text(cx, y_box + box_h - hh / 2 + 0.003, lab,
                ha="center", va="center", fontsize=9.5, fontweight="bold", color="white")
        # subtitle
        ax.text(cx, y_box + box_h - hh - 0.040, sub,
                ha="center", va="top", fontsize=8.6, color=C_INK, linespacing=1.35)
        # weapon label
        ax.text(cx, y_box + 0.110, weapon,
                ha="center", va="top", fontsize=7.8, color=col, fontweight="bold", linespacing=1.3)
        # search space
        ax.add_patch(FancyBboxPatch((x + 0.012, y_box + 0.005), box_w - 0.024, 0.062,
                                    boxstyle="round,pad=0.003,rounding_size=0.006",
                                    facecolor="white", edgecolor=col, linewidth=1.0, alpha=0.85))
        ax.text(cx, y_box + 0.057, sp,
                ha="center", va="top", fontsize=7.4, color=col, linespacing=1.25, fontstyle="italic")

    # ---- forward arrows between stages ----
    for i in range(n - 1):
        x_from = centers[i] + box_w / 2 - 0.003
        x_to = centers[i + 1] - box_w / 2 + 0.003
        y_mid = y_box + box_h / 2
        ax.add_patch(FancyArrowPatch((x_from, y_mid), (x_to, y_mid),
                                     arrowstyle="-|>", mutation_scale=18,
                                     color=C_INK, linewidth=2.2,
                                     connectionstyle="arc3,rad=0"))

    # ---- regression loop: big curved arrow from stage 5 back to stage 1/2 ----
    arc_y_top = y_box + box_h + 0.135
    ax.add_patch(FancyArrowPatch((centers[4], y_box + box_h + 0.015),
                                 (centers[0], y_box + box_h + 0.015),
                                 arrowstyle="-|>", mutation_scale=22,
                                 color=C_GREEN, linewidth=2.6,
                                 connectionstyle="arc3,rad=-0.42",
                                 linestyle="--"))
    ax.text((centers[0] + centers[4]) / 2, arc_y_top,
            "REGRESSION TEST  --  close the loop: fix verified, bug pinned, no future regression",
            ha="center", va="center", fontsize=10.5, fontweight="bold", color=C_GREEN,
            bbox=dict(boxstyle="round,pad=0.4", facecolor=C_GRN_L, edgecolor=C_GREEN, linewidth=1.4))

    # ---- bottom: the three questions this method answers ----
    ax.add_patch(FancyBboxPatch((0.04, 0.05), 0.92, 0.28,
                                boxstyle="round,pad=0.006,rounding_size=0.010",
                                facecolor="#FAFAFA", edgecolor="#455A64", linewidth=1.4))
    ax.text(0.5, 0.305, "The Three Questions Localization Must Answer  (any one wrong -> all downstream wrong)",
            ha="center", va="top", fontsize=11, fontweight="bold", color="#263238")

    qs = [
        ("Q1", "Which frame first\ndiverged?", "DIVERGENCE FRAME",
         "binary-search the snapshot + input-replay blind zone", C_BLUE),
        ("Q2", "Which field first\ndiverged at that frame?", "DIVERGENCE FIELD",
         "World.Diff three-level drill:  type-level -> entity-level -> field-level", C_PURPLE),
        ("Q3", "Which operation made\nthat field diverge?", "ROOT CAUSE OPERATION",
         "minimal repro + write-log diff + code-change bisect -> ONE line", C_ORANGE),
    ]
    qw = 0.27
    for i, (qn, q, tag, desc, col) in enumerate(qs):
        qx = 0.07 + i * (qw + 0.02)
        ax.add_patch(FancyBboxPatch((qx, 0.075), qw, 0.20,
                                    boxstyle="round,pad=0.004,rounding_size=0.008",
                                    facecolor="white", edgecolor=col, linewidth=1.6))
        # Q badge
        ax.add_patch(Circle((qx + 0.030, 0.245), 0.018,
                            facecolor=col, edgecolor="white", linewidth=1.5))
        ax.text(qx + 0.030, 0.245, qn, ha="center", va="center",
                fontsize=9, fontweight="bold", color="white")
        ax.text(qx + 0.058, 0.250, q, ha="left", va="center",
                fontsize=9, fontweight="bold", color=C_INK, linespacing=1.25)
        ax.text(qx + qw / 2, 0.175, tag, ha="center", va="center",
                fontsize=9.5, fontweight="bold", color=col)
        ax.text(qx + qw / 2, 0.130, desc, ha="center", va="center",
                fontsize=7.8, color="#37474F", linespacing=1.3, fontstyle="italic")

    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.18)
    plt.close(fig)
    print("saved:", OUT)


# =====================================================================
# FIG 25-02 : Process-level reconnect five-step root cause chain (right-to-left)
# =====================================================================
def fig_02():
    OUT = OUT_DIR + "fig-25-02-reconnect-root-cause-chain.png"
    fig, ax = plt.subplots(figsize=(17, 9), dpi=150)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

    ax.text(0.5, 0.975, "Five-Step Root Cause Chain:  Process-Level Reconnect Bug  (5 Whys, right-to-left)",
            ha="center", va="top", fontsize=15.5, fontweight="bold", color=C_INK)
    ax.text(0.5, 0.945,
            "The symptom (right) is 4 mechanism-hops away from the root cause (left).  Each hop alone looks correct -- only the source is the disease.",
            ha="center", va="top", fontsize=10, color=C_GREY, fontstyle="italic")

    # ---- direction banner ----
    ax.add_patch(FancyBboxPatch((0.04, 0.875), 0.92, 0.040,
                                boxstyle="round,pad=0.004,rounding_size=0.008",
                                facecolor="#263238", edgecolor="#263238"))
    ax.text(0.10, 0.895, "ROOT CAUSE  (fix here)",
            ha="left", va="center", fontsize=10, fontweight="bold", color=C_GREEN)
    ax.text(0.90, 0.895, "SYMPTOM  (what user sees)",
            ha="right", va="center", fontsize=10, fontweight="bold", color=C_RED)
    # big reverse arrow
    ax.add_patch(FancyArrowPatch((0.86, 0.895), (0.14, 0.895),
                                 arrowstyle="-|>", mutation_scale=20,
                                 color=C_AMBER, linewidth=2.4))
    ax.text(0.5, 0.895, "5 WHYS  --  reverse causal chain",
            ha="center", va="center", fontsize=10, fontweight="bold", color="white")

    # ---- 5 step boxes (left=cause, right=symptom) ----
    # steps[i] = (why_num, mechanism, why_answer_short, role, color, fill)
    steps = [
        ("WHY 5\n(root)",
         "Credential lives in MEMORY only\n(NullReconnectCredentialStore)",
         "Host does not know it must\ninject IReconnectCredentialStore\n(zero-dep default hides this)",
         "C-5", C_GREEN, C_GRN_L),
        ("WHY 4",
         "Process exit -> memory cleared\n-> ReconnectToken LOST",
         "Token only persisted if host\ninjects a file/PlayerPrefs store;\ndefault does nothing",
         "C-4", C_TEAL, C_TEAL_L),
        ("WHY 3",
         "Client has NO token after restart\n-> ConnectAsync picks\n'brand-new join' branch",
         "Without token, client cannot\nprove it is the same player;\nfalls back to fresh JoinRequest",
         "C-3", C_BLUE, C_BLUE_L),
        ("WHY 2",
         "Bare JoinRequest reaches server's\n'TopNumber' (take-over) branch\n-> token mismatch",
         "Take-over branch now requires\nReconnectToken (P0-2 fix);\nbare request is REJECTED",
         "C-2", C_PURPLE, C_PUR_L),
        ("WHY 1\n(symptom)",
         "Server degrades to 'create new room'\n-> player lands in EMPTY room\n(not the original battle)",
         "Teammate sees player 'leave';\nplayer thinks they disconnected\nbut actually joined a new room",
         "C-1", C_RED, C_RED_L),
    ]

    n = len(steps)
    box_w = 0.165
    box_h = 0.46
    gap = (1.0 - 0.04 * 2 - n * box_w) / (n - 1)
    x0 = 0.04
    y_box = 0.30

    centers = []
    for i, (why, mech, ans, role, col, fill) in enumerate(steps):
        x = x0 + i * (box_w + gap)
        cx = x + box_w / 2
        centers.append(cx)
        # box
        ax.add_patch(FancyBboxPatch((x, y_box), box_w, box_h,
                                    boxstyle="round,pad=0.005,rounding_size=0.012",
                                    facecolor=fill, edgecolor=col, linewidth=2.2))
        # why badge header
        hh = 0.058
        ax.add_patch(FancyBboxPatch((x, y_box + box_h - hh), box_w, hh,
                                    boxstyle="round,pad=0.005,rounding_size=0.012",
                                    facecolor=col, edgecolor=col))
        ax.text(cx, y_box + box_h - hh / 2 + 0.003, why,
                ha="center", va="center", fontsize=9.5, fontweight="bold", color="white", linespacing=1.1)
        # mechanism
        ax.text(cx, y_box + box_h - hh - 0.030, "MECHANISM",
                ha="center", va="top", fontsize=7.6, fontweight="bold", color=col)
        ax.text(cx, y_box + box_h - hh - 0.055, mech,
                ha="center", va="top", fontsize=8.4, color=C_INK, linespacing=1.35)
        # answer
        ay = y_box + 0.135
        ax.add_patch(FancyBboxPatch((x + 0.010, y_box + 0.012), box_w - 0.020, ay - y_box - 0.012,
                                    boxstyle="round,pad=0.003,rounding_size=0.006",
                                    facecolor="white", edgecolor=col, linewidth=1.0, alpha=0.9))
        ax.text(cx, ay - 0.008, "WHY-ANSWER",
                ha="center", va="top", fontsize=7.4, fontweight="bold", color=col)
        ax.text(cx, ay - 0.030, ans,
                ha="center", va="top", fontsize=7.8, color="#37474F", linespacing=1.3, fontstyle="italic")
        # role badge
        ax.add_patch(FancyBboxPatch((x + box_w / 2 - 0.030, y_box - 0.022), 0.060, 0.024,
                                    boxstyle="round,pad=0.003,rounding_size=0.008",
                                    facecolor=col, edgecolor=col))
        ax.text(cx, y_box - 0.010, role, ha="center", va="center",
                fontsize=8.5, fontweight="bold", color="white")

    # ---- arrows between steps (each points right: cause -> effect) ----
    for i in range(n - 1):
        x_from = centers[i] + box_w / 2 - 0.003
        x_to = centers[i + 1] - box_w / 2 + 0.003
        y_mid = y_box + box_h / 2
        ax.add_patch(FancyArrowPatch((x_from, y_mid), (x_to, y_mid),
                                     arrowstyle="-|>", mutation_scale=20,
                                     color=C_INK, linewidth=2.4,
                                     connectionstyle="arc3,rad=0"))
        # "so" label above each arrow
        ax.text((x_from + x_to) / 2, y_mid + 0.030, "therefore",
                ha="center", va="bottom", fontsize=8, color=C_GREY, fontstyle="italic")

    # ---- ROOT CAUSE marker on the leftmost box ----
    rcx = centers[0]
    rcy = y_box + box_h + 0.06
    ax.add_patch(FancyBboxPatch((rcx - 0.13, rcy - 0.035), 0.26, 0.045,
                                boxstyle="round,pad=0.004,rounding_size=0.010",
                                facecolor=C_GREEN, edgecolor=C_GREEN, linewidth=2.0))
    ax.text(rcx, rcy - 0.012, "ROOT CAUSE",
            ha="center", va="center", fontsize=12, fontweight="bold", color="white")
    ax.add_patch(FancyArrowPatch((rcx, rcy - 0.035), (rcx, y_box + box_h + 0.005),
                                 arrowstyle="-|>", mutation_scale=18,
                                 color=C_GREEN, linewidth=2.6))

    # ---- SYMPTOM marker on the rightmost box ----
    sx = centers[n - 1]
    sy = y_box + box_h + 0.06
    ax.add_patch(FancyBboxPatch((sx - 0.11, sy - 0.035), 0.22, 0.045,
                                boxstyle="round,pad=0.004,rounding_size=0.010",
                                facecolor=C_RED, edgecolor=C_RED, linewidth=2.0))
    ax.text(sx, sy - 0.012, "SYMPTOM",
            ha="center", va="center", fontsize=12, fontweight="bold", color="white")
    ax.add_patch(FancyArrowPatch((sx, sy - 0.035), (sx, y_box + box_h + 0.005),
                                 arrowstyle="-|>", mutation_scale=18,
                                 color=C_RED, linewidth=2.6))

    # ---- bottom: the lesson ----
    ax.add_patch(FancyBboxPatch((0.04, 0.045), 0.92, 0.18,
                                boxstyle="round,pad=0.006,rounding_size=0.010",
                                facecolor="#FFF8E1", edgecolor=C_AMBER, linewidth=1.8))
    ax.text(0.06, 0.20, "THE LESSON",
            ha="left", va="top", fontsize=11, fontweight="bold", color=C_AMBER)
    lessons = [
        "Fixing the symptom (rightmost: 'new room' logic) only makes the symptom disappear -- the real disease (no credential persistence) remains, and resurfaces in other scenarios.",
        "Fixing a middle hop (e.g. take-over rejection) is also symptomatic -- token is empty, validation will always fail, you cannot 'fix' it there.",
        "ONLY fixing the source (leftmost: host must inject IReconnectCredentialStore) is curative.  90% of desync/reconnect root causes live in 'seemingly unrelated' mechanisms.",
    ]
    yy = 0.175
    for L in lessons:
        ax.text(0.07, yy, "*", ha="left", va="top", fontsize=11, fontweight="bold", color=C_AMBER)
        ax.text(0.085, yy, L, ha="left", va="top", fontsize=8.8, color="#5D4037", linespacing=1.4)
        yy -= 0.045

    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.18)
    plt.close(fig)
    print("saved:", OUT)


# =====================================================================
# FIG 25-03 : Defensive code masks bug -- correct vs wrong (side-by-side)
# =====================================================================
def fig_03():
    OUT = OUT_DIR + "fig-25-03-defensive-code-masks-bug.png"
    fig, ax = plt.subplots(figsize=(16, 10), dpi=150)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

    ax.text(0.5, 0.975, "Defensive Code Can MASK a Bug:  Hash Drift Handling  (Bug 6, dual-track)",
            ha="center", va="top", fontsize=15.5, fontweight="bold", color=C_INK)
    ax.text(0.5, 0.945,
            "Same trigger (incremental hash != full hash = DESYNC SIGNAL), two opposite outcomes.  Filtering a TRUE signal is worse than having no defense at all.",
            ha="center", va="top", fontsize=10, color=C_GREY, fontstyle="italic")

    # ---- shared trigger banner at top ----
    ax.add_patch(FancyBboxPatch((0.20, 0.855), 0.60, 0.055,
                                boxstyle="round,pad=0.005,rounding_size=0.012",
                                facecolor=C_AMBER, edgecolor=C_AMBER, linewidth=2.0))
    ax.text(0.5, 0.898, "TRIGGER",
            ha="center", va="center", fontsize=10, fontweight="bold", color="white")
    ax.text(0.5, 0.875, "incremental_hash  !=  full_hash    (drift detected)",
            ha="center", va="center", fontsize=11, fontweight="bold", color="white", family="monospace")

    # split arrows down to two columns
    ax.add_patch(FancyArrowPatch((0.40, 0.855), (0.25, 0.815),
                                 arrowstyle="-|>", mutation_scale=18, color=C_INK, linewidth=2.0))
    ax.add_patch(FancyArrowPatch((0.60, 0.855), (0.75, 0.815),
                                 arrowstyle="-|>", mutation_scale=18, color=C_INK, linewidth=2.0))

    # ---- two columns ----
    col_w = 0.44
    col_h = 0.62
    y_top = 0.81
    # LEFT: correct
    lx = 0.035
    ax.add_patch(FancyBboxPatch((lx, y_top - col_h), col_w, col_h,
                                boxstyle="round,pad=0.006,rounding_size=0.012",
                                facecolor=C_GRN_L, edgecolor=C_GREEN, linewidth=2.4))
    ax.add_patch(FancyBboxPatch((lx, y_top - 0.058), col_w, 0.058,
                                boxstyle="round,pad=0.006,rounding_size=0.012",
                                facecolor=C_GREEN, edgecolor=C_GREEN))
    ax.text(lx + col_w / 2, y_top - 0.022, "CORRECT HANDLING",
            ha="center", va="center", fontsize=12.5, fontweight="bold", color="white")
    ax.text(lx + col_w / 2, y_top - 0.044, "drift == desync already happened  ->  REPORT IT, KEEP THE CRIME SCENE",
            ha="center", va="center", fontsize=8.2, color="#E8F5E9", fontstyle="italic")

    # RIGHT: wrong
    rx = 0.525
    ax.add_patch(FancyBboxPatch((rx, y_top - col_h), col_w, col_h,
                                boxstyle="round,pad=0.006,rounding_size=0.012",
                                facecolor=C_RED_L, edgecolor=C_RED, linewidth=2.4))
    ax.add_patch(FancyBboxPatch((rx, y_top - 0.058), col_w, 0.058,
                                boxstyle="round,pad=0.006,rounding_size=0.012",
                                facecolor=C_RED, edgecolor=C_RED))
    ax.text(rx + col_w / 2, y_top - 0.022, "ORIGINAL 'DEFENSIVE' HANDLING  (the mask)",
            ha="center", va="center", fontsize=12.5, fontweight="bold", color="white")
    ax.text(rx + col_w / 2, y_top - 0.044, "drift == 'snapshot self-inconsistency'  ->  SILENTLY 'HEAL' IT",
            ha="center", va="center", fontsize=8.2, color="#FFEBEE", fontstyle="italic")

    # ---- 4-step vertical flow inside each column ----
    def step_block(xc, yc, num, title, code, note, col, fill, code_col):
        # num badge
        ax.add_patch(Circle((xc - 0.18, yc), 0.016,
                            facecolor=col, edgecolor="white", linewidth=1.5))
        ax.text(xc - 0.18, yc, num, ha="center", va="center",
                fontsize=8.5, fontweight="bold", color="white")
        # title
        ax.text(xc - 0.155, yc + 0.003, title, ha="left", va="center",
                fontsize=9.5, fontweight="bold", color=col)
        # code box
        ax.add_patch(FancyBboxPatch((xc - 0.195, yc - 0.055), 0.39, 0.045,
                                    boxstyle="round,pad=0.003,rounding_size=0.006",
                                    facecolor="white", edgecolor=col, linewidth=1.0))
        ax.text(xc, yc - 0.033, code, ha="center", va="center",
                fontsize=8.0, color=code_col, family="monospace", linespacing=1.3)
        # note
        ax.text(xc, yc - 0.075, note, ha="center", va="top",
                fontsize=7.8, color="#37474F", fontstyle="italic", linespacing=1.3)

    # LEFT steps
    lcx = lx + col_w / 2
    step_block(lcx, 0.72, "1", "DETECT drift",
               "if (incHash != fullHash)",
               "drift IS the desync signal --\n99% means someone mutated state\noutside the XOR channel",
               C_GREEN, "white", "#1B5E20")
    step_block(lcx, 0.60, "2", "FIRE OnHashDrift event",
               "OnHashDrift?.Invoke(inc, full, delta);",
               "event fires regardless of recovery\npolicy -- observers always see it",
               C_GREEN, "white", "#1B5E20")
    step_block(lcx, 0.48, "3", "POLICY decides control flow",
               "Throw   ->  HashDriftException\nContinue->  log + carry on (compat)",
               "dev mode = Throw (expose root cause)\nprod mode = Continue (backward compat)",
               C_GREEN, "white", "#1B5E20")
    step_block(lcx, 0.36, "4", "KEEP the crime scene",
               "// do NOT overwrite inc with full\n// snapshot keeps both hashes",
               "LoadState re-check finds drift ->\nscene preserved, alarm sounded,\npost-mortem can root-cause",
               C_GREEN, "white", "#1B5E20")
    # outcome
    ax.add_patch(FancyBboxPatch((lx + 0.02, y_top - col_h + 0.015), col_w - 0.04, 0.055,
                                boxstyle="round,pad=0.004,rounding_size=0.008",
                                facecolor=C_GREEN, edgecolor=C_GREEN))
    ax.text(lcx, y_top - col_h + 0.052, "OUTCOME",
            ha="center", va="top", fontsize=8.5, fontweight="bold", color="white")
    ax.text(lcx, y_top - col_h + 0.030, "bug EXPOSED at first drift  ->  located & fixed in dev",
            ha="center", va="center", fontsize=9, fontweight="bold", color="white")

    # RIGHT steps
    rcx = rx + col_w / 2
    step_block(rcx, 0.72, "1", "DETECT drift",
               "if (incHash != fullHash)",
               "SAME trigger -- but author\ninterprets drift as 'snapshot\nself-inconsistency noise'",
               C_RED, "white", "#B71C1C")
    step_block(rcx, 0.60, "2", "SILENTLY log",
               "// Log.Warn(\"hash drift, healing\")",
               "no event fired, no exception,\nno observer can react",
               C_RED, "white", "#B71C1C")
    step_block(rcx, 0.48, "3", "'HEAL' the snapshot",
               "incHash = fullHash;   // overwrite!",
               "incremental hash 'calibrated' to\nfull hash -- snapshot now looks\nself-consistent",
               C_RED, "white", "#B71C1C")
    step_block(rcx, 0.36, "4", "DESTROY the crime scene",
               "// subsequent snapshots inherit\n// the 'healed' (false) hash",
               "LoadState re-check == True forever\n(both hashes already aligned)\nscene WIPED, alarm NEVER fires",
               C_RED, "white", "#B71C1C")
    # outcome
    ax.add_patch(FancyBboxPatch((rx + 0.02, y_top - col_h + 0.015), col_w - 0.04, 0.055,
                                boxstyle="round,pad=0.004,rounding_size=0.008",
                                facecolor=C_RED, edgecolor=C_RED))
    ax.text(rcx, y_top - col_h + 0.052, "OUTCOME",
            ha="center", va="top", fontsize=8.5, fontweight="bold", color="white")
    ax.text(rcx, y_top - col_h + 0.030, "bug BURIED  ->  desync runs silent, post-mortem finds nothing",
            ha="center", va="center", fontsize=9, fontweight="bold", color="white")

    # ---- bottom banner: the judge criterion ----
    ax.add_patch(FancyBboxPatch((0.04, 0.025), 0.92, 0.12,
                                boxstyle="round,pad=0.006,rounding_size=0.010",
                                facecolor="#263238", edgecolor="#263238"))
    ax.text(0.5, 0.125, "JUDGE CRITERION  --  is your defense filtering NOISE or SIGNAL?",
            ha="center", va="top", fontsize=11.5, fontweight="bold", color=C_AMBER)
    ax.text(0.07, 0.090, "NOISE (safe to filter):",
            ha="left", va="top", fontsize=9.5, fontweight="bold", color=C_GREEN)
    ax.text(0.07, 0.068, "network jitter, transient retry blips, random CPU glitch  ->  defense HELPS stability",
            ha="left", va="top", fontsize=8.6, color="#A5D6A7", fontstyle="italic")
    ax.text(0.52, 0.090, "SIGNAL (must NEVER filter):",
            ha="left", va="top", fontsize=9.5, fontweight="bold", color=C_RED)
    ax.text(0.52, 0.068, "hash drift, logic exception, ownership violation  ->  filtering these CRIMES against the bug",
            ha="left", va="top", fontsize=8.6, color="#EF9A9A", fontstyle="italic")
    ax.text(0.5, 0.040, "Same lesson applies to:  catch-all-and-swallow  /  silent degrade-to-default  /  Debug.Assert off in Release",
            ha="center", va="top", fontsize=8.4, color="#ECEFF1", fontstyle="italic")

    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.18)
    plt.close(fig)
    print("saved:", OUT)


# =====================================================================
# FIG 25-04 : Fake bug identification decision tree
# =====================================================================
def fig_04():
    OUT = OUT_DIR + "fig-25-04-fake-bug-decision-tree.png"
    fig, ax = plt.subplots(figsize=(16, 11), dpi=150)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

    ax.text(0.5, 0.975, "Fake-Bug Identification Decision Tree  (Chapter 25 signature -- weed out false positives BEFORE fixing)",
            ha="center", va="top", fontsize=14.5, fontweight="bold", color=C_INK)
    ax.text(0.5, 0.945,
            "Three groups of questions.  If any group's 'fake-bug criterion' hits, do NOT touch it -- it is not a bug.  All three miss => real bug.",
            ha="center", va="top", fontsize=10, color=C_GREY, fontstyle="italic")

    # ---- root node ----
    root_cx, root_cy = 0.5, 0.875
    ax.add_patch(FancyBboxPatch((root_cx - 0.13, root_cy - 0.030), 0.26, 0.052,
                                boxstyle="round,pad=0.005,rounding_size=0.012",
                                facecolor=C_INK, edgecolor=C_INK))
    ax.text(root_cx, root_cy + 0.010, "Suspect looks like a bug",
            ha="center", va="center", fontsize=10.5, fontweight="bold", color="white")
    ax.text(root_cx, root_cy - 0.014, "(overflow? div-zero? float? object? mismatch?)",
            ha="center", va="center", fontsize=7.8, color="#B0BEC5", fontstyle="italic")

    # ---- three group nodes (level 1) ----
    groups = [
        # (cx, label, question, color, fill)
        (0.175, "GROUP 1", "Language / Runtime\nSPEC guarantee it?",   C_BLUE,   C_BLUE_L),
        (0.500, "GROUP 2", "Does it touch\nthe LOGIC path?\n(serialize/hash/physics)", C_PURPLE, C_PUR_L),
        (0.825, "GROUP 3", "Is it DESIGN INTENT\n(contract/trade-off)\nnot an oversight?", C_TEAL,   C_TEAL_L),
    ]
    g_cy = 0.74
    g_w, g_h = 0.235, 0.10
    for cx, lab, q, col, fill in groups:
        ax.add_patch(FancyArrowPatch((root_cx, root_cy - 0.030), (cx, g_cy + g_h / 2),
                                     arrowstyle="-|>", mutation_scale=16,
                                     color=C_GREY, linewidth=1.8,
                                     connectionstyle="arc3,rad=0"))
        ax.add_patch(FancyBboxPatch((cx - g_w / 2, g_cy - g_h / 2), g_w, g_h,
                                    boxstyle="round,pad=0.005,rounding_size=0.012",
                                    facecolor=fill, edgecolor=col, linewidth=2.2))
        ax.add_patch(FancyBboxPatch((cx - g_w / 2, g_cy + g_h / 2 - 0.026), g_w, 0.026,
                                    boxstyle="round,pad=0.005,rounding_size=0.012",
                                    facecolor=col, edgecolor=col))
        ax.text(cx, g_cy + g_h / 2 - 0.013, lab,
                ha="center", va="center", fontsize=9.5, fontweight="bold", color="white")
        ax.text(cx, g_cy - 0.012, q, ha="center", va="center",
                fontsize=8.4, color=C_INK, linespacing=1.3, fontweight="bold")

    # ---- under each group: YES => FAKE BUG (leaf), NO => continue down ----
    leaf_cy = 0.50
    leaf_w, leaf_h = 0.235, 0.18

    # helper to draw a YES-branch leaf (fake bug) and a NO-branch arrow continuing
    def group_branches(gcx, gcol, gfill, yes_label, yes_examples, yes_rule):
        # YES branch: leaf on the left-ish under group
        # leaf box
        ax.add_patch(FancyArrowPatch((gcx - 0.02, g_cy - g_h / 2), (gcx - 0.05, leaf_cy + leaf_h / 2),
                                     arrowstyle="-|>", mutation_scale=15,
                                     color=C_GREEN, linewidth=2.0,
                                     connectionstyle="arc3,rad=0.15"))
        ax.text(gcx - 0.085, (g_cy - g_h / 2 + leaf_cy + leaf_h / 2) / 2 + 0.018, "YES",
                ha="center", va="center", fontsize=9, fontweight="bold", color=C_GREEN,
                bbox=dict(boxstyle="round,pad=0.18", facecolor="white", edgecolor=C_GREEN, linewidth=1.2))
        # leaf (fake bug)
        ax.add_patch(FancyBboxPatch((gcx - 0.135, leaf_cy - leaf_h / 2), leaf_w, leaf_h,
                                    boxstyle="round,pad=0.005,rounding_size=0.012",
                                    facecolor=C_GRN_L, edgecolor=C_GREEN, linewidth=2.2))
        ax.add_patch(FancyBboxPatch((gcx - 0.135, leaf_cy + leaf_h / 2 - 0.030), leaf_w, 0.030,
                                    boxstyle="round,pad=0.005,rounding_size=0.012",
                                    facecolor=C_GREEN, edgecolor=C_GREEN))
        ax.text(gcx - 0.135 + leaf_w / 2, leaf_cy + leaf_h / 2 - 0.015, "FAKE BUG  --  do NOT fix",
                ha="center", va="center", fontsize=8.8, fontweight="bold", color="white")
        ax.text(gcx - 0.135 + leaf_w / 2, leaf_cy + 0.040, yes_label,
                ha="center", va="top", fontsize=8.2, color="#1B5E20", fontweight="bold", linespacing=1.3)
        ax.text(gcx - 0.135 + leaf_w / 2, leaf_cy + 0.005, yes_examples,
                ha="center", va="top", fontsize=7.2, color="#2E7D32", linespacing=1.3, fontstyle="italic")
        # rule footer
        ax.add_patch(FancyBboxPatch((gcx - 0.128, leaf_cy - leaf_h / 2 + 0.005), leaf_w - 0.014, 0.030,
                                    boxstyle="round,pad=0.003,rounding_size=0.006",
                                    facecolor="white", edgecolor=C_GREEN, linewidth=1.0, alpha=0.9))
        ax.text(gcx - 0.135 + leaf_w / 2, leaf_cy - leaf_h / 2 + 0.020, yes_rule,
                ha="center", va="center", fontsize=6.9, color="#1B5E20", linespacing=1.25)
        # NO branch: arrow down to converge at bottom REAL BUG node
        ax.add_patch(FancyArrowPatch((gcx + 0.02, g_cy - g_h / 2), (gcx + 0.05, leaf_cy + leaf_h / 2 - 0.04),
                                     arrowstyle="-|>", mutation_scale=14,
                                     color=C_GREY, linewidth=1.4,
                                     connectionstyle="arc3,rad=-0.15"))
        ax.text(gcx + 0.080, (g_cy - g_h / 2 + leaf_cy) / 2, "NO",
                ha="center", va="center", fontsize=8.5, fontweight="bold", color=C_GREY,
                bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor=C_GREY, linewidth=1.0))

    group_branches(0.175, C_BLUE, C_BLUE_L,
                   "Spec already covers it",
                   "fake #1  LFloat(long) ctor overflow\n   (never called + wraparound is DEFINED)\nfake #2  LFloat / int div-by-zero\n   (C# long/0 ALWAYS throws)",
                   "rule: grep call sites + check\nUB-vs-defined semantics")
    group_branches(0.500, C_PURPLE, C_PUR_L,
                   "Stays OUTSIDE determinism boundary",
                   "fake #3  Sqrt double initial guess\n   (only affects convergence rounds)\nfake #4  LockstepMetrics float\n   (pure diagnostics, not serialized)\nfake #5  LRigidbody3D.UserData object\n   (not in serialize/hash path)",
                   "rule: does the value ENTER\nthe hash?  if no -> safe")
    group_branches(0.825, C_TEAL, C_TEAL_L,
                   "It IS the design (a trade-off)",
                   "fake #6  QuadTree cross-boundary\n   object stored in MULTIPLE nodes\n   (space-partition standard practice,\n    query de-dups via HashSet)",
                   "rule: oversight or trade-off?\nknow the data structure's\ncanonical design")

    # ---- converge NO arrows to bottom REAL BUG node ----
    real_cx, real_cy = 0.5, 0.20
    for gcx in [0.175, 0.500, 0.825]:
        ax.add_patch(FancyArrowPatch((gcx + 0.05, leaf_cy + leaf_h / 2 - 0.04),
                                     (real_cx, real_cy + 0.040),
                                     arrowstyle="-|>", mutation_scale=14,
                                     color=C_RED, linewidth=1.6,
                                     connectionstyle="arc3,rad=0" if gcx == 0.5 else "arc3,rad=0.15"))
    # real bug node
    ax.add_patch(FancyBboxPatch((real_cx - 0.18, real_cy - 0.040), 0.36, 0.080,
                                boxstyle="round,pad=0.006,rounding_size=0.014",
                                facecolor=C_RED, edgecolor=C_RED, linewidth=2.6))
    ax.text(real_cx, real_cy + 0.018, "ALL THREE MISS  =>  REAL BUG",
            ha="center", va="center", fontsize=11, fontweight="bold", color="white")
    ax.text(real_cx, real_cy - 0.008, "enter Three-Step Localization (fig-25-01)",
            ha="center", va="center", fontsize=8.4, color="#FFEBEE", fontstyle="italic")
    ax.text(real_cx, real_cy - 0.025, "fix it -- with the full root-cause workflow",
            ha="center", va="center", fontsize=8.0, color="#FFEBEE", fontstyle="italic")

    # ---- legend strip at very bottom ----
    ax.add_patch(FancyBboxPatch((0.04, 0.02), 0.92, 0.075,
                                boxstyle="round,pad=0.005,rounding_size=0.010",
                                facecolor="#FAFAFA", edgecolor="#455A64", linewidth=1.4))
    ax.text(0.06, 0.080, "THE THREE CAPABILITIES",
            ha="left", va="top", fontsize=9.5, fontweight="bold", color="#263238")
    caps = [
        ("Language spec",      "know C# spec vs C/C++ UB",            C_BLUE),
        ("Determinism boundary", "know what enters hash vs not",      C_PURPLE),
        ("Design intent",      "know canonical data-structure design", C_TEAL),
    ]
    cx = 0.06
    for name, desc, col in caps:
        ax.add_patch(FancyBboxPatch((cx, 0.032), 0.018, 0.018,
                                    boxstyle="round,pad=0.002,rounding_size=0.005",
                                    facecolor=col, edgecolor=col))
        ax.text(cx + 0.026, 0.050, name, ha="left", va="center",
                fontsize=8.8, fontweight="bold", color=col)
        ax.text(cx + 0.026, 0.036, desc, ha="left", va="center",
                fontsize=7.6, color="#37474F", fontstyle="italic")
        cx += 0.305
    ax.text(0.98, 0.080,
            "8 of 15 suspects were EXCLUDED as fake bugs in 4 audit rounds  --  this IS engineering maturity",
            ha="right", va="top", fontsize=8.2, color=C_AMBER, fontweight="bold", fontstyle="italic")

    plt.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.18)
    plt.close(fig)
    print("saved:", OUT)


if __name__ == "__main__":
    fig_01()
    fig_02()
    fig_03()
    fig_04()
    print("all done")
