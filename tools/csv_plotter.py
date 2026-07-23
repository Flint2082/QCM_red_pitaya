"""Interactive plotter for QCM run CSVs exported from the web UI.

The exported file (qcm_YYYY-MM-DD-HH-MM-SS.csv) mixes two kinds of rows:
  * measurement rows  - all the channel columns are filled, event_* are blank
  * event rows        - only event_type / event_detail are filled (lock lost,
                        disconnections, ...) and show up as vertical markers.

One event row is special: SETTINGS carries a JSON blob of the settings the run
was acquired with. It is not drawn as a marker — tick "Show settings" to overlay
it on the plot (it is also printed to the console on load).

Usage:
    python tools/csv_plotter.py [path/to/qcm_run.csv]

With no argument it opens a file dialog, or falls back to the most recent
qcm_*.csv found in the current dir, ./data, or your Downloads folder.

Use the check boxes on the left to choose which channels are drawn. Because the
channels span very different units (nm, Hz, °C, 0-1), tick "Normalize" to scale
every selected series to its own 0-1 range so any combination is comparable.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.widgets import CheckButtons

# CSV column -> (legend label, line colour). Order sets the checkbox order.
CHANNELS = [
    ("thickness_comp_nm",   "Comp. Thickness (nm)",   "tab:green"),
    ("thickness_uncomp_nm", "Uncomp. Thickness (nm)", "tab:olive"),
    ("temperature_c",       "Temperature (°C)",       "tab:red"),
    ("freq_mass_hz",        "Mass Freq (Hz)",         "tab:blue"),
    ("freq_temp_hz",        "Temp Freq (Hz)",         "tab:cyan"),
    ("comp_mass_freq_hz",   "Comp. Mass Freq (Hz)",   "tab:orange"),
    ("amp_mass",            "Mass Amplitude",         "tab:purple"),
    ("phase_mass",          "Mass Phase",             "tab:pink"),
    ("amp_temp",            "Temp Amplitude",         "tab:brown"),
    ("phase_temp",          "Temp Phase",             "tab:gray"),
    ("lock_mass",           "Mass Lock",              "tab:orange"),
    ("lock_temp",           "Temp Lock",              "darkgoldenrod"),
]

# Channels shown when the window first opens.
DEFAULT_ON = {"thickness_comp_nm", "thickness_uncomp_nm", "temperature_c", "comp_mass_freq_hz"}

LOCK_COLUMNS = {"lock_mass", "lock_temp"}


def find_default_csv():
    """Return the most recent qcm_*.csv from a few likely locations, or None."""
    candidates = []
    for d in (Path.cwd(), Path.cwd() / "data", Path.home() / "Downloads"):
        if d.is_dir():
            candidates += list(d.glob("qcm_*.csv"))
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def pick_csv():
    """Resolve the CSV path from argv, a file dialog, or an auto-search."""
    if len(sys.argv) > 1:
        return Path(sys.argv[1])

    try:  # a file dialog is friendlier, but tkinter may be unavailable
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        chosen = filedialog.askopenfilename(
            title="Select a QCM run CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir=str((Path.home() / "Downloads") if (Path.home() / "Downloads").is_dir() else Path.cwd()),
        )
        root.destroy()
        if chosen:
            return Path(chosen)
    except Exception:
        pass

    return find_default_csv()


def to_numeric(series, column):
    """Coerce a column to float, mapping the lock booleans to 1/0."""
    if column in LOCK_COLUMNS:
        mapping = {"true": 1.0, "false": 0.0, "1": 1.0, "0": 0.0}
        return series.astype(str).str.strip().str.lower().map(mapping)
    return pd.to_numeric(series, errors="coerce")


def format_settings(settings):
    """Flatten the nested settings dict into aligned 'a.b.c: value' lines."""
    if not settings:
        return "No settings recorded in this file."
    lines = []

    def walk(node, prefix=""):
        for key in sorted(node):
            value = node[key]
            if isinstance(value, dict):
                walk(value, f"{prefix}{key}.")
            else:
                lines.append((f"{prefix}{key}", value))

    walk(settings)
    width = max(len(k) for k, _ in lines)
    return "\n".join(f"{k.ljust(width)} : {v}" for k, v in lines)


def load(path):
    """Split the CSV into measurement data, event markers, and the run settings."""
    df = pd.read_csv(path)

    et = df.get("event_type")
    is_event = et.notna() & (et.astype(str).str.strip() != "") if et is not None else pd.Series(False, index=df.index)

    data = df[~is_event].copy()
    events = df[is_event].copy()

    # SETTINGS rows are run metadata, not timeline events — pull them out so they
    # don't clutter the plot with a marker at t=0.
    settings = {}
    if not events.empty:
        is_settings = events["event_type"].astype(str).str.strip() == "SETTINGS"
        for detail in events.loc[is_settings, "event_detail"].dropna():
            try:
                settings = json.loads(detail)
            except (ValueError, TypeError):
                pass  # unparseable / older file — just show nothing
        events = events[~is_settings].copy()

    t0 = pd.to_numeric(data["timestamp_s"], errors="coerce").min()
    data["t_rel"] = pd.to_numeric(data["timestamp_s"], errors="coerce") - t0
    if not events.empty:
        events["t_rel"] = pd.to_numeric(events["timestamp_s"], errors="coerce") - t0

    return data, events, t0, settings


def main():
    path = pick_csv()
    if path is None or not Path(path).is_file():
        print("No CSV found. Pass one explicitly:\n    python tools/csv_plotter.py path/to/qcm_run.csv")
        sys.exit(1)

    data, events, _, settings = load(path)
    t = data["t_rel"].to_numpy()

    settings_text = format_settings(settings)
    print(f"\n=== Run settings — {Path(path).name} ===\n{settings_text}\n")

    fig, ax = plt.subplots(figsize=(12, 7))
    fig.canvas.manager.set_window_title(f"QCM Plotter — {Path(path).name}")
    plt.subplots_adjust(left=0.32, right=0.92, bottom=0.08, top=0.93)

    # One line per available channel; raw_y keeps the unscaled values for toggling.
    lines, raw = {}, {}
    available = [c for c in CHANNELS if c[0] in data.columns]
    for column, label, color in available:
        y = to_numeric(data[column], column).to_numpy(dtype=float)
        raw[column] = y
        (line,) = ax.plot(t, y, label=label, color=color, linewidth=1.4)
        line.set_visible(column in DEFAULT_ON)
        lines[column] = line

    # Vertical markers for logged events (lock lost, disconnect, ...).
    event_artists = []
    for _, ev in events.iterrows():
        if np.isfinite(ev["t_rel"]):
            vl = ax.axvline(ev["t_rel"], color="crimson", linestyle=":", linewidth=1, alpha=0.7)
            txt = ax.text(ev["t_rel"], 1.0, f" {ev['event_type']}", rotation=90,
                          va="top", ha="left", fontsize=7, color="crimson",
                          alpha=0.8, transform=ax.get_xaxis_transform())
            event_artists += [vl, txt]

    ax.set_xlabel("Time since start (s)")
    ax.set_title(Path(path).name)
    ax.grid(True, alpha=0.3)

    # Run settings, overlaid on request (hidden by default so it never obscures data).
    settings_box = ax.text(
        0.995, 0.985, settings_text, transform=ax.transAxes,
        ha="right", va="top", fontsize=6.5, family="monospace", zorder=5,
        bbox=dict(boxstyle="round", facecolor="lightyellow", edgecolor="gray", alpha=0.9),
    )
    settings_box.set_visible(False)

    # --- channel check boxes -------------------------------------------------
    labels = [label for _, label, _ in available]
    states = [c[0] in DEFAULT_ON for c in available]
    ax_checks = fig.add_axes([0.02, 0.30, 0.22, 0.6])
    ax_checks.set_title("Channels", fontsize=10)
    channel_check = CheckButtons(ax_checks, labels, states)
    for text, (_, _, color) in zip(channel_check.labels, available):
        text.set_color(color)
        text.set_fontsize(9)

    # --- options check boxes -------------------------------------------------
    ax_opts = fig.add_axes([0.02, 0.06, 0.22, 0.18])
    ax_opts.set_title("Options", fontsize=10)
    options_check = CheckButtons(
        ax_opts, ["Normalize", "Show events", "Show settings"],
        [False, bool(event_artists), False],
    )

    label_to_column = {label: column for column, label, _ in available}

    def redraw(_=None):
        normalize, show_events, show_settings = options_check.get_status()
        settings_box.set_visible(show_settings)
        channel_status = dict(zip(labels, channel_check.get_status()))

        for label, on in channel_status.items():
            column = label_to_column[label]
            line = lines[column]
            line.set_visible(on)
            y = raw[column]
            if normalize:
                lo, hi = np.nanmin(y), np.nanmax(y)
                line.set_ydata((y - lo) / (hi - lo) if hi > lo else np.zeros_like(y))
            else:
                line.set_ydata(y)

        for artist in event_artists:
            artist.set_visible(show_events)

        # Rescale to whatever is currently visible.
        visible = [lines[label_to_column[l]] for l, on in channel_status.items() if on]
        if visible:
            ys = np.concatenate([ln.get_ydata() for ln in visible])
            ys = ys[np.isfinite(ys)]
            if ys.size:
                pad = (ys.max() - ys.min()) * 0.05 or 1.0
                ax.set_ylim(ys.min() - pad, ys.max() + pad)

        ax.set_ylabel("Normalized (per-series 0–1)" if normalize else "Value (mixed units)")
        ax.legend(visible, [ln.get_label() for ln in visible], loc="upper left", fontsize=8) if visible else ax.legend().set_visible(False)
        fig.canvas.draw_idle()

    channel_check.on_clicked(redraw)
    options_check.on_clicked(redraw)
    redraw()

    plt.show()


if __name__ == "__main__":
    main()
