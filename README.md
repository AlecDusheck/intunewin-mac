# intunewin-on-mac

Build and install-test Intune Win32 packages (`.intunewin`) on a Mac. Pure-Python packager
(no `IntuneWinAppUtil.exe`), plus a disposable Windows VM on QEMU with snapshot rollback for
testing installs the way Intune runs them (as SYSTEM, then detection rules).

```bash
./scripts/setup.sh                                   # brew qemu msitools, venv, guest drivers
./bin/iwm vm setup --iso ~/Downloads/Win11_ARM64.iso # unattended install, ~15 min, snapshots "clean"
./bin/iwm build 7zip --test                          # download -> .intunewin -> install-test -> report
```

| Command | Purpose |
|---|---|
| `iwm build <recipe> [--arch x64\|arm64] [--test]` | package + Graph manifest + portal notes in `dist/` |
| `iwm test <recipe\|file.intunewin> [--uninstall]` | restore clean VM, install as SYSTEM, run detection, report |
| `iwm pack <folder> --setup x.msi` / `inspect` / `extract` | work with arbitrary packages |
| `iwm vm setup\|start\|stop\|snapshot\|restore\|run\|ssh\|screenshot` | drive the VM |
| `iwm publish <file.intunewin>` | upload via Microsoft Graph (beta) |

Recipes live in `recipes/*.yaml` (see `iwm/recipes.py` for fields). Set `IWM_WORKSPACE=<dir>` to
use `<dir>/recipes`, `<dir>/dist` and `<dir>/reports` instead, e.g. an organization's config repo;
the VM, download cache and work dirs always stay in this checkout. Reports land in
`reports/<recipe>/latest/report.md`. Requires Apple Silicon, Homebrew, Python 3.10+, and a
Windows ARM64 ISO. See `CLAUDE.md` for the Claude Code workflow.
