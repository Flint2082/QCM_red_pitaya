"""Admittance-circle plotter for QCM sweep CSVs written by the Red Pitaya.

The front end is a capacitance-compensated bridge (Ferrari et al. 2008, thesis
fig 6-2): the difference signal it hands the lock-in is

    V_A - V_B = V_in * Z_F * (Y_xtal - jwCc)

so the demodulated amplitude * exp(j * phase) in a sweep CSV is the crystal's
admittance times an unknown complex gain. Across the few kHz around one mode
that gain is constant, which makes each resonance a circle in the complex plane
sitting on a slowly varying background. This tool fits

    H(f) = b0 + b1*(f - fc) + sum_k K_k / (1 + 2j*Q_k*(f - fs_k)/fs_k)

to the complex data, with one term for the mode and one for any other resonance
(a spur) inside the fit window. Nothing is subtracted for the plot — the point
is to see what the background does. Data and fit are only rotated by
-arg(K_main), which turns the mode's circle to open along +G: its diameter |K|
is 1/R1 in arbitrary units (demod counts), arg(K) is the instrument phase, and
the circle starts from the background rather than the origin. Spurs show as
their own smaller circles, nested inside the mode's.
Every mode measured on the same front end should need the same instrument
phase (about -1.36 rad at 6.0-6.6 MHz), which is the check that the model fits.

The window shows, for each fitted mode:
  * top left  - the G-B plane: measured points plus the full fit (background
                and every resonance), with fs and fs +/- FWHM/2 marked on it,
                each spur labelled with its fs, and the fitted background at fs
                as a black diamond
  * top right - a zoom to +/-120 counts around the origin: the arrow from the
                origin to the diamond is the background, and the same fit
                without it is drawn dashed for comparison
  * bottom    - raw |signal| against detuning on a log scale, against the full
                fit, the background level (dotted) and the fit without the
                background (dashed)

Printed per mode: fs, Q, FWHM, 1/R1 (arb.) and the instrument phase, each with
the fit's 1-sigma error, and the background at fs.

The modes are only 40-55 Hz wide. A sweep stepped coarser than about FWHM/3
puts at most a couple of points on the circle, so what is drawn is the model's
reconstruction rather than a measurement, and Q tends to come out low (by up to
~12 % at 500 Hz steps). The tool says so and prints the 10 Hz, +/-10 kHz sweep
to take instead. Coarse fits are also why the fit is multi-start: with one point
near the top, the answer depends on where the search begins.

Usage:
    python tools/admittance_plotter.py [path/to/qcm_sweep.csv]
    python tools/admittance_plotter.py sweep.csv --peaks 2
    python tools/admittance_plotter.py sweep.csv --mode-freq 5984500 6571500
    python tools/admittance_plotter.py sweep.csv --save circle.png

By default the strongest resonance in the sweep is fitted; --peaks N fits the N
strongest and --mode-freq the one(s) nearest the frequencies given, which is how
to pick a mode out of a wide sweep. With no file argument it opens a file
dialog, or falls back to the most recent qcm_sweep_*.csv found in the current
dir, ./data/sweeps, or your Downloads folder.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.widgets import CheckButtons
from scipy.ndimage import median_filter
from scipy.optimize import least_squares
from scipy.signal import find_peaks

# Resonance detection. A resonance is a short excursion of the complex signal
# away from the bridge background, which is taken as a running median over
# BACKGROUND_HZ (at least BACKGROUND_STEPS points) — much wider than a mode, so
# the mode cannot drag it along. A peak in that excursion has to stand
# MIN_PEAK_SNR times the point-to-point noise clear of its surroundings
# (prominence, not height, so the flanks of a strong mode never count as a
# second one), and peaks closer than PEAK_SEPARATION_STEPS are one feature, as
# in sweep_plotter.py.
BACKGROUND_HZ         = 4_000
BACKGROUND_STEPS      = 40
MIN_PEAK_SNR          = 20
PEAK_SEPARATION_STEPS = 5
LISTED_PEAKS          = 8

# --mode-freq takes the strongest resonance within this distance of the
# frequency given (and at least SNAP_STEPS sweep steps).
SNAP_HZ    = 1_000
SNAP_STEPS = 3

# Fit window. Half-width in Hz, and never fewer than MIN_WINDOW_STEPS steps
# either side of the mode, so a coarse sweep still leaves the fit enough points.
WINDOW_HZ        = 10_000
MIN_WINDOW_STEPS = 20

# Another resonance inside the window is fitted alongside the mode once it is at
# least this fraction of the mode's height; left out, its circle bends the
# background and pulls the mode's fit. At most MAX_SPURS of them, strongest first.
MIN_SPUR_FRACTION = 0.02
MAX_SPURS         = 3

# Above this step a mode has at most one or two points on it, and the fit lands
# wherever its start point leads, so it is restarted from fs offsets spread across
# one step and every Q in START_LOG_Q; the best fit wins.
MULTISTART_STEP_HZ = 100
MULTISTART_SHIFTS  = 25
START_LOG_Q        = (4.5, 5.0, 5.5)

# The circle counts as measured with at least this many points per FWHM.
MIN_POINTS_PER_FWHM = 3

# What to suggest instead of a sweep that is too coarse.
SUGGEST_SPAN_HZ = 10_000
SUGGEST_STEP_HZ = 10

# Half-width of the zoom on the origin, in counts: enough to show where the
# background puts the start of every circle.
ZOOM_COUNTS = 120

# b1 is per 10 kHz, so the background slope is the same order as b0 in the fit.
BG_SLOPE_HZ = 1e4

MEAS_COLOR = "tab:blue"
FIT_COLOR  = "tab:orange"
MARK_COLOR = "crimson"
BG_COLOR   = "black"


def find_default_csv():
    """Return the most recent qcm_sweep_*.csv from a few likely locations."""
    candidates = []
    for d in (Path.cwd(), Path.cwd() / "data" / "sweeps", Path.home() / "Downloads"):
        if d.is_dir():
            candidates += list(d.glob("qcm_sweep_*.csv"))
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def pick_csv(arg):
    """Resolve the CSV path from the command line, a file dialog, or an auto-search."""
    if arg:
        return Path(arg)

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
    points = points.dropna(subset=["frequency_hz", "amplitude", "phase"]).sort_values("frequency_hz")

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


# ----------------------------------------------------------------- detection
def excursion(freqs, H):
    """|signal - background| per point, and the point-to-point noise level."""
    step = float(np.median(np.diff(freqs)))
    width = int(max(BACKGROUND_STEPS, BACKGROUND_HZ / step)) | 1
    bg = (median_filter(H.real, width, mode="nearest")
          + 1j * median_filter(H.imag, width, mode="nearest"))
    noise = float(np.median(np.abs(np.diff(H)))) / np.sqrt(2)
    return np.abs(H - bg), noise


def find_resonances(freqs, exc, noise):
    """Every resonance that stands clear of the noise, as (frequency, height),
    tallest first."""
    idx, _ = find_peaks(exc, prominence=MIN_PEAK_SNR * noise, distance=PEAK_SEPARATION_STEPS)
    idx = idx[np.argsort(exc[idx])[::-1]]
    return [(float(freqs[i]), float(exc[i])) for i in idx]


def snap(f0, resonances, step):
    """The strongest detected resonance close to f0, else f0 itself."""
    near = [r for r in resonances if abs(r[0] - f0) <= max(SNAP_HZ, SNAP_STEPS * step)]
    return near[0][0] if near else f0


# ----------------------------------------------------------------------- fit
def lorentzian(f, K, fs, Q):
    return K / (1 + 2j * Q * (f - fs) / fs)


def model(p, f, fc, n):
    """Background b0 + b1*(f - fc) plus n resonances; p holds b0, b1 as (re, im)
    pairs, then (|K|, arg K, fs - fc, log10 Q) per resonance."""
    h = (p[0] + 1j * p[1]) + (p[2] + 1j * p[3]) * (f - fc) / BG_SLOPE_HZ
    for k in range(n):
        a, ph, d, lq = p[4 + 4 * k: 8 + 4 * k]
        h = h + lorentzian(f, a * np.exp(1j * ph), fc + d, 10 ** lq)
    return h


def fit_mode(freqs, H, f_main, spurs, half):
    """Fit the mode near f_main, plus a term per spur, over f_main +/- half.

    Returns the window's data, the parameter vector and one dict per resonance
    (the mode first): fs, Q, D = |K| (1/R1, arb.), rot = arg K (the instrument
    phase), each with a 1-sigma error from the fit's Jacobian.
    """
    sel = np.abs(freqs - f_main) <= half
    ff, hh = freqs[sel], H[sel]
    fc = f_main
    guesses = [f_main] + list(spurs)
    n = len(guesses)
    step = float(np.median(np.diff(ff)))

    edge = np.abs(ff - fc) > 0.8 * half
    rim = hh[edge] if edge.any() else hh
    bg = np.median(rim.real) + 1j * np.median(rim.imag)

    def residual(p):
        d = model(p, ff, fc, n) - hh
        return np.concatenate([d.real, d.imag])

    shifts = (np.linspace(-0.5, 0.5, MULTISTART_SHIFTS) * step
              if step > MULTISTART_STEP_HZ else [0.0])
    best = None
    for shift in shifts:
        for lq in START_LOG_Q:
            p0 = [bg.real, bg.imag, 0.0, 0.0]
            for k, g in enumerate(guesses):
                i = np.argmin(np.abs(ff - g))
                kk = hh[i] - bg
                p0 += [abs(kk) * 1.2, np.angle(kk), ff[i] - fc + (shift if k == 0 else 0.0), lq]
            r = least_squares(residual, p0, x_scale="jac", max_nfev=6000)
            if best is None or r.cost < best.cost:
                best = r

    # Fold every resonance to |K| > 0 and arg K in (-pi, pi].
    p = best.x.copy()
    for k in range(n):
        if p[4 + 4 * k] < 0:
            p[4 + 4 * k] *= -1
            p[5 + 4 * k] += np.pi
        p[5 + 4 * k] = (p[5 + 4 * k] + np.pi) % (2 * np.pi) - np.pi

    dof = max(1, 2 * len(ff) - len(p))
    s2 = 2 * best.cost / dof
    err = np.sqrt(np.diag(np.linalg.pinv(best.jac.T @ best.jac) * s2))
    modes = []
    for k in range(n):
        a, ph, d, lq = p[4 + 4 * k: 8 + 4 * k]
        modes.append(dict(D=a, rot=ph, fs=fc + d, Q=10 ** lq,
                          eD=err[4 + 4 * k], erot=err[5 + 4 * k], efs=err[6 + 4 * k],
                          eQ=np.log(10) * err[7 + 4 * k]))
    return dict(ff=ff, hh=hh, p=p, n=n, fc=fc, rms=np.sqrt(s2), modes=modes, step=step)


def background(r, f):
    p = r["p"]
    return (p[0] + 1j * p[1]) + (p[2] + 1j * p[3]) * (f - r["fc"]) / BG_SLOPE_HZ


def rotation(r):
    """Undo the mode's instrument phase, so its circle opens along +G."""
    return np.exp(-1j * r["modes"][0]["rot"])


