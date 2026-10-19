"""Admittance circles from QCM sweep CSVs.

Front end (thesis fig 6-2, Ferrari bridge): V_A - V_B = V_in * Z_F * (Y_xtal - jwCc),
so the demodulated complex signal A*exp(j*phase) is the crystal admittance times
an unknown, slowly varying complex gain. Over a few kHz that gain is constant:
fit  H(f) = b0 + b1*u + sum_k K_k / (1 + 2jQ_k(f - fs_k)/fs_k)  and rotate by
-arg(K_main) so the motional circle sits on the +G axis, diameter = 1/R1 (arb.).
"""
import sys
import numpy as np, pandas as pd
from scipy.optimize import least_squares
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURF, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
MEAS, FIT, SPUR = "#2a78d6", "#eb6834", "#1baf7a"

coarse_path, fine_path, out = sys.argv[1], sys.argv[2], sys.argv[3]


def load(path):
    df = pd.read_csv(path)
    df = df[df.event_type.isna()]
    return df.frequency_hz.to_numpy(float), df.amplitude.to_numpy(float) * np.exp(1j * df.phase.to_numpy(float))


def lor(ff, K, fs, Q):
    return K / (1 + 2j * Q * (ff - fs) / fs)


def model(p, ff, fc, n):
    h = (p[0] + 1j * p[1]) + (p[2] + 1j * p[3]) * (ff - fc) / 1e4
    for k in range(n):
        a, ph, d, lq = p[4 + 4 * k: 8 + 4 * k]
        h = h + lor(ff, a * np.exp(1j * ph), fc + d, 10 ** lq)
    return h


def fit(f, H, fc, half, guesses):
    """guesses: approximate fs of each resonance in the window, main one first."""
    m = np.abs(f - fc) <= half
    ff, hh = f[m], H[m]
    n = len(guesses)
    bg = np.median(hh[np.abs(ff - fc) > half * 0.8])
    res = lambda p: np.concatenate([(model(p, ff, fc, n) - hh).real, (model(p, ff, fc, n) - hh).imag])
    step = np.median(np.diff(ff))
    shifts = np.arange(-240, 241, 20) if step > 100 else [0.0]
    best = None
    for s0 in shifts:
        for lq in (4.5, 5.0, 5.5):
            p0 = [bg.real, bg.imag, 0, 0]
            for k, g in enumerate(guesses):
                i = np.argmin(np.abs(ff - g)); kk = hh[i] - bg
                p0 += [abs(kk) * 1.2, np.angle(kk), ff[i] - fc + (s0 if k == 0 else 0), lq]
            r = least_squares(res, p0, x_scale="jac", max_nfev=6000)
            if best is None or r.cost < best.cost:
                best = r
    p = best.x.copy()
    for k in range(n):
        if p[4 + 4 * k] < 0:
            p[4 + 4 * k] *= -1; p[5 + 4 * k] += np.pi
        p[5 + 4 * k] = (p[5 + 4 * k] + np.pi) % (2 * np.pi) - np.pi
    dof = max(1, 2 * len(ff) - len(p)); s2 = 2 * best.cost / dof
    err = np.sqrt(np.diag(np.linalg.pinv(best.jac.T @ best.jac) * s2))
    modes = []
    for k in range(n):
        a, ph, d, lq = p[4 + 4 * k: 8 + 4 * k]
        modes.append(dict(D=a, rot=ph, fs=fc + d, Q=10 ** lq,
                          eD=err[4 + 4 * k], efs=err[6 + 4 * k], eQ=np.log(10) * err[7 + 4 * k]))
    return dict(ff=ff, hh=hh, p=p, n=n, fc=fc, rms=np.sqrt(s2), modes=modes, step=step)


def report(label, r):
    print(f"{label}  (step {r['step']:.0f} Hz, {len(r['ff'])} pts, fit rms {r['rms']:.2f} counts)")
    for i, m in enumerate(r["modes"]):
        print(f"   {'main' if i == 0 else 'spur'}: fs = {m['fs']:,.1f} +/- {m['efs']:.1f} Hz   "
              f"Q = {m['Q']:,.0f} (+/-{m['eQ']*100:.0f} %)   FWHM = {m['fs']/m['Q']:.1f} Hz   "
              f"1/R1 = {m['D']:.0f} +/- {m['eD']:.0f} counts   inst. phase = {m['rot']:+.3f} rad")


fc_, Hc_ = load(coarse_path)
ff_, Hf_ = load(fine_path)
c_mode   = fit(fc_, Hc_, 5_984_500, 12_000, [5_984_500])
b_coarse = fit(fc_, Hc_, 6_571_500, 12_000, [6_571_500, 6_567_750])
b_fine   = fit(ff_, Hf_, 6_567_600, 10_000, [6_571_220, 6_567_630])
report("c-mode, coarse sweep 09:53", c_mode)
report("b-mode, coarse sweep 09:53", b_coarse)
report("b-mode, fine sweep 14:12 (25 C)", b_fine)



