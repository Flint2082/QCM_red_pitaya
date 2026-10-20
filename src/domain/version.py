"""What the instrument is running: the control software and the FPGA bitstream.

Reported by GET /version, shown in the web UI and recorded in every run's
SETTINGS row, so a run says which code and which bitstream produced it.

The software version is the git commit of this checkout, named by
`git describe`: "v1.2.0" on a tagged commit, "v1.2.0-3-g1a2b3c4" three commits
past that tag, a bare "1a2b3c4" before there are any tags, with "-dirty"
appended when the firmware had uncommitted changes. Releases are tagged on the
development PC and arrive on the Pitaya with the next `git pull`.

The commit is taken once, at startup, because that is the code this process
loaded: a later `git pull` changes the files but not what is running until the
service restarts. Its *name* is looked up on every request instead, so a tag
added after the commit was tested names it without a restart. restart_needed
reports a checkout that has moved on from what is running.

The bitstream is identified by the files main.py loaded at startup: the .fpg
the register map came from (its name and build date) and a SHA-256 of
top.bit.bin. A bitstream that also carries a `build_time` register (Unix
seconds, UTC, set at compile time) is read back from the FPGA itself, which is
the one source that cannot disagree with what is actually loaded.
"""

import hashlib
import os
import subprocess
from datetime import datetime, timezone

REPO_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

# The paths that make up the running firmware. -dirty and restart_needed look at
# these only: in normal operation the server writes into data/ (settings, run
# logs, and crystal profiles, two of which are tracked as examples), so a check
# over the whole tree would call every box that has finished a run dirty.
FIRMWARE_PATHS = ("src", "bitstream", "model_composer/qcm_rp/outputs", "deploy")

# FPGA register holding the bitstream's build time, if the bitstream has one.
BUILD_TIME_REGISTER = "build_time"


def _git(*args) -> str | None:
    """Output of a git command run in this checkout, or None when git cannot
    answer: not installed, a copy without .git (the scp / USB-stick install),
    or a clone damaged by a power cut. A version is never worth failing over.

    --no-optional-locks keeps `git status` from taking index.lock, so a request
    arriving mid-`git pull` cannot make the pull fail on a lock it holds."""
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", REPO_DIR, *args],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _local_changes() -> str | None:
    """Uncommitted changes to tracked firmware files, as `git status` lists
    them: "" when clean, None when git cannot tell. Untracked files do not
    count, as with `git describe --dirty`."""
    return _git("status", "--porcelain", "--untracked-files=no", "--", *FIRMWARE_PATHS)


class SoftwareVersion:
    def __init__(self):
        self.commit = _git("rev-parse", "HEAD")
        self._changes = _local_changes()
        self.dirty = bool(self._changes) if self._changes is not None else None
        self.commit_date = _git("log", "-1", "--format=%cI", self.commit) if self.commit else None

    def name(self) -> str:
        if not self.commit:
            return "unknown"
        name = _git("describe", "--tags", "--always", "--abbrev=7", self.commit) or self.commit[:7]
        return name + "-dirty" if self.dirty else name

    def restart_needed(self) -> bool | None:
        """Whether the firmware on disk is no longer what is running: HEAD has
        moved (a pull) or the set of locally modified files has changed. Further
        edits to a file that was already modified at startup go unnoticed."""
        if not self.commit:
            return None
        head, changes = _git("rev-parse", "HEAD"), _local_changes()
        if head is None or changes is None:
            return None
        return head != self.commit or changes != self._changes

    def as_dict(self) -> dict:
        return {
            "version":         self.name(),
            "commit":          self.commit,
            "commit_date":     self.commit_date,
            "dirty":           self.dirty,
            "restart_needed":  self.restart_needed(),
        }


def _sha256(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def describe_bitstream(fpga) -> dict:
    """The bitstream main.py loaded at startup. Call it right after the load, so
    the files hashed and parsed here are the ones that went into the FPGA."""
    fpg = getattr(fpga, "register_map_path", None)
    fabric_build_time = None
    if fpga.has_register(BUILD_TIME_REGISTER):
        raw = fpga.read_register(BUILD_TIME_REGISTER)
        fabric_build_time = datetime.fromtimestamp(raw, timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "fpg":               os.path.basename(fpg) if fpg else None,
        # From the .fpg, in the build machine's local time.
        "build_date":        fpga.get_build_date(fpg) if fpg else None,
        "bitstream_sha256":  _sha256(fpga.BITSTREAM_PATH),
        # Read back from the FPGA; None for a bitstream without the register.
        "fabric_build_time": fabric_build_time,
    }


class VersionInfo:
    """Everything GET /version reports. Build it once the bitstream and register
    map are loaded."""

    def __init__(self, fpga):
        self.software = SoftwareVersion()
        self.fpga = describe_bitstream(fpga)

    def as_dict(self) -> dict:
        return {"software": self.software.as_dict(), "fpga": dict(self.fpga)}