def background_fit(r, f):
    """The fitted background alone, in the mode's rotated frame."""
    return background(r, f) * rotation(r)


def full_fit(r, f):
    """The whole model — background plus every resonance — in the rotated frame."""
    return model(r["p"], f, r["fc"], r["n"]) * rotation(r)


def dense(r):
    """Frequencies to draw the fit at: cubed spacing crowds them near each fs,
    where the curve moves fastest. Kept inside the fit window."""
    span = r["ff"][-1] - r["ff"][0]
    fd = np.sort(np.concatenate([mm["fs"] + np.linspace(-1, 1, 6001) ** 3 * span for mm in r["modes"]]))
    return fd[(fd >= r["ff"][0]) & (fd <= r["ff"][-1])]


def points_per_fwhm(r):
    m = r["modes"][0]
    return m["fs"] / m["Q"] / r["step"]


def report(label, r, settings):
    """Print one fitted mode, its spurs, and whether the sweep resolved it."""
    print(f"{label}: fit window {r['ff'][0]:,.0f} – {r['ff'][-1]:,.0f} Hz "
          f"({len(r['ff'])} pts at {r['step']:.0f} Hz, fit rms {r['rms']:.2f} counts)")
    for i, m in enumerate(r["modes"]):
        print(f"   {'mode' if i == 0 else 'spur'}: fs    = {m['fs']:,.2f} ± {m['efs']:.2f} Hz\n"
              f"         Q     = {m['Q']:,.0f} (± {m['eQ'] * 100:.1f} %)\n"
              f"         FWHM  = {m['fs'] / m['Q']:.1f} Hz\n"
              f"         1/R1  = {m['D']:.1f} ± {m['eD']:.1f} (arb., demod counts)\n"
              f"         phase = {m['rot']:+.3f} ± {m['erot']:.3f} rad (instrument)")
    b = background_fit(r, np.array([r["modes"][0]["fs"]]))[0]
    print(f"   background at fs: {abs(b):.1f} counts (G = {b.real:+.1f}, B = {b.imag:+.1f}, rotated frame)")

    density = points_per_fwhm(r)
    if density < MIN_POINTS_PER_FWHM:
        fs = r["modes"][0]["fs"]
        osc = settings.get("oscillator_idx", 1)
        settle = settings.get("settle_time", 0.05)
        start = round(fs - SUGGEST_SPAN_HZ, -2)
        stop = round(fs + SUGGEST_SPAN_HZ, -2)
        print(f"   ! {r['step']:.0f} Hz steps against a {fs / r['modes'][0]['Q']:.0f} Hz FWHM "
              f"({density:.2f} points per FWHM): the circle is reconstructed from the\n"
              f"     model, not measured, and Q from a sweep this coarse tends to come out low —\n"
              f"     by more than the ± above, which is the fit's formal error only.\n"
              f"     For a measured circle sweep ±{SUGGEST_SPAN_HZ / 1e3:.0f} kHz at {SUGGEST_STEP_HZ} Hz steps:\n"
              f"         qcm.start_sweep({osc}, {start:.0f}, {stop:.0f}, {SUGGEST_STEP_HZ}, {settle})")
    print()


