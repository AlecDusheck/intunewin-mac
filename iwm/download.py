"""HTTP download helpers with progress, hash verification and cache reuse."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

UA = "intunewin-on-mac/0.1 (+https://github.com/) python-requests"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def filename_from_url(url: str, resp: Optional[requests.Response] = None) -> str:
    if resp is not None:
        cd = resp.headers.get("content-disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
        if m:
            return m.group(1).strip()
        url = resp.url or url
    name = url.split("?")[0].rstrip("/").split("/")[-1]
    return name or "download.bin"


def download(url: str, dest: Path, sha256: Optional[str] = None, refresh: bool = False, quiet: bool = False) -> Path:
    """Download url to dest (a file path). Reuses an existing file unless refresh=True.
    A sidecar `<dest>.meta.json` records the source URL, ETag, size and hash."""
    dest = Path(dest)
    meta_path = dest.with_name(dest.name + ".meta.json")
    if dest.exists() and not refresh:
        if sha256 and sha256_of(dest).lower() != sha256.lower():
            raise ValueError(f"{dest} exists but sha256 does not match; re-run with --refresh")
        if not quiet:
            print(f"  using cached {dest.name} ({dest.stat().st_size:,} bytes)", file=sys.stderr)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with requests.get(url, stream=True, timeout=60, headers={"User-Agent": UA}, allow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        h = hashlib.sha256()
        t0 = time.time()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if not quiet and total:
                    pct = done * 100 // total
                    print(f"\r  downloading {dest.name}: {pct}% ({done // 1048576} MB)", end="", file=sys.stderr)
        if not quiet:
            print(f"\r  downloaded {dest.name}: {done:,} bytes in {time.time() - t0:.0f}s", file=sys.stderr)
        digest = h.hexdigest()
        if sha256 and digest.lower() != sha256.lower():
            tmp.unlink(missing_ok=True)
            raise ValueError(f"sha256 mismatch for {url}: expected {sha256}, got {digest}")
        tmp.replace(dest)
        meta_path.write_text(json.dumps({
            "url": url, "final_url": r.url, "etag": r.headers.get("etag"),
            "last_modified": r.headers.get("last-modified"), "size": done, "sha256": digest,
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }, indent=2))
    return dest


def github_latest_release(repo: str) -> dict:
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=30,
                     headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    r.raise_for_status()
    return r.json()


def resolve_github_asset(repo: str, asset_regex: str) -> tuple[str, str, str]:
    """Return (tag, asset_name, download_url) for the first asset matching asset_regex."""
    rel = github_latest_release(repo)
    rx = re.compile(asset_regex)
    for a in rel.get("assets", []):
        if rx.search(a["name"]):
            return rel["tag_name"], a["name"], a["browser_download_url"]
    raise LookupError(f"no asset in {repo} {rel.get('tag_name')} matches /{asset_regex}/")


def head_info(url: str) -> dict:
    r = requests.head(url, allow_redirects=True, timeout=30, headers={"User-Agent": UA})
    return {"status": r.status_code, "size": int(r.headers.get("content-length") or 0),
            "etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified"),
            "type": r.headers.get("content-type"), "final_url": r.url,
            "filename": filename_from_url(url, r)}
