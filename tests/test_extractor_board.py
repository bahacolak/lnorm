from extractor import extract_board_members


def test_extract_board_members_replacement_pattern():
    text = """
PARLA ENERJİ YATIRIMLARI ANONİM ŞİRKETİ
YÖNETİM KURULU / YETKİLİLER
Daha önceden Yönetim Kurulu Üyesi olan ENGİN KAVAS'ın önceki üyeliği sona ermiştir.
Yerine ENES ERCANLI 26.9.2025 tarihine kadar Yönetim Kurulu Üyesi olarak seçilmiştir.
"""
    members, issues = extract_board_members(
        text,
        {1: text},
        "5) 13-09-2024 Yönetim Kurulu Atama.pdf",
        allow_llm=False,
    )
    names = {member.name: member.action for member in members}
    assert names["ENGİN KAVAS"] == "görevden_alma"
    assert names["ENES ERCANLI"] == "atama"
    assert not issues


def test_extract_board_members_drops_unverified_names():
    text = """
PARLA ENERJİ YATIRIMLARI ANONİM ŞİRKETİ
YÖNETİM KURULU / YETKİLİLER
ENES ERCANLI 26.9.2025 tarihine kadar Yönetim Kurulu Üyesi olarak seçilmiştir.
"""
    members, issues = extract_board_members(
        text,
        {1: text},
        "5) 13-09-2024 Yönetim Kurulu Atama.pdf",
        allow_llm=False,
        verification_texts=["başka bir içerik"],
    )
    assert len(members) == 1  # kept, but flagged
    assert any(issue.action == "flagged_unverified" for issue in issues)


def test_extract_board_members_line_based_term_end():
    text = """
AYDEM HOLDING ANONİM ŞİRKETİ 8.9.2028 tarihine kadar Yönetim Kurulu Üyesi olarak seçilmiştir.
Tüzel kişi adına; Türkiye Cumhuriyeti Uyruklu 155***26 Kimlik No'lu, DENİZLİ / MERKEZEFENDİ adresinde ikamet eden SERDAR MARANGOZ hareket edecektir.
"""
    members, issues = extract_board_members(
        text,
        {1: text},
        "8) 11-09-2025 Yönetim Kurulu Atama.pdf",
        allow_llm=False,
    )
    assert members[0].name == "AYDEM HOLDING ANONİM ŞİRKETİ"
    assert members[0].term_end == "08.09.2028"
    assert members[0].representative == "SERDAR MARANGOZ"
    assert not issues


def test_extract_board_members_broad_gorevden_alma():
    """Test that OCR-corrupted 'üyeliği sona ermiştir' patterns are caught."""
    text = """
PARLA ENERJİ YATIRIMLARI ANONİM ŞİRKETİ
YÖNETİM KURULU / YETKİLİLER
EMİRHAN KARAYAY 26.9.2025 tarihine kadar Yönetim Kurulu Üyesi olarak seçilmiştir.
Daha önceden Yönetim Kurulu Üyesi olan Türkiye Uyruklu 270***46 Kimlik No'lu DENİZLİ / MERKEZEFENDİ adresinde ikamet eden MEHMET GÖKAY ÜSTÜN'in önceki üydüğü sona ermiştir.
"""
    members, issues = extract_board_members(
        text,
        {1: text},
        "3) 10-10-2023 Yönetim Kurulu Atama.pdf",
        allow_llm=False,
    )
    names_actions = {m.name: m.action for m in members}
    assert "MEHMET GÖKAY ÜSTÜN" in names_actions
    assert names_actions["MEHMET GÖKAY ÜSTÜN"] == "görevden_alma"
    assert "EMİRHAN KARAYAY" in names_actions
    assert names_actions["EMİRHAN KARAYAY"] == "atama"


def test_extract_board_members_enes_ercanli_sona_ermis():
    """Test 'en öncaki işçiliğe sona ermiştir' OCR corruption."""
    text = """
AYDEM HOLDING ANONİM ŞİRKETİ 8.9.2028 tarihine kadar Yönetim Kurulu Üyesi olarak seçilmiştir.
Türkiye Cumhuriyeti Uyruklu 155***26 Kimlik No'lu, DENİZLİ / MERKEZEFENDİ adresinde ikamet eden SERDAR MARANGÖZ hareket edecektir.
Daha önceden Yönetim Kurulu Üyesi olan Türkiye Cumhuriyeti Uyruklu 355***10 Kimlik No'lu ISTANBUL / BAYRAMPAŞA adresinde ikamet eden ENES ERCANLI, en öncaki işçiliğe sona ermiştir.
"""
    members, issues = extract_board_members(
        text,
        {1: text},
        "8) 11-09-2025 Yönetim Kurulu Atama.pdf",
        allow_llm=False,
    )
    names_actions = {m.name: m.action for m in members}
    assert "ENES ERCANLI" in names_actions
    assert names_actions["ENES ERCANLI"] == "görevden_alma"