# ---------------------------------------------------------------------- plot
# Every panel shows the signal as measured: nothing is subtracted, only rotated
# by the mode's instrument phase so the axes read as G and B. The point is to
# see what the background does. Artists are returned by role so the Options
# check boxes can switch them.
def circle_panel(ax, r, label):
    """G-B plane: the measured signal and the full fit, background included."""
    m = r["modes"][0]
    fwhm = m["fs"] / m["Q"]
    density = points_per_fwhm(r)
    Y = r["hh"] * rotation(r)
    Yfit = full_fit(r, dense(r))
    b = background_fit(r, np.array([m["fs"]]))[0]

    ax.axhline(0, color="gray", lw=0.8, alpha=0.5)
    ax.axvline(0, color="gray", lw=0.8, alpha=0.5)
    (fit_line,) = ax.plot(Yfit.real, Yfit.imag, color=FIT_COLOR, lw=2, zorder=2,
                          label="fit: background + resonances")
    meas = ax.scatter(Y.real, Y.imag, s=18 if density >= MIN_POINTS_PER_FWHM else 30,
                      color=MEAS_COLOR, edgecolor="none", zorder=3,
                      label=f"measured, {r['step']:.0f} Hz steps")
    (bg_mark,) = ax.plot(b.real, b.imag, "D", ms=7, color=BG_COLOR, mec="white", mew=1, zorder=5,
                         label="fitted background at fs")

    marks = []
    for x, text, va in ((0, f"fs = {m['fs']:,.0f} Hz", "center"),
                        (-1, "fs − FWHM/2", "bottom"),
                        (1, "fs + FWHM/2", "top")):
        y = full_fit(r, np.array([m["fs"] + x * fwhm / 2]))[0]
        marks.append(ax.plot(y.real, y.imag, "o", ms=9, mfc="none", mec=MARK_COLOR, mew=1.2, zorder=2.5)[0])
        marks.append(ax.annotate(text, (y.real, y.imag), xytext=(9, 0), textcoords="offset points",
                                 fontsize=8.5, color=MARK_COLOR, va=va))
    # A smaller spur circle is labelled from inside, clear of the mode's fs
    # label; one that outgrows the mode (--mode-freq on the weaker of two) from
    # outside, for the same reason.
    for s in r["modes"][1:]:
        y = full_fit(r, np.array([s["fs"]]))[0]
        outside = s["D"] > m["D"]
        marks.append(ax.annotate(f"spur\n{s['fs']:,.0f} Hz", (y.real, y.imag),
                                 xytext=(9, 0) if outside else (-8, 0), textcoords="offset points",
                                 fontsize=8.5, color="gray", ha="left" if outside else "right",
                                 va="center"))

    # Framed on the fit, the background point and the origin; the headroom
    # above the top is for the legend.
    xs = np.concatenate([Yfit.real, [b.real, 0.0]])
    ys = np.concatenate([Yfit.imag, [b.imag, 0.0]])
    size = max(np.ptp(xs), np.ptp(ys))
    ax.annotate("f below fs", (b.real, b.imag + 0.42 * size), xytext=(10, 0), textcoords="offset points",
                fontsize=8, color="gray", va="center")
    ax.annotate("f above fs", (b.real, b.imag - 0.42 * size), xytext=(10, 0), textcoords="offset points",
                fontsize=8, color="gray", va="center")
    ax.set_aspect("equal")
    ax.set_xlim(xs.min() - 0.12 * size, xs.max() + 0.32 * size)
    ax.set_ylim(ys.min() - 0.12 * size, ys.max() + 0.36 * size)
    ax.grid(True, alpha=0.3)
    ax.set_xlabel("conductance G  (arb. units = demod counts)")
    ax.set_ylabel("susceptance B  (arb. units)")
    note = "resolved" if density >= MIN_POINTS_PER_FWHM else "model-reconstructed"
    ax.set_title(f"{label} — fs = {m['fs']:,.0f} Hz\n"
                 f"Q ≈ {m['Q'] / 1e3:.0f}k   FWHM ≈ {fwhm:.0f} Hz   "
                 f"({note}, {density:.1f} pts/FWHM)", loc="left", fontsize=10)
    ax.legend(loc="upper right", fontsize=8)
    return dict(measured=[meas], fit=[fit_line], background=[bg_mark], marks=marks)


