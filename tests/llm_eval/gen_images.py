"""Generate the schematic images for the vision test cases (V1..V5).

Hand-drawn with matplotlib primitives (no schemdraw dependency): clean black
lines on white, large value labels — legible for the vision model the way a
textbook figure or a whiteboard photo would be.

Run:  python gen_images.py     (writes PNGs into ./images/)
"""

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
LW = 2
C = "black"


def _fig(title, xlim=(0, 12), ylim=(0, 7)):
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.axis("off")
    ax.set_title(title, fontsize=13)
    return fig, ax


def wire(ax, pts):
    xs, ys = zip(*pts)
    ax.plot(xs, ys, color=C, lw=LW)


def resistor_h(ax, x0, x1, y, label):
    """Horizontal resistor zigzag from x0 to x1 at height y."""
    lead = (x1 - x0 - 2.0) / 2
    wire(ax, [(x0, y), (x0 + lead, y)])
    xs = np.linspace(x0 + lead, x1 - lead, 9)
    ys = y + 0.25 * np.array([0, 1, -1, 1, -1, 1, -1, 1, 0])
    ax.plot(xs, ys, color=C, lw=LW)
    wire(ax, [(x1 - lead, y), (x1, y)])
    ax.text((x0 + x1) / 2, y + 0.55, label, fontsize=12, ha="center")


def resistor_v(ax, x, y0, y1, label):
    """Vertical resistor zigzag from y0 (top) down to y1."""
    lead = (y0 - y1 - 2.0) / 2
    wire(ax, [(x, y0), (x, y0 - lead)])
    ys = np.linspace(y0 - lead, y1 + lead, 9)
    xs = x + 0.25 * np.array([0, 1, -1, 1, -1, 1, -1, 1, 0])
    ax.plot(xs, ys, color=C, lw=LW)
    wire(ax, [(x, y1 + lead), (x, y1)])
    ax.text(x + 0.45, (y0 + y1) / 2, label, fontsize=12, va="center")


def cap_v(ax, x, y0, y1, label):
    """Vertical capacitor from y0 (top) down to y1."""
    ym = (y0 + y1) / 2
    wire(ax, [(x, y0), (x, ym + 0.15)])
    ax.plot([x - 0.5, x + 0.5], [ym + 0.15, ym + 0.15], color=C, lw=LW)
    ax.plot([x - 0.5, x + 0.5], [ym - 0.15, ym - 0.15], color=C, lw=LW)
    wire(ax, [(x, ym - 0.15), (x, y1)])
    ax.text(x + 0.65, ym, label, fontsize=12, va="center")


def cap_h(ax, x0, x1, y, label):
    """Horizontal capacitor from x0 to x1 at height y."""
    xm = (x0 + x1) / 2
    wire(ax, [(x0, y), (xm - 0.15, y)])
    ax.plot([xm - 0.15, xm - 0.15], [y - 0.5, y + 0.5], color=C, lw=LW)
    ax.plot([xm + 0.15, xm + 0.15], [y - 0.5, y + 0.5], color=C, lw=LW)
    wire(ax, [(xm + 0.15, y), (x1, y)])
    ax.text(xm, y + 0.75, label, fontsize=12, ha="center")


def ac_source(ax, x, y, label):
    """AC source circle centered (x, y), radius 0.5; leads drawn by caller."""
    ax.add_patch(plt.Circle((x, y), 0.5, fill=False, lw=LW, color=C))
    ax.text(x, y, "~", ha="center", va="center", fontsize=16)
    ax.text(x - 1.6, y, label, fontsize=11, ha="center", va="center")


def ground(ax, x, y):
    ax.plot([x - 0.4, x + 0.4], [y, y], color=C, lw=LW)
    ax.plot([x - 0.25, x + 0.25], [y - 0.15, y - 0.15], color=C, lw=LW)
    ax.plot([x - 0.1, x + 0.1], [y - 0.3, y - 0.3], color=C, lw=LW)


def node(ax, x, y, label, dy=0.4):
    ax.plot(x, y, "o", color=C, ms=5)
    ax.text(x, y + dy, label, fontsize=12, ha="center", fontweight="bold")


