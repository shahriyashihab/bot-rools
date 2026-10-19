"""
Device information generator for realistic session data
CRITICAL: Using realistic mobile devices to prevent account freeze
"""

import random
import hashlib
import secrets
from datetime import datetime

ANDROID_DEVICES = [
    {"model": "Samsung SM-G998B", "sdk": 33, "brand": "samsung", "device": "o1s"},
    {"model": "Samsung SM-S918B", "sdk": 34, "brand": "samsung", "device": "dm3q"},
    {"model": "Samsung SM-G991B", "sdk": 33, "brand": "samsung", "device": "o1s"},
    {"model": "Samsung SM-A546B", "sdk": 33, "brand": "samsung", "device": "a54x"},
    {"model": "Samsung SM-A536B", "sdk": 33, "brand": "samsung", "device": "a53x"},
    {"model": "Samsung SM-G781B", "sdk": 32, "brand": "samsung", "device": "r8s"},
    {"model": "Samsung SM-A525F", "sdk": 32, "brand": "samsung", "device": "a52q"},
    {"model": "Samsung SM-A325F", "sdk": 31, "brand": "samsung", "device": "a32"},
    {"model": "Samsung SM-M515F", "sdk": 31, "brand": "samsung", "device": "m51"},
    {"model": "Xiaomi 2201116PG", "sdk": 33, "brand": "xiaomi", "device": "ingres"},
    {"model": "Xiaomi 2203121C", "sdk": 33, "brand": "xiaomi", "device": "plato"},
    {"model": "Xiaomi 22041219PG", "sdk": 32, "brand": "xiaomi", "device": "ruby"},
    {"model": "Xiaomi M2101K6P", "sdk": 31, "brand": "xiaomi", "device": "alioth"},
    {"model": "Xiaomi M2102J20SG", "sdk": 31, "brand": "xiaomi", "device": "star"},
    {"model": "POCO F4", "sdk": 33, "brand": "poco", "device": "munch"},
    {"model": "POCO X5 Pro", "sdk": 33, "brand": "poco", "device": "redwood"},
    {"model": "Redmi Note 12 Pro", "sdk": 33, "brand": "redmi", "device": "ruby"},
    {"model": "Redmi Note 11", "sdk": 32, "brand": "redmi", "device": "spes"},
    {"model": "OnePlus LE2115", "sdk": 33, "brand": "oneplus", "device": "lemonade"},
    {"model": "OnePlus CPH2449", "sdk": 34, "brand": "oneplus", "device": "salami"},
    {"model": "OnePlus IN2023", "sdk": 32, "brand": "oneplus", "device": "instantnoodle"},
    {"model": "OPPO CPH2399", "sdk": 33, "brand": "oppo", "device": "OP5585L1"},
    {"model": "OPPO CPH2305", "sdk": 32, "brand": "oppo", "device": "OP4F7FL1"},
    {"model": "vivo V2145", "sdk": 33, "brand": "vivo", "device": "PD2145"},
    {"model": "vivo V2136", "sdk": 32, "brand": "vivo", "device": "PD2136"},
    {"model": "Realme RMX3630", "sdk": 33, "brand": "realme", "device": "RE58B8L1"},
    {"model": "Realme RMX3393", "sdk": 33, "brand": "realme", "device": "RE54B4L1"},
    {"model": "Google Pixel 7 Pro", "sdk": 34, "brand": "google", "device": "cheetah"},
    {"model": "Google Pixel 7", "sdk": 34, "brand": "google", "device": "panther"},
    {"model": "Google Pixel 6a", "sdk": 33, "brand": "google", "device": "bluejay"},
    {"model": "Huawei VOG-L29", "sdk": 29, "brand": "huawei", "device": "HWVOG"},
    {"model": "Huawei ELS-NX9", "sdk": 30, "brand": "huawei", "device": "HWELS"},
    {"model": "Motorola moto g84 5G", "sdk": 33, "brand": "motorola", "device": "vicky"},
    {"model": "Motorola edge 40", "sdk": 33, "brand": "motorola", "device": "rtwo"},
    {"model": "Nokia G60 5G", "sdk": 32, "brand": "nokia", "device": "FTN"},
]

