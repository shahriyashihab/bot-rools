# bot/utils/country_codes.py - stable 4-character country display codes
"""Centralized, FROZEN mapping of every COUNTRY_DATA country code to a
four-character uppercase display code (FLAG + " " + CODE) used by the
Country Pricing CORE CONFIG grid.

Guarantees:
  - ONE entry per every key of ``bot.utils.country_data.COUNTRY_DATA``.
  - Every code is exactly 4 characters ([A-Z_]).
  - Every code is UNIQUE - two different countries can never share a label,
    so two similar countries can never be confused in the grid.
  - The mapping is STATIC: the same country always renders the same code,
    before and after deployment. Nothing here is generated at runtime.

``get_country_display_code`` falls back to a deterministic derivation for
codes that are not in COUNTRY_DATA (e.g. imported via bulk JSON import);
those can never collide with the frozen set because unknown codes are not
in this file.
"""

import re

from bot.utils.country_data import COUNTRY_DATA

CC_DISPLAY_CODES = {
    "+880": "BANG",
    "+91": "INDI",
    "+92": "PAKI",
    "+94": "SRLA",
    "+977": "NEPA",
    "+975": "BHUT",
    "+960": "MALD",
    "+93": "AFGH",
    "+213": "ALGE",
    "+244": "ANGO",
    "+229": "BENI",
    "+267": "BOTW",
    "+226": "BURK",
    "+257": "BURU",
    "+237": "CAMR",
    "+238": "CAPE",
    "+236": "CEAF",
    "+235": "CHAD",
    "+269": "COMO",
    "+242": "CONB",
    "+243": "CONK",
    "+225": "COTE",
    "+253": "DJIB",
    "+20": "EGYP",
    "+240": "EQGU",
    "+291": "ERIT",
    "+251": "ETHI",
    "+241": "GABO",
    "+220": "GAMB",
    "+233": "GHAN",
    "+224": "GUIN",
    "+245": "GUIB",
    "+254": "KENY",
    "+266": "LESO",
    "+231": "LIBE",
    "+218": "LIBY",
    "+261": "MADA",
    "+265": "MALW",
    "+223": "MALI",
    "+222": "MAUT",
    "+230": "MAUR",
    "+212": "MORO",
    "+258": "MOZA",
    "+264": "NAMI",
    "+227": "IGER",
    "+234": "NIGE",
    "+250": "RWAN",
    "+239": "SAOT",
    "+221": "SENE",
    "+248": "SEYC",
    "+232": "SIEL",
    "+252": "SOMA",
    "+27": "SAFR",
    "+211": "SSUD",
    "+249": "SUDA",
    "+268": "ESWA",
    "+255": "TANZ",
    "+228": "TOGO",
    "+216": "TUNI",
    "+256": "UGAN",
    "+260": "ZAMB",
    "+263": "ZIMB",
    "+262": "REUA",
    "+262262": "RUNI",
    "+262263": "UNIO",
    "+262692": "NION",
    "+262693": "RUNC",
    "+262269": "MAYO",
    "+262639": "AYOT",
    "+246": "BIOT",
    "+247": "ASCE",
    "+290": "SHEA",
    "+2908": "TRIS",
    "+1": "USAA",
    "+1242": "BAHA",
    "+1246": "BARB",
    "+1264": "ANGU",
    "+1268": "ANTB",
    "+1284": "BVIR",
    "+1340": "USVI",
    "+1345": "CAYM",
    "+1441": "BERM",
    "+1473": "GREN",
    "+1649": "TCAI",
    "+1664": "MONT",
    "+1670": "NMAR",
    "+1671": "GUAM",
    "+1684": "AMSA",
    "+1721": "SINT",
    "+1758": "STLU",
    "+1767": "DOMI",
    "+1784": "STVI",
    "+1787": "PRIA",
    "+1809": "DOMA",
    "+1829": "DOMB",
    "+1849": "DOMC",
    "+1868": "TRIN",
    "+1869": "SKNE",
    "+1876": "JAMA",
    "+1658": "JAMB",
    "+1939": "PRIB",
    "+54": "ARGN",
    "+591": "BOLI",
    "+55": "BRAZ",
    "+56": "CHIL",
    "+57": "COLO",
    "+506": "COSR",
    "+53": "CUBA",
    "+502": "GUAT",
    "+504": "HOND",
    "+52": "MEXI",
    "+505": "NICA",
    "+507": "PANA",
    "+595": "PARA",
    "+51": "PERU",
    "+598": "URUG",
    "+58": "VENE",
    "+501": "BELI",
    "+503": "ELSA",
    "+593": "ECUA",
    "+509": "HAIT",
    "+592": "GUYA",
    "+597": "SURI",
    "+500": "FALK",
    "+508": "STPM",
    "+590": "GUAD",
    "+5901": "GUAA",
    "+5902": "GUAB",
    "+5903": "GUAC",
    "+5904": "GUAE",
    "+5905": "GUAF",
    "+5906": "STMN",
    "+5907": "STBA",
    "+5908": "STBB",
    "+5909": "STMS",
    "+594": "FREG",
    "+596": "MART",
    "+599": "CURA",
    "+5993": "BONA",
    "+5994": "SABA",
    "+5995": "STEU",
    "+5997": "CURC",
    "+5999": "CURD",
    "+297": "ARUB",
    "+61": "AUST",
    "+614": "AUSM",
    "+64": "NEWZ",
    "+675": "PAPN",
    "+676": "TONG",
    "+679": "FIJI",
    "+680": "PALA",
    "+682": "COOK",
    "+683": "NIUE",
    "+685": "SAMO",
    "+686": "KIRI",
    "+687": "NEWC",
    "+688": "TUVA",
    "+689": "FREP",
    "+690": "TOKE",
    "+691": "MICO",
    "+692": "MARS",
    "+677": "SOLO",
    "+678": "VANU",
    "+681": "WALL",
    "+674": "NAUR",
    "+672": "NORF",
    "+6721": "AATA",
    "+6723": "NORB",
    "+86": "CHIN",
    "+852": "HKGA",
    "+853": "MACA",
    "+886": "TAIW",
    "+81": "JAPA",
    "+82": "KORE",
    "+850": "NKOR",
    "+976": "MONG",
    "+60": "MALA",
    "+62": "INDO",
    "+63": "PHIL",
    "+65": "SING",
    "+66": "THAI",
    "+84": "VIET",
    "+855": "CAMB",
    "+856": "LAOS",
    "+95": "MYAN",
    "+673": "BRUN",
    "+670": "TIMO",
    "+966": "SAUD",
    "+971": "UAEA",
    "+970": "PALE",
    "+962": "JORD",
    "+965": "KUWA",
    "+974": "QATA",
    "+968": "OMAN",
    "+967": "YEME",
    "+961": "LEBA",
    "+963": "SYRI",
    "+964": "IRAQ",
    "+972": "ISRA",
    "+973": "BAHR",
    "+98": "IRAN",
    "+90": "TURK",
    "+7": "RUSS",
    "+76": "KAZA",
    "+77": "AZAK",
    "+992": "TAJI",
    "+993": "URKM",
    "+994": "AZER",
    "+995": "GEOR",
    "+996": "KYRG",
    "+998": "UZBE",
    "+33": "FRAN",
    "+44": "UKIN",
    "+49": "GERM",
    "+39": "ITAL",
    "+34": "SPAI",
    "+351": "PORT",
    "+31": "NETH",
    "+32": "BELG",
    "+352": "LUXE",
    "+41": "SWIT",
    "+43": "OSTR",
    "+353": "IREL",
    "+441534": "JERS",
    "+441624": "IOMA",
    "+441481": "GUER",
    "+45": "DENE",
    "+46": "SWED",
    "+47": "NORW",
    "+358": "FINL",
    "+354": "ICEL",
    "+370": "LITH",
    "+371": "LATV",
    "+372": "ESTO",
    "+298": "FAER",
    "+299": "GRNL",
    "+3584": "ALAN",
    "+48": "POLA",
    "+420": "CZEZ",
    "+421": "SLOV",
    "+36": "HUNG",
    "+40": "ROMA",
    "+359": "BULG",
    "+380": "UKRA",
    "+375": "BELA",
    "+373": "MOLD",
    "+30": "GREC",
    "+357": "CYPR",
    "+356": "MALT",
    "+355": "ALBA",
    "+385": "CROA",
    "+386": "SLVN",
    "+387": "BOSN",
    "+381": "SERB",
    "+382": "MNEG",
    "+383": "KOSO",
    "+389": "NMAC",
    "+374": "ARME",
    "+376": "ANDO",
    "+377": "MONA",
    "+378": "SANM",
    "+350": "GIBR",
    "+423": "LIEC",
}