# ---------------------------------------------------------------- plot
# Everything is shown as measured, background included. The only transformation
# is a rotation by the main mode's instrument phase, so the axes read as G and B.
BGC, BARE = "#0b0b0b", "#8a8984"


def rot(r):
    return np.exp(-1j * r["modes"][0]["rot"])


def bg_fit(r, ff):
    p = r["p"]
    return ((p[0] + 1j * p[1]) + (p[2] + 1j * p[3]) * (ff - r["fc"]) / 1e4) * rot(r)


def full_fit(r, ff):
    return model(r["p"], ff, r["fc"], r["n"]) * rot(r)


def dense(r, span=10_000):
    return np.sort(np.concatenate([mm["fs"] + np.linspace(-1, 1, 6001) ** 3 * span for mm in r["modes"]]))


def style(ax):
    ax.set_facecolor(SURF); ax.grid(True, which="both", color=GRID, lw=0.6); ax.set_axisbelow(True)
    for s in ("top", "right"): ax.spines[s].set_visible(False)


def circle_panel(ax, r, title, note):
    m = r["modes"][0]
    Y = r["hh"] * rot(r)
    fd = dense(r)
    Yfit = full_fit(r, fd)
    b = bg_fit(r, np.array([m["fs"]]))[0]
    ax.axhline(0, color=GRID, lw=1); ax.axvline(0, color=GRID, lw=1)
    ax.plot(Yfit.real, Yfit.imag, color=FIT, lw=2, label="fit: background + resonances", zorder=2)
    ax.scatter(Y.real, Y.imag, s=18 if r["step"] < 100 else 30, color=MEAS, edgecolor="none",
               label=f"measured, {r['step']:.0f} Hz steps", zorder=3)
    ax.plot(b.real, b.imag, "D", ms=7, color=BGC, mec=SURF, mew=1, zorder=5, label="fitted background")
    for s_ in r["modes"][1:]:
        y = full_fit(r, np.array([s_["fs"]]))[0]
        ax.annotate(f"spurious mode\n{s_['fs']:,.0f} Hz", (y.real, y.imag), xytext=(-8, 0),
                    textcoords="offset points", fontsize=8.5, color=INK2, ha="right", va="center")
    for x, lab, va in ((0, f"fs = {m['fs']:,.0f} Hz", "center"),
                       (-1, "fs − FWHM/2", "bottom"), (1, "fs + FWHM/2", "top")):
        y = full_fit(r, np.array([m["fs"] + x * m["fs"] / (2 * m["Q"])]))[0]
        ax.plot(y.real, y.imag, "o", ms=9, mfc="none", mec=INK, mew=1.2, zorder=2.5)
        ax.annotate(lab, (y.real, y.imag), xytext=(9, 0), textcoords="offset points",
                    fontsize=8.5, color=INK2, va=va, ha="left")
    ax.annotate("f below fs", (0, m["D"] * 0.42), xytext=(10, 0), textcoords="offset points",
                fontsize=8, color=INK2, va="center")
    ax.annotate("f above fs", (0, -m["D"] * 0.42), xytext=(10, 0), textcoords="offset points",
                fontsize=8, color=INK2, va="center")
    ax.set_aspect("equal"); style(ax)
    lim = m["D"] * 0.62
    ax.set_xlim(-m["D"] * 0.12, m["D"] * 1.32); ax.set_ylim(-lim, lim)
    ax.set_xlabel("conductance G  (arb. units = demod counts)")
    ax.set_ylabel("susceptance B  (arb. units)")
    ax.set_title(f"{title}\nQ ≈ {m['Q']/1e3:.0f}k   FWHM ≈ {m['fs']/m['Q']:.0f} Hz   {note}",
                 loc="left", fontsize=10.5, color=INK)
    ax.legend(loc="lower right", frameon=False, fontsize=8.5)
    return Y


def zoom_panel(ax, r, half=120):
    """Around the origin: where every circle starts and ends. The background is the
    offset between the origin and that point."""
    m = r["modes"][0]
    Y = r["hh"] * rot(r)
    fd = dense(r, span=12_000)
    full, bare = full_fit(r, fd), full_fit(r, fd) - bg_fit(r, fd)
    b = bg_fit(r, np.array([m["fs"]]))[0]
    ax.axhline(0, color=GRID, lw=1); ax.axvline(0, color=GRID, lw=1)
    ax.plot(bare.real, bare.imag, color=BARE, lw=1.4, ls=(0, (4, 3)), label="same fit without background", zorder=2)
    ax.plot(full.real, full.imag, color=FIT, lw=2, label="fit: background + resonances", zorder=3)
    ax.scatter(Y.real, Y.imag, s=12 if r["step"] < 100 else 26, color=MEAS, edgecolor="none",
               label="measured", zorder=4)
    ax.annotate("", xy=(b.real, b.imag), xytext=(0, 0),
                arrowprops=dict(arrowstyle="-|>", color=BGC, lw=1.4, mutation_scale=12), zorder=5)
    ax.plot(b.real, b.imag, "D", ms=7, color=BGC, mec=SURF, mew=1, zorder=6)
    ax.annotate(f"background\n{abs(b):.1f} counts\nG = {b.real:+.1f}, B = {b.imag:+.1f}", (b.real, b.imag),
                xytext=(-10, -14), textcoords="offset points", ha="right", va="top", fontsize=8.5, color=INK)
    ax.set_xlim(-half * 0.9, half * 1.1); ax.set_ylim(-half, half); ax.set_aspect("equal"); style(ax)
    ax.set_xlabel("conductance G  (arb. units)"); ax.set_ylabel("susceptance B  (arb. units)")
    ax.set_title(f"zoom around the origin (±{half} counts)", loc="left", fontsize=10, color=INK)
    ax.legend(loc="upper right", frameon=False, fontsize=8)


