# -*- coding: utf-8 -*-
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

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

RED   = (0.86, 0.18, 0.18)
GREEN = (0.20, 0.70, 0.32)
BLUE  = (0.20, 0.45, 0.88)
AMBER = (0.95, 0.70, 0.18)
PURPLE = (0.55, 0.30, 0.78)
DARK  = (0.45, 0.45, 0.50)
INK   = (0.12, 0.12, 0.12)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=(0.95, 0.95, 0.97), ec=INK, fs=12,
        weight="normal", tc=INK):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                 boxstyle="round,pad=0.02,rounding_size=0.06",
                 facecolor=fc, edgecolor=ec, linewidth=1.4))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.6, rad=0.0):
    cs = "arc3,rad=" + str(rad)
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle="-|>", color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=16))


# ================================================================
# P2-06 figures: explicit Euler and its instability
# ================================================================

def _sim_euler_p206(dt=0.25, n=90):
    """Spring oscillator x'' = -x. Returns arrays for:
    exact / explicit Euler / symplectic Euler (positions, velocities, energies).
    Both Eulers share the SAME velocity update (a_old = -x_old); the only
    difference is whether the position uses the old or the new velocity.
    """
    t = np.arange(n) * dt
    # exact solution: x = cos t, v = -sin t, energy = 0.5 constant
    x_exact = np.cos(t)
    v_exact = -np.sin(t)
    E_exact = 0.5 * np.ones(n)

    # explicit Euler: v_new = v - x*dt ; x_new = x + v_OLD*dt  (old velocity)
    x_exp = np.zeros(n); v_exp = np.zeros(n)
    x_exp[0], v_exp[0] = 1.0, 0.0
    for i in range(1, n):
        v_exp[i] = v_exp[i-1] - x_exp[i-1] * dt
        x_exp[i] = x_exp[i-1] + v_exp[i-1] * dt   # <-- OLD velocity

    # symplectic Euler: v_new = v - x*dt ; x_new = x + v_NEW*dt  (new velocity)
    x_sym = np.zeros(n); v_sym = np.zeros(n)
    x_sym[0], v_sym[0] = 1.0, 0.0
    for i in range(1, n):
        v_sym[i] = v_sym[i-1] - x_sym[i-1] * dt
        x_sym[i] = x_sym[i-1] + v_sym[i] * dt     # <-- NEW velocity

    E_exp = 0.5 * (x_exp**2 + v_exp**2)
    E_sym = 0.5 * (x_sym**2 + v_sym**2)
    return t, x_exact, v_exact, E_exact, x_exp, v_exp, E_exp, x_sym, v_sym, E_sym