# Small runtime cache for codes derived via the fallback (never frozen keys).
_DERIVED_CACHE = {}
# Keys of COUNTRY_DATA that carry a frozen code above.
_FROZEN_CODES = frozenset(CC_DISPLAY_CODES.values())

_DIGIT_LETTER = {"0": "Z", "1": "A", "2": "B", "3": "C", "4": "D",
                 "5": "E", "6": "F", "7": "G", "8": "H", "9": "I"}


def get_country_display_code(country_code: str) -> str:
    """Return the stable 4-character display code for a country code.

    Frozen mapping wins. Unknown codes (not in COUNTRY_DATA, e.g. from a
    bulk JSON import) get a deterministic 4-letter derivation so the grid
    can still render them; derivation never mutates the frozen set.
    """
    code = (country_code or "").strip()
    frozen = CC_DISPLAY_CODES.get(code)
    if frozen:
        return frozen

    # Deterministic fallback: same input always yields the same code.
    cached = _DERIVED_CACHE.get(code)
    if cached:
        return cached

    from bot.utils.country_data import get_country_info
    name, _flag = get_country_info(code)
    letters = re.sub(r"[^A-Za-z]", "", name or "").upper()
    if not letters:
        # Unknown code whose "name" is the numeric code itself: map the
        # digits to letters so the label is still 4 uppercase Latin chars.
        letters = "".join(_DIGIT_LETTER.get(d, "X")
                          for d in re.sub(r"\D", "", code))
    base = letters[:4].ljust(4, "X")

    # Resolve against the frozen set + already-derived codes so a derived
    # code can never collide with a real country's label.
    taken = set(_FROZEN_CODES)
    taken.update(_DERIVED_CACHE.values())
    if base not in taken:
        _DERIVED_CACHE[code] = base
        return base

    digits = re.sub(r"\D", "", code)
    for d in reversed(digits):
        letter = _DIGIT_LETTER.get(d, "X")
        for cand in (base[:3] + letter, base[:2] + letter + base[3],
                     base[:1] + letter + base[2:]):
            if cand not in taken:
                _DERIVED_CACHE[code] = cand
                return cand
    # Exhausted: append positional letters A, B, C ... (deterministic).
    for ordinal in range(26):
        cand = base[:3] + chr(ord("A") + ordinal)
        if cand not in taken:
            _DERIVED_CACHE[code] = cand
            return cand
    _DERIVED_CACHE[code] = base
    return base