def zoom_panel(ax, r):
    """Around the origin, where every circle starts and ends: the background is
    the offset between the origin and that point."""
    m = r["modes"][0]
    Y = r["hh"] * rotation(r)
    fd = dense(r)
    full = full_fit(r, fd)
    bare = full - background_fit(r, fd)
    b = background_fit(r, np.array([m["fs"]]))[0]
    half = max(ZOOM_COUNTS, 1.6 * abs(b))  # wider when the background is further out

    ax.axhline(0, color="gray", lw=0.8, alpha=0.5)
    ax.axvline(0, color="gray", lw=0.8, alpha=0.5)
    (bare_line,) = ax.plot(bare.real, bare.imag, color="gray", lw=1.4, ls=(0, (4, 3)), zorder=2,
                           label="same fit without background")
    (fit_line,) = ax.plot(full.real, full.imag, color=FIT_COLOR, lw=2, zorder=3,
                          label="fit: background + resonances")
    meas = ax.scatter(Y.real, Y.imag, s=12 if points_per_fwhm(r) >= MIN_POINTS_PER_FWHM else 26,
                      color=MEAS_COLOR, edgecolor="none", zorder=4, label="measured")
    arrow = ax.annotate("", xy=(b.real, b.imag), xytext=(0, 0), zorder=5,
                        arrowprops=dict(arrowstyle="-|>", color=BG_COLOR, lw=1.4, mutation_scale=12))
    (bg_mark,) = ax.plot(b.real, b.imag, "D", ms=7, color=BG_COLOR, mec="white", mew=1, zorder=6)
    # Labelled from a fixed spot right of centre, below the axis: the circles
    # leave the origin upwards and come back from below, so that corner stays
    # clear wherever the background happens to sit.
    bg_text = ax.annotate(f"background at fs\n{abs(b):.1f} counts\nG = {b.real:+.1f}, B = {b.imag:+.1f}",
                          (b.real, b.imag), xytext=(0.97, 0.42), textcoords="axes fraction",
                          ha="right", va="top", fontsize=8.5,
                          arrowprops=dict(arrowstyle="-", color="gray", lw=0.6, shrinkB=5))

    ax.set_aspect("equal")
    ax.set_xlim(-half * 0.9, half * 1.1)
    ax.set_ylim(-half, half)
    ax.grid(True, alpha=0.3)
    ax.set_xlabel("conductance G  (arb. units)")
    ax.set_ylabel("susceptance B  (arb. units)")
    # No legend: in a panel this narrow it sits on the curves, and the styles
    # are the ones the |signal| legend below spells out.
    ax.set_title(f"zoom around the origin (±{half:.0f} counts)", loc="left", fontsize=10)
    return dict(measured=[meas], fit=[fit_line], background=[bare_line, arrow, bg_mark, bg_text], marks=[])


