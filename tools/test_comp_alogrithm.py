"""Interactive tester for the temperature-compensation algorithm.

Applies TempCompAlgorithm to a measurement CSV and plots the algorithm's
outputs. Pick a CSV (a file dialog opens if you don't pass one on the command
line), give a starting temperature, and the script anchors the run's reference
to the first row — exactly as the live app does in
QCMInterface.setMeasurementReference — then runs FreqToTemp over every row.

The CSV needs a temp- and a mass-frequency column, recognised under any of
their known aliases (Freq_T/Freq_M, or the run-logger's freq_temp_hz/
freq_mass_hz), so calibration sweeps, the old output.csv export, and the
data/runs/qcm_run_*.csv logs all work. A temperature column, if present, seeds
the default starting temperature and is overlaid as "Recorded Temp" for
comparison; a time column, if present, is used as the x-axis (seconds from the
start). Non-measurement rows (the run logger's SETTINGS event row) are skipped.

Use the check boxes on the left to choose which channels are drawn; because the
channels span different units (°C, Hz, nm), tick "Normalize" to scale each
selected series to its own 0-1 range so any combination is comparable.

Usage:
    python tools/test_comp_alogrithm.py [measurement.csv] [crystal] [start_temp]

All three arguments are optional: with no CSV a file dialog opens, and the
starting temperature is prompted for (defaulting to the first temp value found
in the CSV, or 23 °C). The crystal argument selects the settings profile — a
name in data/crystals/ or a path to a crystal .json — that supplies the
calibration coefficients (fM_*/fT_*) and sensor parameters (mass_sensitivity,
sens_area, freq_virgin), exactly as the live app does.
"""

import csv
import sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import CheckButtons, TextBox

path_root = Path(__file__).parent.parent
sys.path.append(str(path_root))

import src.processing.TempCompAlgorithm as tca
from src.domain.crystal import CrystalManager

# Channels shown when the window first opens (intersected with what's available).
DEFAULT_ON = {"recorded_temp", "calc_temp", "comp_thickness"}


def build_channels(has_recorded_temp):
    """Return the list of (key, label, colour) channels to plot. The recorded
    temperature is only included when the CSV actually carries a temp column."""
    channels = []
    if has_recorded_temp:
        channels.append(("recorded_temp", "Recorded Temp (°C)", "tab:red"))
    channels += [
        ("calc_temp",        "Calculated Temp (°C)",   "tab:orange"),
        ("freq_M",           "Mass Freq (Hz)",         "tab:blue"),
        ("freq_T",           "Temp Freq (Hz)",         "tab:cyan"),
        ("uncomp_thickness", "Uncomp. Thickness (nm)", "tab:olive"),
        ("comp_thickness",   "Comp. Thickness (nm)",   "tab:green"),
        ("comp_freq",        "Comp. Mass Freq (Hz)",   "tab:purple"),
    ]
    return channels


def load_crystal(arg):
    """Load a crystal settings profile by name (from data/crystals/) or from a
    path to a crystal .json file. Returns a CrystalProfile or None if missing."""
    path = Path(arg)
    if path.suffix == ".json" or path.exists():
        return CrystalManager(crystals_dir=str(path.parent)).load(path.stem)
    return CrystalManager().load(str(arg))


