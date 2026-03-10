from src.articles_parser import clean_article_text, parse_articles, parse_changed_articles


def test_clean_article_text_removes_noise_headers():
    text = """
(Devamı 1405. Sayfada)
II ARALIK 2022 SAYI: 10716 TÜRKİYE TİCAR
(Baştarafı 1404.Sayfada)
1. KURULUŞ
Metin
"""
    cleaned = clean_article_text(text)
    assert "Devamı" not in cleaned
    assert "TÜRKİYE TİCAR" not in cleaned


def test_parse_numbered_articles():
    text = """
1. KURULUŞ
Birinci madde.
2. ŞİRKETİN UNVANI
İkinci madde.
3. AMAÇ VE KONU
Üçüncü madde.
"""
    articles, qa = parse_articles(text, "1) 01-12-2022 Kuruluş İlanı.pdf", expected_count=3)
    assert [article.madde_no for article in articles] == [1, 2, 3]
    assert not [item for item in qa if item.sorun_tipi == "madde_bulunamadi"]


def test_parse_changed_articles_filters_ic_yonerge():
    """İç Yönerge sections should be stripped from Esas Sözleşme parsing."""
    text = """
Madde 6-
Şirketin sermayesi 161.852.160,00 Türk Lirası değerindedir.

GENEL KURUL İÇ YÖNERGESİ

MADDE 3-(1) Bu İç Yönergede geçen;
a) Birleşim : Genel kurulun bir günlük toplantısını,

MADDE 6- (1) Toplantı şirket merkezinin bulunduğu yerde yapılır.

MADDE 7-(1) Bu İç Yönergenin 6 ncı maddesi uyarınca açılır.
"""
    articles, qa = parse_changed_articles(text, "2) 21-06-2023 Esas Sözleşme Değişikliği.pdf")
    # Should only get Madde 6 from the Esas Sözleşme part, NOT the İç Yönerge MADDE 3, 6, 7
    madde_nos = [a.madde_no for a in articles]
    assert 6 in madde_nos
    # İç Yönerge MADDE 3 should NOT be in the result
    assert 3 not in madde_nos or all("İç Yönerge" not in a.icerik for a in articles if a.madde_no == 3)


def test_parse_articles_keeps_duplicate_heading_for_normalizer_stage():
    text = """
# 6. SERMAYE
Şirketin sermayesi 100.000 TL'dir.
"""
    articles, _ = parse_articles(text, "x.pdf", expected_count=-1)
    assert articles[0].icerik.startswith("# 6. SERMAYE")
