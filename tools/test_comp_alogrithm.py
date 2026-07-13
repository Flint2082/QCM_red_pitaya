"""Interactive tester for the temperature-compensation algorithm.

Runs TempCompAlgorithm over a calibration sweep and plots the calibration
inputs alongside the algorithm's computed outputs. Use the check boxes on the
left to choose which channels are drawn; because the channels span different
units (°C, Hz, nm), tick "Normalize" to scale each selected series to its own
0-1 range so any combination is comparable.

Usage:
    python tools/test_comp_alogrithm.py [calibration.csv] [crystal]

Defaults: data/calibration_data.csv and the "TestCrystal" profile. The second
argument selects the crystal settings file — either a profile name found in
data/crystals/ or a path to a crystal .json — and supplies the calibration
coefficients (fM_*/fT_*) as well as the sensor parameters (mass_sensitivity,
sens_area, freq_virgin), exactly as the live app does. The calibration CSV
needs Temp / Freq_T / Freq_M columns; the last row is used as the reference
(start) point, as the original script did.
"""

import csv
import sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import CheckButtons

path_root = Path(__file__).parent.parent
sys.path.append(str(path_root))

import src.processing.TempCompAlgorithm as tca
from src.domain.crystal import CrystalManager

# channel key -> (legend label, line colour). Order sets the checkbox order.
CHANNELS = [
    ("input_temp",       "Input Temp (°C)",        "tab:red"),
    ("calc_temp",        "Calculated Temp (°C)",   "tab:orange"),
    ("freq_M",           "Mass Freq (Hz)",         "tab:blue"),
    ("freq_T",           "Temp Freq (Hz)",         "tab:cyan"),
    ("uncomp_thickness", "Uncomp. Thickness (nm)", "tab:olive"),
    ("comp_thickness",   "Comp. Thickness (nm)",   "tab:green"),
    ("comp_freq",        "Comp. Mass Freq (Hz)",   "tab:purple"),
]
# Channels shown when the window first opens.
DEFAULT_ON = {"input_temp", "calc_temp", "comp_thickness"}


def load_crystal(arg):
    """Load a crystal settings profile by name (from data/crystals/) or from a
    path to a crystal .json file. Returns a CrystalProfile or None if missing."""
    path = Path(arg)
    if path.suffix == ".json" or path.exists():
        return CrystalManager(crystals_dir=str(path.parent)).load(path.stem)
    return CrystalManager().load(str(arg))


def load_calibration(path):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return {
        "Temp":   np.array([float(r["Temp"])   for r in rows]),
        "Freq_T": np.array([float(r["Freq_T"]) for r in rows]),
        "Freq_M": np.array([float(r["Freq_M"]) for r in rows]),
    }


def run_algorithm(cal, profile):
    """Run TempCompAlgorithm over every calibration point, returning channel arrays.

    Coefficients and sensor parameters come from the crystal settings profile,
    wired the same way the live app does in QCMInterface.setMeasurementReference.
    """
    coefficients = {
        'fM_0': profile.fM_0, 'fM_1': profile.fM_1, 'fM_2': profile.fM_2, 'fM_3': profile.fM_3,
        'fT_0': profile.fT_0, 'fT_1': profile.fT_1, 'fT_2': profile.fT_2, 'fT_3': profile.fT_3,
    }
    # Use the last row as the start/reference point (matches the original script).
    temp_comp = tca.TempCompAlgorithm(
        coefficients=coefficients,
        T_start=cal["Temp"][-1],
        fT_start=cal["Freq_T"][-1],
        fM_start=cal["Freq_M"][-1],
        sens_area=profile.sens_area,
        mass_sensitivity=profile.mass_sensitivity,
        freq_virgin=profile.freq_virgin,
    )

    calc_temp, uncomp, comp, comp_freq = [], [], [], []
    for fT, fM in zip(cal["Freq_T"], cal["Freq_M"]):
        t, u, c, cf = temp_comp.FreqToTemp(fT=fT, fM=fM)
        # FreqToTemp returns Nones on failure — store NaN so the point is skipped.
        calc_temp.append(np.nan if t is None else t)
        uncomp.append(np.nan if u is None else u)
        comp.append(np.nan if c is None else c)
        comp_freq.append(np.nan if cf is None else cf)

    return {
        "input_temp":       cal["Temp"],
        "freq_M":           cal["Freq_M"],
        "freq_T":           cal["Freq_T"],
        "calc_temp":        np.array(calc_temp, dtype=float),
        "uncomp_thickness": np.array(uncomp, dtype=float),
        "comp_thickness":   np.array(comp, dtype=float),
        "comp_freq":        np.array(comp_freq, dtype=float),
    }


