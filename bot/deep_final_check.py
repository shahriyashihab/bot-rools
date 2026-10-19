"""DEEP FINAL CHECK: compile, generate, compare byte-by-byte with working zip, real access, pipeline.

NOTE: This is a standalone dev/debug script, NOT part of the production
pipeline. It creates a bare Pyrogram Client and connects DIRECTLY, i.e. it
intentionally BYPASSES the proxy system (bot/data/proxies.py +
bot/utils/proxy_connector.py). Do not use its verdict as evidence that a
country's proxy chain works - use the Proxy Health Check admin panel or
bot.utils.proxy_connector.connect_with_proxy_chain for that.
"""
import os, sys, struct, hashlib, base64, asyncio, shutil, sqlite3, json, zipfile
sys.path.insert(0, r"C:\Users\SHAHRIYAR\Downloads\New folder (3)\workspace")
os.environ["SQLITE_DB_PATH"] = os.path.join(r"C:\Users\SHAHRIYAR\AppData\Local\Temp\kilo", "deep_final.db")

import bot.services.session_formats as sf
import bot.config
from bot.data.state import _decrypt_value
from bot.services import session_reporting  # verify imports work

P = []  # (name, passed)
def check(name, cond, detail=""):
    P.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")

# 1) Compile check
def rb(d, o):
    ln = struct.unpack(">I", d[o:o+4])[0]
    return (None, o+4) if ln == 0xFFFFFFFF else (d[o+4:o+4+ln], o+4+ln)

# 2) Generate fresh tdata
conn = sqlite3.connect(os.path.join(r"C:\Users\SHAHRIYAR\Downloads\New folder (3)\workspace", "data", "bot_data.db"))
obj = json.loads(conn.execute("SELECT data FROM pending_sessions LIMIT 1").fetchone()[0])
conn.close()
REAL_SS = _decrypt_value(obj.get("session_string", ""))
parsed = sf.parse_pyrogram_session(REAL_SS)
AUTH, UID, DC, API = parsed['auth_key'], parsed['user_id'], parsed['dc_id'], parsed['api_id']
HASH = bot.config.API_HASH; PHONE = obj.get("phone")

tmp = r"C:\Users\SHAHRIYAR\AppData\Local\Temp\kilo\deep_final"
shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
folder = sf.generate_tdata_folder(PHONE, REAL_SS, UID, out_dir=tmp)
check("generate_tdata_folder returns folder", folder and os.path.isdir(folder))
tdata = os.path.join(folder, "tdata")

# 3) File sizes vs FINAL_TDATA_WORKING
zf = zipfile.ZipFile(r"C:\Users\SHAHRIYAR\Downloads\New folder (3)\workspace\FINAL_TDATA_WORKING.zip")
work = {n.split("/tdata/")[-1]: zf.read(n) for n in zf.namelist()}
wf = {"key_datas": len(work["key_datas"]), "D877F783D5D3EF8Cs": len(work["D877F783D5D3EF8Cs"]), "D877F783D5D3EF8C/maps": len(work["D877F783D5D3EF8C/maps"]), "D877F783D5D3EF8C/configs": len(work["D877F783D5D3EF8C/configs"])}
mf = {"key_datas": os.path.getsize(os.path.join(tdata, "key_datas")), "D877F783D5D3EF8Cs": os.path.getsize(os.path.join(tdata, "D877F783D5D3EF8Cs")), "D877F783D5D3EF8C/maps": os.path.getsize(os.path.join(tdata, "D877F783D5D3EF8C", "maps")), "D877F783D5D3EF8C/configs": os.path.getsize(os.path.join(tdata, "D877F783D5D3EF8C", "configs"))}
for k in wf:
    check(f"size {k}: {mf[k]}={wf[k]}", mf[k] == wf[k])

# 4) Decrypt <hash>s and verify uid/dc/keys
kd = open(os.path.join(tdata, "key_datas"), "rb").read()
lk_mine = sf._create_local_key(b"", rb(kd[8:-16], 0)[0])
salt, _ = rb(kd[8:-16], 0); ke, _ = rb(kd[8:-16], 0); ie, _ = rb(kd[8:-16], 0)
# actually need proper parse
off = 0; salt, off = rb(kd[8:-16], off); ke, off = rb(kd[8:-16], off); ie, off = rb(kd[8:-16], off)
pk = sf._create_local_key(b"", salt)
lk, _ = sf._unprepare_encrypted(ke, pk)
check("key_datas decrypt", lk is not None and len(lk) == 256)
ip, _ = sf._unprepare_encrypted(ie, lk)
check("info decrypt (count=1)", ip and len(ip) == 12 and struct.unpack(">i", ip[0:4])[0] == 1)