def build_core_config_grid_labels(country_codes) -> dict:
    """Display-code map for a set of country codes, collision-safe.

    Returns ``{country_code: 4-char code}``. Derived (non-frozen) codes are
    disambiguated within THIS set only; frozen codes always win.
    """
    result = {}
    for code in country_codes:
        result[code] = get_country_display_code(code)
    return result


# ------------------------------------------------------------------
# Stable 6-character proxy display labels ("Add Proxy to Country" grid).
#
# Same guarantees as CC_DISPLAY_CODES: FROZEN, exactly 6 chars ([A-Z_]),
# and two different countries can never share one label. Short names are
# padded with "_" to the fixed width (INDIA_, NEPAL_, SAUDI_ ...); names
# whose first six letters would collide (or that are spec-pinned) carry
# curated entries below, so nothing is blindly runtime-truncated.
# ------------------------------------------------------------------

PX_LABEL_LEN = 6

PX_DISPLAY_CODES = {
    # Curated (spec-pinned / visually clearer than the plain slice):
    "+966": "SAUDI_",   # Saudi Arabia
    "+971": "UAEARB",   # United Arab Emirates (UAE)
    "+44": "UKING_",    # United Kingdom
    # Curated collision resolutions (same first-6 letters as another name):
    "+222": "MAURIT",   # Mauritania (vs Mauritius)
    "+230": "MAURIS",   # Mauritius
    "+245": "GUIBS_",   # Guinea-Bissau (vs Guinea)
    "+1767": "DOMINC",  # Dominica (vs Dominican Republic)
    "+246": "BRITIN",   # British Indian Ocean Territory (vs British Virgin Is.)
    "+594": "FRGUIA",   # French Guiana (vs French Polynesia)
    "+689": "FRPOLY",   # French Polynesia
    "+6721": "ANTAR_",  # Australian Antarctic Territory (vs Australia)
}