def magnitude_panel(ax, r):
    m = r["modes"][0]
    fine = r["step"] < 100
    fd = np.sort(np.concatenate([np.linspace(r["ff"][0], r["ff"][-1], 20001)] +
                                [mm["fs"] + np.linspace(-300, 300, 2001) for mm in r["modes"]]))
    ax.semilogy(fd - m["fs"], np.abs(full_fit(r, fd) - bg_fit(r, fd)), color=BARE, lw=1.4, ls=(0, (4, 3)),
                label="fit without background", zorder=2)
    ax.semilogy(fd - m["fs"], np.abs(bg_fit(r, fd)), color=BGC, lw=1.2, ls=(0, (1, 2)),
                label="background level", zorder=2)
    ax.semilogy(fd - m["fs"], np.abs(full_fit(r, fd)), color=FIT, lw=2, label="fit: background + resonances", zorder=3)
    ax.semilogy(r["ff"] - m["fs"], np.abs(r["hh"]), "o", ms=2.2 if fine else 5, color=MEAS, mec="none",
                label=f"measured |signal| ({r['step']:.0f} Hz steps)", zorder=4)
    if r["n"] > 1:
        s_ = r["modes"][1]
        ax.annotate(f"spurious mode\n{s_['fs']:,.0f} Hz", (s_["fs"] - m["fs"], s_["D"]), xytext=(-10, 0),
                    textcoords="offset points", fontsize=8.5, color=INK2, ha="right", va="center")
    if fine:
        ax.set_xlim(r["ff"][0] - m["fs"], r["ff"][-1] - m["fs"])
    else:
        ax.set_xlim(-10_000, 10_000)
    style(ax)
    ax.set_xlabel(f"f − fs  (Hz),   fs = {m['fs']:,.0f} Hz")
    ax.set_ylabel("|signal|  (arb. units, log)")
    ax.legend(frameon=False, fontsize=8, loc="lower left" if fine else "upper left")


plt.rcParams.update({"font.size": 10, "axes.edgecolor": INK2, "axes.labelcolor": INK2,
                     "xtick.color": INK2, "ytick.color": INK2, "text.color": INK})
fig, axes = plt.subplots(3, 2, figsize=(13, 17.5), facecolor=SURF,
                         gridspec_kw=dict(height_ratios=[1.2, 1.0, 0.85]))

circle_panel(axes[0, 0], b_fine, "b-mode (temp) — fine sweep 14:12, 25 °C", "(spurious mode kept)")
Yc = circle_panel(axes[0, 1], c_mode, "c-mode (mass) — coarse sweep 09:53", "(1 pt near top: circle is a fit)")
ax = axes[0, 1]
ipk = np.argmax(np.abs(Yc))
ax.annotate(f"{c_mode['ff'][ipk]:,.0f} Hz — the only\nmeasured point near resonance",
            (Yc[ipk].real, Yc[ipk].imag), xytext=(30, -45), textcoords="offset points",
            ha="left", va="top", fontsize=8.5, color=INK, arrowprops=dict(arrowstyle="-", color=INK2, lw=0.8))
ax.annotate(f"other {len(Yc) - 1} points (±0.5–12 kHz)\nall sit near the background point",
            (Yc[ipk - 1].real, Yc[ipk - 1].imag), xytext=(30, -60), textcoords="offset points",
            ha="left", va="top", fontsize=8.5, color=INK, arrowprops=dict(arrowstyle="-", color=INK2, lw=0.8))

zoom_panel(axes[1, 0], b_fine)
zoom_panel(axes[1, 1], c_mode)
magnitude_panel(axes[2, 0], b_fine)
magnitude_panel(axes[2, 1], c_mode)

fig.suptitle("SC-cut QCM crystal — admittance as measured, background included "
             f"(only rotated by the instrument phase, {b_fine['modes'][0]['rot']:+.2f} rad)",
             fontsize=12, color=INK, x=0.02, ha="left")
fig.tight_layout(rect=(0, 0, 1, 0.98))
fig.savefig(out, dpi=150, facecolor=SURF)
print("saved", out)