def fig_p206_1_update_order():
    """fig-p206-1: explicit vs symplectic Euler update order, side by side.
    The ONLY difference is whether the position uses v_old or v_new."""
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(12.5, 6.4))

    RSOFT = (0.98, 0.90, 0.90)   # explicit (bad) tint
    GSOFT = (0.90, 0.95, 0.90)   # symplectic (good) tint

    # ---- left: explicit Euler ----
    box(axL, 1.0, 5.2, 4.6, 0.9, "start of step\nold state  (x_old, v_old)  +  force_old",
        fc=(0.93, 0.93, 0.96), fs=11.5, weight="bold")
    box(axL, 1.0, 3.9, 4.6, 0.95,
        "v_new = v_old + a_old * dt\n(a_old = force_old / m)",
        fc="white", ec=INK, fs=11)
    box(axL, 1.0, 2.6, 4.6, 0.95,
        "x_new = x_old + v_OLD * dt",
        fc=RSOFT, ec=RED, fs=11.5, weight="bold", tc=RED)
    box(axL, 1.0, 1.3, 4.6, 0.9, "next state\nenergy grows  |lambda|>1  ->  DIVERGES",
        fc=RSOFT, ec=RED, fs=11, weight="bold", tc=RED)
    arrow(axL, (3.3, 5.2), (3.3, 4.85), color=INK, lw=1.7)
    arrow(axL, (3.3, 3.9), (3.3, 3.55), color=INK, lw=1.7)
    arrow(axL, (3.3, 2.6), (3.3, 2.2), color=INK, lw=1.7)
    axL.text(3.3, 6.4, "Explicit Euler", ha="center", fontsize=14,
             weight="bold", color=RED)
    axL.text(3.3, 0.55, "position updated with the OLD velocity",
             ha="center", fontsize=10.5, color=RED)

    # ---- right: symplectic Euler ----
    box(axR, 1.0, 5.2, 4.6, 0.9, "start of step\nold state  (x_old, v_old)  +  force_old",
        fc=(0.93, 0.93, 0.96), fs=11.5, weight="bold")
    box(axR, 1.0, 3.9, 4.6, 0.95,
        "v_new = v_old + a_old * dt\n(a_old = force_old / m)",
        fc="white", ec=INK, fs=11)
    box(axR, 1.0, 2.6, 4.6, 0.95,
        "x_new = x_old + v_NEW * dt",
        fc=GSOFT, ec=GREEN, fs=11.5, weight="bold", tc=GREEN)
    box(axR, 1.0, 1.3, 4.6, 0.9, "next state\nenergy bounded  |lambda|=1  ->  STABLE",
        fc=GSOFT, ec=GREEN, fs=11, weight="bold", tc=GREEN)
    arrow(axR, (3.3, 5.2), (3.3, 4.85), color=INK, lw=1.7)
    arrow(axR, (3.3, 3.9), (3.3, 3.55), color=INK, lw=1.7)
    arrow(axR, (3.3, 2.6), (3.3, 2.2), color=INK, lw=1.7)
    axR.text(3.3, 6.4, "Symplectic (semi-implicit) Euler", ha="center",
             fontsize=14, weight="bold", color=GREEN)
    axR.text(3.3, 0.55, "position updated with the NEW velocity",
             ha="center", fontsize=10.5, color=GREEN)

    fig.suptitle("The only difference: position uses v_old  vs  v_new   "
                 "(one line swap  ->  diverge or conserve)",
                 fontsize=12.5, weight="bold", y=0.99)
    for ax in (axL, axR):
        ax.set_xlim(0, 6.6); ax.set_ylim(0.2, 6.8)
        ax.set_aspect("equal"); clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p206-1-update-order.png"))
    plt.close(fig)


def fig_p206_2_divergence_trajectory():
    """fig-p206-2: explicit Euler amplitude grows unbounded vs exact cos(t)
    and bounded symplectic Euler. dt=0.25, 90 steps (peak |x| ~ 14.6)."""
    dt = 0.25
    t, x_true, v_true, E_true, x_exp, v_exp, E_exp, x_sym, v_sym, E_sym = \
        _sim_euler_p206(dt=dt, n=90)
    fig, ax = plt.subplots(figsize=(11.5, 6.0))
    ax.plot(t, x_true, color=BLUE, lw=2.6, label="exact physics  x = cos(t)  (amplitude = 1)")
    ax.plot(t, x_sym, color=GREEN, lw=2.0, linestyle="--",
            label="symplectic Euler  (amplitude bounded ~ 1.008)")
    ax.plot(t, x_exp, color=RED, lw=2.4, linestyle=":",
            label="explicit Euler  (amplitude blows up -> 14.6!)")
    ax.set_xlim(0, t[-1])
    ax.set_ylim(-16, 18)
    ax.set_xlabel("time t", fontsize=12)
    ax.set_ylabel("position x", fontsize=12)
    ax.axhline(0, color=(0.78, 0.78, 0.80), lw=0.8)
    ax.set_title("Explicit Euler on x'' = -x : amplitude keeps growing until it flies off",
                 fontsize=13, weight="bold")
    ax.legend(fontsize=11, loc="upper left", framealpha=0.95)
    ax.grid(True, alpha=0.3)
    fig.savefig(os.path.join(OUT, "fig-p206-2-divergence-trajectory.png"))
    plt.close(fig)