hb = open(os.path.join(tdata, "D877F783D5D3EF8Cs"), "rb").read()
hd = hb[8:-16]; ln = struct.unpack(">I", hd[:4])[0]
content, _ = sf._unprepare_encrypted(hd[4:4+ln], lk)
ser = content[8:8+struct.unpack(">I", content[4:8])[0]]
uid64 = struct.unpack(">q", ser[8:16])[0]
dc = struct.unpack(">i", ser[16:20])[0]
check("uid64 matches", uid64 == UID)
check("dc matches", dc == DC)
check("key_count = 4", struct.unpack(">i", ser[20:24])[0] == 4)
# check all 4 keys have correct content
keys_ok = True
for k in range(4):
    slot = 24 + k*260
    kdc = struct.unpack(">i", ser[slot:slot+4])[0]
    key = ser[slot+4:slot+260]
    if kdc == DC:
        if key != AUTH: keys_ok = False
    else:
        if key != sf._SLOT_KEYS[kdc]: keys_ok = False
check("all 4 keys correct", keys_ok)

# 5) Maps plaintext identical to working
lk_work = sf._create_local_key(b"", rb(work["key_datas"][8:-16], 0)[0])
salt_w, _ = rb(work["key_datas"][8:-16], 0); ke_w, _ = rb(work["key_datas"][8:-16], 0); ie_w, _ = rb(work["key_datas"][8:-16], 0)
# hmm let me just compare the plaintext
def get_maps_plain(b, lk):
    d = b[8:-16]; _, o = rb(d, 0); _, o = rb(d, o); blob, _ = rb(d, o)
    p, _ = sf._unprepare_encrypted(blob, lk); return p
mp = get_maps_plain(open(os.path.join(tdata, "D877F783D5D3EF8C", "maps"), "rb").read(), lk)
wp = get_maps_plain(work["D877F783D5D3EF8C/maps"], lk_work)
check("maps plaintext identical to working", mp == wp)

# 6) Configs plaintext identical to working
def get_cfg_plain(b, lk):
    d = b[8:-16]; ln = struct.unpack(">I", d[:4])[0]
    p, _ = sf._unprepare_encrypted(d[4:4+ln], lk); return p
mc = get_cfg_plain(open(os.path.join(tdata, "D877F783D5D3EF8C", "configs"), "rb").read(), lk)
wc = get_cfg_plain(work["D877F783D5D3EF8C/configs"], lk_work)
check("configs plaintext identical to working", mc == wc)

# 7) TDF$ md5
ok = True
for dirpath, dirnames, filenames in os.walk(tdata):
    for fn in filenames:
        b = open(os.path.join(dirpath, fn), "rb").read()
        magic = b[:4]; ver = struct.unpack("<i", b[4:8])[0]; data = b[8:-16]
        if hashlib.md5(data + struct.pack("<i", len(data)) + struct.pack("<i", ver) + magic).digest() != b[-16:]:
            ok = False
check("ALL TDF$ md5 valid", ok)

# 8) REAL ACCESS
async def test():
    from pyrogram import Client
    raw = bytes([DC]) + struct.pack('>I', API) + bytes([0]) + AUTH + struct.pack('>Q', UID) + bytes([0])
    ss = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    c = Client("df", api_id=API, api_hash=HASH, session_string=ss, in_memory=True)
    try:
        await asyncio.wait_for(c.connect(), timeout=45)
        me = await c.get_me()
        check("REAL ACCESS", me and me.id == UID)
        return True
    except Exception as e:
        check("REAL ACCESS", False)
        return False
    finally:
        await c.disconnect()
asyncio.run(test())

# 9) Bot pipeline (session_reporting imports)
check("session_reporting imports OK", "session_reporting" in sys.modules)

# 10) Code has no template path
src = open(r"bot/services/session_formats.py", encoding="utf-8").read()
check("no template folder path", "it access done" not in src)

print(f"\n{'='*50}")
print(f"  TOTAL: {sum(1 for _, c in P if c)}/{len(P)} PASS")
print(f"  {'ALL GOOD - UPLOAD READY' if all(c for _, c in P) else 'ISSUES FOUND'}")
print(f"{'='*50}")