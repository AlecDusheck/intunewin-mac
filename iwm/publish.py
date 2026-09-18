"""
Publish a built package to Intune through Microsoft Graph (win32LobApp).

Flow (per Microsoft's documented content upload sequence):
  1. POST   /deviceAppManagement/mobileApps                      (win32LobApp body from build)
  2. POST   .../contentVersions                                    -> version id
  3. POST   .../contentVersions/{v}/files                           (name, size, sizeEncrypted, isDependency=false)
  4. GET    .../files/{f}  until uploadState == azureStorageUriRequestSuccess
  5. PUT    blocks to azureStorageUri (?comp=block&blockid=), then PUT ?comp=blocklist
  6. POST   .../files/{f}/commit  with fileEncryptionInfo (keys from Detection.xml)
  7. GET    .../files/{f}  until uploadState == commitFileSuccess
  8. PATCH  app  { committedContentVersion: v }

Auth: $IWM_GRAPH_TOKEN if set (any token with DeviceManagementApps.ReadWrite.All), otherwise MSAL
device-code flow with the Microsoft Graph PowerShell public client id (or your own app via
--client-id/--tenant).

NOTE: this module has not been exercised against a live tenant from this project yet; the request
shapes follow the public docs and the widely used IntuneWin32App PowerShell module.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import requests

from . import packager

GRAPH = "https://graph.microsoft.com/beta"
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"   # Microsoft Graph Command Line Tools (public)
SCOPES = ["DeviceManagementApps.ReadWrite.All"]
BLOCK = 4 * 1024 * 1024


def get_token(tenant: str = "common", client_id: str = DEFAULT_CLIENT_ID) -> str:
    try:
        import msal
    except ImportError:
        raise SystemExit("msal is required for publishing: pip install msal")
    cache_file = Path.home() / ".iwm-token-cache.json"
    cache = msal.SerializableTokenCache()
    if cache_file.exists():
        cache.deserialize(cache_file.read_text())
    app = msal.PublicClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant}", token_cache=cache)
    result = None
    for acct in app.get_accounts():
        result = app.acquire_token_silent(SCOPES, account=acct)
        if result:
            break
    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise SystemExit(f"device flow failed: {flow}")
        print(flow["message"], file=sys.stderr)
        result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise SystemExit(f"auth failed: {result.get('error_description')}")
    cache_file.write_text(cache.serialize())
    return result["access_token"]


class Graph:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    def req(self, method: str, path: str, **kw) -> dict:
        url = path if path.startswith("http") else GRAPH + path
        r = self.s.request(method, url, timeout=120, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:1500]}")
        return r.json() if r.text else {}


def _prepare_body(manifest: dict) -> dict:
    body = json.loads(json.dumps(manifest))
    for rule in body.get("rules", []):
        if rule.get("@odata.type", "").endswith("PowerShellScriptRule") and rule.get("scriptContent"):
            sc = rule["scriptContent"]
            try:
                base64.b64decode(sc, validate=True)
            except Exception:
                rule["scriptContent"] = base64.b64encode(sc.encode("utf-8")).decode()
    # drop nulls Graph rejects
    for k in [k for k, v in body.items() if v is None]:
        del body[k]
    return body


def publish(intunewin: Path, manifest_path: Path, tenant: str = "common", client_id: str = DEFAULT_CLIENT_ID,
            token: str | None = None, app_id: str | None = None) -> dict:
    """Create the app, or with app_id update an existing one in place: PATCH its metadata (version,
    commands, detection rules) and upload the package as a new content version. Assignments,
    dependencies and supersedence are kept."""
    intunewin = Path(intunewin)
    manifest = json.loads(Path(manifest_path).read_text())
    meta = packager.read_metadata(intunewin)
    enc = meta.encryption.to_graph()
    with zipfile.ZipFile(intunewin) as zf:
        payload_info = zf.getinfo(packager.PAYLOAD_PATH)
        size_encrypted = payload_info.file_size

    # An existing Graph token (e.g. from Connect-MgGraph) avoids a second, device-code sign-in.
    g = Graph(token or os.environ.get("IWM_GRAPH_TOKEN") or get_token(tenant, client_id))
    body = _prepare_body(manifest)
    if app_id:
        cur = g.req("GET", f"/deviceAppManagement/mobileApps/{app_id}")
        if cur.get("@odata.type") != "#microsoft.graph.win32LobApp":
            raise RuntimeError(f"app {app_id} is {cur.get('@odata.type')}, not a Win32 app")
        # Keep the name/description/icon people see in Company Portal; update the package-specific fields.
        for k in ("displayName", "description", "largeIcon", "publisher", "developer", "owner", "notes",
                  "informationUrl", "privacyInformationUrl", "isFeatured"):
            body.pop(k, None)
        print(f"updating app '{cur.get('displayName')}' ({app_id}) to {body.get('displayVersion')}", file=sys.stderr)
        g.req("PATCH", f"/deviceAppManagement/mobileApps/{app_id}", data=json.dumps(body))
    else:
        print(f"creating app '{body['displayName']}'", file=sys.stderr)
        app = g.req("POST", "/deviceAppManagement/mobileApps", data=json.dumps(body))
        app_id = app["id"]
    ver = g.req("POST", f"/deviceAppManagement/mobileApps/{app_id}/microsoft.graph.win32LobApp/contentVersions", data="{}")
    vid = ver["id"]
    f = g.req("POST", f"/deviceAppManagement/mobileApps/{app_id}/microsoft.graph.win32LobApp/contentVersions/{vid}/files",
              data=json.dumps({"@odata.type": "#microsoft.graph.mobileAppContentFile", "name": intunewin.name,
                               "size": meta.unencrypted_content_size, "sizeEncrypted": size_encrypted,
                               "manifest": None, "isDependency": False}))
    fid = f["id"]
    fpath = f"/deviceAppManagement/mobileApps/{app_id}/microsoft.graph.win32LobApp/contentVersions/{vid}/files/{fid}"
    for _ in range(60):
        f = g.req("GET", fpath)
        if f.get("uploadState") == "azureStorageUriRequestSuccess":
            break
        if "fail" in (f.get("uploadState") or "").lower():
            raise RuntimeError(f"storage URI request failed: {f}")
        time.sleep(5)
    else:
        raise TimeoutError("no azure storage URI")
    sas = f["azureStorageUri"]

    print(f"uploading {size_encrypted:,} bytes", file=sys.stderr)
    block_ids = []
    with zipfile.ZipFile(intunewin) as zf, zf.open(packager.PAYLOAD_PATH) as src:
        i = 0
        while True:
            chunk = src.read(BLOCK)
            if not chunk:
                break
            bid = base64.b64encode(f"block-{i:08d}".encode()).decode()
            r = requests.put(f"{sas}&comp=block&blockid={bid}", data=chunk, timeout=300,
                             headers={"x-ms-blob-type": "BlockBlob"})
            if r.status_code >= 400:
                raise RuntimeError(f"block upload failed: {r.status_code} {r.text[:500]}")
            block_ids.append(bid)
            i += 1
            print(f"\r  {i * BLOCK // 1048576} MB", end="", file=sys.stderr)
    print(file=sys.stderr)
    xml = '<?xml version="1.0" encoding="utf-8"?><BlockList>' + "".join(f"<Latest>{b}</Latest>" for b in block_ids) + "</BlockList>"
    r = requests.put(f"{sas}&comp=blocklist", data=xml, timeout=120, headers={"Content-Type": "application/xml"})
    if r.status_code >= 400:
        raise RuntimeError(f"blocklist failed: {r.status_code} {r.text[:500]}")

    g.req("POST", fpath + "/commit", data=json.dumps({"fileEncryptionInfo": enc}))
    for _ in range(120):
        f = g.req("GET", fpath)
        if f.get("uploadState") == "commitFileSuccess":
            break
        if "fail" in (f.get("uploadState") or "").lower():
            raise RuntimeError(f"commit failed: {f}")
        time.sleep(5)
    else:
        raise TimeoutError("commit did not complete")
    g.req("PATCH", f"/deviceAppManagement/mobileApps/{app_id}",
          data=json.dumps({"@odata.type": "#microsoft.graph.win32LobApp", "committedContentVersion": vid}))
    print(f"published app id {app_id}", file=sys.stderr)
    return {"app_id": app_id, "content_version": vid, "file_id": fid,
            "portal": f"https://intune.microsoft.com/#view/Microsoft_Intune_Apps/SettingsMenu/~/0/appId/{app_id}"}