IOS_DEVICES = [
    {"model": "iPhone 15 Pro Max", "version": "17.1.2", "build": "21B101"},
    {"model": "iPhone 15 Pro", "version": "17.1.2", "build": "21B101"},
    {"model": "iPhone 15", "version": "17.1.1", "build": "21B91"},
    {"model": "iPhone 14 Pro Max", "version": "17.1.2", "build": "21B101"},
    {"model": "iPhone 14 Pro", "version": "17.1.1", "build": "21B91"},
    {"model": "iPhone 14", "version": "17.0.3", "build": "21A360"},
    {"model": "iPhone 13 Pro Max", "version": "17.1.2", "build": "21B101"},
    {"model": "iPhone 13 Pro", "version": "17.1.1", "build": "21B91"},
    {"model": "iPhone 13", "version": "17.0.3", "build": "21A360"},
    {"model": "iPhone 12 Pro Max", "version": "16.7.2", "build": "20H115"},
    {"model": "iPhone 12 Pro", "version": "16.7.1", "build": "20H30"},
    {"model": "iPhone 12", "version": "16.6.1", "build": "20G81"},
    {"model": "iPhone SE (3rd generation)", "version": "17.1", "build": "21B74"},
    {"model": "iPhone 11 Pro Max", "version": "16.7.2", "build": "20H115"},
    {"model": "iPhone 11 Pro", "version": "16.7.1", "build": "20H30"},
    {"model": "iPhone 11", "version": "16.6.1", "build": "20G81"},
]

SYSTEM_LANG_CODES = [
    "en", "en-US", "en-GB", "ru", "es", "pt-BR", "de", "fr",
    "it", "ja", "ko", "zh-CN", "zh-TW", "ar", "hi", "bn", "id",
    "ms", "th", "vi", "tr", "pl", "nl", "uk", "cs", "ro", "el"
]

# P7-75 dead code removed: TELEGRAM_VERSIONS was selected but NEVER used
# anywhere (every client hardcodes its own app_version string) - the unused
# pick only suggested variety the transport never actually sent.

def get_random_android_device(rng=None):
    """Get random Android device configuration (rng optional for determinism)."""
    rng = rng or random
    device = rng.choice(ANDROID_DEVICES)
    lang = rng.choice(SYSTEM_LANG_CODES)

    return {
        "device_model": device["model"],
        "system_version": f"Android {device['sdk'] - 20}",
        "app_version": "Telegram Android 0.28.1.1771-arm64-v8a",
        "system_lang_code": lang,
        "lang_pack": "android",
        "lang_code": lang.split("-")[0],
        "sdk": device["sdk"],
        "brand": device["brand"],
        "device_name": device["device"],
    }

def get_random_ios_device(rng=None):
    """Get random iOS device configuration (rng optional for determinism)."""
    rng = rng or random
    device = rng.choice(IOS_DEVICES)
    lang = rng.choice(SYSTEM_LANG_CODES)

    return {
        "device_model": device["model"],
        "system_version": f"iOS {device['version']}",
        "app_version": "Telegram iOS 9.8.1",
        "system_lang_code": lang,
        "lang_pack": "ios",
        "lang_code": lang.split("-")[0],
        "ios_version": device["version"],
        "build": device["build"],
    }

def get_random_device(prefer_android=True):
    """Get random device - 85% Android, 15% iOS by default"""
    if prefer_android:
        if random.random() < 0.85:
            return get_random_android_device()
        else:
            return get_random_ios_device()
    else:
        if random.random() < 0.50:
            return get_random_android_device()
        else:
            return get_random_ios_device()

