from datetime import datetime

from src.consolidator import (
    _extract_ttsg_no_from_filename,
    consolidate_board_members,
    consolidate_company_info,
)
from src.extractor import BoardMember, CompanyInfo


def test_consolidate_company_info_uses_latest_variable_field():
    old = CompanyInfo(sermaye="100.000,00 TL", field_sources={"sermaye": {"pdf": "old", "ttsg_date": "01.12.2022"}})
    new = CompanyInfo(sermaye="161.852.160,00 TL", field_sources={"sermaye": {"pdf": "new", "ttsg_date": "29.12.2023"}})
    consolidated = consolidate_company_info([("old.pdf", old), ("new.pdf", new)])
    assert consolidated.sermaye == "161.852.160,00 TL"
    assert consolidated.field_sources["sermaye"]["pdf"] == "new"


def test_consolidate_board_members_applies_events():
    removed = BoardMember(name="ENGİN KAVAS", action="görevden_alma")
    added = BoardMember(name="ENES ERCANLI", action="atama", term_end="26.09.2025")
    board = consolidate_board_members(
        [("x.pdf", datetime(2024, 9, 13), [removed, added])],
        reference_date=datetime(2026, 3, 10),
    )
    assert [member.name for member in board] == ["ENES ERCANLI", "ENGİN KAVAS"]


def test_consolidate_board_members_filters_expired_terms_and_keeps_active():
    expired = BoardMember(name="EXPIRED MEMBER", action="atama", term_end="26.09.2025")
    active = BoardMember(name="ACTIVE MEMBER", action="atama", term_end="08.09.2028")
    board = consolidate_board_members(
        [
            ("old.pdf", datetime(2024, 9, 13), [expired]),
            ("new.pdf", datetime(2025, 9, 11), [active]),
        ],
        reference_date=datetime(2026, 3, 10),
    )
    assert [member.name for member in board] == ["ACTIVE MEMBER", "EXPIRED MEMBER"]


def test_consolidate_board_members_uses_reference_date_override():
    member = BoardMember(name="ACTIVE MEMBER", action="atama", term_end="11.03.2026")
    board = consolidate_board_members(
        [("new.pdf", datetime(2025, 9, 11), [member])],
        reference_date=datetime(2026, 3, 10),
    )
    assert [item.name for item in board] == ["ACTIVE MEMBER"]


def test_consolidate_board_members_prefers_atama_over_gorevden_alma():
    removed = BoardMember(name="AYDEM HOLDING ANONİM ŞİRKETİ", action="görevden_alma", appointment_ttsg_date="30.10.2024")
    added = BoardMember(
        name="AYDEM HOLDING ANONİM ŞİRKETİ",
        action="atama",
        term_end="08.09.2028",
        representative="SERDAR MARANGOZ",
        appointment_ttsg_date="11.09.2025",
    )
    board = consolidate_board_members([("a.pdf", datetime(2024, 10, 30), [removed]), ("b.pdf", datetime(2025, 9, 11), [added])])
    assert len(board) == 1
    assert board[0].action == "atama"
    assert board[0].representative == "SERDAR MARANGOZ"


def test_extract_ttsg_number_from_filename():
    assert _extract_ttsg_no_from_filename("11-09-2025 SAYI 12345 Yönetim Kurulu.pdf") == "12345"