def fig_p206_3_energy_split():
    """fig-p206-3: energy E=0.5(x^2+v^2). Left: explicit Euler grows 0.5 -> 110
    (geometric, ratio 1+dt^2 per step). Right: symplectic Euler bounded 0.44-0.57."""
    dt = 0.25
    t, x_true, v_true, E_true, x_exp, v_exp, E_exp, x_sym, v_sym, E_sym = \
        _sim_euler_p206(dt=dt, n=90)
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(13.0, 5.6))

    # left: explicit Euler
    axL.plot(t, E_exp, color=RED, lw=2.6,
             label="E_n  (explicit Euler)")
    axL.axhline(0.5, color=BLUE, lw=1.8, linestyle="--",
                label="true energy E = 0.5")
    axL.set_xlim(0, t[-1]); axL.set_ylim(0, 120)
    axL.set_xlabel("time t", fontsize=12)
    axL.set_ylabel("energy  E = 0.5 (x^2 + v^2)", fontsize=12)
    axL.set_title("Explicit Euler:  E grows 0.5 -> %.0f  (ratio %.0fx)"
                  % (E_exp[-1], E_exp[-1] / E_exp[0]),
                  fontsize=12.5, weight="bold", color=RED)
    axL.legend(fontsize=10.5, loc="upper left")
    axL.grid(True, alpha=0.3)

    # right: symplectic Euler
    axR.plot(t, E_sym, color=GREEN, lw=2.2,
             label="E_n  (symplectic Euler)")
    axR.axhline(0.5, color=BLUE, lw=1.8, linestyle="--",
                label="true energy E = 0.5")
    axR.set_xlim(0, t[-1]); axR.set_ylim(0.40, 0.60)
    axR.set_xlabel("time t", fontsize=12)
    axR.set_ylabel("energy  E = 0.5 (x^2 + v^2)", fontsize=12)
    axR.set_title("Symplectic Euler:  E bounded in [%.2f, %.2f],  mean %.2f"
                  % (E_sym.min(), E_sym.max(), E_sym.mean()),
                  fontsize=12.5, weight="bold", color=GREEN)
    axR.legend(fontsize=10.5, loc="upper right")
    axR.grid(True, alpha=0.3)

    fig.suptitle("Same system x''=-x,  same dt=%.2f,  same velocity update:  "
                 "one line swap decides bounded vs blow-up" % dt,
                 fontsize=12.5, weight="bold", y=1.0)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig-p206-3-energy-split.png"))
    plt.close(fig)


def fig_p206_4_amplification():
    """fig-p206-4: per-step amplification |lambda| vs dt.
    Explicit Euler  |lambda| = sqrt(1+dt^2)  > 1 for any dt > 0  (above unit line)
    Symplectic Euler |lambda| = 1                            (on the unit line)."""
    dt = np.linspace(0.0, 1.5, 300)
    lam_exp = np.sqrt(1.0 + dt**2)        # explicit Euler eigenvalue modulus
    lam_sym = np.ones_like(dt)            # symplectic Euler: exactly 1 (dt < 2)

    fig, ax = plt.subplots(figsize=(10.5, 6.0))
    ax.plot(dt, lam_exp, color=RED, lw=2.8,
            label="explicit Euler   |lambda| = sqrt(1 + dt^2)   > 1  always")
    ax.plot(dt, lam_sym, color=GREEN, lw=2.8, linestyle="--",
            label="symplectic Euler   |lambda| = 1   (on the unit circle)")
    ax.axhline(1.0, color=DARK, lw=1.4, linestyle=":",
               label="stability boundary  |lambda| = 1")

    # mark dt=0.25 (the value used in the experiments)
    ax.axvline(0.25, color=AMBER, lw=1.3, linestyle=":")
    lam025 = np.sqrt(1.0 + 0.25**2)
    ax.plot([0.25], [lam025], "o", color=RED, ms=9, zorder=5)
    ax.annotate("dt=0.25  ->  |lambda| = %.4f" % lam025,
                xy=(0.25, lam025), xytext=(0.45, 1.16),
                fontsize=11, color=RED, weight="bold",
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.4))

    # shade the stable region
    ax.fill_between(dt, 0, 1.0, color=GREEN, alpha=0.07)

    ax.set_xlim(0, 1.5); ax.set_ylim(0.85, 1.85)
    ax.set_xlabel("timestep  dt", fontsize=12)
    ax.set_ylabel("per-step amplification  |lambda|", fontsize=12)
    ax.set_title("Algebraic root cause:  explicit Euler never reaches the stable region",
                 fontsize=13, weight="bold")
    ax.legend(fontsize=10.8, loc="upper left", framealpha=0.95)
    ax.grid(True, alpha=0.3)
    fig.savefig(os.path.join(OUT, "fig-p206-4-amplification.png"))
    plt.close(fig)


