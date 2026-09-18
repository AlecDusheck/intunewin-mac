"""
Recipes describe how to obtain an installer and how Intune should install/detect it.

A recipe is a YAML file in recipes/<id>.yaml:

    id: google-chrome-enterprise
    name: Google Chrome Enterprise
    publisher: Google LLC
    description: ...
    sources:                        # one entry per architecture
      x64:   {url: https://..., filename: optional, sha256: optional}
      arm64: {url: https://...}
    latest:                         # optional: resolve URL dynamically
      github_release: owner/repo
      asset: {x64: "regex", arm64: "regex"}
    version: auto                   # auto = from MSI ProductVersion / github tag, or a literal
    setup_file: "{filename}"        # file inside the package that Intune runs (default {filename})
    install: 'msiexec /i "{filename}" /qn /norestart'
    uninstall: 'msiexec /x {product_code} /qn /norestart'
    install_context: system         # system | user
    restart_behavior: basedOnReturnCode   # suppress | force | allow
    detection: auto                 # auto (MSI product code) or a list of rules
    requirements: {min_os: W10_1607, arch: [x64, arm64]}
    extra_files: [scripts/foo.ps1]  # copied into the package next to the installer
    return_codes: default           # or a list of {code: 0, type: success}

Placeholders usable in strings: {filename} {product_code} {product_version} {version}
{product_name} {upgrade_code} {arch} {id} {name}.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from . import download as dl
from . import msi as msimod
from . import packager
from .paths import DIST, RECIPES, WORK

ARCHES = ("x64", "arm64", "x86")

DEFAULT_RETURN_CODES = [
    {"returnCode": 0, "type": "success"},
    {"returnCode": 1707, "type": "success"},
    {"returnCode": 3010, "type": "softReboot"},
    {"returnCode": 1641, "type": "hardReboot"},
    {"returnCode": 1618, "type": "retry"},
]


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(template: str, ctx: dict) -> str:
    return str(template).format_map(_SafeDict(ctx))


@dataclass
class Recipe:
    id: str
    data: dict
    path: Optional[Path] = None

    @property
    def name(self) -> str:
        return self.data.get("name", self.id)

    @property
    def arches(self) -> list[str]:
        src = self.data.get("sources") or {}
        lat = (self.data.get("latest") or {}).get("asset") or {}
        return [a for a in ARCHES if a in src or a in lat]

    def source(self, arch: str) -> dict:
        src = dict((self.data.get("sources") or {}).get(arch) or {})
        latest = self.data.get("latest") or {}
        if latest.get("github_release") and arch in (latest.get("asset") or {}):
            tag, asset_name, url = dl.resolve_github_asset(latest["github_release"], latest["asset"][arch])
            src.setdefault("url", url)
            src.setdefault("filename", asset_name)
            src["version"] = tag.lstrip("v")
        if "url" not in src:
            raise ValueError(f"recipe {self.id} has no source for arch {arch} (has: {', '.join(self.arches) or 'none'})")
        src.setdefault("filename", dl.filename_from_url(src["url"]))
        return src


def recipe_path(name: str) -> Path:
    p = Path(name)
    if p.suffix in (".yaml", ".yml") and p.exists():
        return p
    cand = RECIPES / f"{name}.yaml"
    if cand.exists():
        return cand
    raise FileNotFoundError(f"recipe not found: {name} (looked for {cand})")


def load_recipe(name: str) -> Recipe:
    p = recipe_path(name)
    data = yaml.safe_load(p.read_text()) or {}
    rid = data.get("id") or p.stem
    return Recipe(rid, data, p)


def list_recipes() -> list[Recipe]:
    out = []
    for p in sorted(RECIPES.glob("*.yaml")):
        try:
            out.append(load_recipe(str(p)))
        except Exception as e:  # pragma: no cover
            print(f"warning: {p.name}: {e}", file=sys.stderr)
    return out


# --------------------------------------------------------------------------- fetch

def source_dir(recipe: Recipe, arch: str) -> Path:
    return WORK / recipe.id / arch / "source"


def fetch(recipe: Recipe, arch: str, refresh: bool = False) -> dict:
    """Download the installer (+extra files) into work/<id>/<arch>/source and return a context
    dict with everything needed to render commands and build metadata."""
    src = recipe.source(arch)
    sdir = source_dir(recipe, arch)
    sdir.mkdir(parents=True, exist_ok=True)
    installer = dl.download(src["url"], sdir / src["filename"], sha256=src.get("sha256"), refresh=refresh)

    # copy extra files (paths relative to the recipe file or project root)
    for extra in recipe.data.get("extra_files") or []:
        ep = Path(extra)
        if not ep.is_absolute():
            for base in (recipe.path.parent if recipe.path else Path("."), RECIPES.parent):
                if (base / ep).exists():
                    ep = base / ep
                    break
        if not ep.exists():
            raise FileNotFoundError(f"extra file not found: {extra}")
        if ep.is_dir():
            shutil.copytree(ep, sdir / ep.name, dirs_exist_ok=True)
        else:
            shutil.copy2(ep, sdir / ep.name)

    ctx: dict[str, Any] = {
        "id": recipe.id, "name": recipe.name, "arch": arch, "filename": src["filename"],
        "url": src["url"], "installer": str(installer), "source_dir": str(sdir),
        "publisher": recipe.data.get("publisher", ""),
    }
    info = None
    if installer.suffix.lower() == ".msi":
        info = msimod.inspect_msi(installer)
        if info is None:
            print("warning: msiinfo not found (brew install msitools); MSI metadata will be missing", file=sys.stderr)
        else:
            ctx.update({
                "product_code": info["product_code"], "product_version": info["product_version"],
                "product_name": info["product_name"], "upgrade_code": info["upgrade_code"],
                "msi_arch": info["arch"],
            })
            if info["arch"] not in ("unknown", arch) and not (info["arch"] == "x86" and arch == "x64"):
                print(f"warning: MSI reports architecture {info['arch']} but recipe arch is {arch}", file=sys.stderr)
    ctx["msi"] = info
    ver = recipe.data.get("version", "auto")
    if ver == "auto":
        ver = (info or {}).get("product_version") or src.get("version") or "0.0"
    ctx["version"] = str(ver)
    ctx["setup_file"] = render(recipe.data.get("setup_file", "{filename}"), ctx)
    ctx["install"] = render(recipe.data.get("install", 'msiexec /i "{filename}" /qn /norestart'), ctx)
    ctx["uninstall"] = render(recipe.data.get("uninstall", "msiexec /x {product_code} /qn /norestart"), ctx)
    (sdir.parent / "context.json").write_text(json.dumps({k: v for k, v in ctx.items() if k != "msi"}, indent=2))
    return ctx


# --------------------------------------------------------------------------- detection rules

def detection_rules(recipe: Recipe, ctx: dict) -> list[dict]:
    """Normalise recipe detection rules to Microsoft Graph win32LobApp rule objects."""
    det = recipe.data.get("detection", "auto")
    rules: list[dict] = []
    if det == "auto" or det is None:
        if ctx.get("product_code"):
            det = [{"type": "msi"}]
        else:
            raise ValueError(f"recipe {recipe.id}: detection is 'auto' but the installer is not an MSI; "
                             "add explicit file/registry/script detection rules")
    for r in det:
        t = r.get("type")
        if t == "msi":
            rules.append({
                "@odata.type": "#microsoft.graph.win32LobAppProductCodeRule",
                "ruleType": "detection",
                "productCode": render(r.get("product_code", "{product_code}"), ctx),
                "productVersionOperator": r.get("operator", "notConfigured"),
                "productVersion": render(r["version"], ctx) if r.get("version") else None,
            })
        elif t == "file":
            rules.append({
                "@odata.type": "#microsoft.graph.win32LobAppFileSystemRule",
                "ruleType": "detection",
                "check32BitOn64System": bool(r.get("check32BitOn64System", False)),
                "path": render(r["path"], ctx),
                "fileOrFolderName": render(r["file"], ctx),
                "operationType": r.get("detection", "exists"),   # exists|notExists|version|sizeInMB|modifiedDate|createdDate
                "operator": r.get("operator", "notConfigured"),
                "comparisonValue": render(r["value"], ctx) if r.get("value") is not None else None,
            })
        elif t == "registry":
            rules.append({
                "@odata.type": "#microsoft.graph.win32LobAppRegistryRule",
                "ruleType": "detection",
                "check32BitOn64System": bool(r.get("check32BitOn64System", False)),
                "keyPath": render(r["key"], ctx),
                "valueName": render(r.get("value_name", ""), ctx) or None,
                "operationType": r.get("detection", "exists"),   # exists|doesNotExist|string|integer|version
                "operator": r.get("operator", "notConfigured"),
                "comparisonValue": render(r["value"], ctx) if r.get("value") is not None else None,
            })
        elif t == "script":
            script = r.get("script")
            if r.get("file"):
                # relative to the recipe file, then the workspace (same lookup as extra_files)
                sp = Path(r["file"])
                if not sp.is_absolute():
                    for base in (recipe.path.parent if recipe.path else Path("."), RECIPES.parent):
                        if (base / sp).exists():
                            sp = base / sp
                            break
                script = sp.read_text()
            rules.append({
                "@odata.type": "#microsoft.graph.win32LobAppPowerShellScriptRule",
                "ruleType": "detection",
                "scriptContent": script,   # publish step base64-encodes this
                "enforceSignatureCheck": bool(r.get("enforce_signature", False)),
                "runAs32Bit": bool(r.get("run_as_32bit", False)),
            })
        else:
            raise ValueError(f"unknown detection rule type: {t}")
    return rules


# --------------------------------------------------------------------------- build

def build(recipe: Recipe, arch: str, refresh: bool = False, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or fetch(recipe, arch, refresh=refresh)
    sdir = Path(ctx["source_dir"])
    out_dir = DIST / recipe.id
    base = f"{recipe.id}-{ctx['version']}-{arch}"
    out = out_dir / f"{base}.intunewin"
    msi_info = (ctx.get("msi") or {}).get("msi_info")
    print(f"building {out.name} from {sdir} (setup file: {ctx['setup_file']})", file=sys.stderr)
    res = packager.create_intunewin(sdir, ctx["setup_file"], out, name=ctx["setup_file"], msi=msi_info)
    rules = detection_rules(recipe, ctx)
    manifest = graph_body(recipe, ctx, res, rules)
    (out_dir / f"{base}.intune.json").write_text(json.dumps(manifest, indent=2))
    (out_dir / f"{base}.rules.json").write_text(json.dumps(rules, indent=2))
    # Encryption info is needed by `publish` (kept next to the package; treat dist/ as private).
    (out_dir / f"{base}.encryption.json").write_text(json.dumps({
        **res.metadata.encryption.to_graph(),
        "unencryptedContentSize": res.metadata.unencrypted_content_size,
        "encryptedSize": out.stat().st_size,
    }, indent=2))
    (out_dir / f"{base}.md").write_text(portal_notes(recipe, ctx, res, rules))
    return {"intunewin": str(out), "manifest": str(out_dir / f"{base}.intune.json"),
            "rules": rules, "ctx": {k: v for k, v in ctx.items() if k != "msi"},
            "files": res.files, "size": out.stat().st_size}


def graph_body(recipe: Recipe, ctx: dict, res: packager.BuildResult, rules: list[dict]) -> dict:
    d = recipe.data
    req = d.get("requirements") or {}
    rc = d.get("return_codes", "default")
    if rc == "default" or rc is None:
        rc = DEFAULT_RETURN_CODES
    else:
        rc = [{"returnCode": int(x["code"]), "type": x.get("type", "success")} for x in rc]
    arch = ctx["arch"]
    body = {
        "@odata.type": "#microsoft.graph.win32LobApp",
        "displayName": render(d.get("display_name", "{name}"), ctx),
        "description": render(d.get("description", "{name}"), ctx),
        "publisher": ctx.get("publisher") or ctx.get("manufacturer") or "",
        "developer": d.get("developer", ""),
        "owner": d.get("owner", ""),
        "notes": d.get("notes", f"Built by intunewin-on-mac. Version {ctx['version']} ({arch})."),
        "informationUrl": d.get("homepage"),
        "privacyInformationUrl": d.get("privacy_url"),
        "isFeatured": False,
        "fileName": Path(res.output).name,
        "setupFilePath": ctx["setup_file"],
        "installCommandLine": ctx["install"],
        "uninstallCommandLine": ctx["uninstall"],
        "installExperience": {
            "runAsAccount": d.get("install_context", "system"),
            "deviceRestartBehavior": d.get("restart_behavior", "basedOnReturnCode"),
        },
        "applicableArchitectures": ",".join(req.get("arch") or [arch]),
        "minimumSupportedWindowsRelease": req.get("min_os", "W10_1607"),
        "minimumFreeDiskSpaceInMB": req.get("min_disk_mb"),
        "minimumMemoryInMB": req.get("min_ram_mb"),
        "rules": rules,
        "returnCodes": rc,
    }
    if res.metadata.msi:
        m = res.metadata.msi
        body["msiInformation"] = {
            "productCode": m.product_code, "productVersion": m.product_version,
            "upgradeCode": m.upgrade_code, "requiresReboot": m.requires_reboot,
            "packageType": {"System": "perMachine", "User": "perUser"}.get(m.execution_context, "dualPurpose"),
            "productName": ctx.get("product_name", ""), "publisher": m.publisher,
        }
    return body


def portal_notes(recipe: Recipe, ctx: dict, res: packager.BuildResult, rules: list[dict]) -> str:
    """Human-readable summary of what to enter in the Intune portal."""
    lines = [
        f"# {recipe.name} {ctx['version']} ({ctx['arch']})", "",
        f"Package: `{Path(res.output).name}` ({Path(res.output).stat().st_size:,} bytes, {res.file_count} files)", "",
        "## Program", "",
        f"- Install command: `{ctx['install']}`",
        f"- Uninstall command: `{ctx['uninstall']}`",
        f"- Install behavior: {recipe.data.get('install_context', 'system')}",
        f"- Device restart behavior: {recipe.data.get('restart_behavior', 'basedOnReturnCode')}", "",
        "## Detection rules", "",
    ]
    for r in rules:
        t = r["@odata.type"].split(".")[-1]
        if t == "win32LobAppProductCodeRule":
            lines.append(f"- MSI product code `{r['productCode']}`")
        elif t == "win32LobAppFileSystemRule":
            lines.append(f"- File `{r['path']}\\{r['fileOrFolderName']}` {r['operationType']} {r.get('operator', '')} {r.get('comparisonValue') or ''}".rstrip())
        elif t == "win32LobAppRegistryRule":
            lines.append(f"- Registry `{r['keyPath']}` [{r.get('valueName') or '(default)'}] {r['operationType']} {r.get('operator', '')} {r.get('comparisonValue') or ''}".rstrip())
        else:
            lines.append("- PowerShell detection script (see rules.json)")
    lines += ["", "## Requirements", "",
              f"- Architectures: {', '.join((recipe.data.get('requirements') or {}).get('arch') or [ctx['arch']])}",
              f"- Minimum OS: {(recipe.data.get('requirements') or {}).get('min_os', 'W10_1607')}", ""]
    if ctx.get("product_code"):
        lines += ["## MSI", "", f"- Product code: `{ctx['product_code']}`", f"- Product version: `{ctx['product_version']}`",
                  f"- Upgrade code: `{ctx['upgrade_code']}`", ""]
    return "\n".join(lines)
