#!/usr/bin/env python3
"""Pack + sign the Paprika Bridge extension into a CRX3 (.crx).

Why this exists
---------------
``paprika-bridge`` is normally installed "load unpacked" from the .zip
the hub builds on the fly. That requires Chrome's Developer mode and
gives a *random* extension ID on every machine. A **signed .crx** gives:

  * a STABLE extension ID (derived from the signing key), and
  * the ability to **force-install** it across operator machines via an
    enterprise policy (``ExtensionInstallForcelist`` + the hub's
    ``updates.xml``) -- no Developer-mode nag, auto-updates.

This is the same model as ``paprika-agent.crx`` (a pre-signed, committed
binary the hub serves statically). We pre-build because the hub image
ships no crypto library and the .34 deploy-watcher does NOT rebuild
images -- a committed .crx deploys as a plain ``server/`` file and is
byte-identical on every hub (so the ID is fleet-wide consistent).

What it does
------------
1. Load (or generate) an RSA-2048 signing key.
2. Write the matching PUBLIC key into ``paprika-bridge/manifest.json``'s
   ``key`` field so a *load-unpacked* install pins the SAME stable ID as
   the .crx (idempotent -- only rewrites when it differs).
3. Zip the extension directory and wrap it in a CRX3 container signed
   with SHA256-RSA (PKCS#1 v1.5), the format Chrome 75+ requires.
4. Write ``server/web/extensions/paprika-bridge.crx`` and print the
   extension ID + the exact policy string to force-install it.

The PRIVATE key (``paprika-bridge.pem``) is a SECRET: keep it OUT of
git and off the hubs (anyone with it can ship an update under this ID).
It is only needed to re-pack after editing the extension source.

No Chrome, no protobuf library, no openssl shell-out -- just
``cryptography`` (already a dev dependency).

Usage
-----
    python scripts/pack_paprika_bridge_crx.py
    python scripts/pack_paprika_bridge_crx.py --key /path/to/keep/paprika-bridge.pem

Re-run whenever you change anything under
``server/web/extensions/paprika-bridge/``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import struct
import sys
import zipfile
from pathlib import Path

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
except ImportError:
    sys.exit(
        "this script needs the 'cryptography' package:\n"
        "    pip install cryptography"
    )

REPO = Path(__file__).resolve().parents[1]
DEFAULT_EXT_DIR = REPO / "server" / "web" / "extensions" / "paprika-bridge"
DEFAULT_OUT = REPO / "server" / "web" / "extensions" / "paprika-bridge.crx"
DEFAULT_KEY = REPO / "paprika-bridge.pem"


# ---- minimal protobuf wire encoding (only what CRX3 needs) ---------------

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field_bytes(field_no: int, data: bytes) -> bytes:
    """Encode one length-delimited (wire type 2) field: bytes or a
    nested message."""
    return _varint((field_no << 3) | 2) + _varint(len(data)) + data


# ---- key handling --------------------------------------------------------

def load_or_create_key(path: Path):
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            sys.exit(f"{path} is not an RSA private key")
        return key, False
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows / unsupported fs -- best effort
    return key, True


def spki_der(key) -> bytes:
    """DER-encoded SubjectPublicKeyInfo -- what Chrome hashes for the ID
    and what goes in the manifest ``key`` field."""
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def extension_id(spki: bytes) -> str:
    """Chrome's 32-char a-p extension ID: first 128 bits of
    SHA256(SPKI), each nibble mapped 0..15 -> 'a'..'p'."""
    digest = hashlib.sha256(spki).digest()
    return "".join(
        chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0x0F))
        for b in digest[:16]
    )


# ---- manifest key injection ---------------------------------------------

def ensure_manifest_key(ext_dir: Path, key_b64: str) -> bool:
    """Pin the manifest ``key`` so load-unpacked gets the same stable ID
    as the .crx. Returns True when the file was changed."""
    mpath = ext_dir / "manifest.json"
    manifest = json.loads(mpath.read_text("utf-8"))
    if manifest.get("key") == key_b64:
        return False
    # Insert ``key`` right after manifest_version for tidiness.
    new = {}
    for k, v in manifest.items():
        new[k] = v
        if k == "manifest_version":
            new["key"] = key_b64
    if "key" not in new:
        new["key"] = key_b64
    mpath.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return True


# ---- zip + crx -----------------------------------------------------------

def build_zip(ext_dir: Path) -> bytes:
    """Zip the unpacked extension dir, manifest.json at the top. Mirrors
    the hub's on-the-fly zip so .crx and .zip carry identical bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(ext_dir.rglob("*")):
            if p.is_file():
                z.write(p, arcname=str(p.relative_to(ext_dir)).replace("\\", "/"))
    return buf.getvalue()


def build_crx3(zip_bytes: bytes, key, spki: bytes) -> bytes:
    crx_id = hashlib.sha256(spki).digest()[:16]

    # SignedData { crx_id = field 1 }
    signed_header_data = _field_bytes(1, crx_id)

    # Signature is over: "CRX3 SignedData\x00" + LE32(len) + signed_header
    # + the zip payload.
    sign_input = (
        b"CRX3 SignedData\x00"
        + struct.pack("<I", len(signed_header_data))
        + signed_header_data
        + zip_bytes
    )
    signature = key.sign(sign_input, padding.PKCS1v15(), hashes.SHA256())

    # AsymmetricKeyProof { public_key = field 1, signature = field 2 }
    proof = _field_bytes(1, spki) + _field_bytes(2, signature)
    # CrxFileHeader { sha256_with_rsa = field 2 (repeated), signed_header_data
    #                 = field 10000 }
    header = _field_bytes(2, proof) + _field_bytes(10000, signed_header_data)

    return (
        b"Cr24"
        + struct.pack("<I", 3)              # CRX version 3
        + struct.pack("<I", len(header))
        + header
        + zip_bytes
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Pack + sign paprika-bridge into a CRX3.")
    ap.add_argument("--key", type=Path, default=DEFAULT_KEY,
                    help=f"signing key PEM (default: {DEFAULT_KEY}; generated if absent)")
    ap.add_argument("--ext-dir", type=Path, default=DEFAULT_EXT_DIR,
                    help=f"unpacked extension dir (default: {DEFAULT_EXT_DIR})")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"output .crx path (default: {DEFAULT_OUT})")
    ap.add_argument("--no-manifest-key", action="store_true",
                    help="don't write the public key into manifest.json")
    args = ap.parse_args()

    if not (args.ext_dir / "manifest.json").exists():
        sys.exit(f"no manifest.json under {args.ext_dir}")

    key, created = load_or_create_key(args.key)
    spki = spki_der(key)
    key_b64 = base64.b64encode(spki).decode("ascii")
    ext_id = extension_id(spki)

    changed = False
    if not args.no_manifest_key:
        changed = ensure_manifest_key(args.ext_dir, key_b64)

    zip_bytes = build_zip(args.ext_dir)
    crx = build_crx3(zip_bytes, key, spki)
    args.out.write_bytes(crx)

    print(f"{'generated NEW' if created else 'loaded'} signing key: {args.key}")
    if created:
        print("  ^ SECRET: keep this out of git / off the hubs; back it up to your vault.")
    if changed:
        print(f"updated manifest.json 'key' field in {args.ext_dir / 'manifest.json'}")
    print(f"wrote .crx          : {args.out}  ({len(crx):,} bytes)")
    print(f"extension ID        : {ext_id}")
    print()
    print("Force-install policy (ExtensionInstallForcelist value):")
    print(f"  {ext_id};http://<your-hub>/profiles/extension/paprika-bridge/updates.xml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
