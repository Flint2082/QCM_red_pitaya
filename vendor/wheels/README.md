# Vendored wheels

Python packages committed to the repo so a PC with no internet can install
them. See [OFFLINE.md](../../OFFLINE.md) for the whole offline story; this file
is just about what is in this directory.

```bash
pip install --no-index --find-links vendor/wheels -r requirements-client.txt
```

`--no-index` matters: without it pip tries PyPI first and sits there timing out
on every package before falling back to these files.

## What is here

| set | from | wheels | fits |
|-----|------|--------|------|
| control | [`requirements-client.txt`](../../requirements-client.txt) | requests, websocket-client + deps | **any** OS, **any** Python 3 |
| plotting | [`requirements-plot.txt`](../../requirements-plot.txt) | numpy, pandas, matplotlib, scipy + deps | Windows x64, Python 3.13 |

The control set is pure Python (`-py3-none-any.whl`), so it installs anywhere.
The plotting set is compiled: `numpy-2.4.2-cp313-cp313-win_amd64.whl` fits
**only** 64-bit Windows on Python 3.13. On anything else pip will say
`No matching distribution found` even though the file is sitting right there —
that is the wheel not matching the interpreter, not a missing download.

`charset_normalizer` and `fonttools` are present twice, compiled and pure. That
is deliberate: pip takes the compiled one where it fits and the portable one
where it does not, which is what lets this wheelhouse serve a Linux control PC
as well.

## Adding another target

On a machine that has internet:

```bash
python tools/fetch_wheels.py --platform manylinux2014_x86_64 --python-version 311
```

Wheels for several targets live here side by side — nothing is replaced, and pip
picks per machine. Then commit the new files.

To check a wheelhouse without an offline PC to hand:

```bash
python tools/fetch_wheels.py --verify
```

The versions are pinned in the requirements files, so re-running the fetch
reproduces this set rather than drifting to whatever is newest. The top-level
pins are exact; their dependencies are whatever pip resolved when the wheelhouse
was built, and all of them are here.

## The Pitaya is not covered

These are for control PCs. The board is `linux_armv7l`, and several of its
dependencies have no wheel for that platform on PyPI at all — `pip download`
cannot produce them from another machine. [OFFLINE.md](../../OFFLINE.md) covers
what to do instead.
