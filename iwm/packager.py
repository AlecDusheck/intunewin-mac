"""
Pure-Python implementation of the Microsoft Win32 Content Prep Tool (.intunewin) format.

An .intunewin file is a ZIP archive:
    IntuneWinPackage/Contents/IntunePackage.intunewin   encrypted payload
    IntuneWinPackage/Metadata/Detection.xml              metadata + encryption keys

The payload is the source folder zipped (deflate), then encrypted:
    [HMAC-SHA256 (32 bytes)] [IV (16 bytes)] [AES-256-CBC ciphertext, PKCS7]
The HMAC is computed with MacKey over IV + ciphertext. FileDigest is SHA-256 of
the unencrypted inner zip. Detection.xml carries all keys base64 encoded, which
is also what Intune expects in the Graph `fileEncryptionInfo` on commit.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

TOOL_VERSION = "1.8.6.0"
CHUNK = 4 * 1024 * 1024
INNER_NAME = "IntunePackage.intunewin"
PAYLOAD_PATH = "IntuneWinPackage/Contents/IntunePackage.intunewin"
METADATA_PATH = "IntuneWinPackage/Metadata/Detection.xml"


@dataclass
class EncryptionInfo:
    encryption_key: bytes
    mac_key: bytes
    iv: bytes
    mac: bytes
    file_digest: bytes
    profile_identifier: str = "ProfileVersion1"
    file_digest_algorithm: str = "SHA256"

    def to_xml_dict(self) -> dict:
        b64 = lambda b: base64.b64encode(b).decode()
        return {
            "EncryptionKey": b64(self.encryption_key),
            "MacKey": b64(self.mac_key),
            "InitializationVector": b64(self.iv),
            "Mac": b64(self.mac),
            "ProfileIdentifier": self.profile_identifier,
            "FileDigest": b64(self.file_digest),
            "FileDigestAlgorithm": self.file_digest_algorithm,
        }

    def to_graph(self) -> dict:
        """Shape used by Microsoft Graph mobileAppContentFile commit."""
        b64 = lambda b: base64.b64encode(b).decode()
        return {
            "encryptionKey": b64(self.encryption_key),
            "macKey": b64(self.mac_key),
            "initializationVector": b64(self.iv),
            "mac": b64(self.mac),
            "profileIdentifier": self.profile_identifier,
            "fileDigest": b64(self.file_digest),
            "fileDigestAlgorithm": self.file_digest_algorithm,
        }

    @classmethod
    def from_xml(cls, el: ET.Element) -> "EncryptionInfo":
        g = lambda k: base64.b64decode(el.findtext(k) or "")
        return cls(
            encryption_key=g("EncryptionKey"),
            mac_key=g("MacKey"),
            iv=g("InitializationVector"),
            mac=g("Mac"),
            file_digest=g("FileDigest"),
            profile_identifier=el.findtext("ProfileIdentifier") or "ProfileVersion1",
            file_digest_algorithm=el.findtext("FileDigestAlgorithm") or "SHA256",
        )


@dataclass
class MsiInfo:
    product_code: str
    product_version: str
    package_code: str = ""
    upgrade_code: str = ""
    execution_context: str = "System"   # System | User | Any
    publisher: str = ""
    requires_logon: bool = False
    requires_reboot: bool = False
    is_machine_install: bool = True
    is_user_install: bool = False
    includes_services: bool = False
    contains_system_registry_keys: bool = False
    contains_system_folders: bool = False

    def to_xml_dict(self) -> dict:
        b = lambda v: "true" if v else "false"
        return {
            "MsiProductCode": self.product_code,
            "MsiProductVersion": self.product_version,
            "MsiPackageCode": self.package_code,
            "MsiUpgradeCode": self.upgrade_code,
            "MsiExecutionContext": self.execution_context,
            "MsiRequiresLogon": b(self.requires_logon),
            "MsiRequiresReboot": b(self.requires_reboot),
            "MsiIsMachineInstall": b(self.is_machine_install),
            "MsiIsUserInstall": b(self.is_user_install),
            "MsiIncludesServices": b(self.includes_services),
            "MsiContainsSystemRegistryKeys": b(self.contains_system_registry_keys),
            "MsiContainsSystemFolders": b(self.contains_system_folders),
            "MsiPublisher": self.publisher,
        }


@dataclass
class PackageMetadata:
    name: str
    setup_file: str
    unencrypted_content_size: int
    encryption: EncryptionInfo
    file_name: str = INNER_NAME
    msi: Optional[MsiInfo] = None
    tool_version: str = TOOL_VERSION

    def to_xml(self) -> bytes:
        root = ET.Element(
            "ApplicationInfo",
            {
                "xmlns:xsd": "http://www.w3.org/2001/XMLSchema",
                "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
                "ToolVersion": self.tool_version,
            },
        )
        ET.SubElement(root, "Name").text = self.name
        ET.SubElement(root, "UnencryptedContentSize").text = str(self.unencrypted_content_size)
        ET.SubElement(root, "FileName").text = self.file_name
        ET.SubElement(root, "SetupFile").text = self.setup_file
        enc = ET.SubElement(root, "EncryptionInfo")
        for k, v in self.encryption.to_xml_dict().items():
            ET.SubElement(enc, k).text = v
        if self.msi:
            m = ET.SubElement(root, "MsiInfo")
            for k, v in self.msi.to_xml_dict().items():
                ET.SubElement(m, k).text = v
        ET.indent(root, space="  ")
        return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8")

    @classmethod
    def from_xml(cls, data: bytes) -> "PackageMetadata":
        root = ET.fromstring(data)
        msi = None
        m = root.find("MsiInfo")
        if m is not None:
            t = lambda k: (m.findtext(k) or "")
            msi = MsiInfo(
                product_code=t("MsiProductCode"),
                product_version=t("MsiProductVersion"),
                package_code=t("MsiPackageCode"),
                upgrade_code=t("MsiUpgradeCode"),
                execution_context=t("MsiExecutionContext") or "System",
                publisher=t("MsiPublisher"),
                requires_logon=t("MsiRequiresLogon") == "true",
                requires_reboot=t("MsiRequiresReboot") == "true",
                is_machine_install=t("MsiIsMachineInstall") != "false",
                is_user_install=t("MsiIsUserInstall") == "true",
                includes_services=t("MsiIncludesServices") == "true",
                contains_system_registry_keys=t("MsiContainsSystemRegistryKeys") == "true",
                contains_system_folders=t("MsiContainsSystemFolders") == "true",
            )
        return cls(
            name=root.findtext("Name") or "",
            setup_file=root.findtext("SetupFile") or "",
            unencrypted_content_size=int(root.findtext("UnencryptedContentSize") or 0),
            encryption=EncryptionInfo.from_xml(root.find("EncryptionInfo")),
            file_name=root.findtext("FileName") or INNER_NAME,
            msi=msi,
            tool_version=root.get("ToolVersion", TOOL_VERSION),
        )


# --------------------------------------------------------------------------- zip helpers

def _iter_files(source_dir: Path) -> Iterable[Path]:
    for p in sorted(source_dir.rglob("*")):
        if p.is_file() and p.name != ".DS_Store" and "__MACOSX" not in p.parts:
            yield p


def zip_folder(source_dir: Path, zip_path: Path) -> int:
    """Zip the *contents* of source_dir (files at the archive root). Returns file count."""
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in _iter_files(source_dir):
            zf.write(p, p.relative_to(source_dir).as_posix())
            n += 1
    if n == 0:
        raise ValueError(f"no files found in {source_dir}")
    return n


# --------------------------------------------------------------------------- crypto

def encrypt_file(plain_path: Path, out_path: Path) -> EncryptionInfo:
    key = secrets.token_bytes(32)
    mac_key = secrets.token_bytes(32)
    iv = secrets.token_bytes(16)
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    padder = padding.PKCS7(128).padder()
    h = hmac.new(mac_key, digestmod=hashlib.sha256)
    digest = hashlib.sha256()
    with open(plain_path, "rb") as f, open(out_path, "wb") as out:
        out.write(b"\0" * 32)          # HMAC placeholder
        out.write(iv)
        h.update(iv)
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            ct = enc.update(padder.update(chunk))
            h.update(ct)
            out.write(ct)
        ct = enc.update(padder.finalize()) + enc.finalize()
        h.update(ct)
        out.write(ct)
        mac = h.digest()
        out.seek(0)
        out.write(mac)
    return EncryptionInfo(key, mac_key, iv, mac, digest.digest())


def decrypt_file(enc_path: Path, out_path: Path, info: EncryptionInfo, verify: bool = True) -> None:
    with open(enc_path, "rb") as f:
        mac = f.read(32)
        iv = f.read(16)
        if verify:
            if mac != info.mac:
                raise ValueError("Mac in Detection.xml does not match the file header")
            h = hmac.new(info.mac_key, digestmod=hashlib.sha256)
            h.update(iv)
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
            if not hmac.compare_digest(h.digest(), mac):
                raise ValueError("HMAC verification failed: payload is corrupt or keys are wrong")
            f.seek(48)
        dec = Cipher(algorithms.AES(info.encryption_key), modes.CBC(iv)).decryptor()
        unpadder = padding.PKCS7(128).unpadder()
        digest = hashlib.sha256()
        with open(out_path, "wb") as out:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                pt = unpadder.update(dec.update(chunk))
                digest.update(pt)
                out.write(pt)
            pt = unpadder.update(dec.finalize()) + unpadder.finalize()
            digest.update(pt)
            out.write(pt)
    if verify and info.file_digest and digest.digest() != info.file_digest:
        raise ValueError("FileDigest mismatch after decryption")


# --------------------------------------------------------------------------- public API

@dataclass
class BuildResult:
    output: Path
    metadata: PackageMetadata
    file_count: int
    inner_zip_sha256: str
    files: list = field(default_factory=list)


def create_intunewin(
    source_dir: Path,
    setup_file: str,
    output: Path,
    name: Optional[str] = None,
    msi: Optional[MsiInfo] = None,
) -> BuildResult:
    source_dir = Path(source_dir)
    output = Path(output)
    if not (source_dir / setup_file).is_file():
        raise FileNotFoundError(f"setup file {setup_file!r} not found in {source_dir}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="iwm-") as td:
        td = Path(td)
        inner = td / "inner.zip"
        n = zip_folder(source_dir, inner)
        payload = td / INNER_NAME
        info = encrypt_file(inner, payload)
        meta = PackageMetadata(
            name=name or setup_file,
            setup_file=setup_file,
            unencrypted_content_size=inner.stat().st_size,
            encryption=info,
            msi=msi,
        )
        with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as zf:
            zf.write(payload, PAYLOAD_PATH)
            zf.writestr(METADATA_PATH, meta.to_xml(), compress_type=zipfile.ZIP_DEFLATED)
        files = [p.relative_to(source_dir).as_posix() for p in _iter_files(source_dir)]
        return BuildResult(output, meta, n, info.file_digest.hex(), files)


def read_metadata(intunewin: Path) -> PackageMetadata:
    with zipfile.ZipFile(intunewin) as zf:
        return PackageMetadata.from_xml(zf.read(METADATA_PATH))


def extract_intunewin(intunewin: Path, out_dir: Path, verify: bool = True) -> PackageMetadata:
    """Decrypt an .intunewin into out_dir (the original source folder contents)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(intunewin) as zf, tempfile.TemporaryDirectory(prefix="iwm-") as td:
        meta = PackageMetadata.from_xml(zf.read(METADATA_PATH))
        enc = Path(td) / "payload.bin"
        with zf.open(PAYLOAD_PATH) as src, open(enc, "wb") as dst:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
        inner = Path(td) / "inner.zip"
        decrypt_file(enc, inner, meta.encryption, verify=verify)
        with zipfile.ZipFile(inner) as iz:
            iz.extractall(out_dir)
    return meta


def decrypt_to_zip(intunewin: Path, zip_out: Path, verify: bool = True) -> PackageMetadata:
    """Decrypt an .intunewin to the plain inner zip (handy for shipping to a VM)."""
    with zipfile.ZipFile(intunewin) as zf, tempfile.TemporaryDirectory(prefix="iwm-") as td:
        meta = PackageMetadata.from_xml(zf.read(METADATA_PATH))
        enc = Path(td) / "payload.bin"
        with zf.open(PAYLOAD_PATH) as src, open(enc, "wb") as dst:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
        decrypt_file(enc, Path(zip_out), meta.encryption, verify=verify)
    return meta


def list_contents(intunewin: Path) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="iwm-") as td:
        inner = Path(td) / "inner.zip"
        decrypt_to_zip(intunewin, inner)
        with zipfile.ZipFile(inner) as iz:
            return [{"name": i.filename, "size": i.file_size} for i in iz.infolist()]
