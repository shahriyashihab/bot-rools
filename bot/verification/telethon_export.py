"""
Telethon session export helpers.
=====================================================================
Converts Pyrogram session strings / SQLite databases into Telethon
v7 session files, and resolves user access hashes.
"""

import os
import struct
import base64
import sqlite3
from datetime import datetime
from typing import Optional

from pyrogram.client import Client

from bot.utils.net_timeouts import (
    connect_with_timeout,
    op_with_timeout,
    teardown_with_timeout,
)


async def get_user_access_hash(client: Client, user_id: int) -> int:
    """
    Get access_hash for a user from the Pyrogram client.
    This is needed for Telethon session files to work with external bots.
    
    Args:
        client: Connected Pyrogram client
        user_id: Telegram user ID
    
    Returns:
        access_hash as integer, or 0 if not available
    """
    try:
        input_peer = await op_with_timeout(client.resolve_peer(user_id), f"resolve-peer:{user_id}")
        access_hash = getattr(input_peer, 'access_hash', None)
        if access_hash is not None:
            return access_hash
        return 0
    except Exception as e:
        print(f"⚠️ Could not get access_hash: {e}")
        return 0


def session_string_to_telethon(session_string: str, telethon_output_path: str, user_id: Optional[int] = None, phone: Optional[str] = None, first_name: Optional[str] = None, access_hash: Optional[int] = None) -> bool:
    """
    Convert Pyrogram session string directly to Telethon v7 SQLite database.
    This creates a proper .session file that works with session checkers and @NewLonamiBot.
    
    CRITICAL FIX v2: Correct Pyrogram session format (271 bytes):
    - dc_id (1 byte): index 0
    - api_id (4 bytes, big-endian): index 1-4
    - test_mode (1 byte): index 5
    - auth_key (256 bytes): index 6-261
    - user_id (8 bytes, big-endian): index 262-269
    - is_bot (1 byte): index 270
    """
    try:
        output_dir = os.path.dirname(os.path.abspath(telethon_output_path))
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)
        
        decoded = base64.urlsafe_b64decode(session_string + "=" * (-len(session_string) % 4))
        decoded_len = len(decoded)
        
        print(f"📦 Pyrogram session decoded: {decoded_len} bytes")
        
        dc_id = None
        auth_key = None
        extracted_user_id = None
        
        if decoded_len == 271:
            dc_id = decoded[0]
            api_id = struct.unpack('>I', decoded[1:5])[0]
            test_mode = decoded[5]
            auth_key = decoded[6:262]
            extracted_user_id = struct.unpack('>Q', decoded[262:270])[0]
            is_bot = decoded[270]
            print(f"✅ Pyrogram v2 format (271 bytes): DC={dc_id}, API={api_id}, test={test_mode}, user={extracted_user_id}, bot={is_bot}")
            # P6-64: auth-key bytes are CREDENTIAL MATERIAL - never log them
            # (the old "first 10 hex" made a log file a partial-key leak).
        elif decoded_len >= 270:
            dc_id = decoded[0]
            api_id = struct.unpack('>I', decoded[1:5])[0]
            test_mode = decoded[5]
            auth_key = decoded[6:262]
            extracted_user_id = struct.unpack('>Q', decoded[262:270])[0]
            is_bot = decoded[270] if decoded_len > 270 else 0
            print(f"✅ Pyrogram v2 format ({decoded_len} bytes): DC={dc_id}, API={api_id}, user={extracted_user_id}")
        elif decoded_len == 267:
            dc_id, test_mode, auth_key, extracted_user_id, is_bot = struct.unpack(">B?256sQ?", decoded)
            print(f"📦 Pyrogram format (267 bytes): DC={dc_id}, user={extracted_user_id}")
        elif decoded_len == 265:
            dc_id = decoded[0]
            auth_key = decoded[1:257]
            extracted_user_id = struct.unpack('<q', decoded[257:265])[0]
            print(f"📦 Legacy format (265 bytes): DC={dc_id}, user={extracted_user_id}")
        elif decoded_len == 261:
            dc_id = decoded[0]
            auth_key = decoded[1:257]
            extracted_user_id = struct.unpack('<i', decoded[257:261])[0]
            print(f"📦 Very old format (261 bytes): DC={dc_id}, user={extracted_user_id}")
        else:
            print(f"⚠️ Unknown session string length: {decoded_len}")
            dc_id = decoded[0]
            if decoded_len >= 262:
                auth_key = decoded[6:262]
            elif decoded_len >= 257:
                auth_key = decoded[1:257]
            else:
                auth_key = decoded[1:min(257, decoded_len)]
        
        if extracted_user_id and extracted_user_id > 0:
            # The session string embeds the ACCOUNT's real user id. If the
            # caller provides a different id (e.g. the operator's id after a
            # restart recreated the file), the parsed id is authoritative so
            # the Telethon entity row never carries the wrong identity.
            if user_id is None:
                user_id = extracted_user_id
            else:
                try:
                    if int(user_id) != int(extracted_user_id):
                        print(f"⚠️ session_string_to_telethon: caller user_id={user_id} "
                              f"conflicts with session user_id={extracted_user_id} - using "
                              f"the session's REAL account id")
                        user_id = extracted_user_id
                except (TypeError, ValueError):
                    user_id = extracted_user_id
        
        if auth_key is None or len(auth_key) != 256:
            print(f"⚠️ Invalid auth_key length: {len(auth_key) if auth_key else 0}, expected 256")
            return False
        
        if os.path.exists(telethon_output_path):
            os.remove(telethon_output_path)
        
        conn = sqlite3.connect(telethon_output_path)
        cursor = conn.cursor()
        
        cursor.execute("CREATE TABLE version (version INTEGER PRIMARY KEY)")
        cursor.execute("INSERT INTO version VALUES (7)")
        
        cursor.execute("""
            CREATE TABLE sessions (
                dc_id INTEGER PRIMARY KEY,
                server_address TEXT,
                port INTEGER,
                auth_key BLOB,
                takeout_id INTEGER
            )
        """)
        
        dc_servers = {
            1: "149.154.175.53", 
            2: "149.154.167.51", 
            3: "149.154.175.100", 
            4: "149.154.167.91", 
            5: "91.108.56.130"
        }
        server_address = dc_servers.get(dc_id, "149.154.167.51")
        
        cursor.execute("""
            INSERT INTO sessions (dc_id, server_address, port, auth_key, takeout_id)
            VALUES (?, ?, ?, ?, NULL)
        """, (dc_id, server_address, 443, auth_key))
        
        cursor.execute("""
            CREATE TABLE entities (
                id INTEGER PRIMARY KEY,
                hash INTEGER NOT NULL,
                username TEXT,
                phone INTEGER,
                name TEXT,
                date INTEGER
            )
        """)
        
        if user_id:
            phone_int = None
            if phone:
                try:
                    phone_int = int(phone.replace('+', '').replace(' ', ''))
                except (ValueError, TypeError):
                    phone_int = None
            
            entity_hash = access_hash if access_hash else 0
            entity_name = first_name if first_name else ""
            current_time = int(datetime.now().timestamp())
            
            cursor.execute("""
                INSERT INTO entities (id, hash, username, phone, name, date)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, entity_hash, None, phone_int, entity_name, current_time))
            print(f"✅ User entity added to Telethon session: ID={user_id}, Phone={phone_int}")
        
        cursor.execute("""
            CREATE TABLE sent_files (
                md5_digest BLOB,
                file_size INTEGER,
                type INTEGER,
                id INTEGER,
                hash INTEGER,
                PRIMARY KEY (md5_digest, file_size, type)
            )
        """)
        
        cursor.execute("""
            CREATE TABLE update_state (
                id INTEGER PRIMARY KEY,
                pts INTEGER,
                qts INTEGER,
                date INTEGER,
                seq INTEGER
            )
        """)
        
        conn.commit()
        conn.close()
        
        print(f"✅ Telethon v7 session created from string: {telethon_output_path}")
        print(f"   DC: {dc_id}, Server: {server_address}, User: {user_id}")
        return True
        
    except Exception as e:
        print(f"❌ Session string to Telethon conversion error: {e}")
        import traceback
        traceback.print_exc()
        return False


def find_existing_access_hash(phone: str, user_id) -> int:
    """Look up a previously resolved access_hash for this account from any
    Telethon session file already on disk (cheap, NO Telegram API call).

    The OTP-time export (``otp.py``) writes the account's REAL access hash
    into the entity row of its Telethon file. When a later path regenerates
    the Telethon file on the fly (monitor fast path / recovery / full
    validation) we prefer that real hash so the delivered file never carries
    the ``hash=0`` placeholder.

    Returns the found access_hash (int) or 0 when nothing usable exists.
    """
    safe = str(phone or "").replace("+", "").replace(" ", "")
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        uid = 0
    if not safe or not uid:
        return 0
    for folder in ("sessions", "verified_sessions"):
        if not os.path.isdir(folder):
            continue
        try:
            for fn in os.listdir(folder):
                if not fn.endswith(".session") or safe not in fn:
                    continue
                try:
                    conn = sqlite3.connect(os.path.join(folder, fn))
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name='entities'"
                        )
                        if not cur.fetchone():
                            continue
                        cur.execute("SELECT id, hash FROM entities")
                        for row in cur.fetchall():
                            try:
                                if int(row[0] or 0) == uid and int(row[1] or 0):
                                    return int(row[1])
                            except (TypeError, ValueError):
                                continue
                    finally:
                        conn.close()
                except Exception:
                    continue
        except OSError:
            continue
    return 0


async def resolve_user_access_hash(
    session_string: str,
    phone: str,
    user_id=None,
    api_id=None,
    api_hash=None,
) -> int:
    """Resolve the ACCOUNT's real access_hash for a Telethon entity.

    Priority:
      1. A previously written Telethon file on disk (``find_existing_access_hash``
         - zero API cost, the OTP-time file already carries the real hash).
      2. Live resolve: connect a fresh in-memory client with the session string
         (rotated API, real device info) and call ``get_user_access_hash``.

    Any failure falls back to 0 (the auth key is what logs the account in; the
    entity hash is cosmetic metadata). This guarantees the on-the-fly Telethon
    files delivered by the monitor/recovery paths carry the real account hash
    instead of the placeholder 0.
    """
    import asyncio
    import time as _time

    if user_id:
        try:
            existing = await asyncio.to_thread(find_existing_access_hash, phone, user_id)
            if existing:
                print(f"🔑 ACCESS HASH: reused existing hash {existing} for {phone}")
                return existing
        except Exception:
            pass

    temp_client = None
    try:
        from bot import config
        from bot.verification.api import get_rotated_api
        from bot.utils.device_generator import generate_device_info
        from pyrogram.client import Client

        if not api_id or not api_hash:
            try:
                api_id, api_hash, _ = get_rotated_api()
            except Exception:
                pass
        api_id = api_id or config.API_ID
        api_hash = api_hash or config.API_HASH

        uid = user_id
        if not uid:
            try:
                from bot.services.session_formats import parse_pyrogram_session
                parsed = parse_pyrogram_session(session_string or "")
                if parsed and parsed.get("user_id"):
                    uid = parsed["user_id"]
            except Exception:
                pass
        if not uid:
            return 0

        device_info = generate_device_info()

        def _make_access_hash_client(proxy):
            kwargs = dict(
                name=f"access_hash_{phone}_{int(_time.time() * 1000)}",
                api_id=api_id,
                api_hash=api_hash,
                session_string=session_string,
                in_memory=True,
                device_model=device_info["device_model"],
                system_version=device_info["system_version"],
                app_version="Telegram Android 10.14.5",
            )
            if proxy:
                kwargs["proxy"] = proxy
            return Client(**kwargs)

        # Route through the account's country proxy when derivable from the
        # phone number (no proxy configured -> direct, exactly as before).
        _proxy_cc = None
        try:
            from bot.utils.country_data import get_country_code_from_phone
            _proxy_cc = get_country_code_from_phone(phone) if phone else None
        except Exception:
            _proxy_cc = None
        from bot.utils.proxy_connector import connect_with_proxy_chain
        temp_client = await connect_with_proxy_chain(
            _make_access_hash_client, _proxy_cc, f"access-hash:{phone}"
        )
        hash_val = await get_user_access_hash(temp_client, int(uid))
        print(f"🔑 ACCESS HASH: live-resolved {hash_val} for {phone} (uid={uid})")
        return int(hash_val or 0)
    except Exception as e:
        print(f"⚠️ ACCESS HASH: could not resolve for {phone} (fallback 0): {e}")
        return 0
    finally:
        if temp_client is not None:
            try:
                await teardown_with_timeout(temp_client, "telethon-access-hash")
            except Exception:
                pass


def pyrogram_to_telethon(pyrogram_session_path, telethon_output_path):
    """Convert Pyrogram SQLite session to Telethon v7 format (compatible with session checkers)"""
    try:
        pyro_conn = sqlite3.connect(pyrogram_session_path)
        pyro_cursor = pyro_conn.cursor()
        
        pyro_cursor.execute("SELECT dc_id, auth_key FROM sessions LIMIT 1")
        result = pyro_cursor.fetchone()
        
        if not result:
            print("⚠️ No session data in Pyrogram database")
            pyro_conn.close()
            return False
        
        dc_id, auth_key = result
        pyro_conn.close()
        
        if os.path.exists(telethon_output_path):
            os.remove(telethon_output_path)
        
        tele_conn = sqlite3.connect(telethon_output_path)
        tele_cursor = tele_conn.cursor()
        
        tele_cursor.execute("CREATE TABLE version (version INTEGER PRIMARY KEY)")
        tele_cursor.execute("INSERT INTO version VALUES (7)")
        
        tele_cursor.execute("""
            CREATE TABLE sessions (
                dc_id INTEGER PRIMARY KEY,
                server_address TEXT,
                port INTEGER,
                auth_key BLOB,
                takeout_id INTEGER
            )
        """)
        
        dc_servers = {
            1: "149.154.175.53", 
            2: "149.154.167.51", 
            3: "149.154.175.100", 
            4: "149.154.167.91", 
            5: "91.108.56.130"
        }
        server_address = dc_servers.get(dc_id, "149.154.167.51")
        
        tele_cursor.execute("""
            INSERT INTO sessions (dc_id, server_address, port, auth_key, takeout_id)
            VALUES (?, ?, ?, ?, NULL)
        """, (dc_id, server_address, 443, auth_key))
        
        tele_cursor.execute("""
            CREATE TABLE entities (
                id INTEGER PRIMARY KEY,
                hash INTEGER NOT NULL,
                username TEXT,
                phone INTEGER,
                name TEXT,
                date INTEGER
            )
        """)
        
        tele_cursor.execute("""
            CREATE TABLE sent_files (
                md5_digest BLOB,
                file_size INTEGER,
                type INTEGER,
                id INTEGER,
                hash INTEGER,
                PRIMARY KEY (md5_digest, file_size, type)
            )
        """)
        
        tele_cursor.execute("""
            CREATE TABLE update_state (
                id INTEGER PRIMARY KEY,
                pts INTEGER,
                qts INTEGER,
                date INTEGER,
                seq INTEGER
            )
        """)
        
        tele_conn.commit()
        tele_conn.close()
        
        print(f"✅ Telethon v7 session created: {telethon_output_path}")
        return True
        
    except Exception as e:
        print(f"❌ Telethon conversion error: {e}")
        return False