def fig_p206_5_phase_space():
    """fig-p206-5: phase space (x, v). Exact = closed unit circle.
    Symplectic Euler = near-closed loop. Explicit Euler = outward spiral."""
    dt = 0.25
    n = 200
    t, x_true, v_true, E_true, x_exp, v_exp, E_exp, x_sym, v_sym, E_sym = \
        _sim_euler_p206(dt=dt, n=n)

    fig, ax = plt.subplots(figsize=(8.8, 8.8))
    # exact: unit circle
    ax.plot(x_true, v_true, color=BLUE, lw=2.6,
            label="exact:  closed unit circle  (E = 0.5)")
    # symplectic: near-closed loop
    ax.plot(x_sym, v_sym, color=GREEN, lw=2.0, linestyle="--",
            label="symplectic Euler:  near-closed loop  (bounded)")
    # explicit: outward spiral
    ax.plot(x_exp, v_exp, color=RED, lw=2.2, linestyle=":",
            label="explicit Euler:  outward spiral  (energy injected)")

    # starting point
    ax.plot([1.0], [0.0], "o", color=INK, ms=9, zorder=5)
    ax.text(1.05, -0.18, "start (1, 0)", fontsize=10.5, color=INK)
    # origin
    ax.plot([0], [0], "+", color=DARK, ms=14, mew=2)

    ax.set_aspect("equal")
    ax.set_xlim(-4.0, 4.0); ax.set_ylim(-4.0, 4.0)
    ax.set_xlabel("position x", fontsize=12)
    ax.set_ylabel("velocity v", fontsize=12)
    ax.axhline(0, color=(0.78, 0.78, 0.80), lw=0.7)
    ax.axvline(0, color=(0.78, 0.78, 0.80), lw=0.7)
    ax.set_title("Phase space (x, v):  closed circle  vs  outward spiral",
                 fontsize=13, weight="bold")
    ax.legend(fontsize=10.8, loc="upper left", framealpha=0.95)
    ax.grid(True, alpha=0.3)
    fig.savefig(os.path.join(OUT, "fig-p206-5-phase-space.png"))
    plt.close(fig)


def fig_p206_6_stepsize_growth():
    """fig-p206-6: energy ratio E/E0 vs step under several dt, semi-log axis.
    Each line is straight (geometric growth, slope set by 1+dt^2). Smaller dt
    => shallower line, but every line still rises (no dt makes it flat)."""
    dts = [0.50, 0.25, 0.10, 0.05]
    colors = [RED, AMBER, GREEN, BLUE]
    n = 120

    fig, ax = plt.subplots(figsize=(11.0, 6.2))
    for dt, c in zip(dts, colors):
        steps = np.arange(n)
        x = np.zeros(n); v = np.zeros(n); x[0], v[0] = 1.0, 0.0
        for i in range(1, n):
            v[i] = v[i-1] - x[i-1] * dt
            x[i] = x[i-1] + v[i-1] * dt       # explicit Euler (old velocity)
        E = 0.5 * (x**2 + v**2)
        ax.plot(steps, E / E[0], color=c, lw=2.4,
                label="dt = %.2f    per-step factor = %.5f"
                      % (dt, 1.0 + dt*dt))

    ax.set_yscale("log")
    ax.set_xlim(0, n - 1)
    ax.set_ylim(0.9, 1e7)
    ax.set_xlabel("step number", fontsize=12)
    ax.set_ylabel("energy ratio   E / E_0   (log scale)", fontsize=12)
    ax.set_title("Step size only slows divergence: every dt still blows up geometrically",
                 fontsize=13, weight="bold")
    ax.legend(fontsize=11, loc="upper left", framealpha=0.95)
    ax.grid(True, alpha=0.3, which="both")
    fig.savefig(os.path.join(OUT, "fig-p206-6-stepsize-growth.png"))
    plt.close(fig)


