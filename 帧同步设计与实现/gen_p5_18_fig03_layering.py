"""fig-18-sdk-layering: SDK layering. Core/Network engine-agnostic pure layer,
Games pure-logic layer, Clients render-adaptation layer (Raylib/Unity/...).
Core zero-dependency + host injects IReconnectCredentialStore / ILockstepLogger."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch, FancyArrowPatch

fig, ax = plt.subplots(figsize=(15, 10))
ax.set_xlim(0, 15)
ax.set_ylim(0, 10.5)
ax.axis("off")

ax.text(7.5, 10.15,
        "SDK Layering  --  engine-agnostic core, host injects platform concerns",
        ha="center", va="center", fontsize=14, fontweight="bold", color="#0b3d91")

# ============ Main stacked layers (left 2/3) ============
# Four layers, top to bottom: Clients / Games / Network / Core
# Each layer is a wide box. Left column shows layer role, dependency arrows on right.

layers = [
    # (label, sub, fill, edge, text_color, y, h)
    ("Clients  (render layer)",
     "RaylibClient   |   UnityClient   |   WebGL/other engines\n"
     "-> writes Unity PlayerPrefs / Raylib file / localStorage\n"
     "-> calls engine graphics API (Vector3, float)",
     "#fde2b0", "#a06000", "#1a1a1a", 7.0, 1.7),
    ("Games  (pure logic, reusable)",
     "TankGame   |   BomberGame   |   your game\n"
     "-> implements ISimulation (6 methods: Init/Tick/SaveState/LoadState/Hash/Reset)\n"
     "-> pure logic, no engine reference, directly reusable",
     "#cfe8d4", "#2a7a2a", "#1a1a1a", 5.0, 1.7),
    ("Lockstep.Network  (transport + driver + builder)",
     "LockstepServerBuilder / LockstepClientBuilder / LockstepDriver\n"
     "IServerTransport / INetworkClient / IReconnectCredentialStore / ILockstepLogger\n"
     "-> depends ONLY on Core + .NET BCL (no engine)",
     "#d6e4f5", "#0b3d91", "#1a1a1a", 3.0, 1.7),
    ("Lockstep.Core  (pure, zero-dependency)",
     "LFloat / LMath (Q48.16)   |   SafeECS / ISimulation\n"
     "deterministic PRNG   |   BitWriter serialization   |   replay\n"
     "-> ZERO engine dependency (csproj has no UnityEngine / Raylib)",
     "#e8e8e8", "#1a1a1a", "#1a1a1a", 1.0, 1.7),
]

box_x = 0.6
box_w = 9.2
for label, sub, fc, ec, tc, y, h in layers:
    rect = FancyBboxPatch((box_x, y), box_w, h,
                          boxstyle="round,pad=0.02,rounding_size=0.08",
                          facecolor=fc, edgecolor=ec, linewidth=1.6)
    ax.add_patch(rect)
    ax.text(box_x + 0.3, y + h - 0.28, label,
            ha="left", va="center", fontsize=11.5, fontweight="bold",
            color=ec, family="monospace")
    ax.text(box_x + 0.3, y + h / 2 - 0.2, sub,
            ha="left", va="center", fontsize=9.2, color=tc,
            family="monospace", linespacing=1.3)

# Dependency-direction arrows on the LEFT (downward: upper layer depends on lower)
# Show "depends on ->" between adjacent layers
arrow_x_left = 0.15
for i in range(3):
    y_top = layers[i][5]          # y of current layer
    y_bot = layers[i + 1][5] + layers[i + 1][6]  # top of next layer
    mid = (y_top + y_bot) / 2
    ax.annotate("", xy=(arrow_x_left, y_bot - 0.02),
                xytext=(arrow_x_left, y_top + 0.02),
                arrowprops=dict(arrowstyle="->", color="#444", lw=1.4))
ax.text(arrow_x_left, (layers[0][5] + layers[3][5] + layers[3][6]) / 2,
        "depends\non", ha="center", va="center", fontsize=8.5, color="#444",
        fontstyle="italic", rotation=90)

# Strict bottom border around Core = zero-dependency badge
core_y, core_h = layers[3][5], layers[3][6]
badge = FancyBboxPatch((box_x + box_w - 2.7, core_y + 0.12), 2.5, 0.42,
                       boxstyle="round,pad=0.02,rounding_size=0.05",
                       facecolor="#1a1a1a", edgecolor="black", linewidth=1.0)
ax.add_patch(badge)
ax.text(box_x + box_w - 1.45, core_y + 0.33, "ZERO ENGINE DEP",
        ha="center", va="center", fontsize=8.5, fontweight="bold",
        color="white", family="monospace")

# ============ RIGHT: Host injection panel ============
panel_x = 10.2
panel_w = 4.5
panel = FancyBboxPatch((panel_x, 1.0), panel_w, 7.7,
                       boxstyle="round,pad=0.03,rounding_size=0.1",
                       facecolor="#f5f5f5", edgecolor="#3a3a3a", linewidth=1.4,
                       linestyle="--")
ax.add_patch(panel)
ax.text(panel_x + panel_w / 2, 8.35,
        "Host injects\n(host = concrete platform app)",
        ha="center", va="center", fontsize=10.5, fontweight="bold",
        color="#3a3a3a", family="monospace", linespacing=1.2)

# Three injectable interfaces
interfaces = [
    ("ILockstepLogger",
     "Log(level, msg)",
     ".NET:  ConsoleLogger\nUnity:  UnityLogger (Debug.Log)\nProd:   structured logger"),
    ("IReconnectCredentialStore",
     "Save / Load / Clear",
     "Unity:   PlayerPrefs\nDesktop: file (5 lines)\nDefault:  NullStore (no-op)"),
    ("INetworkClient  (transport)",
     "Send / Receive / Connect",
     "Desktop:  UDP / TCP / KCP\nWebGL:    WebSocket (wss)\nUnity WS: NativeWebSocket"),
]
iy = 7.7
for name, methods, impls in interfaces:
    box = FancyBboxPatch((panel_x + 0.2, iy - 2.05), panel_w - 0.4, 1.85,
                         boxstyle="round,pad=0.02,rounding_size=0.06",
                         facecolor="white", edgecolor="#0b3d91", linewidth=1.2)
    ax.add_patch(box)
    ax.text(panel_x + 0.35, iy - 0.25, name,
            ha="left", va="center", fontsize=9.8, fontweight="bold",
            color="#0b3d91", family="monospace")
    ax.text(panel_x + 0.35, iy - 0.62, methods,
            ha="left", va="center", fontsize=8.5, color="#666",
            family="monospace", fontstyle="italic")
    ax.text(panel_x + 0.35, iy - 1.3, impls,
            ha="left", va="center", fontsize=8.3, color="#1a1a1a",
            family="monospace", linespacing=1.3)
    iy -= 2.15

# Arrows from host panel into Network/Core layers (injection)
# target = Network layer middle
target_y = 3.0 + 1.7 / 2
for src_y in (6.7, 4.55, 2.4):
    ax.annotate("", xy=(box_x + box_w + 0.05, target_y),
                xytext=(panel_x + 0.1, src_y),
                arrowprops=dict(arrowstyle="->", color="#0b3d91",
                                lw=1.2, linestyle=":",
                                connectionstyle="arc3,rad=-0.15"))

ax.text(panel_x - 0.15, target_y + 0.85, "inject", ha="center", va="center",
        fontsize=8.5, color="#0b3d91", fontstyle="italic", rotation=0)

# ============ BOTTOM: Logic/Presentation boundary ============
boundary_y = 0.5
ax.plot([0.6, 9.8], [boundary_y, boundary_y], color="#7a2a2a", linewidth=2.0,
        linestyle="-")
ax.text(5.2, boundary_y - 0.28,
        "logic/presentation boundary  ->  ToFloat() only at render layer "
        "(logic stays fixed-point)",
        ha="center", va="center", fontsize=9, fontweight="bold",
        color="#7a2a2a", family="monospace")

# tiny note bottom-right
ax.text(14.85, 0.15,
        "Default safe  +  host opts in to enable",
        ha="right", va="center", fontsize=8, color="#666",
        fontstyle="italic", family="monospace")

plt.tight_layout()
out = "c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-18-sdk-layering.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
print("saved:", out)