def main():
    cal_path = Path(sys.argv[1]) if len(sys.argv) > 1 else path_root / "data" / "calibration_data.csv"
    crystal_arg = sys.argv[2] if len(sys.argv) > 2 else "TestCrystal"

    if not cal_path.is_file():
        print(f"Calibration file not found: {cal_path}")
        sys.exit(1)

    profile = load_crystal(crystal_arg)
    if profile is None:
        available = ", ".join(CrystalManager().list_names()) or "(none)"
        print(f"Crystal settings not found: {crystal_arg}")
        print(f"Available profiles in data/crystals/: {available}")
        sys.exit(1)

    cal = load_calibration(cal_path)
    data = run_algorithm(cal, profile)
    x = np.arange(len(cal["Temp"]))

    fig, ax = plt.subplots(figsize=(12, 7))
    fig.canvas.manager.set_window_title(f"Temp-Comp Tester — {cal_path.name} / {profile.name}")
    plt.subplots_adjust(left=0.30, right=0.92, bottom=0.08, top=0.93)

    # One line per channel; raw keeps the unscaled values for the Normalize toggle.
    lines, raw = {}, {}
    for column, label, color in CHANNELS:
        y = data[column]
        raw[column] = y
        (line,) = ax.plot(x, y, label=label, color=color, linewidth=1.4, marker="o", markersize=3)
        line.set_visible(column in DEFAULT_ON)
        lines[column] = line

    ax.set_xlabel("Calibration sample #")
    ax.set_title(cal_path.name)
    ax.grid(True, alpha=0.3)

    # --- channel check boxes -------------------------------------------------
    labels = [label for _, label, _ in CHANNELS]
    states = [c[0] in DEFAULT_ON for c in CHANNELS]
    ax_checks = fig.add_axes([0.02, 0.30, 0.24, 0.6])
    ax_checks.set_title("Channels", fontsize=10)
    channel_check = CheckButtons(ax_checks, labels, states)
    for text, (_, _, color) in zip(channel_check.labels, CHANNELS):
        text.set_color(color)
        text.set_fontsize(9)

    # --- options check box ---------------------------------------------------
    ax_opts = fig.add_axes([0.02, 0.12, 0.24, 0.10])
    ax_opts.set_title("Options", fontsize=10)
    options_check = CheckButtons(ax_opts, ["Normalize"], [False])

    label_to_column = {label: column for column, label, _ in CHANNELS}

    def redraw(_=None):
        (normalize,) = options_check.get_status()
        status = dict(zip(labels, channel_check.get_status()))

        for label, on in status.items():
            column = label_to_column[label]
            line = lines[column]
            line.set_visible(on)
            y = raw[column]
            if normalize:
                lo, hi = np.nanmin(y), np.nanmax(y)
                line.set_ydata((y - lo) / (hi - lo) if hi > lo else np.zeros_like(y))
            else:
                line.set_ydata(y)

        # Rescale to whatever is currently visible.
        visible = [lines[label_to_column[l]] for l, on in status.items() if on]
        if visible:
            ys = np.concatenate([ln.get_ydata() for ln in visible])
            ys = ys[np.isfinite(ys)]
            if ys.size:
                pad = (ys.max() - ys.min()) * 0.05 or 1.0
                ax.set_ylim(ys.min() - pad, ys.max() + pad)

        ax.set_ylabel("Normalized (per-series 0–1)" if normalize else "Value (mixed units)")
        if visible:
            ax.legend(visible, [ln.get_label() for ln in visible], loc="upper left", fontsize=8)
        elif ax.get_legend():
            ax.get_legend().set_visible(False)
        fig.canvas.draw_idle()

    channel_check.on_clicked(redraw)
    options_check.on_clicked(redraw)
    redraw()

    plt.show()


if __name__ == "__main__":
    main()
