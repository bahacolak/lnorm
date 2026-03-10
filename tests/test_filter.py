from filter import filter_target_company


def test_filter_cuts_foreign_registry_tail():
    text = {
        1: """
PARLA ENERJİ YATIRIMLARI ANONİM ŞİRKETİ
Adres : Adalet Mah. Hasan Gönüllü Bul. No: 15
Yukarıda bilgileri verilen şirket ile ilgili olarak tescil edildiği ilan olunur.
DENETÇİLER
Yeni Denetçi
PWC BAĞIMSIZ DENETİM A.Ş.
T.C. ELAZIĞ TİCARET SİCİLİ MÜDÜRLÜĞÜ'NDEN
İlan Sıra No: 2906
"""
    }
    result = filter_target_company(text)
    assert result.text is not None
    assert "PWC BAĞIMSIZ DENETİM" in result.text
    assert "ELAZIĞ" not in result.text


def test_filter_returns_not_found_without_target():
    result = filter_target_company({1: "BAŞKA ŞİRKET ANONİM ŞİRKETİ"})
    assert result.status == "not_found"


def test_filter_includes_registry_header_block():
    text = {
        1: """
T.C. DENİZLİ TİCARET SİCİLİ MÜDÜRLÜĞÜ'NDEN
İlan Sıra No: 5786
Mersis No: 0721091699100001
Ticaret Sicil/Dosya No: 49606
PARLA ENERJİ YATIRIMLARI ANONİM ŞİRKETİ
Adres : Adalet Mah. Hasan Gönüllü Bul. No: 15
"""
    }
    result = filter_target_company(text)
    assert result.text is not None
    assert "Ticaret Sicil/Dosya No: 49606" in result.text
    assert "Mersis No: 0721091699100001" in result.text
