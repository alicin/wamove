"""Decrypt a crypt14 database using its matching WhatsApp-provided key file."""
from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import crypt15


class Crypt14Error(Exception):
    pass


# WhatsApp also stores the same 131-byte key as a Java-serialized byte array.
_JAVA_PREFIX = bytes.fromhex("aced0005757200025b42acf317f8060854e0020000787000000083")


def parse_key(data: bytes) -> bytes:
    if data.startswith(_JAVA_PREFIX) and len(data) == len(_JAVA_PREFIX) + 131:
        data = data[len(_JAVA_PREFIX):]
    if len(data) != 131 or data[:2] != b"\x00\x01" or data[2] not in (1, 2, 3):
        raise Crypt14Error("expected a matching WhatsApp crypt14 key file (131 bytes or serialized byte array)")
    if hashlib.sha256(data[35:51]).digest() != data[51:83] or any(data[83:99]):
        raise Crypt14Error("invalid crypt14 key file")
    return data[99:131]


def decrypt(data: bytes, keyfile: bytes) -> bytes:
    key = parse_key(keyfile)
    try:
        offset, ivs = crypt15._header(data)
    except crypt15.Crypt15Error as exc:
        raise Crypt14Error("invalid crypt14 header") from exc
    body = data[offset:]
    for iv in ivs:
        for trailer in (0, 16):
            if len(body) <= trailer + 16:
                continue
            try:
                compressed = AESGCM(key).decrypt(iv, body[:len(body) - trailer], None)
                plain = zlib.decompress(compressed)
            except (InvalidTag, zlib.error):
                continue
            if plain.startswith(crypt15.SQLITE_MAGIC):
                return plain
    raise Crypt14Error("key does not match the crypt14 backup or the backup is damaged")


def decrypt_file(src: Path, dst: Path, keyfile: Path) -> None:
    plain = decrypt(src.read_bytes(), keyfile.read_bytes())
    tmp = dst.with_name(dst.name + ".part")
    if tmp.exists():
        raise Crypt14Error(f"temporary output already exists: {tmp}")
    try:
        with tmp.open("xb") as out:
            out.write(plain)
        tmp.chmod(0o600)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