def magnitude_panel(ax, r):
    """Raw |signal| against detuning, with the fit taken apart into background
    and resonances."""
    m = r["modes"][0]
    fd = np.sort(np.concatenate([np.linspace(r["ff"][0], r["ff"][-1], 20001)]
                                + [mm["fs"] + np.linspace(-300, 300, 2001) for mm in r["modes"]]))
    fd = fd[(fd >= r["ff"][0]) & (fd <= r["ff"][-1])]
    full, bg = full_fit(r, fd), background_fit(r, fd)
    resolved = points_per_fwhm(r) >= MIN_POINTS_PER_FWHM

    (bare_line,) = ax.semilogy(fd - m["fs"], np.abs(full - bg), color="gray", lw=1.4, ls=(0, (4, 3)),
                               zorder=2, label="fit without background")
    (bg_line,) = ax.semilogy(fd - m["fs"], np.abs(bg), color=BG_COLOR, lw=1.2, ls=(0, (1, 2)),
                             zorder=2, label="background level")
    (fit_line,) = ax.semilogy(fd - m["fs"], np.abs(full), color=FIT_COLOR, lw=2, zorder=3,
                              label="fit: background + resonances")
    (meas,) = ax.semilogy(r["ff"] - m["fs"], np.abs(r["hh"]), "o", ms=2.2 if resolved else 5,
                          color=MEAS_COLOR, mec="none", zorder=4,
                          label=f"measured |signal| ({r['step']:.0f} Hz steps)")
    for s in r["modes"][1:]:
        ax.annotate(f"spur\n{s['fs']:,.0f} Hz", (s["fs"] - m["fs"], s["D"]), xytext=(-10, 0),
                    textcoords="offset points", fontsize=8.5, color="gray", ha="right", va="center")

    # A band of empty decades below the lowest curve, for the legend: the
    # curves fill the top of the panel and the spur labels sit up there too.
    curves = np.concatenate([np.abs(full - bg), np.abs(bg), np.abs(full), np.abs(r["hh"])])
    lo, hi = curves[curves > 0].min(), curves.max()
    decades = np.log10(hi / lo)
    ax.set_ylim(lo / 10 ** (0.45 * decades), hi * 10 ** (0.08 * decades))
    ax.set_xlim(r["ff"][0] - m["fs"], r["ff"][-1] - m["fs"])
    ax.grid(True, which="both", alpha=0.3)
    ax.set_xlabel(f"f − fs  (Hz),   fs = {m['fs']:,.0f} Hz")
    ax.set_ylabel("|signal|  (arb., log)")
    ax.legend(loc="lower center", ncol=2, fontsize=7.5)
    return dict(measured=[meas], fit=[fit_line], background=[bare_line, bg_line], marks=[])


