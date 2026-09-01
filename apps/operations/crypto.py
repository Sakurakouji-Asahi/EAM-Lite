from __future__ import annotations

import base64
import hashlib
import os
import struct
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


MAGIC = b"EAMLITEBK1"
SALT_SIZE = 16
NONCE_SIZE = 12
TAG_SIZE = 16
KDF_ITERATIONS = 600_000
CHUNK_SIZE = 1024 * 1024


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    if not isinstance(passphrase, str) or len(passphrase) < 12:
        raise ValueError("备份口令至少需要 12 个字符。")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def encrypt_file(
    source: Path,
    destination: Path,
    *,
    passphrase: str,
    salt: bytes | None = None,
    iterations: int = KDF_ITERATIONS,
) -> dict:
    source = Path(source)
    destination = Path(destination)
    salt = os.urandom(SALT_SIZE) if salt is None else bytes(salt)
    if len(salt) != SALT_SIZE:
        raise ValueError("备份加密盐长度非法。")
    if iterations < 100_000 or iterations > 5_000_000:
        raise ValueError("备份 KDF 迭代参数非法。")
    nonce = os.urandom(NONCE_SIZE)
    key = _derive_key(passphrase, salt, iterations)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        output_file.write(MAGIC)
        output_file.write(struct.pack(">I", iterations))
        output_file.write(salt)
        output_file.write(nonce)
        for chunk in iter(lambda: input_file.read(CHUNK_SIZE), b""):
            output_file.write(encryptor.update(chunk))
        output_file.write(encryptor.finalize())
        output_file.write(encryptor.tag)
    return {
        "format": MAGIC.decode("ascii"),
        "cipher": "AES-256-GCM",
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": iterations,
        "salt": base64.b64encode(salt).decode("ascii"),
        "salt_bytes": SALT_SIZE,
    }


def encryption_metadata(source: Path) -> dict:
    source = Path(source)
    minimum = len(MAGIC) + 4 + SALT_SIZE + NONCE_SIZE + TAG_SIZE
    if not source.is_file() or source.stat().st_size < minimum:
        raise ValueError("备份包过短或已损坏。")
    with source.open("rb") as input_file:
        if input_file.read(len(MAGIC)) != MAGIC:
            raise ValueError("不是受支持的 EAM-Lite 加密备份包。")
        iterations = struct.unpack(">I", input_file.read(4))[0]
        salt = input_file.read(SALT_SIZE)
    return {
        "format": MAGIC.decode("ascii"),
        "cipher": "AES-256-GCM",
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": iterations,
        "salt": base64.b64encode(salt).decode("ascii"),
        "salt_bytes": SALT_SIZE,
    }


def decrypt_file(source: Path, destination: Path, *, passphrase: str) -> None:
    source = Path(source)
    destination = Path(destination)
    minimum = len(MAGIC) + 4 + SALT_SIZE + NONCE_SIZE + TAG_SIZE
    if source.stat().st_size < minimum:
        raise ValueError("备份包过短或已损坏。")
    with source.open("rb") as input_file:
        if input_file.read(len(MAGIC)) != MAGIC:
            raise ValueError("不是受支持的 EAM-Lite 加密备份包。")
        iterations = struct.unpack(">I", input_file.read(4))[0]
        if iterations < 100_000 or iterations > 5_000_000:
            raise ValueError("备份包 KDF 参数非法。")
        salt = input_file.read(SALT_SIZE)
        nonce = input_file.read(NONCE_SIZE)
        ciphertext_length = source.stat().st_size - minimum
        input_file.seek(-TAG_SIZE, os.SEEK_END)
        tag = input_file.read(TAG_SIZE)
        input_file.seek(len(MAGIC) + 4 + SALT_SIZE + NONCE_SIZE)
        key = _derive_key(passphrase, salt, iterations)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        destination.parent.mkdir(parents=True, exist_ok=True)
        remaining = ciphertext_length
        with destination.open("wb") as output_file:
            while remaining:
                chunk = input_file.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise ValueError("备份包密文长度不完整。")
                remaining -= len(chunk)
                output_file.write(decryptor.update(chunk))
            output_file.write(decryptor.finalize())
