"""iwm command line interface."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, packager, recipes as rmod
from .paths import CACHE, DIST, RECIPES, REPORTS, ROOT, VMDIR, WORK, WORKSPACE


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ------------------------------------------------------------------ doctor
def cmd_doctor(a) -> int:
    from .vm.qemu import host_arch
    checks = []
    def chk(name, ok, hint="", optional=False):
        checks.append((name, ok, hint, optional)); return ok
    chk("python >= 3.10", sys.version_info >= (3, 10), "brew install python")
    for mod in ("cryptography", "yaml", "requests"):
        try:
            __import__(mod); chk(f"python module {mod}", True)
        except ImportError:
            chk(f"python module {mod}", False, "pip install -r requirements.txt")
    try:
        import msal  # noqa
        chk("python module msal (publish)", True)
    except ImportError:
        chk("python module msal (optional, publish)", False, "pip install msal", optional=True)
    chk("qemu-system-aarch64/x86_64", bool(shutil.which("qemu-system-aarch64") or shutil.which("qemu-system-x86_64")), "brew install qemu")
    chk("qemu-img", bool(shutil.which("qemu-img")), "brew install qemu")
    chk("msiinfo (MSI metadata)", bool(shutil.which("msiinfo")), "brew install msitools")
    chk("hdiutil", bool(shutil.which("hdiutil")))
    chk("ssh/scp", bool(shutil.which("ssh") and shutil.which("scp")))
    chk("virtio-win.iso cached", (CACHE / "virtio-win.iso").exists(), "downloaded automatically by `iwm vm setup`")
    from .vm.qemu import VM
    vm = VM(a.vm)
    chk(f"VM '{a.vm}' created", vm.exists, "iwm vm setup --iso <windows.iso>")
    chk(f"VM '{a.vm}' installed + 'clean' snapshot", vm.exists and vm.cfg.installed and vm.has_snapshot("clean"), "iwm vm setup --iso <windows.iso>")
    print(f"intunewin-on-mac {__version__}  host={host_arch()}  root={ROOT}  workspace={WORKSPACE}")
    bad = 0
    for name, ok, hint, optional in checks:
        print(f"  [{'ok' if ok else '--'}] {name}" + (f"   -> {hint}" if not ok and hint else ""))
        bad += (not ok) and (not optional)
    return 0 if bad == 0 else 1


# ------------------------------------------------------------------ recipes
def cmd_recipe_list(a) -> int:
    for r in rmod.list_recipes():
        print(f"{r.id:32} {r.name:32} arch={','.join(r.arches)}  {r.data.get('publisher', '')}")
    return 0


def cmd_recipe_show(a) -> int:
    r = rmod.load_recipe(a.recipe)
    print(r.path.read_text())
    return 0


def cmd_recipe_new(a) -> int:
    p = RECIPES / f"{a.id}.yaml"
    if p.exists() and not a.force:
        print(f"{p} exists (use --force to overwrite)", file=sys.stderr); return 1
    is_msi = (a.url or "").lower().endswith(".msi")
    install = 'msiexec /i "{filename}" /qn /norestart' if is_msi else '"{filename}" /S'
    uninstall = "msiexec /x {product_code} /qn /norestart" if is_msi else '"%ProgramFiles%\\App\\uninstall.exe" /S'
    detection = "auto" if is_msi else (
        "\n  - type: file\n    path: '%ProgramFiles%\\App'\n    file: app.exe\n    detection: exists")
    text = "\n".join([
        f"id: {a.id}",
        f"name: {a.name or a.id}",
        f"publisher: {a.publisher or ''}",
        f"description: {a.name or a.id}",
        "homepage:",
        "sources:",
        "  x64:",
        f"    url: {a.url or 'https://example.com/installer.msi'}",
        "  # arm64:",
        "  #   url:",
        "version: auto",
        f"install: '{install}'",
        f"uninstall: '{uninstall}'",
        "install_context: system",
        "restart_behavior: basedOnReturnCode",
        f"detection: {detection}",
        "requirements:",
        "  arch: [x64]",
        "  min_os: W10_1607",
        "",
    ])
    p.write_text(text)
    print(p)
    return 0


# ------------------------------------------------------------------ fetch / build
def cmd_fetch(a) -> int:
    r = rmod.load_recipe(a.recipe)
    for arch in a.arch or r.arches[:1]:
        ctx = rmod.fetch(r, arch, refresh=a.refresh)
        _print({k: v for k, v in ctx.items() if k not in ("msi",)})
    return 0


def cmd_build(a) -> int:
    r = rmod.load_recipe(a.recipe)
    arches = a.arch or ([r.arches[0]] if r.arches else [])
    if not arches:
        print("recipe has no sources", file=sys.stderr); return 1
    for arch in arches:
        res = rmod.build(r, arch, refresh=a.refresh)
        print(f"\n{res['intunewin']}  ({res['size']:,} bytes)")
        print(f"  manifest: {res['manifest']}")
        print(f"  install:  {res['ctx']['install']}")
        print(f"  uninstall:{res['ctx']['uninstall']}")
        print(f"  detection: {json.dumps(res['rules'])}")
        if a.test:
            rc = _test_built(a, r, arch, res)
            if rc:
                return rc
    return 0


def cmd_pack(a) -> int:
    src = Path(a.source).resolve()
    setup = a.setup or next((p.name for p in src.iterdir() if p.suffix.lower() in (".msi", ".exe")), None)
    if not setup:
        print("no setup file given and none found", file=sys.stderr); return 1
    out = Path(a.output) if a.output else DIST / f"{src.name}.intunewin"
    msi = None
    if setup.lower().endswith(".msi"):
        from . import msi as msimod
        info = msimod.inspect_msi(src / setup)
        msi = info["msi_info"] if info else None
    res = packager.create_intunewin(src, setup, out, msi=msi)
    print(f"{res.output}  ({res.output.stat().st_size:,} bytes, {res.file_count} files)")
    return 0


def cmd_inspect(a) -> int:
    meta = packager.read_metadata(Path(a.file))
    d = {"name": meta.name, "setup_file": meta.setup_file, "unencrypted_size": meta.unencrypted_content_size,
         "tool_version": meta.tool_version, "msi": meta.msi.to_xml_dict() if meta.msi else None}
    if a.contents:
        d["contents"] = packager.list_contents(Path(a.file))
    if a.keys:
        d["encryption"] = meta.encryption.to_graph()
    _print(d)
    return 0


def cmd_extract(a) -> int:
    out = Path(a.output) if a.output else WORK / "extract" / Path(a.file).stem
    meta = packager.extract_intunewin(Path(a.file), out)
    print(f"extracted {meta.setup_file} and friends to {out}")
    return 0


# ------------------------------------------------------------------ vm
def _vm(a):
    from .vm.qemu import VM
    return VM(a.vm)


def cmd_vm_setup(a) -> int:
    vm = _vm(a)
    if not vm.exists:
        if not a.iso:
            print("--iso <windows.iso> is required the first time", file=sys.stderr); return 1
        vm.create(Path(a.iso), arch=a.arch, memory_mb=a.memory, cpus=a.cpus, disk_gb=a.disk,
                  ssh_port=a.ssh_port, edition=a.edition, password=a.password)
    if vm.cfg.installed:
        print("VM already installed; nothing to do"); return 0
    vm.install(display=a.display, timeout=a.timeout)
    _print(vm.status())
    return 0


def cmd_vm_start(a) -> int:
    vm = _vm(a); vm.start(display=a.display)
    if a.wait:
        vm.wait_ssh()
    return 0


def cmd_vm_stop(a) -> int:
    _vm(a).stop(force=a.force); return 0


def cmd_vm_status(a) -> int:
    _print(_vm(a).status()); return 0


def cmd_vm_delete(a) -> int:
    vm = _vm(a)
    if not a.yes:
        print(f"this deletes {vm.dir} (use --yes)", file=sys.stderr); return 1
    vm.delete(); return 0


def cmd_vm_ssh(a) -> int:
    vm = _vm(a)
    if a.command:
        r = vm.ssh(" ".join(a.command), check=False)
        sys.stdout.write(r.stdout or ""); sys.stderr.write(r.stderr or "")
        return r.returncode
    return vm.ssh_interactive()


def cmd_vm_push(a) -> int:
    _vm(a).push(Path(a.local), a.remote); return 0


def cmd_vm_pull(a) -> int:
    _vm(a).pull(a.remote, Path(a.local)); return 0


def cmd_vm_screenshot(a) -> int:
    p = _vm(a).screenshot(Path(a.output) if a.output else None); print(p); return 0


def cmd_vm_snapshot(a) -> int:
    print(_vm(a).snapshot(a.name, live=None if a.mode == "auto" else a.mode == "live")); return 0


def cmd_vm_restore(a) -> int:
    print(_vm(a).restore(a.name, start=not a.no_start)); return 0


def cmd_vm_snapshots(a) -> int:
    for s in _vm(a).snapshots():
        print(s["raw"])
    return 0


def cmd_vm_delete_snapshot(a) -> int:
    _vm(a).delete_snapshot(a.name); return 0


def cmd_vm_info(a) -> int:
    vm = _vm(a); vm.ensure_running(); _print(vm.agent("info")); return 0


def cmd_vm_apps(a) -> int:
    vm = _vm(a); vm.ensure_running()
    for app in vm.agent("apps"):
        print(f"{app.get('DisplayName')}  {app.get('DisplayVersion') or ''}  [{app.get('Publisher') or ''}]")
    return 0


def cmd_vm_run(a) -> int:
    """Run a command in the guest as SYSTEM (like Intune) and print the result."""
    import tempfile, uuid
    vm = _vm(a); vm.ensure_running()
    jid = uuid.uuid4().hex[:8]
    job = {"command": " ".join(a.command), "workdir": a.workdir or "C:\\iwm", "timeout": a.timeout, "account": a.account}
    with tempfile.TemporaryDirectory() as td:
        jp = Path(td) / f"{jid}.json"; jp.write_text(json.dumps(job)); vm.push(jp, f"C:\\iwm\\jobs\\{jid}.json")
    res = vm.agent("run", Job=f"C:\\iwm\\jobs\\{jid}.json", _timeout=str(a.timeout + 120))
    sys.stdout.write(res.get("output") or "")
    print(f"\n[rc={res.get('rc')} in {res.get('duration_s')}s as {res.get('account')}]", file=sys.stderr)
    return 0 if res.get("rc") == 0 else 1


# ------------------------------------------------------------------ test
def _test_built(a, r, arch, res) -> int:
    from .testing import run_test
    vm = _vm(a)
    ctx = res["ctx"]
    rep = run_test(vm, Path(res["intunewin"]), ctx["install"], ctx["uninstall"], res["rules"],
                   label=f"{r.id}-{arch}", account=r.data.get("install_context", "system"),
                   do_uninstall=a.uninstall, keep_snapshot=a.keep_snapshot, restore_after=not a.no_restore,
                   timeout=a.timeout)
    print(f"\n{'PASS' if rep['passed'] else 'FAIL'}  report: {rep['report_dir']}/report.md")
    return 0 if rep["passed"] else 2


def cmd_test(a) -> int:
    vm = _vm(a)
    if not (vm.exists and vm.cfg.installed):
        print("no installed test VM; run: iwm vm setup --iso <windows.iso>", file=sys.stderr); return 1
    target = a.target
    if target.endswith(".intunewin") and Path(target).exists():
        # ad-hoc package: needs install command + rules
        from .testing import run_test
        rules = json.loads(Path(a.rules).read_text()) if a.rules else []
        if not a.install:
            meta = packager.read_metadata(Path(target))
            a.install = f'msiexec /i "{meta.setup_file}" /qn /norestart' if meta.setup_file.lower().endswith(".msi") else f'"{meta.setup_file}"'
            if meta.msi and not rules:
                rules = [{"@odata.type": "#microsoft.graph.win32LobAppProductCodeRule", "ruleType": "detection",
                          "productCode": meta.msi.product_code, "productVersionOperator": "notConfigured", "productVersion": None}]
        if not rules:
            print("--rules rules.json is required for non-MSI packages", file=sys.stderr); return 1
        rep = run_test(vm, Path(target), a.install, a.uninstall_cmd, rules, label=Path(target).stem,
                       account=a.account, do_uninstall=a.uninstall, keep_snapshot=a.keep_snapshot,
                       restore_after=not a.no_restore, timeout=a.timeout)
        print(f"\n{'PASS' if rep['passed'] else 'FAIL'}  report: {rep['report_dir']}/report.md")
        return 0 if rep["passed"] else 2
    r = rmod.load_recipe(target)
    arch = a.arch or (vm.cfg.arch if vm.cfg.arch in r.arches else r.arches[0])
    if arch != vm.cfg.arch:
        print(f"note: testing the {arch} package on a {vm.cfg.arch} guest (Windows will run it under emulation)", file=sys.stderr)
    res = rmod.build(r, arch, refresh=a.refresh)
    return _test_built(a, r, arch, res)


# ------------------------------------------------------------------ publish
def cmd_publish(a) -> int:
    from .publish import publish
    pkg = Path(a.intunewin)
    manifest = Path(a.manifest) if a.manifest else pkg.with_name(pkg.name.replace(".intunewin", ".intune.json"))
    if not manifest.exists():
        print(f"manifest not found: {manifest}", file=sys.stderr); return 1
    _print(publish(pkg, manifest, tenant=a.tenant, client_id=a.client_id))
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="iwm", description="Build, inspect and test Intune Win32 (.intunewin) packages on macOS.")
    p.add_argument("--vm", default="win11", help="test VM name (default: win11)")
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("doctor", help="check host prerequisites"); s.set_defaults(fn=cmd_doctor)

    r = sp.add_parser("recipe", help="manage recipes"); rs = r.add_subparsers(dest="rcmd", required=True)
    x = rs.add_parser("list"); x.set_defaults(fn=cmd_recipe_list)
    x = rs.add_parser("show"); x.add_argument("recipe"); x.set_defaults(fn=cmd_recipe_show)
    x = rs.add_parser("new"); x.add_argument("id"); x.add_argument("--name"); x.add_argument("--publisher"); x.add_argument("--url"); x.add_argument("--force", action="store_true"); x.set_defaults(fn=cmd_recipe_new)

    s = sp.add_parser("fetch", help="download the installer for a recipe"); s.add_argument("recipe"); s.add_argument("--arch", action="append", choices=rmod.ARCHES); s.add_argument("--refresh", action="store_true"); s.set_defaults(fn=cmd_fetch)

    s = sp.add_parser("build", help="download + build the .intunewin for a recipe"); s.add_argument("recipe"); s.add_argument("--arch", action="append", choices=rmod.ARCHES); s.add_argument("--refresh", action="store_true")
    s.add_argument("--test", action="store_true", help="also install-test in the VM"); _test_opts(s); s.set_defaults(fn=cmd_build)

    s = sp.add_parser("pack", help="package an arbitrary folder"); s.add_argument("source"); s.add_argument("--setup", help="setup file name inside the folder"); s.add_argument("-o", "--output"); s.set_defaults(fn=cmd_pack)
    s = sp.add_parser("inspect", help="show Detection.xml metadata of an .intunewin"); s.add_argument("file"); s.add_argument("--contents", action="store_true"); s.add_argument("--keys", action="store_true"); s.set_defaults(fn=cmd_inspect)
    s = sp.add_parser("extract", help="decrypt an .intunewin to a folder"); s.add_argument("file"); s.add_argument("-o", "--output"); s.set_defaults(fn=cmd_extract)

    s = sp.add_parser("test", help="install-test a recipe or .intunewin in the VM"); s.add_argument("target", help="recipe id or path to .intunewin")
    s.add_argument("--arch", choices=rmod.ARCHES); s.add_argument("--refresh", action="store_true")
    s.add_argument("--install", help="install command (ad-hoc .intunewin only)"); s.add_argument("--uninstall-cmd"); s.add_argument("--rules", help="rules.json (ad-hoc .intunewin only)")
    s.add_argument("--account", default="system", choices=["system", "user"]); _test_opts(s); s.set_defaults(fn=cmd_test)

    s = sp.add_parser("publish", help="upload a built package to Intune via Microsoft Graph"); s.add_argument("intunewin"); s.add_argument("--manifest"); s.add_argument("--tenant", default="common"); s.add_argument("--client-id", default=None); s.set_defaults(fn=cmd_publish)

    v = sp.add_parser("vm", help="manage the Windows test VM"); vs = v.add_subparsers(dest="vcmd", required=True)
    x = vs.add_parser("setup", help="create + unattended-install Windows from an ISO, then snapshot 'clean'")
    x.add_argument("--iso"); x.add_argument("--arch", choices=["arm64", "x64"]); x.add_argument("--memory", type=int, default=6144); x.add_argument("--cpus", type=int, default=4)
    x.add_argument("--disk", type=int, default=64); x.add_argument("--ssh-port", type=int, default=2222); x.add_argument("--edition", default="Windows 11 Pro"); x.add_argument("--password")
    x.add_argument("--display", help="e.g. cocoa to watch the install in a window"); x.add_argument("--timeout", type=int, default=3600); x.set_defaults(fn=cmd_vm_setup)
    x = vs.add_parser("start"); x.add_argument("--display"); x.add_argument("--wait", action="store_true"); x.set_defaults(fn=cmd_vm_start)
    x = vs.add_parser("stop"); x.add_argument("--force", action="store_true"); x.set_defaults(fn=cmd_vm_stop)
    x = vs.add_parser("status"); x.set_defaults(fn=cmd_vm_status)
    x = vs.add_parser("delete"); x.add_argument("--yes", action="store_true"); x.set_defaults(fn=cmd_vm_delete)
    x = vs.add_parser("ssh", help="interactive shell, or run a PowerShell command"); x.add_argument("command", nargs="*"); x.set_defaults(fn=cmd_vm_ssh)
    x = vs.add_parser("run", help="run a command as SYSTEM in the guest"); x.add_argument("command", nargs="+"); x.add_argument("--workdir"); x.add_argument("--timeout", type=int, default=1800); x.add_argument("--account", default="system", choices=["system", "user"]); x.set_defaults(fn=cmd_vm_run)
    x = vs.add_parser("push"); x.add_argument("local"); x.add_argument("remote"); x.set_defaults(fn=cmd_vm_push)
    x = vs.add_parser("pull"); x.add_argument("remote"); x.add_argument("local"); x.set_defaults(fn=cmd_vm_pull)
    x = vs.add_parser("screenshot"); x.add_argument("-o", "--output"); x.set_defaults(fn=cmd_vm_screenshot)
    x = vs.add_parser("snapshot"); x.add_argument("name"); x.add_argument("--mode", choices=["auto", "live", "offline"], default="auto"); x.set_defaults(fn=cmd_vm_snapshot)
    x = vs.add_parser("restore"); x.add_argument("name"); x.add_argument("--no-start", action="store_true"); x.set_defaults(fn=cmd_vm_restore)
    x = vs.add_parser("snapshots"); x.set_defaults(fn=cmd_vm_snapshots)
    x = vs.add_parser("delete-snapshot"); x.add_argument("name"); x.set_defaults(fn=cmd_vm_delete_snapshot)
    x = vs.add_parser("info"); x.set_defaults(fn=cmd_vm_info)
    x = vs.add_parser("apps", help="list installed programs in the guest"); x.set_defaults(fn=cmd_vm_apps)
    return p


def _test_opts(s):
    s.add_argument("--uninstall", action="store_true", help="also test the uninstall command")
    s.add_argument("--keep-snapshot", action="store_true", help="keep a snapshot of the installed state")
    s.add_argument("--no-restore", action="store_true", help="leave the VM in the post-test state")
    s.add_argument("--timeout", type=int, default=1800)


def main(argv=None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        if "--debug" in (argv or sys.argv):
            raise
        return 1
