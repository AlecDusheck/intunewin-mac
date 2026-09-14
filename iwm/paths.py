import os
from pathlib import Path

ROOT = Path(os.environ.get("IWM_HOME", Path(__file__).resolve().parent.parent))
CACHE = ROOT / "cache"
WORK = ROOT / "work"
DIST = ROOT / "dist"
VMDIR = ROOT / "vm"
RECIPES = ROOT / "recipes"
REPORTS = ROOT / "reports"
GUEST = Path(__file__).resolve().parent / "vm" / "guest"

for _d in (CACHE, WORK, DIST, VMDIR, REPORTS):
    _d.mkdir(parents=True, exist_ok=True)
