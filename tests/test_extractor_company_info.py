from extractor import extract_company_info
from filter import FilterResult


def test_extract_company_info_detects_auditor():
    text = """
PARLA ENERJİ YATIRIMLARI ANONİM ŞİRKETİ
Adres : Adalet Mah. Hasan Gönüllü Bul. No: 15 İç Kapı No: 1 Merkezefendi / Denizli
MERSİS No: 0123456789012345
Ticaret Sicil No: 194877-5
DENİZLİ TİCARET SİCİLİ MÜDÜRLÜĞÜ
Yukarıda bilgileri verilen şirket ile ilgili olarak 22.12.2025 tarihinde tescil edildiği ilan olunur.
DENETÇİLER
Yeni Denetçi
PWC BAĞIMSIZ DENETİM VE SMMM A.Ş.
"""
    info, issues = extract_company_info(
        text,
        {1: text},
        "9) 22-12-2025 Denetçi Değişikliği.pdf",
        allow_llm=False,
        filter_result=FilterResult(text=text, status="ok"),
    )
    assert info.denetci == "PWC BAĞIMSIZ DENETİM VE SMMM A.Ş."
    assert info.mersis_no == "0123456789012345"
    assert info.ticaret_sicil_no == "194877-5"
    assert not issues


def test_extract_company_info_drops_unverified_fields():
    text = """
PARLA ENERJİ YATIRIMLARI ANONİM ŞİRKETİ
Adres : Adalet Mah. Hasan Gönüllü Bul. No: 15 İç Kapı No: 1 Merkezefendi / Denizli
MERSİS No: 0123456789012345
"""
    info, issues = extract_company_info(
        text,
        {1: text},
        "1) 01-12-2022 Kuruluş İlanı.pdf",
        allow_llm=False,
        filter_result=FilterResult(text=text, status="ok"),
        verification_texts=[text.replace("0123456789012345", "")],
    )
    assert info.mersis_no == "0123456789012345"  # kept, but flagged
    assert any(issue.field_name == "mersis_no" and issue.action == "flagged_unverified" for issue in issues)


def test_extract_company_info_parses_auditor_from_table():
    text = """
# DENETÇİLER
Yeni Denetçi
| Kimlik | Uyruk | Adı Soyadı / Firma Adı | Adres | Başlangıç | Bitiş |
| --- | --- | --- | --- | --- | --- |
| 0166802248500015 | Türkiye Cumhuriyeti | PWC BAĞIMSIZ DENETİM VE SMMM A.Ş. | İstanbul | 1.1.2025 | 31.12.2025 |
"""
    info, issues = extract_company_info(
        text,
        {1: text},
        "9) 22-12-2025 Denetçi Değişikliği.pdf",
        allow_llm=False,
        filter_result=FilterResult(text=text, status="ok"),
    )
    assert info.denetci == "PWC BAĞIMSIZ DENETİM VE SMMM A.Ş."
    assert not issues


def test_extract_company_info_keeps_corrupted_auditor_for_case_study():
    """OCR-corrupted denetçi names are kept verbatim for case-study coverage."""
    text = """
PARLA ENERJİ YATIRIMLARI ANONİM ŞİRKETİ
22.12.2025 tarihinde tescil edildiği ilan olunur.
DENETÇİLER
Yeni Denetçi
| Kimlik | Uyruk | Adı Soyadı / Firma Adı | Adres | Başlangıç | Bitiş |
| --- | --- | --- | --- | --- | --- |
| 0166802248500015 | Türkiye | PWC BAŞIMAZ MURDITEL YIL KERKEM YETENMEMEL MALİ MÜŞAVİRLİK ANONİM ŞİRKETİ | İstanbul | 1.1.2025 | 31.12.2025 |
"""
    info, issues = extract_company_info(
        text,
        {1: text},
        "9) 22-12-2025 Denetçi Değişikliği.pdf",
        allow_llm=False,
        filter_result=FilterResult(text=text, status="ok"),
    )
    assert info.denetci == "PWC BAŞIMAZ MURDITEL YIL KERKEM YETENMEMEL MALİ MÜŞAVİRLİK ANONİM ŞİRKETİ"
    assert not issues


def test_extract_company_info_mudurluk_from_header():
    """Should extract müdürlük from 'T.C. DENİZLİ TİCARET SİCİLİ MÜDÜRLÜĞÜ'NDEN'."""
    text = """
T.C. DENİZLİ TİCARET SİCİLİ MÜDÜRLÜĞÜ'NDEN
PARLA ENERJİ YATIRIMLARI ANONİM ŞİRKETİ
Ticaret Sicil/Dosya No: 49606
Adres: Adalet Mah. Hasan Gönüllü Bul. No: 15 İç Kapı No: 1 Merkezefendi / Denizli
Yukarıda bilgileri verilen şirket 30.10.2024 tarihinde tescil edildiği ilan olunur.
"""
    info, issues = extract_company_info(
        text,
        {1: text},
        "6) 30-10-2024 Yönetim Kurulu Atama.pdf",
        allow_llm=False,
        filter_result=FilterResult(text=text, status="ok"),
    )
    assert "Denizli" in info.ticaret_sicil_mudurlugu
    assert info.ticaret_sicil_no == "49606"