def _px_label_from_name(name: str) -> str:
    """Deterministic 6-char label from a name: letters only, upper, '_' pad."""
    letters = re.sub(r"[^A-Za-z]", "", name or "").upper()
    return letters[:PX_LABEL_LEN].ljust(PX_LABEL_LEN, "_")


# Precompute derived labels for every known COUNTRY_DATA key at IMPORT
# time: the same input always yields the same label in every process.
# EVERY code gets its OWN unique label (duplicate territory names and
# same-first-6-letters names are disambiguated deterministically with
# digit-derived letters, exactly like the 4-char fallback below).
_PX_TAKEN = set(PX_DISPLAY_CODES.values())
_PX_DERIVED = {}
for _code in sorted(COUNTRY_DATA):
    if _code in PX_DISPLAY_CODES:
        continue
    _name = COUNTRY_DATA[_code]["name"]
    _base = _px_label_from_name(_name)
    if _base not in _PX_TAKEN:
        _PX_TAKEN.add(_base)
        _PX_DERIVED[_code] = _base
        continue
    # Deterministic disambiguation for a genuinely different code whose
    # label base collides: swap in letters derived from the dial code.
    _digits = "".join(_DIGIT_LETTER.get(d, "X") for d in re.sub(r"\D", "", _code))
    _resolved = False
    for _pos in (5, 4, 3, 2, 1, 0):
        for _ch in _digits:
            _cand = _base[:_pos] + _ch + _base[_pos + 1:]
            if _cand not in _PX_TAKEN:
                _PX_TAKEN.add(_cand)
                _PX_DERIVED[_code] = _cand
                _resolved = True
                break
        if _resolved:
            break
    if not _resolved:  # pragma: no cover - practically unreachable
        for _ordinal in range(26):
            _cand = _base[:5] + chr(ord("A") + _ordinal)
            if _cand not in _PX_TAKEN:
                _PX_TAKEN.add(_cand)
                _PX_DERIVED[_code] = _cand
                _resolved = True
                break
    if not _resolved:  # pragma: no cover - practically unreachable
        _PX_TAKEN.add(_base)
        _PX_DERIVED[_code] = _base

# Lazy fallback for codes that are NOT in COUNTRY_DATA (bulk imports etc.),
# mirroring get_country_display_code's lazy derivation.
_PX_LAZY_CACHE = {}


def get_country_proxy_label(country_code: str) -> str:
    """Return the stable 6-character display label for a country code.

    Frozen curated map wins, then the import-time derived map, then a
    deterministic lazy derivation for unknown codes. Never mutates the
    frozen set.
    """
    code = (country_code or "").strip()
    frozen = PX_DISPLAY_CODES.get(code)
    if frozen:
        return frozen
    derived = _PX_DERIVED.get(code)
    if derived:
        return derived

    cached = _PX_LAZY_CACHE.get(code)
    if cached:
        return cached

    from bot.utils.country_data import get_country_info
    name, _flag = get_country_info(code)
    letters = re.sub(r"[^A-Za-z]", "", name or "").upper()
    if name == code or not letters:
        # Unknown code whose "name" is the numeric code itself.
        letters = "".join(_DIGIT_LETTER.get(d, "X")
                          for d in re.sub(r"\D", "", code))
    base = letters[:PX_LABEL_LEN].ljust(PX_LABEL_LEN, "_")

    taken = set(_PX_TAKEN)
    taken.update(_PX_LAZY_CACHE.values())
    if base not in taken:
        _PX_LAZY_CACHE[code] = base
        return base

    digits = re.sub(r"\D", "", code)
    for d in reversed(digits):
        letter = _DIGIT_LETTER.get(d, "X")
        for cand in (base[:5] + letter, base[:4] + letter + base[5],
                     base[:3] + letter + base[4:]):
            if cand not in taken:
                _PX_LAZY_CACHE[code] = cand
                return cand
    for ordinal in range(26):
        cand = base[:5] + chr(ord("A") + ordinal)
        if cand not in taken:
            _PX_LAZY_CACHE[code] = cand
            return cand
    _PX_LAZY_CACHE[code] = base
    return base