# ================================================================
# P2-07 figures: semi-implicit (symplectic) Euler vs Verlet
# ================================================================

def _sim_harmonic_p207(dt=0.35, n=80):
    """Harmonic oscillator x'' = -x under four integrators:
    exact / explicit Euler / symplectic Euler / Verlet.
    Returns positions and energies E = 0.5*(x^2 + v^2).
    """
    n_steps = n
    t = np.arange(n_steps + 1) * dt
    x_exact = np.cos(t)
    v_exact = -np.sin(t)
    E_exact = 0.5 * (x_exact**2 + v_exact**2)

    # explicit Euler: v_new = v - x*dt ; x_new = x + v*dt   (old velocity)
    x_exp = np.zeros(n_steps + 1); v_exp = np.zeros(n_steps + 1)
    x_exp[0], v_exp[0] = 1.0, 0.0
    for i in range(n_steps):
        v_new = v_exp[i] - x_exp[i] * dt
        x_new = x_exp[i] + v_exp[i] * dt
        v_exp[i+1], x_exp[i+1] = v_new, x_new

    # symplectic Euler: v_new = v - x*dt ; x_new = x + v_new*dt   (new velocity)
    x_sym = np.zeros(n_steps + 1); v_sym = np.zeros(n_steps + 1)
    x_sym[0], v_sym[0] = 1.0, 0.0
    for i in range(n_steps):
        v_sym[i+1] = v_sym[i] - x_sym[i] * dt
        x_sym[i+1] = x_sym[i] + v_sym[i+1] * dt

    # Verlet (position form): x_{n+1} = 2 x_n - x_{n-1} + a_n dt^2
    x_ver = np.zeros(n_steps + 1); v_ver = np.zeros(n_steps + 1)
    x_ver[0] = 1.0
    v0 = 0.0 - x_ver[0] * dt
    x_ver[1] = x_ver[0] + v0 * dt
    for i in range(1, n_steps):
        a = -x_ver[i]
        x_ver[i+1] = 2.0 * x_ver[i] - x_ver[i-1] + a * dt * dt
    for i in range(1, n_steps):
        v_ver[i] = (x_ver[i+1] - x_ver[i-1]) / (2.0 * dt)
    v_ver[0] = (x_ver[1] - x_ver[0]) / dt
    v_ver[n_steps] = (x_ver[n_steps] - x_ver[n_steps-1]) / dt

    E_exp = 0.5 * (x_exp**2 + v_exp**2)
    E_sym = 0.5 * (x_sym**2 + v_sym**2)
    E_ver = 0.5 * (x_ver**2 + v_ver**2)
    return t, x_exact, x_exp, x_sym, x_ver, E_exact, E_exp, E_sym, E_ver


def fig_p207_trajectory_compare():
    """fig-04: four integrators on x'' = -x (extends P0 fig-02 with Verlet)."""
    dt = 0.35
    t, x_true, x_exp, x_sym, x_ver, *_ = _sim_harmonic_p207(dt=dt, n=60)
    fig, ax = plt.subplots(figsize=(10.5, 5.8))
    ax.plot(t, x_true, color=BLUE, lw=2.6, label="exact physics (energy conserved)")
    ax.plot(t, x_sym, color=GREEN, lw=2.0, linestyle="--",
            label="symplectic Euler  (stable, bounded)")
    ax.plot(t, x_ver, color=PURPLE, lw=1.9, linestyle="-.",
            label="Verlet  (stable, bounded)")
    ax.plot(t, x_exp, color=RED, lw=2.2, linestyle=":",
            label="explicit Euler  (energy blows up!)")
    ax.set_xlim(0, t[-1]); ax.set_ylim(-9, 16)
    ax.set_xlabel("time t", fontsize=12)
    ax.set_ylabel("position x", fontsize=12)
    ax.axhline(0, color=(0.78, 0.78, 0.80), lw=0.8)
    ax.set_title("Four integrators on x'' = -x   (spring oscillator)",
                 fontsize=13, weight="bold")
    ax.legend(fontsize=10.8, loc="upper left", framealpha=0.95)
    ax.grid(True, alpha=0.3)
    fig.savefig(os.path.join(OUT, "fig-04-four-integrators-trajectory.png"))
    plt.close(fig)


