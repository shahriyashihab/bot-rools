# bot/services/session_formats.py
"""Session format generators for the per-format success reporting.

Produces the three additional session formats the admin panel can enable for
SUCCESS accounts:

- TXT:  a plain-text file with the Pyrogram session string + account info
        (the widely used "session txt" format; consumed by string-session
        tools and account checkers).
- TData: a Telegram Desktop ``tdata`` folder generated from the account's
        session (standard Telegram Desktop DBI format, no passcode).

Ban safety:
- Generation is a LOCAL file operation only (no Telegram API calls), so it can
  never get an account banned.
- Every generator is fail-safe: on any error it returns ``None`` and logs the
  reason; the caller simply skips that format's report.
"""
import base64
import hashlib
import io
import logging
import os
import shutil
import sqlite3
import struct
import time
import uuid
import zipfile
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _zip_folder(folder: str) -> Optional[str]:
    """Zip a folder into a ``.zip`` next to it (with a safe arcname).

    Used to bundle a generated Telegram Desktop ``tdata`` folder into a single
    file so it can be embedded inside the outer export ZIP.
    """
    try:
        zip_path = folder.rstrip("/\\") + ".zip"
        if os.path.exists(zip_path):
            os.remove(zip_path)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            base = os.path.basename(folder.rstrip("/\\"))
            for dirpath, dirnames, filenames in os.walk(folder):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    arc = os.path.join(base, os.path.relpath(full, folder)).replace("\\", "/")
                    zf.write(full, arc)
        return zip_path
    except Exception as e:
        logger.warning("zip folder failed for %s: %s", folder, e)
        return None


def generate_all_session_formats(
    phone: str,
    session_string: str,
    user_id,
    api_id,
    api_hash,
    session_id: str = "",
    out_dir: str = "export_sessions",
    first_name: str = "",
    telethon_file: str = "",
    pyrogram_file: str = "",
    formats: Optional[set] = None,
) -> Dict[str, str]:
    """Generate session formats for one account into ``out_dir``.

    Produces (when possible):
      - ``telethon``: a Telethon v7 SQLite ``.session`` file
      - ``pyrogram``: a valid Pyrogram SQLite ``.session`` file
      - ``tdata``:    a Telegram Desktop ``tdata`` folder, zipped
      - ``txt``:      a plain-text session file

    ``formats`` optionally limits generation to a subset (e.g. ``{"telethon"}``)
    so the admin panel can export ONE chosen format only. ``None`` (default)
    generates every format.

    This is the single source for the admin export (Session Management and
    Country Based Export). Before this fix the admin export only shipped the
    Telethon file and silently dropped pyrogram / tdata / txt.

    Returns a dict ``format_key -> file path``; formats that could not be
    produced are simply absent (fail-safe, never raises).
    """
    results: Dict[str, str] = {}
    if not phone:
        phone = f"unknown_{session_id or user_id or '0'}"
    safe = str(phone).replace('+', '').replace(' ', '')
    os.makedirs(out_dir, exist_ok=True)
    try:
        user_id = int(user_id or 0) or None
    except (TypeError, ValueError):
        user_id = None

    if formats is None:
        formats = {"telethon", "pyrogram", "tdata", "txt"}
    else:
        formats = set(formats)

    # --- telethon -----------------------------------------------------
    if "telethon" in formats:
        try:
            tel_path = os.path.join(out_dir, f"{safe}_telethon.session")
            if telethon_file and os.path.exists(telethon_file):
                shutil.copy2(telethon_file, tel_path)
            else:
                from bot.verification.telethon_export import session_string_to_telethon
                ok = session_string_to_telethon(
                    session_string, tel_path,
                    user_id=user_id, phone=phone,
                    first_name=first_name, access_hash=None,
                )
                if not ok:
                    tel_path = None
            if tel_path and os.path.exists(tel_path):
                results["telethon"] = tel_path
        except Exception as e:
            logger.warning("export telethon failed for %s: %s", phone, e)

    # --- pyrogram -----------------------------------------------------
    if "pyrogram" in formats:
        try:
            pyro_path = os.path.join(out_dir, f"{safe}_pyrogram.session")
            generated = generate_pyrogram_file(session_string, pyro_path,
                                               user_id=user_id, phone=phone)
            if generated and os.path.exists(generated):
                results["pyrogram"] = generated
        except Exception as e:
            logger.warning("export pyrogram failed for %s: %s", phone, e)

    # --- tdata --------------------------------------------------------
    if "tdata" in formats:
        try:
            folder = generate_tdata_folder(phone, session_string, user_id, out_dir=out_dir)
            if folder and os.path.isdir(folder):
                tdata_zip = _zip_folder(folder)
                if tdata_zip and os.path.exists(tdata_zip):
                    results["tdata"] = tdata_zip
                shutil.rmtree(folder, ignore_errors=True)
        except Exception as e:
            logger.warning("export tdata failed for %s: %s", phone, e)

    # --- txt ----------------------------------------------------------
    if "txt" in formats:
        try:
            txt_path = generate_txt_file(phone, session_string, user_id,
                                         api_id, api_hash,
                                         first_name=first_name, out_dir=out_dir)
            if txt_path and os.path.exists(txt_path):
                results["txt"] = txt_path
        except Exception as e:
            logger.warning("export txt failed for %s: %s", phone, e)

    return results

# ------------------------------------------------------------------
# Session string parsing (shared)
# ------------------------------------------------------------------

