"""
Windows test VM on QEMU (the same engine UTM uses), driven headlessly from Python.

Layout of vm/<name>/:
    vm.json           configuration
    disk.qcow2        OS disk (internal qcow2 snapshots hold the "clean" state and any others)
    efi_vars.qcow2    UEFI NVRAM (qcow2 so live `savevm` snapshots include it)
    config.iso        autounattend.xml + drivers + OpenSSH + guest agent + ssh public key
    id_ed25519[.pub]  key pair used to reach the guest
    qmp.sock          QEMU monitor socket while running
    qemu.pid / qemu.log
    screenshots/

Snapshots: `savevm` over QMP (live, includes RAM, restore in seconds) when the accelerator
allows it; otherwise offline `qemu-img snapshot` with a stop/start around it. Both are
internal qcow2 snapshots and are listed the same way.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from ..paths import CACHE, GUEST, VMDIR
from .qmp import QMP, QMPError
from .unattend import autounattend_xml

VIRTIO_WIN_URL = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
OPENSSH_URLS = {
    "arm64": "https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.0.0.0p2-Preview/OpenSSH-ARM64.zip",
    "x64": "https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.0.0.0p2-Preview/OpenSSH-Win64.zip",
}
# virtio-win.iso subfolders to bundle (relative to the ISO root); "w11" drivers also work on 10.
VIRTIO_DRIVERS = {
    "arm64": ["NetKVM/w11/ARM64", "viorng/w11/ARM64", "Balloon/w11/ARM64", "viogpudo/w11/ARM64"],
    "x64": ["NetKVM/w11/amd64", "viorng/w11/amd64", "Balloon/w11/amd64", "viogpudo/w11/amd64"],
}


def host_arch() -> str:
    m = platform.machine().lower()
    return "arm64" if m in ("arm64", "aarch64") else "x64"


def log(msg: str) -> None:
    print(f"[vm] {msg}", file=sys.stderr, flush=True)


@dataclass
class VMConfig:
    name: str = "win11"
    arch: str = "arm64"
    iso: str = ""
    edition: str = "Windows 11 Pro"
    memory_mb: int = 6144
    cpus: int = 4
    disk_gb: int = 64
    ssh_port: int = 2222
    user: str = "iwm"
    password: str = "iwm-Test1!"
    installed: bool = False
    live_snapshots: Optional[bool] = None     # None = unknown, probe on first use
    extra_args: list = field(default_factory=list)


class VM:
    def __init__(self, name: str = "win11"):
        self.dir = VMDIR / name
        self.cfg_path = self.dir / "vm.json"
        self.cfg = VMConfig(name=name)
        if self.cfg_path.exists():
            self.cfg = VMConfig(**{**asdict(self.cfg), **json.loads(self.cfg_path.read_text())})

    # ------------------------------------------------------------------ paths
    @property
    def disk(self) -> Path: return self.dir / "disk.qcow2"
    @property
    def efi_vars(self) -> Path: return self.dir / "efi_vars.qcow2"
    @property
    def config_iso(self) -> Path: return self.dir / "config.iso"
    @property
    def ssh_key(self) -> Path: return self.dir / "id_ed25519"
    @property
    def qmp_sock(self) -> Path: return self.dir / "qmp.sock"
    @property
    def pid_file(self) -> Path: return self.dir / "qemu.pid"
    @property
    def log_file(self) -> Path: return self.dir / "qemu.log"
    @property
    def exists(self) -> bool: return self.cfg_path.exists() and self.disk.exists()

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path.write_text(json.dumps(asdict(self.cfg), indent=2))

    # ------------------------------------------------------------------ tooling
    @staticmethod
    def qemu_prefix() -> Path:
        for cand in (os.environ.get("QEMU_PREFIX"), "/opt/homebrew/opt/qemu", "/usr/local/opt/qemu"):
            if cand and Path(cand, "share", "qemu").exists():
                return Path(cand)
        q = shutil.which("qemu-system-aarch64") or shutil.which("qemu-system-x86_64")
        if q:
            return Path(q).resolve().parent.parent
        raise RuntimeError("QEMU not found. Install it with: brew install qemu")

    def qemu_bin(self) -> str:
        b = "qemu-system-aarch64" if self.cfg.arch == "arm64" else "qemu-system-x86_64"
        p = shutil.which(b)
        if not p:
            raise RuntimeError(f"{b} not found. Install it with: brew install qemu")
        return p

    def firmware(self) -> tuple[Path, Path]:
        share = self.qemu_prefix() / "share" / "qemu"
        if self.cfg.arch == "arm64":
            return share / "edk2-aarch64-code.fd", share / "edk2-arm-vars.fd"
        return share / "edk2-x86_64-code.fd", share / "edk2-i386-vars.fd"

    # ------------------------------------------------------------------ create
    def create(self, iso: Path, arch: Optional[str] = None, memory_mb: int = 6144, cpus: int = 4,
               disk_gb: int = 64, ssh_port: int = 2222, edition: str = "Windows 11 Pro",
               password: Optional[str] = None) -> None:
        if self.exists:
            raise RuntimeError(f"VM {self.cfg.name} already exists at {self.dir}; delete it first (iwm vm delete)")
        iso = Path(iso).resolve()
        if not iso.is_file():
            raise FileNotFoundError(iso)
        self.cfg.arch = arch or host_arch()
        self.cfg.iso = str(iso)
        self.cfg.memory_mb, self.cfg.cpus, self.cfg.disk_gb, self.cfg.ssh_port = memory_mb, cpus, disk_gb, ssh_port
        self.cfg.edition = edition
        if password:
            self.cfg.password = password
        self.cfg.installed = False
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "screenshots").mkdir(exist_ok=True)
        log(f"creating VM {self.cfg.name} ({self.cfg.arch}, {memory_mb} MB, {cpus} vCPU, {disk_gb} GB) from {iso.name}")
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(self.disk), f"{disk_gb}G"], check=True, capture_output=True)
        code, vars_ = self.firmware()
        subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(vars_), str(self.efi_vars)], check=True)
        if not self.ssh_key.exists():
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "iwm", "-f", str(self.ssh_key)], check=True)
        self.build_config_iso()
        self.save()

    def build_config_iso(self) -> Path:
        """config.iso: autounattend.xml at the root + iwm/ folder with drivers, OpenSSH, agent, key."""
        with tempfile.TemporaryDirectory(prefix="iwm-cfg-") as td:
            td = Path(td)
            (td / "autounattend.xml").write_text(autounattend_xml(
                arch=self.cfg.arch, edition=self.cfg.edition, username=self.cfg.user,
                password=self.cfg.password, computer_name=f"IWM-{self.cfg.name.upper()[:10]}"))
            iwm = td / "iwm"
            iwm.mkdir()
            shutil.copy2(GUEST / "setup.ps1", iwm / "setup.ps1")
            shutil.copy2(GUEST / "agent.ps1", iwm / "agent.ps1")
            shutil.copy2(self.ssh_key.with_suffix(".pub"), iwm / "authorized_keys")
            # OpenSSH portable zip
            from ..download import download
            ssh_zip = download(OPENSSH_URLS[self.cfg.arch], CACHE / Path(OPENSSH_URLS[self.cfg.arch]).name)
            shutil.copy2(ssh_zip, iwm / "OpenSSH.zip")
            # virtio drivers
            virtio = download(VIRTIO_WIN_URL, CACHE / "virtio-win.iso")
            self._copy_virtio_drivers(virtio, iwm / "drivers")
            if self.config_iso.exists():
                self.config_iso.unlink()
            log("building config.iso")
            subprocess.run(["hdiutil", "makehybrid", "-quiet", "-iso", "-joliet", "-default-volume-name", "IWMCFG",
                            "-o", str(self.config_iso), str(td)], check=True)
        return self.config_iso

    def _copy_virtio_drivers(self, virtio_iso: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        out = subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-noverify", str(virtio_iso)],
                             check=True, capture_output=True, text=True).stdout
        mount = out.strip().split("\t")[-1].strip()
        try:
            for rel in VIRTIO_DRIVERS[self.cfg.arch]:
                src = Path(mount) / rel
                if src.is_dir():
                    shutil.copytree(src, dest / rel.split("/")[0], dirs_exist_ok=True)
                else:
                    log(f"warning: {rel} not found in {virtio_iso.name}")
        finally:
            subprocess.run(["hdiutil", "detach", "-quiet", mount], check=False)

    # ------------------------------------------------------------------ command line
    def qemu_args(self, install: bool = False, display: Optional[str] = None) -> list[str]:
        c = self.cfg
        code_fw, _ = self.firmware()
        a = [self.qemu_bin(), "-name", f"iwm-{c.name}", "-accel", "hvf", "-cpu", "host",
             "-smp", str(c.cpus), "-m", f"{c.memory_mb}M", "-rtc", "base=localtime,clock=host"]
        if c.arch == "arm64":
            a += ["-machine", "virt,highmem=on"]
            a += ["-drive", f"if=pflash,format=raw,readonly=on,file={code_fw}"]
            a += ["-drive", f"if=pflash,format=qcow2,file={self.efi_vars}"]
        else:
            a += ["-machine", "q35"]
            a += ["-drive", f"if=pflash,format=raw,readonly=on,file={code_fw}"]
            a += ["-drive", f"if=pflash,format=qcow2,file={self.efi_vars}"]
        a += ["-device", "qemu-xhci,id=xhci", "-device", "usb-kbd", "-device", "usb-tablet"]
        a += ["-device", "ramfb"] if c.arch == "arm64" else ["-vga", "std"]
        a += ["-drive", f"file={self.disk},if=none,id=hd0,format=qcow2,discard=unmap",
              "-device", "nvme,drive=hd0,serial=iwm0001,bootindex=0"]
        if install:
            a += ["-drive", f"file={c.iso},if=none,id=cd0,media=cdrom,readonly=on,format=raw,file.locking=off",
                  "-device", "usb-storage,drive=cd0,removable=on,bootindex=1"]
        # the config ISO is always attached: cheap, and lets setup.ps1 re-run on demand
        a += ["-drive", f"file={self.config_iso},if=none,id=cd1,media=cdrom,readonly=on,format=raw,file.locking=off",
              "-device", "usb-storage,drive=cd1,removable=on"]
        a += ["-device", "virtio-net-pci,netdev=n0",
              "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{c.ssh_port}-:22"]
        a += ["-device", "virtio-rng-pci", "-device", "virtio-balloon-pci"]
        a += ["-qmp", f"unix:{self.qmp_sock},server,nowait", "-pidfile", str(self.pid_file)]
        a += ["-display", display or "none"]
        a += list(c.extra_args)
        return a

    # ------------------------------------------------------------------ lifecycle
    def pid(self) -> Optional[int]:
        try:
            pid = int(self.pid_file.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            return None

    def is_running(self) -> bool:
        return self.pid() is not None

    def qmp(self) -> QMP:
        return QMP(self.qmp_sock)

    def start(self, install: bool = False, display: Optional[str] = None) -> int:
        if self.is_running():
            return self.pid()
        if not self.exists:
            raise RuntimeError("VM not created; run `iwm vm setup --iso <windows.iso>` first")
        for p in (self.qmp_sock, self.pid_file):
            p.unlink(missing_ok=True)
        args = self.qemu_args(install=install, display=display)
        (self.dir / "last-command.txt").write_text(" \\\n  ".join(args))
        logf = open(self.log_file, "ab")
        logf.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start install={install}\n".encode())
        proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        for _ in range(100):
            if self.qmp_sock.exists() and self.pid_file.exists():
                break
            if proc.poll() is not None:
                raise RuntimeError(f"qemu exited immediately (code {proc.returncode}); see {self.log_file}")
            time.sleep(0.1)
        log(f"started {self.cfg.name} (pid {proc.pid}, ssh port {self.cfg.ssh_port})")
        return proc.pid

    def stop(self, force: bool = False, timeout: int = 120) -> None:
        pid = self.pid()
        if not pid:
            return
        if not force:
            try:
                self.ssh("shutdown /s /t 0 /f", timeout=20, check=False)
            except Exception:
                pass
            try:
                with self.qmp() as q:
                    q.execute("system_powerdown")
            except Exception:
                pass
            for _ in range(timeout):
                if not self.is_running():
                    break
                time.sleep(1)
        if self.is_running():
            log("forcing qemu to quit")
            try:
                with self.qmp() as q:
                    q.execute("quit")
            except Exception:
                os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not self.is_running():
                    break
                time.sleep(0.5)
        self.pid_file.unlink(missing_ok=True)
        self.qmp_sock.unlink(missing_ok=True)

    def delete(self) -> None:
        self.stop(force=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def status(self) -> dict:
        st = {"name": self.cfg.name, "exists": self.exists, "running": self.is_running(), "pid": self.pid(),
              "arch": self.cfg.arch, "installed": self.cfg.installed, "ssh_port": self.cfg.ssh_port,
              "dir": str(self.dir), "live_snapshots": self.cfg.live_snapshots}
        if self.exists:
            st["snapshots"] = self.snapshots()
            st["disk_bytes"] = self.disk.stat().st_size
        if st["running"]:
            st["ssh"] = self.ssh_ready()
        return st

    # ------------------------------------------------------------------ screenshots / keys
    def screenshot(self, path: Optional[Path] = None) -> Path:
        path = Path(path) if path else self.dir / "screenshots" / f"{time.strftime('%Y%m%d-%H%M%S')}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.qmp() as q:
            q.screendump(path, "png")
        return path

    def screen_brightness(self) -> float:
        """Mean pixel brightness 0..1 of the current display (uses a raw PPM screendump)."""
        with tempfile.NamedTemporaryFile(suffix=".ppm", delete=False) as tf:
            path = Path(tf.name)
        try:
            with self.qmp() as q:
                q.execute("screendump", filename=str(path))
            data = path.read_bytes()
            # P6\n<w> <h>\n255\n<rgb bytes>
            parts = data.split(b"\n", 3)
            pixels = parts[3] if len(parts) == 4 else b""
            if not pixels:
                return 1.0
            sample = pixels[::97]
            return sum(sample) / (255.0 * len(sample))
        finally:
            path.unlink(missing_ok=True)

    _KEYMAP = {" ": "spc", "\n": "ret", "\t": "tab", ":": "shift-semicolon", ";": "semicolon", "\\": "backslash",
               "/": "slash", ".": "dot", ",": "comma", "-": "minus", "_": "shift-minus", "=": "equal", "+": "shift-equal",
               "\"": "shift-apostrophe", "'": "apostrophe", "(": "shift-9", ")": "shift-0", "*": "shift-8", "!": "shift-1",
               "?": "shift-slash", "$": "shift-4", "%": "shift-5", "&": "shift-7", "#": "shift-3", "@": "shift-2",
               "<": "shift-comma", ">": "shift-dot", "[": "bracket_left", "]": "bracket_right", "|": "shift-backslash"}

    def type_text(self, text: str, delay: float = 0.04) -> None:
        """Type text into the guest console via QMP send-key (US layout, ASCII only)."""
        with self.qmp() as q:
            for ch in text:
                k = self._KEYMAP.get(ch)
                if k is None:
                    k = ("shift-" + ch.lower()) if ch.isalpha() and ch.isupper() else ch
                keys = k.split("-", 1) if k.startswith("shift-") else [k]
                q.execute("send-key", keys=[{"type": "qcode", "data": x} for x in keys])
                time.sleep(delay)

    def send_keys(self, *keys: str) -> None:
        with self.qmp() as q:
            q.send_key(*keys)

    # ------------------------------------------------------------------ ssh
    def ssh_base(self, port_flag: str = "-p") -> list[str]:
        return ["-i", str(self.ssh_key), port_flag, str(self.cfg.ssh_port), "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=10",
                "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15"]

    def ssh_target(self) -> str:
        return f"{self.cfg.user}@127.0.0.1"

    def ssh(self, command: str, timeout: Optional[int] = 600, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        """Run a command in the guest (default shell is PowerShell)."""
        cmd = ["ssh"] + self.ssh_base() + [self.ssh_target(), command]
        r = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout, errors="replace")
        if check and r.returncode != 0:
            raise RuntimeError(f"ssh command failed ({r.returncode}): {command}\n{(r.stderr or '')[-2000:]}\n{(r.stdout or '')[-2000:]}")
        return r

    def ssh_interactive(self) -> int:
        return subprocess.call(["ssh", "-t", "-i", str(self.ssh_key), "-p", str(self.cfg.ssh_port),
                                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                                "-o", "LogLevel=ERROR", self.ssh_target()])

    def push(self, local: Path, remote: str) -> None:
        subprocess.run(["scp", "-q"] + self.ssh_base("-P") + [str(local), f"{self.ssh_target()}:{remote}"], check=True)

    def pull(self, remote: str, local: Path) -> None:
        subprocess.run(["scp", "-q"] + self.ssh_base("-P") + [f"{self.ssh_target()}:{remote}", str(local)], check=True)

    def port_open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.cfg.ssh_port), timeout=2):
                return True
        except OSError:
            return False

    def ssh_ready(self) -> bool:
        if not self.port_open():
            return False
        try:
            r = self.ssh("Write-Output iwm-ok", timeout=25, check=False)
            return r.returncode == 0 and "iwm-ok" in (r.stdout or "")
        except Exception:
            return False

    def wait_ssh(self, timeout: int = 900, interval: int = 10, screenshots: bool = False) -> None:
        t0 = time.time()
        n = 0
        while time.time() - t0 < timeout:
            if not self.is_running():
                raise RuntimeError(f"qemu is not running (see {self.log_file})")
            if self.ssh_ready():
                log(f"ssh ready after {int(time.time() - t0)}s")
                return
            n += 1
            if screenshots and n % 6 == 0:
                try:
                    self.screenshot(self.dir / "screenshots" / f"wait-{int(time.time() - t0):05d}s.png")
                except Exception:
                    pass
            time.sleep(interval)
        raise TimeoutError(f"guest ssh not reachable after {timeout}s")

    def agent(self, action: str, **kw: str) -> dict:
        timeout = int(kw.pop("_timeout", 3600))
        args = " ".join(f"-{k} '{v}'" for k, v in kw.items())
        r = self.ssh(f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\iwm\\agent.ps1 -Action {action} {args}",
                     timeout=timeout)
        out = (r.stdout or "").strip()
        try:
            return json.loads(out[out.index("{"):] if "{" in out else out)
        except (ValueError, json.JSONDecodeError):
            raise RuntimeError(f"agent returned non-JSON output:\n{out[-3000:]}\n{(r.stderr or '')[-1000:]}")

    # ------------------------------------------------------------------ snapshots
    def snapshots(self) -> list[dict]:
        r = subprocess.run(["qemu-img", "snapshot", "-l", "-U", str(self.disk)], capture_output=True, text=True)
        out = []
        for line in r.stdout.splitlines()[2:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit():
                out.append({"id": parts[0], "name": parts[1], "raw": line.strip()})
        return out

    def has_snapshot(self, name: str) -> bool:
        return any(s["name"] == name for s in self.snapshots())

    def _probe_live(self) -> bool:
        """Can this accelerator save/restore VM state live? Cached in vm.json."""
        if self.cfg.live_snapshots is not None:
            return self.cfg.live_snapshots
        ok = False
        if self.is_running():
            try:
                with self.qmp() as q:
                    res = q.hmp("savevm iwm-probe")
                    ok = not res.strip()
                    if ok:
                        q.hmp("delvm iwm-probe")
                    else:
                        log(f"live snapshots unavailable: {res.strip()[:200]}")
            except Exception as e:
                log(f"live snapshot probe failed: {e}")
            self.cfg.live_snapshots = ok
            self.save()
        return ok

    def snapshot(self, name: str, live: Optional[bool] = None) -> str:
        """Create/overwrite an internal qcow2 snapshot. Returns 'live' or 'offline'."""
        if self.is_running() and (live is not False) and self._probe_live():
            with self.qmp() as q:
                if self.has_snapshot(name):
                    q.hmp(f"delvm {name}")
                res = q.hmp(f"savevm {name}")
            if res.strip():
                raise RuntimeError(f"savevm failed: {res.strip()}")
            log(f"live snapshot '{name}' saved")
            return "live"
        was_running = self.is_running()
        if was_running:
            log("stopping VM for an offline snapshot")
            self.stop()
        if self.has_snapshot(name):
            subprocess.run(["qemu-img", "snapshot", "-d", name, str(self.disk)], check=True)
        subprocess.run(["qemu-img", "snapshot", "-c", name, str(self.disk)], check=True)
        log(f"offline snapshot '{name}' saved")
        if was_running:
            self.start()
        return "offline"

    def restore(self, name: str, start: bool = True) -> str:
        if not self.has_snapshot(name):
            raise RuntimeError(f"snapshot '{name}' does not exist (have: {', '.join(s['name'] for s in self.snapshots()) or 'none'})")
        if self.is_running() and self._probe_live():
            with self.qmp() as q:
                res = q.hmp(f"loadvm {name}")
            if not res.strip():
                log(f"live restore of '{name}' done")
                return "live"
            log(f"loadvm failed ({res.strip()[:200]}); falling back to offline restore")
        if self.is_running():
            self.stop(force=True)   # state is being discarded anyway
        subprocess.run(["qemu-img", "snapshot", "-a", name, str(self.disk)], check=True)
        log(f"offline restore of '{name}' done")
        if start:
            self.start()
        return "offline"

    def delete_snapshot(self, name: str) -> None:
        if self.is_running() and self._probe_live():
            with self.qmp() as q:
                q.hmp(f"delvm {name}")
        else:
            was = self.is_running()
            if was:
                self.stop()
            subprocess.run(["qemu-img", "snapshot", "-d", name, str(self.disk)], check=True)
            if was:
                self.start()

    # ------------------------------------------------------------------ unattended install
    def install(self, display: Optional[str] = None, timeout: int = 3600) -> None:
        """Boot from the Windows ISO with autounattend, wait for the guest agent, snapshot 'clean'."""
        if self.cfg.installed:
            raise RuntimeError("already installed; use `iwm vm delete` to start over")
        log("starting unattended Windows install (this takes 15-40 minutes)")
        self.start(install=True, display=display)
        # Windows media prints "Press any key to boot from CD or DVD" for a few seconds. Press Enter
        # only while the screen is still (almost) black: once Setup's blue UI is up, a stray Enter
        # would hit its Cancel button.
        t0 = time.time()
        while time.time() - t0 < 90:
            try:
                if self.screen_brightness() < 0.08:
                    with self.qmp() as q:
                        q.send_key("ret")
                else:
                    break
            except Exception:
                pass
            time.sleep(1)
        shots = self.dir / "screenshots"
        last_shot = 0
        while time.time() - t0 < timeout:
            if not self.is_running():
                raise RuntimeError(f"qemu exited during install; see {self.log_file}")
            if time.time() - last_shot > 60:
                try:
                    self.screenshot(shots / f"install-{int(time.time() - t0):05d}s.png")
                except Exception:
                    pass
                last_shot = time.time()
                log(f"installing... {int(time.time() - t0) // 60} min elapsed (screenshots in {shots})")
            if self.port_open() and self.ssh_ready():
                try:
                    info = self.agent("info")
                    if info.get("ready"):
                        log(f"guest ready: {info.get('os')} build {info.get('build')} ({info.get('arch')})")
                        break
                except Exception as e:
                    log(f"agent not ready yet: {str(e)[:120]}")
            time.sleep(15)
        else:
            raise TimeoutError("Windows install did not finish in time; check screenshots")
        self.cfg.installed = True
        self.save()
        log("shutting down to take the baseline snapshot")
        self.stop()
        self.snapshot("clean")
        log("baseline snapshot 'clean' created; VM is ready for testing")

    def ensure_running(self, wait: int = 600) -> None:
        if not self.cfg.installed:
            raise RuntimeError("VM is not installed yet; run `iwm vm setup --iso <windows.iso>`")
        if not self.is_running():
            self.start()
        self.wait_ssh(timeout=wait)
