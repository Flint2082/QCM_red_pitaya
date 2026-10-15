"""Populate vendor/wheels/ so a PC with no internet can install what it needs.

Run this on a machine that *does* have internet, commit the result, and the
offline PC installs straight out of the repo:

    pip install --no-index --find-links vendor/wheels -r requirements-client.txt

The control set (requests, websocket-client) is pure Python, so what is
committed already works on any OS and any Python 3. The plotting set (numpy,
pandas, matplotlib, scipy) is compiled: those wheels only fit the OS and Python
minor version they were fetched for, which is why this script exists — point it
at another target and it adds that one alongside.

    python tools/fetch_wheels.py                          # this machine's own target
    python tools/fetch_wheels.py --platform win_amd64 --python-version 313
    python tools/fetch_wheels.py --platform manylinux2014_x86_64 --python-version 311
    python tools/fetch_wheels.py --set client             # just the small portable set
    python tools/fetch_wheels.py --verify                 # can the wheelhouse satisfy it?

Nothing is deleted: wheels for several targets live side by side and pip picks
the one that fits the machine it is installing on.
"""

import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHEELS = os.path.join(REPO, "vendor", "wheels")

# Which requirements file each set comes from. "server" is the Pitaya's own
# dependencies: fetching those needs --platform/--python-version matching the
# board (linux_armv7l), and several of them have no wheel for it at all, so it
# is offered but not part of the default.
SETS = {
    "client": "requirements-client.txt",
    "plot":   "requirements-plot.txt",
    "server": "requirements.txt",
}
DEFAULT_SETS = ("client", "plot")


def pip(*args: str) -> int:
    """Run pip in this interpreter and stream its output."""
    cmd = [sys.executable, "-m", "pip", *args]
    print("  $ " + " ".join(cmd[2:]))
    return subprocess.call(cmd)


def fetch(req: str, platform: str | None, python_version: str | None) -> bool:
    """Download one requirements file's wheels for one target."""
    args = ["download", "--only-binary=:all:", "--dest", WHEELS, "-r",
            os.path.join(REPO, req)]
    if platform:
        # pip requires the version to be pinned down as soon as the target is
        # not the running interpreter, hence both flags travelling together.
        args += ["--platform", platform, "--python-version", python_version or
                 f"{sys.version_info.major}{sys.version_info.minor}"]
    return pip(*args) == 0


def fetch_pure() -> list[str]:
    """Also grab the pure-Python build of anything in the wheelhouse that
    publishes one, and say which.

    Some packages ship both a compiled wheel and a `py3-none-any` fallback —
    charset-normalizer, which arrives as a dependency of requests, is the one
    that matters here. Fetching the fallback too is what lets a wheelhouse built
    on Windows still install on a Linux or a different-Python control PC: pip
    takes the compiled wheel where it fits and the portable one where it does
    not.

    Driven off the wheels actually present rather than off the requirements
    files, because the package that needs this is usually a dependency nobody
    named. Most attempts fail — numpy and friends have no pure build — which is
    expected and not worth printing, so pip is run quietly here.
    """
    added = []
    for wheel in sorted(os.listdir(WHEELS)):
        if not wheel.endswith(".whl") or "-none-any.whl" in wheel:
            continue  # already portable
        name, version = wheel.split("-")[:2]
        name = name.replace("_", "-")
        before = set(os.listdir(WHEELS))
        subprocess.call(
            [sys.executable, "-m", "pip", "download", "--quiet", "--no-deps",
             "--only-binary=:all:", "--implementation", "py", "--abi", "none",
             "--platform", "any", "--dest", WHEELS, f"{name}=={version}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        added += sorted(set(os.listdir(WHEELS)) - before)
    for w in added:
        print(f"  + portable fallback: {w}")
    return added


def verify(req: str) -> bool:
    """Resolve the requirements against the wheelhouse alone, installing
    nothing. This is the check that matters: it answers "will the offline PC be
    able to do this" without needing the offline PC."""
    ok = pip("install", "--dry-run", "--quiet", "--no-index",
             "--find-links", WHEELS, "-r", os.path.join(REPO, req)) == 0
    print(f"  {req}: {'OK' if ok else 'CANNOT be satisfied from vendor/wheels'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", dest="sets", action="append", choices=sorted(SETS),
                    help="which requirements to fetch (repeatable; default: client + plot)")
    ap.add_argument("--platform", help="pip platform tag, e.g. win_amd64, "
                                       "manylinux2014_x86_64, linux_armv7l")
    ap.add_argument("--python-version", help="target Python, e.g. 313 (needs --platform)")
    ap.add_argument("--verify", action="store_true",
                    help="only check that vendor/wheels can satisfy the requirements")
    args = ap.parse_args()

    chosen = tuple(args.sets or DEFAULT_SETS)
    os.makedirs(WHEELS, exist_ok=True)

    if args.verify:
        print(f"Checking {WHEELS} against:")
        return 0 if all(verify(SETS[s]) for s in chosen) else 1

    if args.python_version and not args.platform:
        ap.error("--python-version only means something with --platform")

    target = (f"{args.platform} / Python {args.python_version}" if args.platform
              else f"this machine ({sys.platform}, Python "
                   f"{sys.version_info.major}.{sys.version_info.minor})")
    print(f"Fetching wheels for {target} into {WHEELS}\n")

    failed = []
    for name in chosen:
        req = SETS[name]
        print(f"[{name}] {req}")
        if not fetch(req, args.platform, args.python_version):
            failed.append(name)
        print()

    # Once, at the end: this walks the whole wheelhouse, so per-set runs would
    # only repeat the same work.
    print("[portable fallbacks]")
    fetch_pure()
    print()

    total = 0
    count = 0
    for f in sorted(os.listdir(WHEELS)):
        if f.endswith(".whl"):
            total += os.path.getsize(os.path.join(WHEELS, f))
            count += 1
    print(f"vendor/wheels now holds {count} wheels, {total / 1024 ** 2:.1f} MB")

    if failed:
        # A set that will not resolve for a target is normal for the Pitaya:
        # numpy and pydantic-core have no armv7 wheels on PyPI. Say so plainly
        # instead of leaving a half-filled wheelhouse looking complete.
        print(f"\nCould not fully fetch: {', '.join(failed)}")
        print("Some of those packages have no wheel for that target and would have to "
              "be built from source. OFFLINE.md covers what to do about the Pitaya.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