def plot(path, results, settings_text, save):
    # Per mode: the circle and the zoom side by side, |signal| underneath.
    n = len(results)
    width = min(3.1 + 8.2 * n, 19.5)  # about a screen wide; more modes narrow the columns
    fig = plt.figure(figsize=(width, 9.5))
    grid = fig.add_gridspec(2, 2 * n, width_ratios=[1.3, 1] * n, height_ratios=[1.25, 1],
                            left=3.1 / width, right=0.98, bottom=0.07, top=0.87,
                            wspace=0.35, hspace=0.32)
    fig.canvas.manager.set_window_title(f"QCM Admittance — {Path(path).name}")
    fig.suptitle(f"{Path(path).name}\nadmittance as measured — nothing subtracted, "
                 "only rotated by each mode's instrument phase", fontsize=11)

    groups = dict(measured=[], fit=[], background=[], marks=[])
    first = None
    for k, r in enumerate(results):
        ax_circle = fig.add_subplot(grid[0, 2 * k])
        if k == 0:
            first = ax_circle
        for panel in (circle_panel(ax_circle, r, f"mode {k + 1}"),
                      zoom_panel(fig.add_subplot(grid[0, 2 * k + 1]), r),
                      magnitude_panel(fig.add_subplot(grid[1, 2 * k:2 * k + 2]), r)):
            for role, artists in panel.items():
                groups[role] += artists

    settings_box = first.text(
        0.01, 0.99, settings_text, transform=first.transAxes,
        ha="left", va="top", fontsize=6.5, family="monospace", zorder=7,
        bbox=dict(boxstyle="round", facecolor="lightyellow", edgecolor="gray", alpha=0.9),
    )
    settings_box.set_visible(False)

    # --- options check boxes -------------------------------------------------
    ax_opts = fig.add_axes([0.2 / width, 0.55, 2.6 / width, 0.28])
    ax_opts.set_title("Options", fontsize=10)
    options_check = CheckButtons(
        ax_opts,
        ["Measured", "Fit", "Background", "Markers", "Show settings"],
        [True, True, True, True, False],
    )

    def redraw(_=None):
        show_meas, show_fit, show_bg, show_marks, show_settings = options_check.get_status()
        for role, show in (("measured", show_meas), ("fit", show_fit),
                           ("background", show_bg), ("marks", show_marks)):
            for artist in groups[role]:
                artist.set_visible(show)
        settings_box.set_visible(show_settings)
        fig.canvas.draw_idle()

    options_check.on_clicked(redraw)
    redraw()

    if save:
        ax_opts.set_visible(False)  # the check boxes mean nothing in a picture
        fig.savefig(save, dpi=150, bbox_inches="tight")
        ax_opts.set_visible(True)
        print(f"saved {save}")

    plt.show()