def pick_csv():
    """Open a file dialog to choose a CSV; fall back to a console prompt if no
    GUI file dialog is available. Returns a Path, or None if cancelled/empty."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        chosen = filedialog.askopenfilename(
            title="Choose a measurement CSV",
            initialdir=str(path_root / "data"),
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        root.destroy()
        return Path(chosen) if chosen else None
    except Exception:
        entered = input("Path to measurement CSV: ").strip().strip('"')
        return Path(entered) if entered else None


def prompt_start_temp(default):
    """Ask for the starting temperature [°C]; empty input keeps the default."""
    raw = input(f"Starting temperature [°C] (default {default:g}): ").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"'{raw}' is not a number; using {default:g} °C")
        return default


# Logical field -> accepted column names (first present one wins). Covers the
# calibration sweep, the old output.csv export, and the run-logger CSVs
# (data/runs/qcm_run_*.csv), which use their own header names.
COLUMN_ALIASES = {
    "Freq_T": ["Freq_T", "freq_temp_hz"],
    "Freq_M": ["Freq_M", "freq_mass_hz"],
    "Temp":   ["Temp", "Temp_C", "temperature_c"],
    "Time":   ["Time", "timestamp_s"],
}


def _resolve(fields, aliases):
    """First column name from `aliases` that exists in the CSV, or None."""
    return next((c for c in aliases if c in fields), None)


def load_measurement(path):
    """Read a measurement CSV. Needs a temp- and a mass-frequency column (under
    any of their known aliases); picks up optional temperature and time columns
    when present. Non-measurement rows — e.g. the run logger's SETTINGS event
    row, whose frequency cells are blank — are skipped. Raises ValueError on
    bad input."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError("CSV has no data rows")

    fields = rows[0].keys()
    col_ft = _resolve(fields, COLUMN_ALIASES["Freq_T"])
    col_fm = _resolve(fields, COLUMN_ALIASES["Freq_M"])
    if not col_ft or not col_fm:
        wanted = COLUMN_ALIASES["Freq_T"] if not col_ft else COLUMN_ALIASES["Freq_M"]
        raise ValueError(
            f"no {'temp' if not col_ft else 'mass'} frequency column "
            f"(looked for {' / '.join(wanted)}; found: {', '.join(fields)})"
        )
    col_temp = _resolve(fields, COLUMN_ALIASES["Temp"])
    col_time = _resolve(fields, COLUMN_ALIASES["Time"])

    def num(row, col):
        v = row.get(col, "")
        return float(v) if v not in (None, "") else None

    # Keep only rows that carry both frequencies; event/blank rows are dropped.
    ft, fm, temp, tim = [], [], [], []
    for r in rows:
        a, b = num(r, col_ft), num(r, col_fm)
        if a is None or b is None:
            continue
        ft.append(a)
        fm.append(b)
        if col_temp:
            t = num(r, col_temp)
            temp.append(np.nan if t is None else t)
        if col_time:
            tv = num(r, col_time)
            tim.append(np.nan if tv is None else tv)

    if not ft:
        raise ValueError("CSV has no measurement rows with numeric frequencies")

    data = {"Freq_T": np.array(ft), "Freq_M": np.array(fm)}
    if col_temp:
        data["Temp"] = np.array(temp)
    if col_time:
        t = np.array(tim)
        data["Time"] = t - t[0]  # seconds from the start of the run
    return data


def run_algorithm(data, profile, T_start):
    """Run TempCompAlgorithm over every row, returning channel arrays.

    The reference (start) point is the first row — fT_start/fM_start come from
    Freq_T[0]/Freq_M[0] and T_start is supplied by the caller, mirroring
    QCMInterface.setMeasurementReference. Coefficients and sensor parameters
    come from the crystal settings profile.
    """
    coefficients = {
        'fM_0': profile.fM_0, 'fM_1': profile.fM_1, 'fM_2': profile.fM_2, 'fM_3': profile.fM_3,
        'fT_0': profile.fT_0, 'fT_1': profile.fT_1, 'fT_2': profile.fT_2, 'fT_3': profile.fT_3,
    }
    temp_comp = tca.TempCompAlgorithm(
        coefficients=coefficients,
        T_start=T_start,
        fT_start=data["Freq_T"][0],
        fM_start=data["Freq_M"][0],
        sens_area=profile.sens_area,
        mass_sensitivity=profile.mass_sensitivity,
        freq_virgin=profile.freq_virgin,
    )

    calc_temp, uncomp, comp, comp_freq = [], [], [], []
    for fT, fM in zip(data["Freq_T"], data["Freq_M"]):
        t, u, c, cf = temp_comp.FreqToTemp(fT=fT, fM=fM)
        # FreqToTemp already returns the absolute temperature (it equals T_start at
        # the reference row), so use it as-is — no offset. Nones (failures) become
        # NaN so the point is skipped.
        calc_temp.append(np.nan if t is None else t)
        uncomp.append(np.nan if u is None else u)
        comp.append(np.nan if c is None else c)
        comp_freq.append(np.nan if cf is None else cf)

    result = {
        "freq_M":           data["Freq_M"],
        "freq_T":           data["Freq_T"],
        "calc_temp":        np.array(calc_temp, dtype=float),
        "uncomp_thickness": np.array(uncomp, dtype=float),
        "comp_thickness":   np.array(comp, dtype=float),
        "comp_freq":        np.array(comp_freq, dtype=float),
    }
    if "Temp" in data:
        result["recorded_temp"] = data["Temp"]
    return result