def fig_p207_energy_compare():
    """fig-05: energy curves. explicit blows up; symplectic/Verlet bounded."""
    dt = 0.35
    t, _, _, _, _, E_true, E_exp, E_sym, E_ver = _sim_harmonic_p207(dt=dt, n=60)
    fig, ax = plt.subplots(figsize=(10.5, 5.8))
    ax.plot(t, E_true * np.ones_like(t), color=BLUE, lw=2.4,
            label="exact physics (E = 0.5, constant)")
    ax.plot(t, E_sym, color=GREEN, lw=2.0, linestyle="--",
            label="symplectic Euler  (E bounded oscillation)")
    ax.plot(t, E_ver, color=PURPLE, lw=1.9, linestyle="-.",
            label="Verlet  (E bounded oscillation)")
    ax.plot(t, E_exp, color=RED, lw=2.3, linestyle=":",
            label="explicit Euler  (E grows unbounded!)")
    ax.set_xlim(0, t[-1]); ax.set_ylim(0.2, 6.5)
    ax.set_xlabel("time t", fontsize=12)
    ax.set_ylabel("energy  E = 0.5 (x^2 + v^2)", fontsize=12)
    ax.set_title("Energy behavior:  explicit Euler leaks energy in,  "
                 "symplectic/Verlet keep it bounded",
                 fontsize=12.5, weight="bold")
    ax.legend(fontsize=10.8, loc="upper left", framealpha=0.95)
    ax.grid(True, alpha=0.3)
    fig.savefig(os.path.join(OUT, "fig-05-energy-comparison.png"))
    plt.close(fig)


def fig_p208_fixed_vs_variable_dt():
    rng_var = np.random.RandomState(7)
    g = 9.8
    restitution = 0.8
    y_floor = 0.0

    def simulate(dt_sequence):
        n = len(dt_sequence)
        x = np.zeros(n)
        pos, vel = 5.0, 0.0
        for i in range(n):
            dt = dt_sequence[i]
            vel -= g * dt
            pos += vel * dt
            if pos <= y_floor and vel < 0:
                vel = -vel * restitution
                pos = y_floor + (y_floor - pos) * restitution
            x[i] = pos
        return x

    T_total = 6.0
    dt_fixed = np.full(int(T_total * 120), 1.0 / 120.0)
    t_fixed = np.cumsum(dt_fixed) - dt_fixed[0]
    x_fixed = simulate(dt_fixed)

    n_var = int(T_total * 120)
    raw_dt = 1.0 / rng_var.uniform(45, 200, size=n_var)
    raw_dt = np.clip(raw_dt, 1.0 / 240.0, 1.0 / 30.0)
    t_var = np.cumsum(raw_dt) - raw_dt[0]
    x_var = simulate(raw_dt)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7.2))

    ax1.plot(t_fixed, x_fixed, color=BLUE, lw=2.2,
             label="fixed dt = 1/120  (reproducible, stable bounces)")
    ax1.plot(t_var, x_var, color=RED, lw=1.4, alpha=0.9,
             label="variable dt  (jittering frame rate  ->  drift)")
    ax1.axhline(0, color=(0.55, 0.45, 0.35), lw=2.0)
    ax1.text(0.08, 0.15, "ground y=0", color="white", fontsize=10, va="center")
    ax1.set_xlim(0, T_total)
    ax1.set_ylim(-0.3, 6.0)
    ax1.set_xlabel("time  (s)", fontsize=11)
    ax1.set_ylabel("ball height  (m)", fontsize=11)
    ax1.set_title("Fixed vs variable timestep: same integrator, different destiny",
                  fontsize=13, weight="bold")
    ax1.legend(fontsize=10.5, loc="upper right")
    ax1.grid(True, alpha=0.3)

    ax2.plot(t_fixed, dt_fixed * 1000.0, color=BLUE, lw=1.6,
             label="fixed dt  (flat line, every step identical)")
    ax2.plot(t_var, raw_dt * 1000.0, color=RED, lw=0.9, alpha=0.85,
             label="variable dt  (jittering  ->  step-size noise)")
    ax2.set_xlim(0, T_total)
    ax2.set_ylim(0, 34)
    ax2.set_xlabel("time  (s)", fontsize=11)
    ax2.set_ylabel("dt fed to physics  (ms)", fontsize=11)
    ax2.set_title("What the physics actually receives each frame",
                  fontsize=12.5, weight="bold")
    ax2.legend(fontsize=10.5, loc="upper right")
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig-p208-1-fixed-vs-variable-dt.png"))
    plt.close(fig)