def v1_rc_lowpass():
    fig, ax = _fig("Circuit 1")
    ac_source(ax, 2, 3.5, "Vin\nAC 1V")
    wire(ax, [(2, 4.0), (2, 5)])
    resistor_h(ax, 2, 7, 5, "R1 = 2.2k")
    node(ax, 7, 5, "out")
    wire(ax, [(7, 5), (8.5, 5)])
    cap_v(ax, 7, 5, 2, "C1 = 100n")
    ground(ax, 7, 2)
    wire(ax, [(2, 3.0), (2, 2)])
    ground(ax, 2, 2)
    fig.savefig(f"{OUT}/v1_rc_lowpass.png", bbox_inches="tight", facecolor="white")


def v2_divider():
    fig, ax = _fig("Circuit 2")
    ac_source(ax, 2.5, 3.5, "Vin\nAC 1V")
    wire(ax, [(2.5, 4.0), (2.5, 6), (6, 6)])
    resistor_v(ax, 6, 6, 3.5, "R1 = 10k")
    ax.plot(6, 3.5, "o", color=C, ms=5)
    wire(ax, [(6, 3.5), (7.5, 3.5)])
    ax.text(7.5, 3.9, "out", fontsize=12, ha="center", fontweight="bold")
    resistor_v(ax, 6, 3.5, 1.2, "R2 = 10k")
    ground(ax, 6, 1.2)
    wire(ax, [(2.5, 3.0), (2.5, 1.2)])
    ground(ax, 2.5, 1.2)
    fig.savefig(f"{OUT}/v2_divider.png", bbox_inches="tight", facecolor="white")


def v3_rc_highpass():
    fig, ax = _fig("Circuit 3")
    ac_source(ax, 2, 3.5, "Vin\nAC 1V")
    wire(ax, [(2, 4.0), (2, 5)])
    cap_h(ax, 2, 7, 5, "C1 = 100n")
    node(ax, 7, 5, "out")
    wire(ax, [(7, 5), (8.5, 5)])
    resistor_v(ax, 7, 5, 2, "R1 = 1.6k")
    ground(ax, 7, 2)
    wire(ax, [(2, 3.0), (2, 2)])
    ground(ax, 2, 2)
    fig.savefig(f"{OUT}/v3_rc_highpass.png", bbox_inches="tight", facecolor="white")


def v4_opamp_inv():
    fig, ax = _fig("Circuit 4", xlim=(0, 14))
    ac_source(ax, 1.5, 3.0, "Vin\nAC 1V")
    wire(ax, [(1.5, 3.5), (1.5, 4.5)])
    resistor_h(ax, 1.5, 6, 4.5, "R1 = 1k")
    node(ax, 6, 4.5, "n1")
    # opamp triangle: inputs on the left at x=7, output at x=10
    wire(ax, [(6, 4.5), (7, 4.5)])
    ax.plot([7, 7, 10, 7], [5.5, 3.5, 4.5, 5.5], color=C, lw=LW)
    ax.text(7.35, 4.5 + 0.55, "−", fontsize=14, va="center")
    ax.text(7.35, 4.5 - 0.55, "+", fontsize=14, va="center")
    ax.text(8.1, 4.5, "U1", fontsize=11, va="center")
    # + input to ground
    wire(ax, [(7, 3.9), (6.5, 3.9), (6.5, 1.5)])
    ground(ax, 6.5, 1.5)
    # feedback resistor over the top
    wire(ax, [(6, 4.5), (6, 6.3)])
    resistor_h(ax, 6, 10.5, 6.3, "R2 = 10k")
    wire(ax, [(10.5, 6.3), (10.5, 4.5)])
    node(ax, 10.5, 4.5, "out")
    wire(ax, [(10, 4.5), (12, 4.5)])
    wire(ax, [(1.5, 2.5), (1.5, 1.5)])
    ground(ax, 1.5, 1.5)
    fig.savefig(f"{OUT}/v4_opamp_inv.png", bbox_inches="tight", facecolor="white")


def v5_not_a_circuit():
    x = np.linspace(0, 4 * np.pi, 400)
    fig, ax = plt.subplots(figsize=(6, 3.5), dpi=110)
    ax.plot(x, np.exp(-x / 6) * np.sin(3 * x))
    ax.set_xlabel("t (s)")
    ax.set_ylabel("y")
    ax.set_title("Measured response")
    fig.savefig(f"{OUT}/v5_not_a_circuit.png", bbox_inches="tight",
                facecolor="white")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for f in (v1_rc_lowpass, v2_divider, v3_rc_highpass, v4_opamp_inv,
              v5_not_a_circuit):
        f()
        print("drew", f.__name__)
