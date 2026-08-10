"""Interactive plotter for QCM sweep CSVs written by the Red Pitaya.

Every sweep is logged on the Pitaya as it is taken (data/sweeps/) and can be
downloaded from the ANALYSIS tab's SAVE CSV button, `GET /sweeps/{name}/download`,
or `QCMClient.download_latest_sweep()`. The file holds:
  * a SETTINGS event row  - JSON of the sweep parameters (oscillator, span, step,
                            settle time), printed on load and available as an
                            overlay
  * one row per point     - frequency_hz, amplitude, phase

Amplitude and phase share the frequency axis: amplitude on the left, phase on
the right. The two strongest well-separated maxima are marked, which is the same
"peak / 2nd peak" the web UI reports — use them to read off the resonance and
whatever else the crystal is doing nearby.

Usage:
    python tools/sweep_plotter.py [path/to/qcm_sweep.csv]

With no argument it opens a file dialog, or falls back to the most recent
qcm_sweep_*.csv found in the current dir, ./data/sweeps, or your Downloads
folder.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.widgets import CheckButtons

# A resonance covers several samples, so the runner-up sample is nearly always
# the flank of the tallest peak. Ignore this many steps either side of a peak
# already taken, so the second marker is a genuinely separate feature. Matches
# PEAK_SEPARATION_STEPS in the web UI, so both report the same pair.
PEAK_SEPARATION_STEPS = 5

AMP_COLOR   = "tab:blue"
PHASE_COLOR = "tab:purple"


def find_default_csv():
    """Return the most recent qcm_sweep_*.csv from a few likely locations."""
    candidates = []
    for d in (Path.cwd(), Path.cwd() / "data" / "sweeps", Path.home() / "Downloads"):
        if d.is_dir():
            candidates += list(d.glob("qcm_sweep_*.csv"))
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
            title="Select a QCM sweep CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir=str((Path.home() / "Downloads") if (Path.home() / "Downloads").is_dir() else Path.cwd()),
        )
        root.destroy()
        if chosen:
            return Path(chosen)
    except Exception:
        pass

    return find_default_csv()


def load(path):
    """Split the CSV into sweep points and the recorded sweep parameters."""
    df = pd.read_csv(path)

    et = df.get("event_type")
    is_event = et.notna() & (et.astype(str).str.strip() != "") if et is not None else pd.Series(False, index=df.index)

    points = df[~is_event].copy()
    events = df[is_event].copy()

    settings, notes = {}, []
    for _, ev in events.iterrows():
        kind = str(ev["event_type"]).strip()
        if kind == "SETTINGS":
            try:
                settings = json.loads(ev["event_detail"])
            except (ValueError, TypeError):
                pass  # unparseable / older file — just show nothing
        else:
            notes.append(f"{kind}: {ev['event_detail']}")

    for column in ("frequency_hz", "amplitude", "phase"):
        points[column] = pd.to_numeric(points[column], errors="coerce")
    points = points.dropna(subset=["frequency_hz"]).sort_values("frequency_hz")

    return points, settings, notes


def format_settings(settings, notes):
    """Flatten the settings dict into aligned 'key: value' lines."""
    if not settings and not notes:
        return "No sweep parameters recorded in this file."
    lines = [(k, settings[k]) for k in sorted(settings)]
    text = ""
    if lines:
        width = max(len(k) for k, _ in lines)
        text = "\n".join(f"{k.ljust(width)} : {v}" for k, v in lines)
    if notes:
        text += ("\n\n" if text else "") + "\n".join(notes)
    return text


def find_peaks(freqs, amps, count=2):
    """The `count` highest well-separated maxima, tallest first.

    Deliberately not a local-maximum test: on a noisy trace that finds dozens of
    bumps. Taking the global maximum and then excluding a window around it is
    what makes "2nd peak" mean a separate resonance.
    """
    step = np.median(np.diff(freqs)) if freqs.size > 1 else 0.0
    sep = PEAK_SEPARATION_STEPS * abs(step)
    peaks = []
    remaining = np.isfinite(amps)
    for _ in range(count):
        if not remaining.any():
            break
        idx = int(np.flatnonzero(remaining)[np.argmax(amps[remaining])])
        peaks.append((freqs[idx], amps[idx]))
        remaining &= np.abs(freqs - freqs[idx]) > sep
    return peaks


def main():
    path = pick_csv()
    if path is None or not Path(path).is_file():
        print("No sweep CSV found. Pass one explicitly:\n"
              "    python tools/sweep_plotter.py path/to/qcm_sweep.csv")
        sys.exit(1)

    points, settings, notes = load(path)
    if points.empty:
        print(f"{Path(path).name} contains no sweep points (aborted before the first step?).")
        sys.exit(1)

    freqs = points["frequency_hz"].to_numpy(dtype=float)
    amps  = points["amplitude"].to_numpy(dtype=float)
    phase = points["phase"].to_numpy(dtype=float)

    settings_text = format_settings(settings, notes)
    print(f"\n=== Sweep parameters — {Path(path).name} ===\n{settings_text}\n")

    peaks = find_peaks(freqs, amps)
    for i, (f, a) in enumerate(peaks, start=1):
        label = "Peak" if i == 1 else f"Peak {i}"
        print(f"{label:8} {f:>12,.0f} Hz   amplitude {a:.5f}")
    print()

    fig, ax = plt.subplots(figsize=(12, 7))
    fig.canvas.manager.set_window_title(f"QCM Sweep — {Path(path).name}")
    plt.subplots_adjust(left=0.26, right=0.90, bottom=0.10, top=0.93)

    ax_phase = ax.twinx()
    (amp_line,)   = ax.plot(freqs, amps, color=AMP_COLOR, linewidth=1.4, label="Amplitude")
    (phase_line,) = ax_phase.plot(freqs, phase, color=PHASE_COLOR, linewidth=1.0,
                                  alpha=0.75, label="Phase")

    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Amplitude", color=AMP_COLOR)
    ax_phase.set_ylabel("Phase (rad)", color=PHASE_COLOR)
    ax.tick_params(axis="y", colors=AMP_COLOR)
    ax_phase.tick_params(axis="y", colors=PHASE_COLOR)
    ax.grid(True, alpha=0.3)
    ax.set_title(Path(path).name)

    # Peak markers: the tallest solid, the runner-up dashed.
    peak_artists = []
    for i, (f, a) in enumerate(peaks):
        style = "-" if i == 0 else "--"
        vl = ax.axvline(f, color="crimson", linestyle=style, linewidth=1, alpha=0.8)
        dot = ax.plot([f], [a], "o", color="crimson", markersize=5)[0]
        txt = ax.annotate(f"{'peak' if i == 0 else '2nd peak'}\n{f:,.0f} Hz\n{a:.4f}",
                          xy=(f, a), xytext=(6, -4), textcoords="offset points",
                          fontsize=8, color="crimson", va="top")
        peak_artists += [vl, dot, txt]

    settings_box = ax.text(
        0.995, 0.985, settings_text, transform=ax.transAxes,
        ha="right", va="top", fontsize=6.5, family="monospace", zorder=5,
        bbox=dict(boxstyle="round", facecolor="lightyellow", edgecolor="gray", alpha=0.9),
    )
    settings_box.set_visible(False)

    # --- options check boxes -------------------------------------------------
    ax_opts = fig.add_axes([0.02, 0.60, 0.20, 0.24])
    ax_opts.set_title("Options", fontsize=10)
    options_check = CheckButtons(
        ax_opts, ["Amplitude", "Phase", "Peaks", "Show settings"],
        [True, True, bool(peak_artists), False],
    )

    def redraw(_=None):
        show_amp, show_phase, show_peaks, show_settings = options_check.get_status()
        amp_line.set_visible(show_amp)
        phase_line.set_visible(show_phase)
        for artist in peak_artists:
            artist.set_visible(show_peaks)
        settings_box.set_visible(show_settings)
        fig.canvas.draw_idle()

    options_check.on_clicked(redraw)
    redraw()

    plt.show()


if __name__ == "__main__":
    main()