def fig_p208_substeps():
    fig, ax = plt.subplots(figsize=(11.5, 4.6))

    ax.add_patch(FancyBboxPatch((0.3, 2.2), 10.8, 1.1,
                 boxstyle="round,pad=0.02,rounding_size=0.06",
                 facecolor=(0.90, 0.94, 1.0), edgecolor=BLUE, linewidth=2.0))
    ax.text(5.7, 2.75, "one call  b2World_Step( dt = 1/60 s,  subStepCount = 4 )",
            ha="center", va="center", fontsize=12.5, weight="bold", color=INK)

    sub_n = 4
    sub_w = 10.0 / sub_n
    for i in range(sub_n):
        x0 = 0.7 + i * sub_w
        ax.add_patch(Rectangle((x0, 0.8), sub_w - 0.18, 1.0,
                     facecolor=(0.90, 0.95, 0.90), edgecolor=GREEN, linewidth=1.5))
        ax.text(x0 + (sub_w - 0.18) / 2, 1.3,
                "sub-step  h = dt/4\n= 1/240 s",
                ha="center", va="center", fontsize=10.5, color=INK)
        arrow(ax, (x0 + (sub_w - 0.18) / 2, 2.2),
                  (x0 + (sub_w - 0.18) / 2, 1.82), color=GREEN, lw=1.4)

    ax.text(5.7, 0.35,
            "Integrator and constraint solver run once per h.\n"
            "Smaller h  =>  more accurate, more stable, more CPU.",
            ha="center", va="center", fontsize=11, color=DARK)

    ax.set_xlim(0, 11.4)
    ax.set_ylim(0, 3.6)
    ax.set_aspect("equal")
    clean_ax(ax)
    ax.set_title("subStepCount : one fixed timestep carved into N smaller physics steps",
                 fontsize=13, weight="bold")
    fig.savefig(os.path.join(OUT, "fig-p208-2-substeps.png"))
    plt.close(fig)


