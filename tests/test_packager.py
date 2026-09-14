"""Run with: python3 -m pytest tests/  (or python3 tests/test_packager.py)"""
import hashlib, os, sys, tempfile, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from iwm import packager
from iwm.vm.unattend import autounattend_xml
import xml.etree.ElementTree as ET


def test_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"; src.mkdir()
        (src / "setup.exe").write_bytes(os.urandom(5 * 1024 * 1024 + 13))
        (src / "sub").mkdir(); (src / "sub" / "config.json").write_text('{"a":1}')
        out = Path(td) / "pkg.intunewin"
        res = packager.create_intunewin(src, "setup.exe", out)
        assert res.file_count == 2
        meta = packager.read_metadata(out)
        assert meta.setup_file == "setup.exe" and meta.unencrypted_content_size > 5 * 1024 * 1024
        with zipfile.ZipFile(out) as z:
            assert sorted(z.namelist()) == sorted([packager.PAYLOAD_PATH, packager.METADATA_PATH])
            assert z.getinfo(packager.PAYLOAD_PATH).file_size == meta.unencrypted_content_size + 48 + (16 - meta.unencrypted_content_size % 16)
        dst = Path(td) / "out"
        packager.extract_intunewin(out, dst)
        assert (dst / "setup.exe").read_bytes() == (src / "setup.exe").read_bytes()
        assert (dst / "sub" / "config.json").read_text() == '{"a":1}'
        # tamper with the encrypted payload -> HMAC failure (bypassing the outer zip CRC)
        with zipfile.ZipFile(out) as z:
            payload = bytearray(z.read(packager.PAYLOAD_PATH))
        payload[100] ^= 0xFF
        enc = Path(td) / "payload.bin"; enc.write_bytes(payload)
        try:
            packager.decrypt_file(enc, Path(td) / "plain.zip", meta.encryption)
            raise AssertionError("tampered payload was accepted")
        except ValueError as e:
            assert "HMAC" in str(e)


def test_unattend_is_valid_xml():
    for arch in ("arm64", "x64"):
        root = ET.fromstring(autounattend_xml(arch=arch, password="p<a&b>"))
        assert root.tag.endswith("unattend")
        assert ("arm64" if arch == "arm64" else "amd64") in ET.tostring(root).decode()


if __name__ == "__main__":
    test_roundtrip(); test_unattend_is_valid_xml(); print("ok")
