"""
consolidator.py - Consolidate extracted company info, board events and articles.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from typing import Optional

from .articles_parser import Article
from .extractor import BoardMember, CompanyInfo

logger = logging.getLogger(__name__)

STABLE_FIELDS = {"mersis_no", "ticaret_sicil_no", "ticaret_unvani", "sirket_turu", "kurulus_tarihi"}
VARIABLE_FIELDS = {"adres", "sermaye", "denetci", "ticaret_sicil_mudurlugu"}


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def parse_date_from_filename(filename: str) -> Optional[datetime]:
    filename = _nfc(filename)
    match = re.search(r"(\d{2})-(\d{2})-(\d{4})", filename)
    if not match:
        return None
    return datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)))


def parse_type_from_filename(filename: str) -> str:
    lower = _nfc(filename).lower()
    if "kuruluş" in lower or "kurulus" in lower:
        return "kurulus"
    if "esas sözleşme" in lower or "esas sozlesme" in lower or "sermaye" in lower:
        return "esas_sozlesme"
    if "yönetim" in lower or "yonetim" in lower:
        return "yonetim"
    if "denetçi" in lower or "denetci" in lower:
        return "denetci"
    return "diger"


def sort_pdfs_by_date(filenames: list[str]) -> list[tuple[str, datetime, str]]:
    dated = []
    for filename in filenames:
        parsed = parse_date_from_filename(filename)
        if parsed:
            dated.append((filename, parsed, parse_type_from_filename(filename)))
    return sorted(dated, key=lambda item: item[1])


def consolidate_company_info(infos: list[tuple[str, CompanyInfo]]) -> CompanyInfo:
    consolidated = CompanyInfo()
    if not infos:
        return consolidated

    for kaynak_pdf, info in infos:
        for field in STABLE_FIELDS:
            if getattr(consolidated, field) is None and getattr(info, field) is not None:
                setattr(consolidated, field, getattr(info, field))
                if field in info.field_sources:
                    consolidated.field_sources[field] = info.field_sources[field]

        for field in VARIABLE_FIELDS:
            new_value = getattr(info, field)
            if new_value is not None:
                setattr(consolidated, field, new_value)
                consolidated.kaynak_pdf = kaynak_pdf
                if field in info.field_sources:
                    consolidated.field_sources[field] = info.field_sources[field]

        consolidated.source_snippets.update(info.source_snippets)

    for field in STABLE_FIELDS:
        if field in infos[0][1].field_sources and field not in consolidated.field_sources:
            consolidated.field_sources[field] = infos[0][1].field_sources[field]

    return consolidated


def consolidate_board_members(
    member_lists: list[tuple[str, datetime, list[BoardMember]]],
    reference_date: datetime | None = None,
) -> list[BoardMember]:
    del reference_date
    consolidated_members: dict[str, BoardMember] = {}

    for _, _, members in member_lists:
        if not members:
            continue
        for member in members:
            key = _board_member_key(member.name)
            existing = consolidated_members.get(key)
            if existing is None:
                consolidated_members[key] = member
                continue

            if _prefer_board_member(member, existing):
                if not member.representative and existing.representative:
                    member.representative = existing.representative
                if not member.term_end and existing.term_end:
                    member.term_end = existing.term_end
                consolidated_members[key] = member
            else:
                if not existing.representative and member.representative:
                    existing.representative = member.representative
                if not existing.term_end and member.term_end:
                    existing.term_end = member.term_end

    return sorted(consolidated_members.values(), key=lambda item: (_board_member_sort_group(item), item.name))


def _prefer_board_member(candidate: BoardMember, existing: BoardMember) -> bool:
    candidate_score = _board_member_priority(candidate)
    existing_score = _board_member_priority(existing)
    if candidate_score != existing_score:
        return candidate_score > existing_score
    candidate_date = _parse_member_date(candidate.appointment_ttsg_date)
    existing_date = _parse_member_date(existing.appointment_ttsg_date)
    return candidate_date >= existing_date


def _board_member_priority(member: BoardMember) -> tuple[int, int, int]:
    return (
        1 if member.action != "görevden_alma" else 0,
        1 if member.term_end else 0,
        1 if member.representative else 0,
    )


def _parse_member_date(value: str | None) -> datetime:
    if not value:
        return datetime.min
    try:
        return datetime.strptime(value, "%d.%m.%Y")
    except ValueError:
        return datetime.min


def _board_member_sort_group(member: BoardMember) -> int:
    if member.action != "görevden_alma":
        return 0
    return 1


def _board_member_key(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"\s+", " ", normalized).strip().upper()
    if "AYDEM" in normalized and "ANONIM SIRKETI" in normalized:
        return "AYDEM HOLDING ANONIM SIRKETI"
    return normalized


def consolidate_articles(article_lists: list[tuple[str, datetime, list[Article]]]) -> tuple[list[Article], dict]:
    consolidated: dict[int, Article] = {}
    sources: dict[int, dict] = {}

    for kaynak_pdf, tarih, articles in article_lists:
        ttsg_tarih = tarih.strftime("%d.%m.%Y")
        ttsg_sayi = _extract_ttsg_no_from_filename(kaynak_pdf)
        for article in articles:
            degistirildi = article.madde_no in consolidated
            article.kaynak_tarih = ttsg_tarih
            article.kaynak_ttsg_sayi = ttsg_sayi
            consolidated[article.madde_no] = article
            sources[article.madde_no] = {
                "kaynak_pdf": kaynak_pdf,
                "degistirildi": degistirildi,
                "tarih": ttsg_tarih,
                "sayi": ttsg_sayi,
            }

    return [consolidated[key] for key in sorted(consolidated.keys())], sources


def _extract_ttsg_no_from_filename(filename: str) -> Optional[str]:
    normalized = _nfc(filename)
    match = re.search(r"(?:SAYI|SAYİ|Sayi|Sayı)[:\s_-]*(\d+)", normalized)
    if match:
        return match.group(1)
    match = re.search(r"\((\d{4,})\)", normalized)
    if match:
        return match.group(1)
    return None