def generate_device_info(seed: str = "", country_code: str = None):
    """Generate realistic device information for Pyrogram clients.

    P7-75 FINGERPRINT CONSISTENCY: the same Telegram auth key used to present
    a DIFFERENT device_model / app_version / lang_pack on every step
    (login -> 2FA -> spam check -> device check -> delivery) - a server-side
    fingerprinting signal Telegram can observe as "one session, many devices".
    Callers that handle ONE account pass its ``seed`` (the phone number) so
    every step deterministically derives the SAME stable device profile;
    tokens/secret material still get fresh randomness per call (they are
    per-session artifacts, not identity fingerprints). Callers without a
    seed get the old fully-random behaviour.

    CORE CONFIG: when ``country_code`` is given and the admin set a Platform
    override for that country ("android"/"ios"), the device comes from that
    platform's pool - same as the login client. Without an override the
    rng flow is byte-for-byte the legacy one (existing seeds unchanged).
    """
    if seed:
        rng = random.Random(hashlib.sha256(str(seed).encode()).digest())
    else:
        rng = random.SystemRandom()

    # CORE CONFIG per-country platform override (android / ios / "" = none).
    _platform_override = ""
    if country_code:
        try:
            from bot.data.countries import get_country_platform
            _platform_override = (get_country_platform(country_code) or "").lower()
        except Exception:
            _platform_override = ""

    if seed:
        if _platform_override == "android":
            device = get_random_android_device(rng)
        elif _platform_override == "ios":
            device = get_random_ios_device(rng)
        else:
            # 88% Android / 12% iOS, deterministic per seed (legacy flow).
            device = get_random_android_device(rng) if rng.random() < 0.88 else get_random_ios_device(rng)
    else:
        if _platform_override == "android":
            device = get_random_android_device(rng)
        elif _platform_override == "ios":
            device = get_random_ios_device(rng)
        else:
            device = get_random_device()

    device_token = secrets.token_hex(32)
    device_token_secret = secrets.token_hex(16)
    device_secret = secrets.token_hex(16)
    signature = secrets.token_hex(20)
    certificate = secrets.token_hex(20)
    safetynet = secrets.token_urlsafe(200)[:172]
    perf_cat = rng.randint(2, 4)

    tz_offsets = [
        0, 3600, 5400, 7200, 10800, 12600, 14400, 18000, 19800,
        21600, 23400, 25200, 28800, 32400, 34200, 36000, 37800, 39600
    ]
    tz_offset = rng.choice(tz_offsets)

    return {
        "device_token": device_token,
        "device_token_secret": device_token_secret,
        "device_secret": device_secret,
        "signature": signature,
        "certificate": certificate,
        "safetynet": safetynet,
        "perf_cat": perf_cat,
        "tz_offset": tz_offset,
        "ipv6": rng.choice([True, False]),
        "device_model": device["device_model"],
        "system_version": device["system_version"],
        "app_version": device["app_version"],
        "lang_code": device.get("lang_code", "en"),
        "system_lang_code": device.get("system_lang_code", "en"),
    }

def get_device_for_country(country_code: str):
    """Get device configuration optimized for specific country.

    A per-country platform override (Country Pricing -> CORE CONFIG ->
    Platform) wins when the admin set one for this country; otherwise the
    existing per-country heuristics below decide exactly as before.
    """
    # CORE CONFIG override: "android" / "ios" forces the device pool for
    # this country only. Any failure falls through to the legacy heuristics.
    try:
        from bot.data.countries import get_country_platform
        _platform = (get_country_platform(country_code) or "").lower()
    except Exception:
        _platform = ""
    if _platform == "android":
        device = get_random_android_device()
        return device
    if _platform == "ios":
        device = get_random_ios_device()
    else:
        android_heavy_countries = ["+880", "+91", "+92", "+234", "+62", "+84", "+66", "+63"]
        ios_heavy_countries = ["+1", "+44", "+81", "+82", "+49", "+33"]

        if country_code in android_heavy_countries:
            device = get_random_android_device()
        elif country_code in ios_heavy_countries:
            device = get_random_device(prefer_android=False)
        else:
            device = get_random_device()
    
    country_lang_map = {
        "+880": ["bn", "en"],
        "+91": ["hi", "en", "bn", "ta"],
        "+92": ["ur", "en"],
        "+86": ["zh-CN"],
        "+81": ["ja"],
        "+82": ["ko"],
        "+55": ["pt-BR"],
        "+52": ["es"],
        "+34": ["es"],
        "+33": ["fr"],
        "+49": ["de"],
        "+39": ["it"],
        "+351": ["pt"],
        "+48": ["pl"],
        "+20": ["ar"],
        "+966": ["ar"],
        "+971": ["ar"],
        "+62": ["id"],
        "+60": ["ms"],
        "+66": ["th"],
        "+84": ["vi"],
        "+63": ["en", "tl"],
        "+95": ["my"],
        "+959": ["my"],
    }
    
    if country_code in country_lang_map:
        lang = random.choice(country_lang_map[country_code])
        device["lang_code"] = lang
        device["system_lang_code"] = lang
    
    return device

print("✅ Realistic Device Generator Module Loaded")
print(f"   Android devices: {len(ANDROID_DEVICES)}")
print(f"   iOS devices: {len(IOS_DEVICES)}")
