"""Kruti Dev text back to Unicode Hindi, and telling it apart from English."""

from __future__ import annotations

import pytest

from nebula.ingest.legacy_fonts import font_kind, kruti_to_unicode, looks_like_kruti


@pytest.mark.parametrize("kruti, hindi", [
    ("dsUnz ljdkj", "केन्द्र सरकार"),
    ("Hkkjr", "भारत"),
    ("fgUnh", "हिन्दी"),
    ("fo|ky;", "विद्यालय"),
    ("vkaxuokM+h", "आंगनवाड़ी"),
    ("f'k{kk", "शिक्षा"),
    ("cPpk", "बच्चा"),
    ("jkT; ljdkj", "राज्य सरकार"),
    ("dk;ZØe", "कार्यक्रम"),
    ("ewfrZ", "मूर्ति"),
    ("vkaxuckM+h dk;ZdrkZ", "आंगनबाड़ी कार्यकर्ता"),
    ("ftyk dk;ZØe vf/kdkjh", "जिला कार्यक्रम अधिकारी"),
    ("efgyk ,oa cky fodkl foHkkx", "महिला एवं बाल विकास विभाग"),
    ("mÙkj çns'k", "उत्तर प्रदेश"),
    ("e/; çns'k vkSj jktLFkku", "मध्य प्रदेश और राजस्थान"),
    ("fLFkfr", "स्थिति"),
    ("f}rh;", "द्वितीय"),
    ("iks\"k.k", "पोषण"),
    ("cPpksa dks ugha", "बच्चों को नहीं"),
    ("ifj;kstuk dh fLFkfr A", "परियोजना की स्थिति ।"),
    ("lu~ 2026", "सन् 2026"),
])
def test_kruti_dev_converts_to_unicode(kruti, hindi):
    assert kruti_to_unicode(kruti) == hindi


def test_kruti_is_told_apart_from_english():
    hindi = "ftyk dk;ZØe vf/kdkjh }kjk lHkh vkaxuckM+h dsUnzksa dk fujh{k.k fd;k x;k vkSj fjiksVZ Hksth xbZA"
    assert looks_like_kruti(hindi)
    for english in ("The district programme officer inspected all the centres and sent a report.",
                    "Visit us at the office; dj and ls are fine.", "Q3 budget: 12 lakh", ""):
        assert not looks_like_kruti(english)
    assert not looks_like_kruti("पहले से हिंदी में है और यह यूनिकोड है")
    # A short heading only counts when the file is known to use a Kruti font.
    assert not looks_like_kruti("ifj;kstuk dh fLFkfr")
    assert looks_like_kruti("ifj;kstuk dh fLFkfr", font_seen=True)


def test_font_names():
    assert font_kind("/ABCDEF+KrutiDev010") == "kruti"
    assert font_kind("Kruti Dev 010") == "kruti"
    assert font_kind("DevLys 010") == "kruti"
    assert font_kind("Walkman-Chanakya-905") == "legacy"
    assert font_kind("Mangal") is None and font_kind("Arial") is None and font_kind(None) is None
