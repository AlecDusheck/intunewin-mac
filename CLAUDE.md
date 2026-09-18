# CLAUDE.md — how to work in this repo

This project turns "make me an intunewin for X" into a built, tested package. Use `./bin/iwm`
(never `IntuneWinAppUtil.exe`; it does not exist here). Run `./bin/iwm doctor` first if unsure.

## Workspaces

Recipes, `dist/` and `reports/` resolve against `$IWM_WORKSPACE` (default: this checkout). Org-specific
apps live in that org's own repo (with a small `bin/iwm` wrapper that sets `IWM_WORKSPACE`), not here.
The recipes in this repo are generic examples. Paths below are workspace-relative.

## The standard workflow for "package app X"

1. **Recipe.** Check `./bin/iwm recipe list`. If missing, write `recipes/<id>.yaml` yourself
   (copy the closest existing one). Find the vendor's *enterprise/offline* installer URL, prefer MSI,
   include both `x64` and `arm64` sources when the vendor has them. Verify the URL with
   `curl -sIL <url> | grep -iE "^HTTP|content-(length|type)"` before committing to it.
2. **Build.** `./bin/iwm build <id> --arch x64 --arch arm64`. Output lands in `dist/<id>/` with
   `.intunewin`, `.intune.json` (Graph win32LobApp body), `.rules.json`, `.md` (portal notes).
3. **Test** in the VM: `./bin/iwm test <id>` (uses the VM's native arch when the recipe has it) and
   `--uninstall` when the user cares about removal. Read `reports/<id>-<arch>/latest/report.md`.
   Iterate on install switches / detection rules until it passes.
4. **Report back** with: package path(s), install/uninstall command lines, detection rules, the test
   verdict with exit code, and anything the user must set manually in the portal.
   `./bin/iwm publish` exists for Graph upload but is beta; offer it, do not run it unasked.

## Updating a package to a new vendor version

1. `./bin/iwm outdated [recipe…]` compares what each vendor serves now with the newest build in `dist/`
   (every build writes `<id>-<version>-<arch>.source.json`: URL, ETag, Last-Modified, size, sha256).
   GitHub-release recipes compare tags; fixed "latest" URLs compare ETag/Last-Modified/size.
   Recipes with a pinned `sha256` never change by themselves: edit their `url` + `sha256` by hand.
2. Rebuild with `--refresh` (otherwise a fixed "latest" URL reuses the cached, old download), VM-test.
3. `./bin/iwm publish <pkg> --app-id <existing app id>` uploads it as a new content version of the
   *same* Intune app (assignments, dependencies, supersedence and the Company Portal name/description/icon
   stay). Detection must identify the version (MSI product code, a version comparison, or a script
   package's version marker), or devices that have the old version will look "installed" and never update.

Script-driven packages: write the package version somewhere detection can check it (e.g. a registry
value set by install.ps1 from `-PackageVersion {version}`) and bump `version` when the scripts change.
Intune runs install commands from a **32-bit** process: PowerShell scripts should relaunch themselves
via `%WINDIR%\sysnative\WindowsPowerShell\v1.0\powershell.exe` (the VM agent runs 64-bit, so tests
won't catch this).

Vendor bundles (zip / self-extracting exe) can be trimmed at build time with `extract:` (7z member
paths, flattened into the package).

## Silent-install cheat sheet

| Installer type | Install | Uninstall | Detection |
|---|---|---|---|
| MSI | `msiexec /i "{filename}" /qn /norestart` | `msiexec /x {product_code} /qn /norestart` | `auto` (product code) |
| NSIS exe | `"{filename}" /S` | `"%ProgramFiles%\App\uninstall.exe" /S` | file exists / version |
| Inno Setup exe | `"{filename}" /VERYSILENT /NORESTART /SUPPRESSMSGBOXES` | `"%ProgramFiles%\App\unins000.exe" /VERYSILENT` | registry Uninstall key |
| InstallShield | `"{filename}" /s /v"/qn"` | vendor-specific | registry |
| Squirrel / per-user | set `install_context: user` | | HKCU registry / `%LocalAppData%` file |
| Script-driven | `powershell -ExecutionPolicy Bypass -File install.ps1` with `extra_files:` | | script rule |

Return codes 0/1707 success, 3010 soft reboot, 1641 hard reboot, 1618 retry are defaults.

## VM facts

* One VM, name `win11`, Windows 11 Pro ARM64, user `iwm`, SSH on `127.0.0.1:2222`, key in `vm/win11/`.
* x64-only software (e.g. kernel/print/scanner drivers with no ARM64 build) needs an x64 guest:
  `iwm --vm win11-x64 vm setup --iso <Win11_x64.iso> --arch x64 --ssh-port 2223`, then
  `iwm --vm win11-x64 test ...`. On Apple Silicon it runs under TCG emulation (`-cpu max`), several times
  slower; install/boot/shutdown waits scale x4 automatically. Both VMs can run side by side.
* `clean` snapshot is the baseline; `iwm test` always restores it first and after.
* Debugging a failed install: `./bin/iwm vm run "msiexec /i C:\iwm\pkg\<id>\x.msi /qn /l*v C:\iwm\logs\x.log"`
  then `./bin/iwm vm pull C:\iwm\logs\x.log ./x.log`. `./bin/iwm vm screenshot` shows the desktop.
  `./bin/iwm vm ssh` opens PowerShell in the guest. `./bin/iwm vm apps` lists installed programs.
* Never delete the VM (`vm delete`) without asking; recreating it takes 30+ minutes.
* Guest-side code lives in `iwm/vm/guest/agent.ps1`; it is copied at VM setup. After editing it,
  push it manually: `./bin/iwm vm push iwm/vm/guest/agent.ps1 C:\iwm\agent.ps1` then re-snapshot
  `clean` (`./bin/iwm vm snapshot clean`).

## Code map

`iwm/packager.py` format + crypto · `iwm/recipes.py` YAML → package/manifest · `iwm/testing.py`
test orchestration + report · `iwm/vm/qemu.py` VM lifecycle · `iwm/vm/unattend.py` autounattend ·
`iwm/publish.py` Graph upload · `iwm/cli.py` argparse front-end.

Keep recipes vendor-truthful (no third-party mirrors), keep `dist/`, `work/`, `vm/`, `cache/` out
of git (already ignored), and never commit `*.encryption.json`.
