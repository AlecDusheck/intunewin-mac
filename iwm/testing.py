"""
Install-test an .intunewin package inside the Windows VM the way Intune would:

  1. restore the 'clean' snapshot
  2. run the detection rules  -> expect NOT detected
  3. decrypt the package on the host, push the payload, expand it in C:\\iwm\\pkg\\<id>
  4. run the install command line as SYSTEM (or the user) with the package folder as cwd
  5. run the detection rules  -> expect detected
  6. optionally run the uninstall command and re-check detection
  7. write reports/<id>/<timestamp>/ (report.md, report.json, screenshots, install output)
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from . import packager
from .paths import REPORTS
from .vm.qemu import VM, log


def _short(s: str, n: int = 4000) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n // 2] + "\n...\n" + s[-n // 2:]


def run_test(vm: VM, intunewin: Path, install_cmd: str, uninstall_cmd: Optional[str], rules: list[dict],
             label: str, account: str = "system", do_uninstall: bool = False, keep_snapshot: bool = False,
             restore_after: bool = True, timeout: int = 1800, expected_codes: Optional[set[int]] = None) -> dict:
    expected_codes = expected_codes or {0, 1707, 3010, 1641}
    ts = time.strftime("%Y%m%d-%H%M%S")
    rdir = REPORTS / label / ts
    rdir.mkdir(parents=True, exist_ok=True)
    report: dict = {"label": label, "package": str(intunewin), "started": ts, "steps": [], "account": account,
                    "install_cmd": install_cmd, "uninstall_cmd": uninstall_cmd, "rules": rules}
    (rdir / "rules.json").write_text(json.dumps(rules, indent=2))

    def step(name: str, **kw):
        kw["step"] = name
        kw["t"] = round(time.time() - t_start, 1)
        report["steps"].append(kw)
        status = kw.get("status", "")
        log(f"{name}: {status} {kw.get('detail', '')}".strip())

    def shot(name: str):
        try:
            vm.screenshot(rdir / f"{name}.png")
        except Exception:
            pass

    t_start = time.time()
    # 1. clean state
    mode = vm.restore("clean", start=True)
    vm.wait_ssh(timeout=600)
    step("restore-clean", status="ok", detail=f"({mode})")
    info = vm.agent("info")
    report["guest"] = info

    # 2. pre-detection
    pkg_id = uuid.uuid4().hex[:8]
    rules_remote = f"C:\\iwm\\jobs\\{pkg_id}.rules.json"
    with tempfile.TemporaryDirectory() as td:
        rp = Path(td) / "rules.json"
        rp.write_text(json.dumps(rules))
        vm.push(rp, rules_remote)
    pre = vm.agent("detect", Rules=rules_remote)
    report["detect_before"] = pre
    step("detect-before-install", status="not detected (as expected)" if not pre["detected"] else "DETECTED (unexpected on clean image)")

    # 3. push package
    meta = packager.read_metadata(intunewin)
    with tempfile.TemporaryDirectory() as td:
        inner = Path(td) / "payload.zip"
        packager.decrypt_to_zip(intunewin, inner)
        with zipfile.ZipFile(inner) as z:
            names = z.namelist()
        remote_zip = f"C:\\iwm\\pkg\\{pkg_id}.zip"
        t = time.time()
        vm.push(inner, remote_zip)
        pkg_dir = f"C:\\iwm\\pkg\\{pkg_id}"
        vm.ssh(f"Expand-Archive -LiteralPath '{remote_zip}' -DestinationPath '{pkg_dir}' -Force; Remove-Item '{remote_zip}' -Force")
        step("push-package", status="ok", detail=f"{len(names)} files, {inner.stat().st_size:,} bytes in {time.time() - t:.0f}s -> {pkg_dir}")
    if meta.setup_file not in names:
        step("setup-file-check", status="WARNING", detail=f"setup file {meta.setup_file!r} not in payload")

    # 4. install
    job = {"command": install_cmd, "workdir": pkg_dir, "timeout": timeout, "account": account}
    with tempfile.TemporaryDirectory() as td:
        jp = Path(td) / f"{pkg_id}-install.json"
        jp.write_text(json.dumps(job))
        vm.push(jp, f"C:\\iwm\\jobs\\{jp.name}")
    res = vm.agent("run", Job=f"C:\\iwm\\jobs\\{pkg_id}-install.json", _timeout=str(timeout + 120))
    report["install"] = res
    (rdir / "install-output.txt").write_text(res.get("output") or "")
    ok = res.get("rc") in expected_codes and not res.get("timed_out")
    step("install", status="ok" if ok else "FAILED", detail=f"rc={res.get('rc')} in {res.get('duration_s')}s" + (" (timed out)" if res.get("timed_out") else ""))
    shot("after-install")

    # 5. post-detection
    post = vm.agent("detect", Rules=rules_remote)
    report["detect_after"] = post
    step("detect-after-install", status="detected" if post["detected"] else "NOT DETECTED")
    try:
        report["apps_after"] = [a for a in vm.agent("apps") if isinstance(a, dict)]
    except Exception:
        report["apps_after"] = []

    if keep_snapshot:
        try:
            vm.snapshot(f"after-{label}")
            step("snapshot", status="ok", detail=f"after-{label}")
        except Exception as e:
            step("snapshot", status="failed", detail=str(e)[:200])

    # 6. uninstall
    if do_uninstall and uninstall_cmd:
        job = {"command": uninstall_cmd, "workdir": pkg_dir, "timeout": timeout, "account": account}
        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / f"{pkg_id}-uninstall.json"
            jp.write_text(json.dumps(job))
            vm.push(jp, f"C:\\iwm\\jobs\\{jp.name}")
        ures = vm.agent("run", Job=f"C:\\iwm\\jobs\\{pkg_id}-uninstall.json", _timeout=str(timeout + 120))
        report["uninstall"] = ures
        (rdir / "uninstall-output.txt").write_text(ures.get("output") or "")
        uok = ures.get("rc") in expected_codes and not ures.get("timed_out")
        step("uninstall", status="ok" if uok else "FAILED", detail=f"rc={ures.get('rc')} in {ures.get('duration_s')}s")
        post_u = vm.agent("detect", Rules=rules_remote)
        report["detect_after_uninstall"] = post_u
        step("detect-after-uninstall", status="not detected (good)" if not post_u["detected"] else "STILL DETECTED")
        shot("after-uninstall")

    # verdict
    verdict = ok and post["detected"] and not pre["detected"]
    if do_uninstall and uninstall_cmd:
        verdict = verdict and uok and not report["detect_after_uninstall"]["detected"]
    report["passed"] = verdict
    report["duration_s"] = round(time.time() - t_start, 1)

    if restore_after:
        try:
            vm.restore("clean", start=True)
            step("restore-clean", status="ok")
        except Exception as e:
            step("restore-clean", status="failed", detail=str(e)[:200])

    (rdir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    (rdir / "report.md").write_text(render_report(report))
    latest = REPORTS / label / "latest"
    latest.unlink(missing_ok=True)
    latest.symlink_to(rdir.name)
    report["report_dir"] = str(rdir)
    return report


def render_report(r: dict) -> str:
    g = r.get("guest", {})
    lines = [f"# Test report: {r['label']}", "",
             f"**Result: {'PASS' if r.get('passed') else 'FAIL'}** ({r.get('duration_s')}s)", "",
             f"- Package: `{Path(r['package']).name}`",
             f"- Guest: {g.get('os')} build {g.get('build')} {g.get('arch')} ({g.get('ram_mb')} MB)",
             f"- Install ({r.get('account')}): `{r['install_cmd']}`"]
    if r.get("uninstall_cmd"):
        lines.append(f"- Uninstall: `{r['uninstall_cmd']}`")
    lines += ["", "## Steps", "", "| t (s) | step | status | detail |", "|---|---|---|---|"]
    for s in r["steps"]:
        lines.append(f"| {s['t']} | {s['step']} | {s.get('status', '')} | {s.get('detail', '')} |")
    inst = r.get("install") or {}
    lines += ["", "## Install", "", f"Exit code **{inst.get('rc')}** after {inst.get('duration_s')}s", "",
              "```", _short(inst.get("output", "")), "```"]
    for key, title in (("detect_before", "Detection before install"), ("detect_after", "Detection after install"),
                       ("detect_after_uninstall", "Detection after uninstall")):
        d = r.get(key)
        if d:
            lines += ["", f"## {title}: {'detected' if d['detected'] else 'not detected'}", ""]
            for rule in d.get("rules", []):
                rule = dict(rule)
                t = rule.pop("type", "")
                det = rule.pop("detected", None)
                lines.append(f"- {t}: {'detected' if det else 'not detected'} — " + ", ".join(f"{k}={v}" for k, v in rule.items() if v not in (None, "")))
    un = r.get("uninstall")
    if un:
        lines += ["", "## Uninstall", "", f"Exit code **{un.get('rc')}** after {un.get('duration_s')}s", "", "```", _short(un.get("output", "")), "```"]
    apps = r.get("apps_after") or []
    if apps:
        lines += ["", "## Installed programs after install (Uninstall registry)", ""]
        for a in apps[:60]:
            lines.append(f"- {a.get('DisplayName')} {a.get('DisplayVersion') or ''} ({a.get('Publisher') or ''})")
    return "\n".join(lines) + "\n"