def parse_pyrogram_session(session_string: str):
    """Parse a Pyrogram session string into its components.

    Returns a dict with ``dc_id``, ``api_id``, ``test_mode``, ``auth_key``
    (256 bytes), ``user_id``, ``is_bot`` or ``None`` when the string is not a
    valid Pyrogram session.
    """
    if not session_string or not isinstance(session_string, str):
        return None
    try:
        decoded = base64.urlsafe_b64decode(session_string + "=" * (-len(session_string) % 4))
    except Exception as e:
        logger.warning("parse_pyrogram_session: bad base64: %s", e)
        return None
    if not decoded:
        return None
    dlen = len(decoded)
    dc_id = decoded[0]
    api_id = 0
    test_mode = False
    auth_key = None
    user_id = 0
    is_bot = False
    if dlen >= 271:
        dc_id = decoded[0]
        api_id = struct.unpack('>I', decoded[1:5])[0]
        test_mode = bool(decoded[5])
        auth_key = decoded[6:262]
        user_id = struct.unpack('>Q', decoded[262:270])[0]
        is_bot = bool(decoded[270])
    elif dlen == 267:
        dc_id, test_mode, auth_key, user_id, is_bot = struct.unpack(">B?256sQ?", decoded)
    elif dlen == 265:
        dc_id = decoded[0]
        auth_key = decoded[1:257]
        user_id = struct.unpack('<q', decoded[257:265])[0]
    elif dlen == 261:
        dc_id = decoded[0]
        auth_key = decoded[1:257]
        user_id = struct.unpack('<i', decoded[257:261])[0]
    else:
        # Unknown layout: try the two common auth_key offsets.
        if dlen >= 262:
            auth_key = decoded[6:262]
        elif dlen >= 257:
            auth_key = decoded[1:257]
        else:
            return None
    if not auth_key or len(auth_key) != 256:
        logger.warning("parse_pyrogram_session: invalid auth_key length %s",
                       len(auth_key) if auth_key else 0)
        return None
    return {
        "dc_id": dc_id,
        "api_id": api_id,
        "test_mode": test_mode,
        "auth_key": auth_key,
        "user_id": user_id,
        "is_bot": is_bot,
    }


def resolve_session_api(session_string: str, fallback_api_id=None, fallback_api_hash=None):
    """Resolve the API id/hash that actually created a session.

    The Pyrogram session string embeds its own ``api_id`` (bytes 1-5,
    big-endian). We match it against the configured API pool (primary +
    ``EXTRA_APIs``) so the delivered JSON/TXT reports the credentials that
    created the session, not just the primary API (previously the JSON always
    stamped ``config.API_ID``/``config.API_HASH`` even when the session was
    created with a rotated API).

    Returns ``(api_id, api_hash)``:
      - the matching configured pair when the embedded api_id is known,
      - the explicit ``fallback_api_id``/``fallback_api_hash`` pair when the
        api_id is unparseable or unknown and a fallback was given,
      - ``config.API_ID``/``config.API_HASH`` otherwise.

    Note: the auth key is API-independent, so any configured pair still lets a
    buyer connect with the session string; this only makes the reported
    credentials accurate.
    """
    from bot import config

    parsed_api_id = None
    try:
        parsed = parse_pyrogram_session(session_string or "")
        if parsed and parsed.get("api_id"):
            parsed_api_id = int(parsed["api_id"])
    except Exception:
        parsed_api_id = None

    known = []
    if fallback_api_id and fallback_api_hash:
        try:
            known.append((int(fallback_api_id), fallback_api_hash))
        except (TypeError, ValueError):
            pass
    known.append((config.API_ID, config.API_HASH))
    for extra_id, extra_hash in (config.EXTRA_APIS or []):
        try:
            known.append((int(extra_id), extra_hash))
        except (TypeError, ValueError):
            pass
    # Runtime-added APIs (admin panel) are also valid match targets, so a
    # session created with a rotated runtime API reports the exact
    # credentials that created it.
    try:
        from bot.utils.api_rotation import get_all_api_credentials
        for cred in get_all_api_credentials():
            try:
                known.append((int(cred.get("api_id")), cred.get("api_hash")))
            except (TypeError, ValueError):
                continue
    except Exception:
        pass

    if parsed_api_id:
        for api_id, api_hash in known:
            if int(api_id) == parsed_api_id:
                return int(api_id), api_hash

    if fallback_api_id and fallback_api_hash:
        try:
            return int(fallback_api_id), fallback_api_hash
        except (TypeError, ValueError):
            pass
    return config.API_ID, config.API_HASH


# ------------------------------------------------------------------
# TXT format
# ------------------------------------------------------------------

def build_txt_content(session_string: str, phone: str, user_id, api_id, api_hash,
                      first_name: str = "", dc_id=None) -> str:
    """Build the content of a session .txt file.

    Format (commonly used by session checkers / account tools):
        phone: <phone>
        user_id: <id>
        dc_id: <dc>
        api_id: <api_id>
        api_hash: <api_hash>
        session: <string>

    The ``api_id``/``api_hash`` are resolved from the session string (matching
    the configured API pool) so the TXT always reports the credentials that
    actually created the session, not just the primary API.
    """
    # Parse the session once so both dc_id AND the authoritative account
    # user_id come from the session string (DEFENSE-IN-DEPTH: a caller that
    # passes the operator's id must never stamp the wrong account identity
    # into the delivered TXT file).
    parsed = parse_pyrogram_session(session_string)
    if dc_id is None:
        dc_id = parsed["dc_id"] if parsed else ""
    if parsed and parsed.get("user_id"):
        parsed_uid = int(parsed["user_id"])
        try:
            caller_uid = int(user_id or 0)
        except (TypeError, ValueError):
            caller_uid = 0
        if not caller_uid or caller_uid != parsed_uid:
            if caller_uid:
                logger.warning(
                    "build_txt_content: caller user_id=%s conflicts with the "
                    "session's real user_id=%s for %s - using the session's "
                    "REAL account id (wrong identity would have been stamped)",
                    caller_uid, parsed_uid, phone,
                )
            user_id = parsed_uid
    # Resolve the REAL api_id/api_hash that created this session (the caller
    # may pass the primary config API; the session string embeds its own id).
    _api_id, _api_hash = resolve_session_api(session_string, api_id, api_hash)
    lines = [
        f"phone: {phone}",
        f"user_id: {user_id}",
        f"dc_id: {dc_id}",
        f"api_id: {_api_id}",
        f"api_hash: {_api_hash}",
        f"session: {session_string}",
    ]
    if first_name:
        lines.insert(1, f"name: {first_name}")
    return "\n".join(lines) + "\n"


