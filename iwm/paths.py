import os
from pathlib import Path

ROOT = Path(os.environ.get("IWM_HOME", Path(__file__).resolve().parent.parent))
# Recipes and what they produce (packages, test reports). Point IWM_WORKSPACE at another
# repo to keep an organization's apps there while the VM, cache and tooling stay here.
WORKSPACE = Path(os.environ.get("IWM_WORKSPACE", ROOT)).expanduser().resolve()
CACHE = ROOT / "cache"
WORK = ROOT / "work"
DIST = WORKSPACE / "dist"
VMDIR = ROOT / "vm"
RECIPES = WORKSPACE / "recipes"
REPORTS = WORKSPACE / "reports"
GUEST = Path(__file__).resolve().parent / "vm" / "guest"

for _d in (CACHE, WORK, DIST, VMDIR, REPORTS):
    _d.mkdir(parents=True, exist_ok=True)
