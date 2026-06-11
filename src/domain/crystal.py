import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone


def _default_dir() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "..", "..", "data", "crystals")


def sanitize_name(name: str) -> str:
    """Keep alphanumerics, hyphens, underscores and spaces; collapse spaces to underscores."""
    cleaned = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    return cleaned.replace(" ", "_")


@dataclass
class CrystalProfile:
    name: str
    # Lock start frequencies
    freq_mass: float = 5983000.0
    freq_temp: float = 6570000.0
    # Calibration coefficients
    fM_0: float = 0.0
    fM_1: float = 0.0
    fM_2: float = 0.0
    fM_3: float = 0.0
    fT_0: float = 0.0
    fT_1: float = 0.0
    fT_2: float = 0.0
    fT_3: float = 0.0
    # Sensor parameters
    mass_sensitivity: float = -13.3e-8  # kg/(m²·Hz) — negative: added mass lowers the frequency
    sens_area:        float = 5.25e-5  # m²
    # Usage stats
    hours_active:    float = 0.0
    total_deposited: float = 0.0  # nm, cumulative across sessions
    # Metadata
    created:       str = ""
    last_modified: str = ""


class CrystalManager:
    def __init__(self, crystals_dir: str | None = None):
        self.dir = crystals_dir or _default_dir()
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, f"{name}.json")

    def list_names(self) -> list[str]:
        try:
            return sorted(f[:-5] for f in os.listdir(self.dir) if f.endswith(".json"))
        except Exception:
            return []

    def load(self, name: str) -> CrystalProfile | None:
        path = self._path(name)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            data = json.load(f)
        data["name"] = name  # canonical: name always comes from the filename
        valid = {k: data[k] for k in CrystalProfile.__dataclass_fields__ if k in data}
        return CrystalProfile(**valid)

    def save(self, profile: CrystalProfile):
        now = datetime.now(timezone.utc).isoformat()
        if not profile.created:
            profile.created = now
        profile.last_modified = now
        with open(self._path(profile.name), "w") as f:
            json.dump(asdict(profile), f, indent=2)

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def exists(self, name: str) -> bool:
        return os.path.exists(self._path(name))