def generate_txt_file(phone: str, session_string: str, user_id, api_id, api_hash,
                      first_name: str = "", out_dir: str = "verified_sessions",
                      suffix: str = "") -> str:
    """Write a session .txt file. Returns the file path or ``None`` on error."""
    try:
        os.makedirs(out_dir, exist_ok=True)
        safe_phone = str(phone).replace('+', '').replace(' ', '')
        name = f"{safe_phone}{('_' + suffix) if suffix else ''}.txt"
        path = os.path.join(out_dir, name)
        content = build_txt_content(
            session_string, phone, user_id, api_id, api_hash, first_name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("TXT session written: %s", path)
        return path
    except Exception as e:
        logger.error("generate_txt_file failed for %s: %s", phone, e)
        return None


# ------------------------------------------------------------------
# TData format (Telegram Desktop)
# ------------------------------------------------------------------
# Constants from Telegram Desktop storage (storage_databasetdata.cpp /
# localstorage.cpp). The folder is created with an empty local passcode.
_KD_MAGIC = 0x4997B32B        # KdbData unencrypted (legacy)
_KD_MAGIC_ENCRYPTED = 0x4997B32C  # KdbData encrypted
_KD_VERSION = 0x12000
_TDF_MAGIC = 0x234B2E0D
_TDF_VERSION = 0x12000


def _pad16(data: bytes) -> bytes:
    """PKCS#7 pad to a multiple of 16 bytes."""
    pad_len = 16 - (len(data) % 16)
    return data + bytes([pad_len]) * pad_len


# ------------------------------------------------------------------
# Pure-Python AES-256-CBC (no third-party dependency).
#
# Tdata generation MUST work even when the optional ``cryptography`` package is
# not importable in the deployment (Termux / minimal venvs). Earlier the tdata
# helpers imported ``cryptography`` lazily inside the function, so when that
# package was missing ONLY tdata generation threw and failed silently while
# telethon/pyrogram/txt (which don't need it) kept working. The helpers below
# prefer ``cryptography`` for speed but always fall back to this implementation,
# so tdata is generated regardless of the environment.
_AES_SBOX = bytes([
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
])


def _aes_xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a ^= 0x11b
    return a & 0xff


def _aes_gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _aes_xtime(a)
        b >>= 1
    return p & 0xff


def _aes_key_expansion(key: bytes):
    Nk = len(key) // 4
    Nr = Nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(Nk)]
    rcon = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36, 0x6c, 0xd8, 0xab, 0x4d, 0x9a]
    i = Nk
    while len(words) < 4 * (Nr + 1):
        temp = list(words[i - 1])
        if i % Nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_AES_SBOX[b] for b in temp]
            temp[0] ^= rcon[i // Nk - 1]
        elif Nk > 6 and i % Nk == 4:
            temp = [_AES_SBOX[b] for b in temp]
        prev = words[i - Nk]
        words.append([temp[j] ^ prev[j] for j in range(4)])
        i += 1
    round_keys = []
    for r in range(Nr + 1):
        rk = words[4 * r] + words[4 * r + 1] + words[4 * r + 2] + words[4 * r + 3]
        round_keys.append(bytes(rk))
    return round_keys, Nr


def _aes_encrypt_block(block: bytes, round_keys, Nr: int) -> bytes:
    # AES state is stored COLUMN-major in the 16-byte block
    # (state[r][c] = s[r + 4*c], per FIPS-197). The old code indexed the
    # state row-major (s[r*4 + c]) in ShiftRows, which made every encrypted
    # byte wrong and produced tdata folders Telegram Desktop cannot open.
    s = list(block)

    def add_round_key(rk):
        for i in range(16):
            s[i] ^= rk[i]

    add_round_key(round_keys[0])
    for rnd in range(1, Nr):
        # SubBytes
        for i in range(16):
            s[i] = _AES_SBOX[s[i]]
        # ShiftRows (row r shifted LEFT by r: new[(r, c)] = old[(r, (c+r)%4)])
        ns = s[:]
        for r in range(4):
            for c in range(4):
                ns[r + 4 * c] = s[r + 4 * ((c + r) % 4)]
        s = ns
        # MixColumns (per column). The AES state is column-major
        # (state[r][c] = s[r + 4c]), so column c occupies bytes 4c..4c+3.
        # The old code read s[c], s[4+c], s[8+c], s[12+c] (a transposed
        # layout) which produced wrong ciphertext and invalid tdata.
        for c in range(4):
            base = 4 * c
            a0, a1, a2, a3 = s[base], s[base + 1], s[base + 2], s[base + 3]
            s[base] = _aes_gmul(a0, 2) ^ _aes_gmul(a1, 3) ^ a2 ^ a3
            s[base + 1] = a0 ^ _aes_gmul(a1, 2) ^ _aes_gmul(a2, 3) ^ a3
            s[base + 2] = a0 ^ a1 ^ _aes_gmul(a2, 2) ^ _aes_gmul(a3, 3)
            s[base + 3] = _aes_gmul(a0, 3) ^ a1 ^ a2 ^ _aes_gmul(a3, 2)
        add_round_key(round_keys[rnd])
    # Final round (no MixColumns)
    for i in range(16):
        s[i] = _AES_SBOX[s[i]]
    ns = s[:]
    for r in range(4):
        for c in range(4):
            ns[r + 4 * c] = s[r + 4 * ((c + r) % 4)]
    s = ns
    add_round_key(round_keys[Nr])
    return bytes(s)


def _aes_cbc_encrypt_pure(data: bytes, key: bytes, iv: bytes, pad: bool = True) -> bytes:
    if pad:
        pad_len = 16 - (len(data) % 16)
        data = data + bytes([pad_len]) * pad_len
    else:
        if len(data) % 16 != 0:
            raise ValueError("aesEncryptLocal requires block-aligned input")
    round_keys, Nr = _aes_key_expansion(key)
    prev = (iv + b"\x00" * 16)[:16]
    out = b""
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        enc = _aes_encrypt_block(block, round_keys, Nr)
        out += enc
        prev = enc
    return out


def _aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-256-CBC encryption (PKCS#7 padding).

    Uses ``cryptography`` when available, otherwise a self-contained pure-Python
    implementation so Tdata generation never depends on that package being
    installed.
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as _cpadding
        padder = _cpadding.PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC((iv + b"\x00" * 16)[:16]))
        enc = cipher.encryptor()
        return enc.update(padded) + enc.finalize()
    except Exception:
        return _aes_cbc_encrypt_pure(data, key, iv, pad=True)


def _aes_cbc_encrypt_nopad(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-256-CBC encryption WITHOUT padding.

    Telegram Desktop's local-key encryption (``aesEncryptLocal``) operates on
    fixed-size buffers (32 or 256 bytes), both exact multiples of the AES
    block size, so no padding is applied. Adding PKCS#7 padding here would make
    the file 16 bytes longer than Telegram Desktop expects and the folder would
    not open. Prefers ``cryptography``; falls back to pure-Python.
    """
    if len(data) % 16 != 0:
        raise ValueError("aesEncryptLocal requires block-aligned input")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        cipher = Cipher(algorithms.AES(key), modes.CBC((iv + b"\x00" * 16)[:16]))
        enc = cipher.encryptor()
        return enc.update(data) + enc.finalize()
    except Exception:
        return _aes_cbc_encrypt_pure(data, key, iv, pad=False)


def _write_tdf_data(user_id: int, dc_id: int, auth_key: bytes,
                    session_id: int, account_name: str) -> bytes:
    """Serialize the TdfData (accounts + session) payload."""
    buf = io.BytesIO()

    def w32(v):
        # Telegram user IDs regularly exceed int32 (2^31-1). Packing with "<i"
        # then raises struct.error for those accounts, which made the whole
        # TData folder generation fail and the report silently fall back to a
        # text message or nothing at all. Telegram Desktop stores these fields
        # as uint32, so mask + "<I" (wraps like the real client) is correct.
        buf.write(struct.pack("<I", v & 0xFFFFFFFF))

    def w64(v):
        buf.write(struct.pack("<q", v))

    def wbytes(b):
        b = bytes(b)
        w32(len(b))
        buf.write(b)

    def wstr(s):
        wbytes(s.encode("utf-8"))

    # TdfData header
    w32(_TDF_MAGIC)
    w32(_TDF_VERSION)
    w32(1)  # account count

    # Account
    w32(user_id)
    w32(1)  # session count
    wstr(account_name or f"user{user_id}")
    wbytes(hashlib.sha256((account_name or f"user{user_id}").encode("utf-8")).digest())
    w64(session_id)  # main session id

    # Session
    w64(session_id)
    wbytes(auth_key)      # 256-byte auth key
    w64(user_id)
    w32(dc_id)
    wbytes(b"")           # server salt (empty)

    return buf.getvalue()


# ------------------------------------------------------------------
# Pyrogram format (VALID SQLite .session file)
# ------------------------------------------------------------------
# IMPORTANT: A Pyrogram ``.session`` file is a SQLite database using the
# exact schema from ``pyrogram.storage.sqlite_storage.SCHEMA``. Writing the
# session string as plain TEXT into a ``.session`` file (as older code did)
# produces a BROKEN/INVALID session that cannot log in. This helper writes a
# real Pyrogram SQLite file so the delivered "program" session is actually
# usable to access the account.
_PYROGRAM_SCHEMA = """
CREATE TABLE sessions
(
    dc_id     INTEGER PRIMARY KEY,
    api_id    INTEGER,
    test_mode INTEGER,
    auth_key  BLOB,
    date      INTEGER NOT NULL,
    user_id   INTEGER,
    is_bot    INTEGER
);

CREATE TABLE peers
(
    id             INTEGER PRIMARY KEY,
    access_hash    INTEGER,
    type           INTEGER NOT NULL,
    username       TEXT,
    phone_number   TEXT,
    last_update_on INTEGER NOT NULL DEFAULT (CAST(STRFTIME('%s', 'now') AS INTEGER))
);

CREATE TABLE version
(
    number INTEGER PRIMARY KEY
);

CREATE INDEX idx_peers_id ON peers (id);
CREATE INDEX idx_peers_username ON peers (username);
CREATE INDEX idx_peers_phone_number ON peers (phone_number);

CREATE TRIGGER trg_peers_last_update_on
    AFTER UPDATE
    ON peers
BEGIN
    UPDATE peers
    SET last_update_on = CAST(STRFTIME('%s', 'now') AS INTEGER)
    WHERE id = NEW.id;
END;
"""


def generate_pyrogram_file(session_string: str, output_path: str,
                           user_id=None, phone=None,
                           access_hash: int = 0) -> str:
    """Write a VALID Pyrogram SQLite ``.session`` file from a session string.

    Returns the file path on success, or ``None`` on failure. The file can be
    loaded by Pyrogram to actually access the account (unlike a text file with
    the session string inside, which Pyrogram cannot parse).
    """
    try:
        parent = os.path.dirname(os.path.abspath(output_path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

        parsed = parse_pyrogram_session(session_string)
        if not parsed or not parsed.get("auth_key") or len(parsed["auth_key"]) != 256:
            logger.warning("generate_pyrogram_file: cannot parse session for %s", phone)
            return None

        dc_id = int(parsed["dc_id"])
        api_id = int(parsed["api_id"])
        test_mode = 1 if parsed["test_mode"] else 0
        auth_key = parsed["auth_key"]
        uid = int(user_id if user_id else parsed["user_id"] or 0)
        # DEFENSE-IN-DEPTH: the session string embeds the ACCOUNT's REAL user
        # id (parsed["user_id"]). If a caller passes a different id (e.g. the
        # operator's id after a restart recreated the file), the parsed id is
        # authoritative - a .session file must ALWAYS identify the account,
        # never the operator who verified it. Log loudly so the buggy caller
        # is caught instead of silently shipping a wrong identity.
        parsed_uid = int(parsed.get("user_id") or 0)
        if uid and parsed_uid and uid != parsed_uid:
            logger.warning(
                "generate_pyrogram_file: caller user_id=%s conflicts with the "
                "session's real user_id=%s for %s - using the session's REAL "
                "account id (wrong identity would have been stamped)",
                uid, parsed_uid, phone,
            )
            uid = parsed_uid
        is_bot = 1 if parsed["is_bot"] else 0

        if os.path.exists(output_path):
            os.remove(output_path)

        conn = sqlite3.connect(output_path)
        c = conn.cursor()
        c.executescript(_PYROGRAM_SCHEMA)
        c.execute("INSERT INTO version VALUES (3)")
        c.execute(
            "INSERT INTO sessions (dc_id, api_id, test_mode, auth_key, date, user_id, is_bot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dc_id, api_id, test_mode, sqlite3.Binary(auth_key), int(time.time()), uid, is_bot),
        )
        if uid:
            phone_int = None
            if phone:
                try:
                    phone_int = int(str(phone).replace("+", "").replace(" ", ""))
                except (ValueError, TypeError):
                    phone_int = None
            c.execute(
                "INSERT OR REPLACE INTO peers (id, access_hash, type, username, phone_number) "
                "VALUES (?, ?, ?, ?, ?)",
                (uid, int(access_hash or 0), 0, None, phone_int),
            )
        conn.commit()
        conn.close()
        logger.info("Pyrogram session (valid SQLite) written: %s", output_path)
        return output_path
    except Exception as e:
        logger.error("generate_pyrogram_file failed for %s: %s", phone, e)
        return None


def read_pyrogram_session_file(path: str):
    """Read dc_id/api_id/auth_key/user_id from a real Pyrogram ``.session`` file.

    Used as a fallback source for tdata generation when the session *string* is
    unavailable or unparseable. Returns the parsed dict or ``None``.
    """
    try:
        import sqlite3
        if not path or not os.path.exists(path):
            return None
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        # api_id IS persisted in pyrogram v2 session files (SCHEMA: sessions
        # table) - without it the rebuilt session string carries api_id=0 and
        # every file GENERATED from it would be unusable for buyers.
        cur.execute("SELECT dc_id, api_id, auth_key, user_id FROM sessions LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if not row or not row[2] or len(row[2]) != 256:
            return None
        return {
            "dc_id": int(row[0]),
            "api_id": int(row[1] or 0),
            "auth_key": bytes(row[2]),
            "user_id": int(row[3] or 0),
        }
    except Exception as e:
        logger.warning("read_pyrogram_session_file failed for %s: %s", path, e)
        return None


# Pyrogram v2 session-string struct (identical to
# ``pyrogram.storage.memory_storage.MemoryStorage``: big-endian
# dc_id, api_id, test_mode, auth_key(256), user_id, is_bot).
_PYROGRAM_SESSION_STRING_FORMAT = ">BI?256sQ?"


def find_session_string_on_disk(phone: str, expected_account_user_id=None) -> Optional[str]:
    """Best-effort disk recovery of a Pyrogram session string for a phone.

    Scans ``sessions/`` and ``verified_sessions/`` for ``.session`` SQLite
    files whose filename contains the phone digits, reads the
    dc_id/auth_key/user_id triple and rebuilds the exact Pyrogram v2 session
    string. This closes the P2-22 recovery gap: a row whose ``session_string``
    was never persisted (process killed between OTP-accept and the save) can
    still be recovered from the ``.session`` file that ``otp.py`` wrote at
    t=0, instead of the account being rejected on data-absence.

    ``expected_account_user_id`` (the ACCOUNT's Telegram id, stored in
    ``session_data["user_id"]``) is a safety filter: a candidate whose
    embedded user id does not match is ignored, so a filename collision can
    never recover a DIFFERENT account's session.

    Returns the recovered session string or None. Never raises.
    """
    import base64
    import struct
    try:
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        if not digits:
            return None
        candidates = []
        for folder in ("sessions", "verified_sessions"):
            try:
                if not os.path.isdir(folder):
                    continue
                for name in os.listdir(folder):
                    if not name.endswith(".session"):
                        continue
                    name_digits = "".join(ch for ch in name if ch.isdigit())
                    if digits and digits in name_digits:
                        candidates.append(os.path.join(folder, name))
            except OSError:
                continue

        # Prefer Pyrogram-format files; Telethon files fail the reader anyway
        # (their sessions table has no user_id column) and are skipped.
        candidates.sort(
            key=lambda p: (1 if "_telethon" in p else 0, os.path.getmtime(p) if os.path.exists(p) else 0),
            reverse=False,
        )
        for path in candidates:
            parsed = read_pyrogram_session_file(path)
            if not parsed or not parsed.get("auth_key"):
                continue
            account_uid = parsed.get("user_id") or 0
            if expected_account_user_id:
                try:
                    if int(account_uid) != int(expected_account_user_id):
                        continue
                except (TypeError, ValueError):
                    continue
            try:
                packed = struct.pack(
                    _PYROGRAM_SESSION_STRING_FORMAT,
                    int(parsed.get("dc_id", 1) or 1),
                    int(parsed.get("api_id", 0) or 0),
                    False,
                    bytes(parsed["auth_key"]),
                    int(account_uid or 0),
                    False,
                )
                return base64.urlsafe_b64encode(packed).decode().rstrip("=")
            except (struct.error, TypeError, ValueError):
                continue
    except Exception as e:
        logger.warning("find_session_string_on_disk failed for %s: %s", phone, e)
    return None


def validate_session_file(path: str, fmt: str = "pyrogram") -> bool:
    """Validate a generated ``.session`` SQLite database before it is sent.

    A corrupt or malformed ``.session`` file is worse than no file - it can be
    sent to the channel and the buyer cannot load it, while every internal check
    only validated the session *string*, never the generated file. This reads
    the actual SQLite DB and verifies the minimum structure that a real
    client (Pyrogram or Telethon) needs to open the account:

    * pyrogram: ``sessions`` table exists with a 256-byte ``auth_key``.
    * telethon: ``version`` (v7) and ``sessions`` tables exist with a
      256-byte ``auth_key`` and a valid ``dc_id``.

    Returns ``True`` only when the file is structurally valid. A missing,
    unreadable or non-SQLite file returns ``False``.
    """
    try:
        if not path or not os.path.exists(path):
            return False
        if os.path.getsize(path) == 0:
            return False
        conn = sqlite3.connect(path)
        try:
            cur = conn.cursor()
            if fmt == "telethon":
                cur.execute("SELECT version FROM version LIMIT 1")
                ver_row = cur.fetchone()
                if not ver_row or int(ver_row[0]) != 7:
                    return False
                cur.execute("SELECT dc_id, auth_key FROM sessions LIMIT 1")
                s_row = cur.fetchone()
                if not s_row or not s_row[1] or len(s_row[1]) != 256:
                    return False
                try:
                    dc_id = int(s_row[0])
                    if dc_id < 1 or dc_id > 5:
                        return False
                except (TypeError, ValueError):
                    return False
                return True
            # pyrogram (default)
            cur.execute("SELECT dc_id, auth_key, user_id FROM sessions LIMIT 1")
            p_row = cur.fetchone()
            if not p_row or not p_row[1] or len(p_row[1]) != 256:
                return False
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("validate_session_file failed for %s (%s): %s", path, fmt, e)
        return False


# ---------------------------------------------------------------------------
# Modern Telegram Desktop TDF$ container helpers (Desktop 4.7+ storage format)
# ---------------------------------------------------------------------------

_TDF_CONTAINER_MAGIC = b"TDF$"
_TDF_CONTAINER_VERSION = 3004000  # matches the reference "this is ok tdata" (0x002DD660)


def _aes_ecb_block(block16: bytes, key: bytes, encrypt: bool) -> bytes:
    """AES-256 single-block ECB via the `cryptography` library (installed)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    c = Cipher(algorithms.AES(key), modes.ECB())
    t = c.encryptor() if encrypt else c.decryptor()
    return t.update(block16) + t.finalize()


def _aes_ige_encrypt(data: bytes, key32: bytes, iv32: bytes) -> bytes:
    """AES-256-IGE encryption (Telegram Desktop's aesEncryptLocal)."""
    assert len(data) % 16 == 0 and len(key32) == 32 and len(iv32) == 32
    prev_plain = iv32[16:32]
    prev_cipher = iv32[0:16]
    out = b""
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        x = bytes(a ^ b for a, b in zip(block, prev_cipher))
        t = _aes_ecb_block(x, key32, encrypt=True)
        c = bytes(a ^ b for a, b in zip(t, prev_plain))
        out += c
        prev_plain = block
        prev_cipher = c
    return out


def _aes_ige_decrypt(data: bytes, key32: bytes, iv32: bytes) -> bytes:
    """AES-256-IGE decryption (Telegram Desktop's aesDecryptLocal)."""
    assert len(data) % 16 == 0 and len(key32) == 32 and len(iv32) == 32
    prev_plain = iv32[16:32]
    prev_cipher = iv32[0:16]
    out = b""
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        x = bytes(a ^ b for a, b in zip(block, prev_plain))
        d = _aes_ecb_block(x, key32, encrypt=False)
        plain = bytes(a ^ b for a, b in zip(d, prev_cipher))
        out += plain
        prev_plain = plain
        prev_cipher = block
    return out


def _create_local_key(passcode: bytes, salt: bytes) -> bytes:
    """Official ``CreateLocalKey``: PBKDF2-HMAC-SHA512 with the password
    sha512(salt || passcode || salt). 1 iteration for a blank passcode,
    100 000 for a passcode (matches Telegram Desktop)."""
    pw = hashlib.sha512(salt + passcode + salt).digest()
    iterations = 1 if not passcode else 100000
    return hashlib.pbkdf2_hmac("sha512", pw, salt, iterations, 256)


def _prepare_aes_oldmtp(msg_key: bytes, key256: bytes, send: bool = False):
    """Official ``AuthKey::prepareAES_oldmtp`` (the key derivation used by
    ``aesEncryptLocal``/``aesDecryptLocal`` for local storage encryption).

    Returns the 32-byte AES key + 32-byte IGE IV derived from the 16-byte
    msg key (the SHA1 prefix) and the 256-byte auth key.
    """
    x = 0 if send else 8
    sha1_a = hashlib.sha1(msg_key + key256[x:x + 32]).digest()
    sha1_b = hashlib.sha1(key256[32 + x:48 + x] + msg_key + key256[48 + x:64 + x]).digest()
    sha1_c = hashlib.sha1(key256[64 + x:96 + x] + msg_key).digest()
    sha1_d = hashlib.sha1(msg_key + key256[96 + x:128 + x]).digest()
    aes_key = sha1_a[0:8] + sha1_b[8:20] + sha1_c[4:16]
    aes_iv = sha1_a[8:20] + sha1_b[0:8] + sha1_c[16:20] + sha1_d[0:8]
    return aes_key, aes_iv


def _prepare_encrypted(data: bytes, key: bytes) -> bytes:
    """Official ``EncryptedDescriptor``/``PrepareEncrypted``.

    plaintext = uint32(4 + len(data)) + data (+ random padding to a 16-byte
    block). The size field includes the 4 length bytes themselves, exactly as
    Telegram Desktop's ``EncryptedDescriptor`` writes it. output =
    sha1(plaintext)[:16] + AES-256-IGE(plaintext) where the IGE key/IV are
    derived from the SHA1 msg-key via ``prepareAES_oldmtp`` (what Desktop's
    ``aesEncryptLocal`` does).
    """
    payload = struct.pack("<I", 4 + len(data)) + data
    if len(payload) % 16:
        payload += os.urandom(16 - (len(payload) % 16))
    sha1key = hashlib.sha1(payload).digest()[:16]
    aes_key, aes_iv = _prepare_aes_oldmtp(sha1key, key, send=False)
    return sha1key + _aes_ige_encrypt(payload, aes_key, aes_iv)


def _unprepare_encrypted(blob: bytes, key: bytes):
    """Inverse of ``_prepare_encrypted``. Returns (data, ok)."""
    try:
        if len(blob) <= 16 or len(blob) % 16:
            return None, False
        sha1key, body = blob[:16], blob[16:]
        aes_key, aes_iv = _prepare_aes_oldmtp(sha1key, key, send=False)
        plain = _aes_ige_decrypt(body, aes_key, aes_iv)
        if hashlib.sha1(plain).digest()[:16] != sha1key:
            return None, False
        size = struct.unpack("<I", plain[:4])[0]
        return plain[4:size], True
    except Exception:
        return None, False


def _qbytearray(data: bytes) -> bytes:
    """QDataStream QByteArray serialization (Qt default = big-endian length).
    ``None`` serializes as a null QByteArray (length 0xFFFFFFFF)."""
    if data is None:
        return struct.pack(">I", 0xFFFFFFFF)
    return struct.pack(">I", len(data)) + data


def _tdf_wrap(data: bytes) -> bytes:
    """TDF$ container: magic + version + data + md5(data+len+version+magic)."""
    version_le = struct.pack("<i", _TDF_CONTAINER_VERSION)
    md5 = hashlib.md5(data + struct.pack("<i", len(data)) + version_le + _TDF_CONTAINER_MAGIC).digest()
    return _TDF_CONTAINER_MAGIC + version_le + data + md5


def _compute_data_name_key(data_name: str) -> str:
    """Official ``ComputeDataNameKey``: MD5(dataName), first 8 bytes as LE
    uint64 → ToFilePart (16 hex chars, low nibble to high)."""
    md5 = hashlib.md5(data_name.encode("utf-8")).digest()
    key = struct.unpack("<Q", md5[:8])[0]
    chars = []
    for i in range(16):
        v = (key >> (4 * i)) & 0x0F
        chars.append(chr(ord("0") + v) if v < 10 else chr(ord("A") + (v - 10)))
    return "".join(chars)


# ---------------------------------------------------------------------------
# Constants taken from a REAL WORKING Telegram Desktop tdata (verified to open
# in Desktop). These are device/universal values, hardcoded so no template
# folder is needed at runtime.
# ---------------------------------------------------------------------------

# Auth keys for dc 1, 2, 4 used by the reference device (kept as placeholders;
# only the main dc slot is replaced with the session's auth key).
_SLOT_KEYS = {
    1: bytes.fromhex(
        "3d2e3e7ecade46cd73031b497c7a38c761f83de14140cefedd35eeac6da51cfe11be01a142efa6a9d181863f80"
        "3917c34e2a80e4019437422e4bc60ef71e50a7399da8842b07bb509f147e90c851ad1a77cb4e61a7be23cf26df1d3"
        "39553a4f7f5d4d4d59040098752852c48ddc4c2de0341fcd5a63eb65106ef1b242362037b36d8a450f456e90de79"
        "9164a52f9270e0d3f884dd6cb531f6139e7e284e7ff3e848d3071cbf195c68f93201055f431dfc0d2bdee9a30db5e"
        "548aa53756fb73bf36689597059cf430340800a97f97c46d0fdc7a9d91ccff5a8d6099f9b6e9f5e5f22b0f354e49"
        "0e611a520532cbce89543449c32ac010cc3ad144ee0524367e99"),
    2: bytes.fromhex(
        "6a6bb787e8da579b9aec3cd24ec2c082073ae4b7a1fafe6a8a3bd7f465e41b10f1af66f49850f6b5bb657632a12df"
        "c40087ed68d264ea200309b6ce2736a841a4e770749c3ef83fa4ccbe9870396f2cdd6612c2fa325722623dde50f74"
        "c626e7b38ce117b15876325773b7799c359cce60710d1ef4bd640d857da701907fd5480ff27eebf753918cbc34cba1"
        "e5b242b6563a0f1814b737a23a7898191fc3f0675cf8dc59f869aea386743dce89c844604e06a7127631add76fe49"
        "69a538ae75035fc82d733006efc9da0fec4340f3204c963b39fef0df7eb8b8c92293e606504469c9204fe2e814d2"
        "82407f49e73f09fa96399cafb049bb3ff9e6680f8781554"),
    4: bytes.fromhex(
        "33b2afe7d88b2e80a945a8fe16772dc1a96313a1220ea29ac13c952d566fc596111d47ed7d4361a320926b358b2f"
        "a4a7b4e5edb6c31f8a024100ffa1e50cfe941701ed5e5742c403ea54ea87f67bd68bb0e82aa0b940798b406ce9a01"
        "b45828b7b0004485a06d571d3e1a168ab98b5a01d79bfa04bad4d8a473157f06281dbbdd70eb16253a2bc5c862b7e6"
        "48f629907013cd4c5e3d374a0907a9881c5f2abb7e787328852f89171f0781643b0b2fab5c6835fd377463e5bbd3f"
        "82a4952587f9bd70800977a069f951c9ec3a86fdcc89f5e503deca00d6a479d8989b3d74e04601342f453409afe7f"
        "ef6cdb8b598c22d6ec7d18b5544659684d7feaf1bc21c6b"),
}

# NOTE: the account map is written empty (see generate_tdata_folder). The old
# hardcoded map template was removed because it used a modern map key type
# (0x18) that strict/older Telegram Desktop builds reject, and it embedded a
# foreign account's identity. An empty map loads on every reader.

# The MTProto config plaintext (1031 B) from the working tdata (universal).
_CONFIGS_PLAIN = bytes.fromhex(
    "00000403000000010000000000000317fffffffe000000110000000100000000000001bb0000000e3134392e3135342e"
    "3137352e3539000000000000000100000010000001bb0000000e3134392e3135342e3137352e35300000000000000001"
    "00000001000001bb00000027323030313a306232383a663233643a663030313a303030303a303030303a303030303a30"
    "303061000000000000000200000000000001bb0000000e3134392e3135342e3136372e34310000000000000002000000"
    "02000001bb0000000f3134392e3135342e3136372e313531000000000000000200000001000001bb0000002732303031"
    "3a303637633a303465383a663030323a303030303a303030303a303030303a3030306100000000000000020000000300"
    "0001bb00000027323030313a303637633a303465383a663030323a303030303a303030303a303030303a303030620000"
    "00000000000300000000000001bb0000000f3134392e3135342e3137352e313030000000000000000300000001000001"
    "bb00000027323030313a306232383a663233643a663030333a303030303a303030303a303030303a3030306100000000"
    "0000000400000000000001bb0000000e3134392e3135342e3136372e3931000000000000000400000001000001bb0000"
    "0027323030313a303637633a303465383a663030343a303030303a303030303a303030303a3030306100000000000000"
    "0400000002000001bb0000000f3134392e3135342e3136352e313131000000000000000400000003000001bb00000027"
    "323030313a303637633a303465383a663030343a303030303a303030303a303030303a30303062000000000000000500"
    "000001000001bb00000027323030313a306232383a663233663a663030353a303030303a303030303a303030303a3030"
    "3061000000000000000500000000000001bb0000000d39312e3130382e35362e31353200000000000000c90000000800"
    "0001bb0000000d39312e3130382e32332e31303000000000000000c900000009000001bb00000027323030313a306232"
    "383a663233633a303030613a303030663a303030303a303030303a303130300000000000000000000000c800030d4000"
    "000064000334500000138800007530000003e8000493e000007530000005dc000000000002a3007fffffff7fffffff00"
    "000001000000c80000000000000000000000000000001a00680074007400700073003a002f002f0074002e006d006500"
    "2f00093a8000004e2000015f900000753000002710000000040000001a0061007000760033002e007300740065006c00"
    "2e0063006f006d00000001000000000000040000000002276400000000000000000024ea000000000600670069006600"
    "0000140066006f00750072007300710075006100720065"
)


def generate_tdata_folder(phone: str, session_string: str, user_id,
                            out_dir: str = "verified_sessions",
                            session_file: str = None) -> str:
    """Generate a Telegram Desktop ``tdata`` folder (no passcode) - PURE CODE.

    Builds key_datas, <hash>s, maps and configs entirely in code using the
    exact layout of a real working Telegram Desktop tdata. The session's auth
    key goes into the main-dc slot of the MtpAuthorization; the account uid
    is patched into the maps self-data. No template folder is needed.
    """
    try:
        parsed = parse_pyrogram_session(session_string)
        if not parsed and session_file:
            parsed = read_pyrogram_session_file(session_file)
        if not parsed:
            logger.warning("generate_tdata_folder: cannot parse session for %s "
                           "(session_file=%s)", phone, session_file)
            return None
        auth_key = parsed["auth_key"]
        uid = int(user_id or parsed["user_id"] or 0)
        # DEFENSE-IN-DEPTH (same rule as generate_pyrogram_file): the session
        # string embeds the ACCOUNT's REAL user id. If the caller passes a
        # different id (e.g. the operator's id), the parsed id is authoritative
        # so the tdata <hash>s uid64 and maps uid32 always identify the ACCOUNT,
        # never the operator who verified it.
        parsed_uid = int(parsed.get("user_id") or 0)
        if uid and parsed_uid and uid != parsed_uid:
            logger.warning(
                "generate_tdata_folder: caller user_id=%s conflicts with the "
                "session's real user_id=%s for %s - using the session's REAL "
                "account id (wrong identity would have been stamped)",
                uid, parsed_uid, phone,
            )
            uid = parsed_uid
        dc_id = parsed["dc_id"]

        safe_phone = str(phone).replace('+', '').replace(' ', '')
        os.makedirs(out_dir, exist_ok=True)
        folder = os.path.join(out_dir, f"tdata_{safe_phone}_{uuid.uuid4().hex[:12]}")
        tdata_dir = os.path.join(folder, "tdata")
        os.makedirs(tdata_dir, exist_ok=True)
        account_hash = "D877F783D5D3EF8C"

        # ---- 1) key_datas (388B) ------------------------------------------
        salt = os.urandom(32)
        local_key = os.urandom(256)
        passcode_key = _create_local_key(b"", salt)
        key_encrypted = _prepare_encrypted(local_key, passcode_key)
        info_plain = struct.pack(">i", 1) + struct.pack(">i", 0) + struct.pack(">i", -1)
        info_encrypted = _prepare_encrypted(info_plain, local_key)
        key_datas_data = _qbytearray(salt) + _qbytearray(key_encrypted) + _qbytearray(info_encrypted)
        with open(os.path.join(tdata_dir, "key_datas"), "wb") as f:
            f.write(_tdf_wrap(key_datas_data))

        # ---- 2) <hash>s (1132B) - MtpAuthorization with 4 key slots -------
        serialized = struct.pack(">q", -1)           # kWideIdsTag
        serialized += struct.pack(">q", uid)         # uid64
        serialized += struct.pack(">i", dc_id)       # main dc
        serialized += struct.pack(">i", 4)           # key count
        for slot_dc in (1, 2, 4, 5):
            serialized += struct.pack(">i", slot_dc)
            if slot_dc == dc_id:
                serialized += auth_key               # session's auth key
            else:
                # _SLOT_KEYS used to hold only dc 1/2/4, so slot 5 raised
                # KeyError(5) and TData generation failed for every account
                # whose main dc was NOT 5 (the old code could only build tdata
                # for dc5 accounts). Fall back to the dc1 placeholder key for
                # any missing slot - it is a placeholder anyway, exactly like
                # the other non-main slots.
                serialized += _SLOT_KEYS.get(slot_dc, _SLOT_KEYS[1])
        serialized += struct.pack(">i", 0)           # destroy count
        content = struct.pack(">i", 75) + _qbytearray(serialized)
        with open(os.path.join(tdata_dir, account_hash + "s"), "wb") as f:
            f.write(_tdf_wrap(_qbytearray(_prepare_encrypted(content, local_key))))

        # ---- 3) maps - EMPTY map (loads on EVERY Telegram Desktop version) --
        # The account's authorization comes entirely from the MtpAuthorization
        # (the auth key) in the <hash>s file - NOT from the map. An empty map
        # (zero entries) therefore still loads a fully authorized account, and it
        # parses on every Desktop version and every third-party reader.
        #
        # The previous code wrote a hardcoded template map that (a) used a MODERN
        # map key type - 0x18 lskSearchSuggestions - which older/strict Telegram
        # Desktop builds and tdata checkers do NOT recognise, so they abort the
        # map read, load NO account, and show the fresh-login screen ("opens but
        # asks to log in"); and (b) embedded a DIFFERENT account's identity
        # (phone 8801850737389 / username Asian_bot_support), only 4 uid bytes of
        # which were patched, leaking a foreign identity into every tdata. An
        # empty map fixes both. (Verified: with the template map a strict reader
        # loads 0 accounts; with an empty map it loads the account with the
        # correct dc/uid/auth key.)
        os.makedirs(os.path.join(tdata_dir, account_hash), exist_ok=True)
        with open(os.path.join(tdata_dir, account_hash, "maps"), "wb") as f:
            f.write(_tdf_wrap(_qbytearray(None) + _qbytearray(None)
                              + _qbytearray(_prepare_encrypted(b"", local_key))))

        # ---- 4) configs (1084B) - universal MTProto config -----------------
        with open(os.path.join(tdata_dir, account_hash, "configs"), "wb") as f:
            f.write(_tdf_wrap(_qbytearray(_prepare_encrypted(_CONFIGS_PLAIN, local_key))))

        logger.info("TData folder written (pure code): %s (dc=%s uid=%s)",
                    folder, dc_id, uid)
        return folder
    except Exception as e:
        logger.error("generate_tdata_folder failed for %s: %s", phone, e)
        return None
