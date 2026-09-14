#!/bin/bash
# One-shot host setup for intunewin-on-mac. Safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh" >&2; exit 1
fi

echo "==> Homebrew packages (qemu = VM engine, msitools = MSI metadata)"
brew list --formula qemu >/dev/null 2>&1 || brew install qemu
brew list --formula msitools >/dev/null 2>&1 || brew install msitools

echo "==> Python virtualenv"
PY="$(command -v python3.12 || command -v python3.11 || command -v python3)"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> Pre-fetching guest support files (virtio drivers, OpenSSH)"
.venv/bin/python - <<'EOF'
from iwm.download import download
from iwm.paths import CACHE
from iwm.vm.qemu import VIRTIO_WIN_URL, OPENSSH_URLS, host_arch
download(VIRTIO_WIN_URL, CACHE / "virtio-win.iso")
url = OPENSSH_URLS[host_arch()]
download(url, CACHE / url.rsplit("/", 1)[-1])
EOF

echo
./bin/iwm doctor || true
cat <<EOF

Next:
  ./bin/iwm vm setup --iso /path/to/Windows11.iso     # ~20-40 min, fully unattended
  ./bin/iwm build google-chrome-enterprise --test      # build + install-test in the VM

Tip: add $HERE/bin to your PATH.
EOF
