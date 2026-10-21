"""Hindi typed in pre-Unicode fonts, and turning the common one back into real Hindi.

Many Hindi government and NGO documents are typed in fonts like Kruti Dev, which draw Devanagari
over plain Latin codes. Text extraction then gets Latin gibberish ("dsUnz ljdkj" for "केन्द्र
सरकार") that nobody can search for. Nebula spots these by font name (PDF, Word) or by the text
itself (anything else), and converts Kruti Dev and DevLys (the same layout) to Unicode. Other
legacy fonts such as Chanakya use different layouts; those are flagged, not converted.

The conversion follows the standard Kruti Dev 010 keyboard map. Rare glyphs may come out
imperfectly, so converted text is flagged for checking against the original.
"""

from __future__ import annotations

import re

# Kruti Dev 010 / DevLys 010 -> Unicode. Matched longest first at each position.
_MAP: dict[str, str] = {
    # independent vowels
    "v‚": "ऑ", "vks": "ओ", "vkS": "औ", "vk": "आ", "v": "अ", "b±": "ईं", "Ã": "ई", "bZ": "ई", "b": "इ",
    "m": "उ", "Å": "ऊ", ",s": "ऐ", ",": "ए", "_": "ऋ",
    # consonants, with their half forms
    "d": "क", "Dk": "क", "D": "क्", "[k": "ख", "[": "ख्", "x": "ग", "Xk": "ग", "X": "ग्", "?k": "घ", "?": "घ्",
    "Ä": "घ", "³": "ङ", "p": "च", "Pk": "च", "P": "च्", "N": "छ", "t": "ज", "Tk": "ज", "T": "ज्", ">": "झ",
    "÷": "झ्", "¥": "ञ", "V": "ट", "B": "ठ", "M": "ड", "<": "ढ", ".k": "ण", ".": "ण्", "r": "त", "Rk": "त",
    "R": "त्", "Fk": "थ", "F": "थ्", "n": "द", "/k": "ध", "/": "ध्", "èk": "ध", "è": "ध्", "Ë": "ध्", "u": "न",
    "Uk": "न", "U": "न्", "i": "प", "Ik": "प", "I": "प्", "Q": "फ", "¶": "फ्", "c": "ब", "Ck": "ब", "C": "ब्",
    "Hk": "भ", "H": "भ्", "e": "म", "Ek": "म", "E": "म्", ";": "य", "¸": "य्", "j": "र", "y": "ल", "Yk": "ल",
    "Y": "ल्", "G": "ळ", "o": "व", "Ok": "व", "O": "व्", "'k": "श", "'": "श्", "Ük": "श", "Ü": "श्",
    '"k': "ष", '"': "ष्", "l": "स", "Lk": "स", "L": "स्", "g": "ह",
    # conjuncts
    "{k": "क्ष", "{": "क्ष्", "=": "त्र", "«": "त्र्", "K": "ज्ञ", "J": "श्र", "|": "द्य", "}": "द्व", ")": "द्ध",
    "Ø": "क्र", "Dr": "क्त", "Ùk": "त्त", "Ù": "त्त्", "é": "न्न", "™": "न्न्", "–": "दृ", "—": "कृ", "Ñ": "कृ",
    "à": "ह्न", "á": "ह्य", "â": "हृ", "ã": "ह्म", "ºz": "ह्र", "º": "ह्", "í": "द्द", "Ì": "द्द", "ê": "ट्ट",
    "ë": "ट्ठ", "Í": "ट्ट", "Î": "ट्ठ", "ì": "ड्ड", "ï": "ड्ढ", "Ï": "ड्ड", "Ô": "ड्ढ", "Nî": "छ्य", "Vî": "ट्य",
    "Bî": "ठ्य", "Mî": "ड्य", "<î": "ढ्य", "î": "्य", "Vª": "ट्र", "Mª": "ड्र", "<ª": "ढ्र", "Nª": "छ्र",
    "ª": "्र", "Ý": "फ्र", "æ": "द्र", "ç": "प्र", "Á": "प्र", "xz": "ग्र", "nzZ": "र्द्र", ")Z": "र्द्ध",
    "#": "रु", ":": "रू", "z": "्र", "~j": "्र",
    # nukta forms
    "d+": "क़", "[+k": "ख़", "[+": "ख़्", "x+": "ग़", "t+": "ज़", "T+": "ज़्", "M+": "ड़", "<+": "ढ़", "Q+": "फ़",
    "¶+": "फ़्", ";+": "य़", "j+": "ऱ", "u+": "ऩ", "+": "़",
    # vowel signs and marks ("Z" is the reph and "f" the i-sign, both typed out of order; see below)
    "‚": "ॉ", "ks": "ो", "kS": "ौ", "k": "ा", "h": "ी", "q": "ु", "w": "ू", "`": "ृ", "s": "े", "S": "ै",
    "a": "ं", "¡": "ँ", "%": "ः", "W": "ॅ", "~": "्", "•": "ऽ", "·": "ऽ", "È": "ीं", "Ê": "ीZ", "±": "Zं",
    # digits and punctuation
    "å": "०", "ƒ": "१", "„": "२", "…": "३", "†": "४", "‡": "५", "ˆ": "६", "‰": "७", "Š": "८", "‹": "९",
    "A": "।", "^": "‘", "*": "’", "Þ": "“", "ß": "”", "(": ";", "¼": "(", "½": ")", "¿": "{", "À": "}", "¾": "=",
    "-": ".", "&": "-", "]": ",", "@": "/", "\\": "?", "Œ": "॰", "$": "+",
}
_TOKEN = re.compile("|".join(re.escape(k) for k in sorted(_MAP, key=len, reverse=True)))