def main():
    # 1) Choose the CSV — an explicit CLI path overrides the file dialog.
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else pick_csv()
    if csv_path is None:
        print("No CSV selected.")
        sys.exit(1)
    if not csv_path.is_file():
        print(f"CSV file not found: {csv_path}")
        sys.exit(1)

    # 2) Crystal settings profile (coefficients + sensor parameters).
    crystal_arg = sys.argv[2] if len(sys.argv) > 2 else "TestCrystal"
    profile = load_crystal(crystal_arg)
    if profile is None:
        available = ", ".join(CrystalManager().list_names()) or "(none)"
        print(f"Crystal settings not found: {crystal_arg}")
        print(f"Available profiles in data/crystals/: {available}")
        sys.exit(1)

    try:
        data = load_measurement(csv_path)
    except ValueError as e:
        print(f"Could not read {csv_path.name}: {e}")
        sys.exit(1)

    # 3) Starting temperature — CLI arg, else prompt. Default is the first temp
    #    value in the CSV when it has one, otherwise 23 °C (the live-app default).
    default_temp = float(data["Temp"][0]) if "Temp" in data else 23.0
    if len(sys.argv) > 3:
        T_start = float(sys.argv[3])
    else:
        T_start = prompt_start_temp(default_temp)

    result = run_algorithm(data, profile, T_start)
    # T_start is editable live via the text box below, so keep the current value
    # in a mutable holder the callbacks can read and update.
    state = {"T_start": T_start}

    channels = build_channels("recorded_temp" in result)
    default_on = DEFAULT_ON & {key for key, _, _ in channels}

    # x-axis: elapsed time when the CSV has it, otherwise a plain sample index.
    if "Time" in data:
        x, xlabel = data["Time"], "Time (s)"
    else:
        x, xlabel = np.arange(len(result["freq_M"])), "Sample #"
    # Markers help on short sweeps but clutter (and slow down) long runs.
    marker = "o" if len(x) <= 400 else None

    fig, ax = plt.subplots(figsize=(12, 7))
    fig.canvas.manager.set_window_title(
        f"Temp-Comp Tester — {csv_path.name} / {profile.name} / T_start={T_start:g}°C"
    )
    plt.subplots_adjust(left=0.30, right=0.92, bottom=0.08, top=0.93)

    # One line per channel; raw keeps the unscaled values for the Normalize toggle.
    lines, raw = {}, {}
    for column, label, color in channels:
        y = result[column]
        raw[column] = y
        (line,) = ax.plot(x, y, label=label, color=color, linewidth=1.4,
                          marker=marker, markersize=3)
        line.set_visible(column in default_on)
        lines[column] = line

    ax.set_xlabel(xlabel)
    ax.set_title(f"{csv_path.name}  (start temp {T_start:g}°C, reference = first row)")
    ax.grid(True, alpha=0.3)

    # --- channel check boxes -------------------------------------------------
    labels = [label for _, label, _ in channels]
    states = [key in default_on for key, _, _ in channels]
    ax_checks = fig.add_axes([0.02, 0.30, 0.24, 0.6])
    ax_checks.set_title("Channels", fontsize=10)
    channel_check = CheckButtons(ax_checks, labels, states)
    for text, (_, _, color) in zip(channel_check.labels, channels):
        text.set_color(color)
        text.set_fontsize(9)

    # --- options check box ---------------------------------------------------
    ax_opts = fig.add_axes([0.02, 0.12, 0.24, 0.10])
    ax_opts.set_title("Options", fontsize=10)
    options_check = CheckButtons(ax_opts, ["Normalize"], [False])

    # --- starting-temperature text box ---------------------------------------
    # Type a new T_start and press Enter to re-run the algorithm live.
    ax_temp = fig.add_axes([0.12, 0.05, 0.12, 0.045])
    temp_box = TextBox(ax_temp, "T_start (°C)  ", initial=f"{T_start:g}")

    label_to_column = {label: column for column, label, _ in channels}

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

    def recompute(new_T):
        """Re-run the algorithm for a new start temperature and refresh the plot.
        Only the algorithm-derived channels change; freq/recorded stay put."""
        state["T_start"] = new_T
        updated = run_algorithm(data, profile, new_T)
        for column in raw:
            if column in updated:
                raw[column] = updated[column]
        ax.set_title(f"{csv_path.name}  (start temp {new_T:g}°C, reference = first row)")
        redraw()

    def on_temp_submit(text):
        try:
            new_T = float(text)
        except ValueError:
            # Restore the last good value; set_val re-fires this handler with a
            # valid number, so it recomputes harmlessly rather than looping.
            temp_box.set_val(f"{state['T_start']:g}")
            return
        if new_T != state["T_start"]:
            recompute(new_T)

    channel_check.on_clicked(redraw)
    options_check.on_clicked(redraw)
    temp_box.on_submit(on_temp_submit)
    redraw()

    plt.show()


if __name__ == "__main__":
    main()