def parse_args():
    ap = argparse.ArgumentParser(description="Fit and plot QCM admittance circles from a sweep CSV.")
    ap.add_argument("csv", nargs="?", help="sweep CSV (default: file dialog, then newest qcm_sweep_*.csv)")
    ap.add_argument("--mode-freq", type=float, nargs="+", metavar="HZ",
                    help="fit the resonance nearest each of these frequencies (overrides --peaks)")
    ap.add_argument("--peaks", type=int, default=1, metavar="N",
                    help="fit the N strongest resonances (default 1)")
    ap.add_argument("--window", type=float, default=WINDOW_HZ, metavar="HZ",
                    help=f"fit half-width around each mode (default {WINDOW_HZ:,})")
    ap.add_argument("--save", metavar="PNG", help="also save the figure to this file")
    return ap.parse_args()


def main():
    args = parse_args()
    path = pick_csv(args.csv)
    if path is None or not Path(path).is_file():
        print("No sweep CSV found. Pass one explicitly:\n"
              "    python tools/admittance_plotter.py path/to/qcm_sweep.csv")
        sys.exit(1)

    points, settings, notes = load(path)
    if len(points) < 10:
        print(f"{Path(path).name} has {len(points)} sweep points — too few to fit a resonance.")
        sys.exit(1)

    freqs = points["frequency_hz"].to_numpy(dtype=float)
    H = points["amplitude"].to_numpy(dtype=float) * np.exp(1j * points["phase"].to_numpy(dtype=float))
    step = float(np.median(np.diff(freqs)))

    settings_text = format_settings(settings, notes)
    print(f"\n=== Sweep parameters — {Path(path).name} ===\n{settings_text}\n")

    exc, noise = excursion(freqs, H)
    resonances = find_resonances(freqs, exc, noise)
    if args.mode_freq:
        outside = [f for f in args.mode_freq if not freqs[0] <= f <= freqs[-1]]
        if outside:
            print(f"--mode-freq {', '.join(f'{f:,.0f}' for f in outside)} Hz is outside the sweep "
                  f"({freqs[0]:,.0f} – {freqs[-1]:,.0f} Hz).")
            sys.exit(1)
        targets = [snap(f, resonances, step) for f in args.mode_freq]
    else:
        targets = [f for f, _ in resonances[:args.peaks]]
    if not targets:
        print("No resonance stands clear of the noise in this sweep.")
        sys.exit(1)

    print(f"Resonances found ({len(resonances)}, tallest first, noise {noise:.3f} counts):")
    for i, (f, h) in enumerate(resonances[:LISTED_PEAKS], start=1):
        print(f"  {i:2}  {f:>14,.0f} Hz   height {h:9.2f}{'   <- fitted' if f in targets else ''}")
    if len(resonances) > LISTED_PEAKS:
        print(f"  ... {len(resonances) - LISTED_PEAKS} more; pick one with --mode-freq")
    if not args.mode_freq and len(targets) < args.peaks:
        print(f"  (asked for {args.peaks}, only {len(targets)} found)")
    print()

    half = max(args.window, MIN_WINDOW_STEPS * step)
    results = []
    for k, f0 in enumerate(targets, start=1):
        main_height = exc[np.argmin(np.abs(freqs - f0))]
        spurs = [f for f, h in resonances
                 if f != f0 and abs(f - f0) <= half and h >= MIN_SPUR_FRACTION * main_height][:MAX_SPURS]
        r = fit_mode(freqs, H, f0, spurs, half)
        report(f"Mode {k}", r, settings)
        results.append(r)

    if len(results) > 1:
        # One front end, one gain: every mode should need the same rotation.
        phases = [r["modes"][0]["rot"] for r in results]
        print(f"Instrument phase across the {len(results)} modes: {min(phases):+.3f} … {max(phases):+.3f} rad "
              "(should agree; one that does not is a bad fit)\n")

    plot(path, results, settings_text, args.save)


if __name__ == "__main__":
    main()