def fig_p208_accumulator():
    fig, ax = plt.subplots(figsize=(12, 5.4))

    ax.text(0.1, 4.65, "render loop", fontsize=12, weight="bold", color=PURPLE)
    frame_dts = [0.020, 0.014, 0.022, 0.016, 0.024, 0.015]
    x = 0.5
    centers = []
    for d in frame_dts:
        w = d * 120.0
        ax.add_patch(Rectangle((x, 3.9), w, 0.55,
                     facecolor=(0.93, 0.88, 0.97), edgecolor=PURPLE, linewidth=1.3))
        ax.text(x + w / 2, 4.17, "{:.0f}ms".format(d*1000),
                ha="center", va="center", fontsize=9.5, color=INK)
        centers.append((x + w / 2, d))
        x += w + 0.12

    arrow(ax, (centers[0][0], 3.9), (2.0, 3.25), color=PURPLE, lw=1.5, rad=-0.15)
    ax.text(2.0, 3.05, "deposit\nvariable frame dt", ha="center", va="top",
            fontsize=9.5, color=PURPLE)

    ax.add_patch(FancyBboxPatch((0.5, 1.7), 11.0, 0.95,
                 boxstyle="round,pad=0.02,rounding_size=0.08",
                 facecolor=(1.0, 0.94, 0.82), edgecolor=AMBER, linewidth=2.0))
    ax.text(6.0, 2.45, "accumulator (bank of time)  +  remainder carried to next frame",
            ha="center", va="center", fontsize=12, weight="bold", color=INK)
    ax.text(6.0, 2.05, "while ( accumulator >= fixed_dt )  {  b2World_Step(fixed_dt) ;  accumulator -= fixed_dt  }",
            ha="center", va="center", fontsize=10.5, color=DARK,
            family="monospace")

    fixed_dt = 1.0 / 60.0
    n_steps_total = 6
    sx = 0.5
    step_w = fixed_dt * 120.0
    for i in range(n_steps_total):
        ax.add_patch(Rectangle((sx, 0.45), step_w, 0.6,
                     facecolor=(0.90, 0.95, 0.90), edgecolor=GREEN, linewidth=1.3))
        ax.text(sx + step_w / 2, 0.75, "physics\n1/60 s",
                ha="center", va="center", fontsize=9, color=INK)
        sx += step_w + 0.10
    ax.text(0.1, 0.75, "physics", fontsize=12, weight="bold", color=GREEN,
            ha="left", va="center")

    arrow(ax, (3.5, 1.7), (3.5, 1.10), color=AMBER, lw=1.8)
    ax.text(3.7, 1.40, "consume\nfixed dt", ha="left", va="center",
            fontsize=9.5, color=AMBER)

    arrow(ax, (9.5, 1.7), (9.5, 1.10), color=AMBER, lw=1.8)
    ax.text(9.7, 1.40, "remainder\nkept", ha="left", va="center",
            fontsize=9.5, color=AMBER)

    ax.set_xlim(0, 12.2)
    ax.set_ylim(0.2, 5.0)
    ax.set_aspect("equal")
    clean_ax(ax)
    ax.set_title("Accumulator : variable render frame rate drives a fixed-timestep physics",
                 fontsize=13, weight="bold")
    fig.savefig(os.path.join(OUT, "fig-p208-3-accumulator.png"))
    plt.close(fig)


def fig_p208_spiral_of_death():
    fig, ax = plt.subplots(figsize=(10.5, 5.0))

    frames = np.arange(0, 9)
    healthy = 16.7 + 0.5 * np.sin(frames)
    spiral = np.array([16.7, 19, 26, 40, 70, 130, 250, 490, 970], dtype=float)

    ax.bar(frames - 0.20, healthy, width=0.38, color=GREEN,
           label="healthy (fixed dt, 1 step/frame)")
    ax.bar(frames + 0.20, spiral, width=0.38, color=RED,
           label="spiral of death (physics slower than budget)")

    ax.axhline(16.7, color=BLUE, lw=1.6, linestyle="--",
               label="16.7 ms budget (60 fps)")
    ax.set_yscale("log")
    ax.set_ylim(10, 2000)
    ax.set_xlabel("frame number", fontsize=11)
    ax.set_ylabel("frame time  (ms, log scale)", fontsize=11)
    ax.set_title("Spiral of death : physics slower than the frame budget self-amplifies",
                 fontsize=12.5, weight="bold")
    ax.set_xticks(frames)
    ax.legend(fontsize=10, loc="upper left")
    ax.grid(True, alpha=0.3, which="both", axis="y")

    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig-p208-4-spiral-of-death.png"))
    plt.close(fig)


if __name__ == "__main__":
    # P2-06
    fig_p206_1_update_order()
    fig_p206_2_divergence_trajectory()
    fig_p206_3_energy_split()
    fig_p206_4_amplification()
    fig_p206_5_phase_space()
    fig_p206_6_stepsize_growth()
    # P2-07
    fig_p207_trajectory_compare()
    fig_p207_energy_compare()
    # P2-08
    fig_p208_fixed_vs_variable_dt()
    fig_p208_substeps()
    fig_p208_accumulator()
    fig_p208_spiral_of_death()
    print("done:", sorted(f for f in os.listdir(OUT)
                          if f.startswith("fig-0") or f.startswith("fig-p")))
