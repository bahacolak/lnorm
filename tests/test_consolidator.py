from datetime import datetime

from consolidator import consolidate_board_members, consolidate_company_info
from extractor import BoardMember, CompanyInfo


def test_consolidate_company_info_uses_latest_variable_field():
    old = CompanyInfo(sermaye="100.000,00 TL", field_sources={"sermaye": {"pdf": "old", "ttsg_date": "01.12.2022"}})
    new = CompanyInfo(sermaye="161.852.160,00 TL", field_sources={"sermaye": {"pdf": "new", "ttsg_date": "29.12.2023"}})
    consolidated = consolidate_company_info([("old.pdf", old), ("new.pdf", new)])
    assert consolidated.sermaye == "161.852.160,00 TL"
    assert consolidated.field_sources["sermaye"]["pdf"] == "new"


def test_consolidate_board_members_applies_events():
    removed = BoardMember(name="ENGİN KAVAS", action="görevden_alma")
    added = BoardMember(name="ENES ERCANLI", action="atama", term_end="26.09.2025")
    board = consolidate_board_members([("x.pdf", datetime(2024, 9, 13), [removed, added])])
    assert [member.name for member in board] == []


def test_consolidate_board_members_filters_expired_terms_and_keeps_active():
    expired = BoardMember(name="EXPIRED MEMBER", action="atama", term_end="26.09.2025")
    active = BoardMember(name="ACTIVE MEMBER", action="atama", term_end="08.09.2028")
    board = consolidate_board_members(
        [
            ("old.pdf", datetime(2024, 9, 13), [expired]),
            ("new.pdf", datetime(2025, 9, 11), [active]),
        ]
    )
    assert [member.name for member in board] == ["ACTIVE MEMBER"]