_SIGNS = set("ािीुूृेैोौंँः़ॅॉ")      # written after the consonant they belong to
_VIRAMA = "्"
_NUKTA = "़"
_CONSONANT = re.compile(r"[क-हक़-य़]")

# Very common Hindi words as they come out of Kruti Dev text (के का की में है और से को पर भी कि यह था
# एक लिए नहीं तथा द्वारा गया हो जो इस किया सभी साथ अपने हुए जाता सरकार जिला कार्य बाद तक ...).
_KRUTI_WORDS = {
    "ds", "dk", "dh", "esa", "gS", "gSa", "vkSj", "ls", "dks", "ij", "Hkh", "fd", ";g", "Fkk", "Fks", "Fkh", ",d",
    "fy,", "ugha", "rFkk", "}kjk", "x;k", "x;s", "x;h", "gks", "tks", "bl", "mlds", "fd;k", "djsa", "djuk", "lHkh",
    "lkFk", "vius", "gq,", "tkrk", "tkrh", "tk", "jgs", "dqN", "fy;s", ";s", "vkfn", "ljdkj", "ftyk", "dk;Z", "ckn",
    "rd", "o", "dj", "gksxk", "gksxh", "gSaA", "gSA", "FkkA", "fd;s", "mDr", "lacaf/kr", "vuqlkj",
}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def kruti_to_unicode(text: str) -> str:
    """Convert Kruti Dev / DevLys text to Unicode Hindi."""
    s = _TOKEN.sub(lambda m: _MAP[m.group(0)], text)
    return _place_reph(_place_i_sign(s))


def _place_i_sign(s: str) -> str:
    """'f' (the i-sign) is typed before its consonant (cluster); Unicode puts it after."""
    while (i := s.find("f")) != -1:
        j = i + 1
        if j >= len(s) or not _CONSONANT.match(s[j]):
            s = s[:i] + "ि" + s[i + 1:]
            continue
        j += 1
        if j < len(s) and s[j] == _NUKTA:
            j += 1
        while j + 1 < len(s) and s[j] == _VIRAMA and _CONSONANT.match(s[j + 1]):
            j += 2
            if j < len(s) and s[j] == _NUKTA:
                j += 1
        s = s[:i] + s[i + 1:j] + "ि" + s[j:]
    return s


def _place_reph(s: str) -> str:
    """'Z' (the reph, a half र over the letter) is typed after its syllable; Unicode puts र् before it."""
    while (i := s.find("Z")) != -1:
        j = i - 1
        while j >= 0 and s[j] in _SIGNS:
            j -= 1
        while j - 2 >= 0 and s[j - 1] == _VIRAMA and _CONSONANT.match(s[j - 2]):
            j -= 2
        if j < 0 or not _CONSONANT.match(s[j]):
            s = s[:i] + s[i + 1:]
            continue
        s = s[:j] + "र्" + s[j:i] + s[i + 1:]
    return s


def font_kind(name: str | None) -> str | None:
    """'kruti' for Kruti Dev / DevLys (convertible), 'legacy' for other pre-Unicode Hindi fonts, else None."""
    if not name:
        return None
    n = re.sub(r"[^a-z0-9]", "", str(name).rsplit("+", 1)[-1].lower())
    if n.startswith(("krutidev", "kruti", "devlys", "devlis")):
        return "kruti"
    if n.startswith(("chanakya", "walkmanchanakya", "shusha", "akruti", "shreedev", "dvtt", "apsdv")):
        return "legacy"
    return None


def looks_like_kruti(text: str, *, font_seen: bool = False) -> bool:
    """Whether Latin-looking text is really Hindi typed in Kruti Dev.

    It counts very common Hindi words in their Kruti spelling: they make up a large share of any
    Hindi passage and almost never appear in English. When the file is known to use a Kruti font,
    less evidence is needed.
    """
    if not text or _DEVANAGARI.search(text):
        return False
    tokens = text.split()
    if not tokens:
        return False
    found = [t if t in _KRUTI_WORDS else t.rstrip("A") for t in tokens
             if t in _KRUTI_WORDS or t.rstrip("A") in _KRUTI_WORDS]
    if font_seen:
        return len(found) >= 1 and len(found) / len(tokens) >= 0.05
    return len(found) >= 3 and len(set(found)) >= 2 and len(found) / len(tokens) >= 0.12
