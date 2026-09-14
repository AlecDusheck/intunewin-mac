"""Read MSI metadata on macOS using `msiinfo` from the msitools Homebrew package."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .packager import MsiInfo


def msiinfo_available() -> bool:
    return shutil.which("msiinfo") is not None


def _run(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, errors="replace").stdout


def read_properties(msi: Path) -> dict[str, str]:
    out = _run(["msiinfo", "export", str(msi), "Property"])
    props: dict[str, str] = {}
    for line in out.splitlines()[3:]:  # skip header rows
        if "\t" in line:
            k, v = line.split("\t", 1)
            props[k.strip()] = v.strip()
    return props


def read_suminfo(msi: Path) -> dict[str, str]:
    out = _run(["msiinfo", "suminfo", str(msi)])
    info: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return info


def msi_arch(template: str) -> str:
    t = template.split(";")[0].strip().lower()
    if t in ("x64", "amd64"):
        return "x64"
    if t == "arm64":
        return "arm64"
    if t == "intel":
        return "x86"
    return t or "unknown"


def inspect_msi(msi: Path) -> Optional[dict]:
    """Return a dict with product/package codes, version, name, arch and an MsiInfo object.
    Returns None when msiinfo is not installed."""
    if not msiinfo_available():
        return None
    props = read_properties(msi)
    summ = read_suminfo(msi)
    allusers = props.get("ALLUSERS", "")
    ctx = {"1": "System", "2": "Any"}.get(allusers, "User")
    if props.get("MSIINSTALLPERUSER") == "1":
        ctx = "Any"
    info = MsiInfo(
        product_code=props.get("ProductCode", ""),
        product_version=props.get("ProductVersion", ""),
        package_code=next((v for k, v in summ.items() if k.startswith("Revision number")), ""),
        upgrade_code=props.get("UpgradeCode", ""),
        execution_context=ctx,
        publisher=props.get("Manufacturer", ""),
        is_machine_install=ctx != "User",
        is_user_install=ctx == "User",
    )
    return {
        "product_code": info.product_code,
        "product_version": info.product_version,
        "product_name": props.get("ProductName", ""),
        "manufacturer": info.publisher,
        "upgrade_code": info.upgrade_code,
        "package_code": info.package_code,
        "arch": msi_arch(summ.get("Template", "")),
        "execution_context": ctx,
        "msi_info": info,
        "properties": props,
    }
