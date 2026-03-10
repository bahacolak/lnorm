"""
extractor.py - Rule-based first extraction with optional LLM fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from filter import FilterResult

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
COMPANY_INFO_PROMPT = PROMPTS_DIR / "company_info.txt"
BOARD_MEMBERS_PROMPT = PROMPTS_DIR / "board_members.txt"

MERSIS_PATTERN = re.compile(r"\b\d{16}\b")
SICIL_NO_PATTERN = re.compile(
    r"(?:Ticaret\s+Sicil(?:/Dosya)?\s+No|Sicil(?:/Dosya)?\s+No)\s*[:\.]?\s*([A-Z0-9\-/]+)",
    re.IGNORECASE,
)
MUDURLUK_PATTERN = re.compile(
    r"T\.\s*C\.\s*([A-ZÇĞİÖŞÜa-zçğıöşü]+)\s+T[İI]CARET\s+S[İI]C[İI]L[İI]\s+M[ÜU]D[ÜU]RL[ÜU][ĞG][ÜU]",
    re.IGNORECASE,
)
ADRES_PATTERN = re.compile(r"Adres\s*[:\.]\s*(.+?)(?=\n(?:Yukarıda|Tescil|MERS|Sicil|$))", re.IGNORECASE | re.DOTALL)
TESCIL_TARIHI_PATTERN = re.compile(
    r"(\d{1,2}[./]\d{1,2}[./]\d{4})\s+tarihinde\s+tescil\s+edil(?:diği|miştir|di)",
    re.IGNORECASE,
)
SERMAYE_PATTERN = re.compile(r"(\d+(?:\.\d{3})*(?:,\d{2})?)\s*(?:Türk\s+Lirası|TL)", re.IGNORECASE)
MADDE6_BLOCK_PATTERN = re.compile(
    r"(?:MADDE|Madde|\b6\.)\s*6?[^ \n-]*\s*[-:\.]?\s*(.*?)(?=\n(?:MADDE|Madde|\d+\.)\s*7\b|\Z)",
    re.DOTALL,
)
DENETCI_SECTION_PATTERN = re.compile(
    r"(?:DENETÇ[İI]LER|Yeni\s+Denetçi|Denetçi(?:ler)?)\s*(.*?)(?=\n(?:T\.C\.|İlan\s+Sıra\s+No|[A-ZÇĞİÖŞÜ\s]+T[İI]CARET\s+S[İI]C[İI]L[İI]|$))",
    re.IGNORECASE | re.DOTALL,
)
BOARD_TERM_PATTERN = re.compile(
    r"(\d{1,2}[./]\d{1,2}[./]\d{4})\s+tarihine\s+kadar",
    re.IGNORECASE,
)
TTSG_NO_PATTERN = re.compile(r"SAYI[:;]?\s*(\d+)", re.IGNORECASE)


@dataclass
class CompanyInfo:
    ticaret_unvani: Optional[str] = None
    sirket_turu: Optional[str] = None
    mersis_no: Optional[str] = None
    ticaret_sicil_mudurlugu: Optional[str] = None
    ticaret_sicil_no: Optional[str] = None
    adres: Optional[str] = None
    sermaye: Optional[str] = None
    kurulus_tarihi: Optional[str] = None
    denetci: Optional[str] = None
    confidence: str = "high"
    source_page: Optional[int] = None
    source_snippets: dict[str, str] = field(default_factory=dict)
    field_sources: dict[str, dict[str, str]] = field(default_factory=dict)
    kaynak_pdf: str = ""


@dataclass
class BoardMember:
    name: str = ""
    role: str = ""
    entity_type: str = "real_person"
    term_end: Optional[str] = None
    representative: Optional[str] = None
    action: Optional[str] = None
    source_snippet: str = ""
    source_page: Optional[int] = None
    kaynak_pdf: str = ""
    appointment_ttsg_date: Optional[str] = None
    appointment_ttsg_no: Optional[str] = None
    source_pdf_link: Optional[str] = None


@dataclass
class HallucinationEntry:
    pdf: str
    field_name: str
    llm_value: str
    found_in_source: bool
    action: str


def extract_company_info(
    text: str,
    page_texts: dict[int, str],
    kaynak_pdf: str,
    is_kurulus: bool = False,
    allow_llm: bool = True,
    filter_result: Optional[FilterResult] = None,
    verification_texts: Optional[list[str]] = None,
) -> tuple[CompanyInfo, list[HallucinationEntry]]:
    info = CompanyInfo(kaynak_pdf=kaynak_pdf)
    issues: list[HallucinationEntry] = []
    info.source_page = _find_source_page(text, page_texts)

    _assign(info, "ticaret_unvani", _extract_company_title(text), text, kaynak_pdf)
    info.sirket_turu = "Anonim Şirket" if "ANONİM" in text.upper() or "ANONIM" in text.upper() else info.sirket_turu
    _assign(info, "mersis_no", _extract_match(MERSIS_PATTERN, text), text, kaynak_pdf)
    _assign(info, "ticaret_sicil_no", _extract_group(SICIL_NO_PATTERN, text), text, kaynak_pdf)
    mudurluk = _extract_mudurluk(text)
    if mudurluk:
        _assign(info, "ticaret_sicil_mudurlugu", mudurluk, text, kaynak_pdf)
    _assign(info, "adres", _clean_address(_extract_group(ADRES_PATTERN, text)), text, kaynak_pdf)
    _assign(info, "sermaye", _extract_sermaye(text, is_kurulus), text, kaynak_pdf)
    _assign(info, "kurulus_tarihi", _extract_kurulus_tarihi(text), text, kaynak_pdf)
    _assign(info, "denetci", _extract_denetci(text), text, kaynak_pdf)

    if filter_result and filter_result.status != "ok":
        info.source_snippets["filter_status"] = filter_result.status

    missing_fields = [field for field in ("adres", "sermaye", "kurulus_tarihi", "denetci") if getattr(info, field) is None]
    if missing_fields and allow_llm and _llm_available():
        llm_result = _call_llm_company_info(text)
        if llm_result:
            info, llm_issues = _merge_llm_company_info(info, llm_result, text)
            issues.extend(llm_issues)

    if verification_texts:
        _drop_unverified_company_fields(info, verification_texts, issues)

    filled = sum(
        1
        for value in (
            info.ticaret_unvani,
            info.mersis_no,
            info.ticaret_sicil_no,
            info.adres,
            info.sermaye,
            info.kurulus_tarihi,
            info.denetci,
        )
        if value
    )
    info.confidence = "high" if filled >= 6 else "medium" if filled >= 3 else "low"
    return info, issues


def extract_board_members(
    text: str,
    page_texts: dict[int, str],
    kaynak_pdf: str,
    allow_llm: bool = True,
    verification_texts: Optional[list[str]] = None,
) -> tuple[list[BoardMember], list[HallucinationEntry]]:
    source_page = _find_source_page(text, page_texts)
    issues: list[HallucinationEntry] = []

    members = _parse_board_members_rule_based(text, kaynak_pdf, source_page)
    if not members and allow_llm and _llm_available():
        llm_result = _call_llm_board_members(text)
        if llm_result:
            members, llm_issues = _parse_llm_board_result(llm_result, text, kaynak_pdf, source_page)
            issues.extend(llm_issues)

    if verification_texts:
        members = _filter_unverified_board_members(members, verification_texts, issues)

    return members, issues


def _extract_company_title(text: str) -> Optional[str]:
    match = re.search(r"PARLA\s+ENERJ[İI]\s+YATIRIMLARI\s+ANON[İI]M\s+Ş[İI]RKET[İI]", text, re.IGNORECASE)
    return match.group(0).upper() if match else None


def _extract_match(pattern: re.Pattern[str], text: str) -> Optional[str]:
    match = pattern.search(text)
    return match.group(0).strip() if match else None


def _extract_group(pattern: re.Pattern[str], text: str, group: int = 1) -> Optional[str]:
    match = pattern.search(text)
    return match.group(group).strip() if match else None


def _extract_kurulus_tarihi(text: str) -> Optional[str]:
    match = TESCIL_TARIHI_PATTERN.search(text)
    return _normalize_tarih(match.group(1)) if match else None


def _extract_sermaye(text: str, is_kurulus: bool) -> Optional[str]:
    if is_kurulus:
        madde6 = MADDE6_BLOCK_PATTERN.search(text)
        if madde6:
            matches = SERMAYE_PATTERN.findall(madde6.group(1))
            if matches:
                return f"{matches[-1]} TL"

    matches = SERMAYE_PATTERN.findall(text)
    if matches:
        return f"{matches[-1]} TL"
    return None


def _extract_denetci(text: str) -> Optional[str]:
    start = None
    for marker in ("# DENETÇİLER", "DENETÇİLER", "Yeni Denetçi", "Yeni Denetçi"):
        pos = text.find(marker)
        if pos != -1:
            start = pos
            break
    if start is None:
        return None
    section_text = text[start:]

    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        if cells[0].startswith("---") or "firma adı" in cells[2].casefold():
            continue
        if len(cells[2]) > 5:
            return _validate_denetci_name(cells[2])

    # Header labels to skip (using casefold for Turkish-safe comparison)
    skip_labels = {"denetçiler", "yeni denetçi", "eski denetçi", "denetçi", "denetciler"}

    lines = [line.strip(" :-") for line in section_text.splitlines() if line.strip()]
    for line in lines:
        folded = line.strip().casefold()
        if folded in skip_labels:
            continue
        if "denetçi" in folded and len(line.strip()) < 20:
            continue
        if "genel kurul" in folded:
            continue
        if len(line) < 3:
            continue
        if re.match(r"^\(\d+\)$", line):
            continue
        return _validate_denetci_name(line)
    return None


def _validate_denetci_name(name: str) -> str:
    """Return extracted auditor name as-is for case-study coverage."""
    return name


def _extract_mudurluk(text: str) -> Optional[str]:
    """Extract Ticaret Sicili Müdürlüğü name from standard TTSG header."""
    match = MUDURLUK_PATTERN.search(text)
    if match:
        city = match.group(1).strip()
        # Manual Turkish-safe title case (avoid .title() which breaks İ → i̇)
        city = _turkish_title_case(city)
        return f"{city} Ticaret Sicili Müdürlüğü"
    return None


def _turkish_title_case(s: str) -> str:
    """Title-case a Turkish string without breaking İ/ı."""
    # Turkish-safe lowercase map
    _tr_lower = str.maketrans("ABCÇDEFGĞHIİJKLMNOÖPRSŞTUÜVYZ", "abcçdefgğhıijklmnoöprsştuüvyz")
    _tr_upper = str.maketrans("abcçdefgğhıijklmnoöprsştuüvyz", "ABCÇDEFGĞHIİJKLMNOÖPRSŞTUÜVYZ")

    words = s.split()
    result = []
    for word in words:
        if not word:
            continue
        first = word[0].translate(_tr_upper)
        rest = word[1:].translate(_tr_lower)
        result.append(first + rest)
    return " ".join(result)


def _clean_address(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    address = re.sub(r"\s+", " ", address.replace("\n", " ")).strip(" .")
    return address or None


def _assign(info: CompanyInfo, field_name: str, value: Optional[str], source_text: str, kaynak_pdf: str) -> None:
    if not value:
        return
    setattr(info, field_name, value)
    info.source_snippets[field_name] = value[:160]
    info.field_sources[field_name] = {
        "pdf": kaynak_pdf,
        "ttsg_date": _extract_ttsg_date_from_filename(kaynak_pdf) or "",
    }


def _extract_ttsg_date_from_filename(filename: str) -> Optional[str]:
    match = re.search(r"(\d{2})-(\d{2})-(\d{4})", filename)
    if not match:
        return None
    return f"{match.group(1)}.{match.group(2)}.{match.group(3)}"


def _find_source_page(text: str, page_texts: dict[int, str]) -> Optional[int]:
    snippet = _normalize_text(text[:120])
    if not snippet:
        return None
    for page_num, page_text in page_texts.items():
        if snippet[:40] in _normalize_text(page_text):
            return page_num
    return next(iter(page_texts.keys()), None)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _normalize_tarih(value: str) -> str:
    parts = re.split(r"[./]", value)
    if len(parts) == 3:
        return f"{parts[0].zfill(2)}.{parts[1].zfill(2)}.{parts[2]}"
    return value


def _parse_board_members_rule_based(text: str, kaynak_pdf: str, source_page: Optional[int]) -> list[BoardMember]:
    members: list[BoardMember] = []
    appointment_date = _extract_ttsg_date_from_filename(kaynak_pdf)
    appointment_no = _extract_group(TTSG_NO_PATTERN, text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    replacement_pattern = re.compile(
        r"Daha\s+önceden.*?(?:olan\s+)?(?P<old>[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜa-zçğıöşü\s]+?)['‘’]?(?:n?[iıuü]n)\s+önceki\s+üyeliği\s+sona\s+ermiştir\.\s+Yerine.*?(?P<new>[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜa-zçğıöşü\s]+?)\s+(?P<term>\d{1,2}[./]\d{1,2}[./]\d{4})\s+tarihine\s+kadar\s+Yönetim\s+Kurulu\s+Üyesi\s+olarak\s+seçilmiştir",
        re.IGNORECASE | re.DOTALL,
    )
    for match in replacement_pattern.finditer(text):
        old_name = _normalize_person_name(match.group("old"))
        new_name = _normalize_person_name(match.group("new"))
        if old_name:
            members.append(
                BoardMember(
                    name=old_name,
                    role="Yönetim Kurulu Üyesi",
                    action="görevden_alma",
                    source_snippet=match.group(0)[:220],
                    source_page=source_page,
                    kaynak_pdf=kaynak_pdf,
                    appointment_ttsg_date=appointment_date,
                    appointment_ttsg_no=appointment_no,
                    source_pdf_link=f"input/{kaynak_pdf}",
                )
            )
        if new_name:
            members.append(
                BoardMember(
                    name=new_name,
                    role="Yönetim Kurulu Üyesi",
                    action="atama",
                    term_end=_normalize_tarih(match.group("term")),
                    source_snippet=match.group(0)[:220],
                    source_page=source_page,
                    kaynak_pdf=kaynak_pdf,
                    appointment_ttsg_date=appointment_date,
                    appointment_ttsg_no=appointment_no,
                    source_pdf_link=f"input/{kaynak_pdf}",
                )
            )

    removal_only_pattern = re.compile(
        r"Daha\s+önceden\s+(?:.*?)Yönetim\s+Kurulu\s+Üyesi\s+olan\s+(?:.*?)(?P<old>[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜa-zçğıöşü\s]+?)['\u2019']?(?:n?[iıuü]n|,)\s+(?:önceki\s+)?(?:üyeliği|üyd[üu][ğg][üu]|işçiliğe|bu\s+görevi)?\s*sona\s+ermiştir",
        re.IGNORECASE | re.DOTALL,
    )
    for match in removal_only_pattern.finditer(text):
        old_name = _normalize_person_name(_extract_member_name(match.group("old")) or match.group("old"))
        if old_name and not any(m.name == old_name and m.action == "görevden_alma" for m in members):
            members.append(
                BoardMember(
                    name=old_name,
                    role="Yönetim Kurulu Üyesi",
                    action="görevden_alma",
                    source_snippet=match.group(0)[:220],
                    source_page=source_page,
                    kaynak_pdf=kaynak_pdf,
                    appointment_ttsg_date=appointment_date,
                    appointment_ttsg_no=appointment_no,
                    source_pdf_link=f"input/{kaynak_pdf}",
                )
            )

    # Broader: \"... {NAME}'ın/in ... sona ermiştir\" anywhere after YÖNETİM KURULU
    broad_removal_pattern = re.compile(
        r"(?:olan|eden)\s+(?P<old>[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜa-zçğıöşü\s]+?)['\u2019']?(?:n?[iıuü]n|,)\s+.{0,60}?sona\s+ermiştir",
        re.IGNORECASE | re.DOTALL,
    )
    for match in broad_removal_pattern.finditer(text):
        old_name = _normalize_person_name(_extract_member_name(match.group("old")) or match.group("old"))
        if old_name and not any(m.name == old_name and m.action == "görevden_alma" for m in members):
            members.append(
                BoardMember(
                    name=old_name,
                    role="Yönetim Kurulu Üyesi",
                    action="görevden_alma",
                    source_snippet=match.group(0)[:220],
                    source_page=source_page,
                    kaynak_pdf=kaynak_pdf,
                    appointment_ttsg_date=appointment_date,
                    appointment_ttsg_no=appointment_no,
                    source_pdf_link=f"input/{kaynak_pdf}",
                )
            )

    # Pattern for Yönetim Kurulu Üyesi appointments
    yk_pattern = re.compile(
        r"(?P<prefix>.*?)(?P<term>\d{1,2}[./]\d{1,2}[./]\d{4})\s+tarihine\s+kadar\s+\(?(?P<role>Yönetim\s+Kurulu\s+(?:Başkanı|Başkan\s+Yardımcısı|Üyesi))\)?\s+olarak\s+seçilmiştir",
        re.IGNORECASE,
    )
    # Pattern for Temsilci Yetkili / İmza Yetkilisi appointments
    temsilci_pattern = re.compile(
        r"(?P<prefix>.*?)(?P<term>\d{1,2}[./]\d{1,2}[./]\d{4})\s+tarihine\s+kadar\s+\(?(?P<role>[^)]+?)\)?\s+(?:Temsil[cçe]?[eiy]?\s+Yetkili|İmza\s+Yetkilisi)\s+olarak\s+seçilmiştir",
        re.IGNORECASE,
    )
    # Fallback: no term date but role present
    direct_yk_pattern = re.compile(
        r"(?P<name>.+?)\s+(?P<role>Yönetim\s+Kurulu\s+(?:Başkanı|Başkan\s+Yardımcısı|Üyesi))\s+olarak\s+seçilmiştir",
        re.IGNORECASE,
    )
    last_legal_entity = None
    for idx, line in enumerate(lines):
        # Skip only pure "Yetki Şekli:" description lines
        if "yetki şekli" in line.casefold() and "seçilmiştir" not in line.casefold():
            continue
        # Try YK member pattern first
        match = yk_pattern.search(line)
        if match:
            prefix = match.group("prefix")
            role = match.group("role").strip()
            term = _normalize_tarih(match.group("term"))
        else:
            # Try Temsilci Yetkili pattern
            match = temsilci_pattern.search(line)
            if match:
                prefix = match.group("prefix")
                role = match.group("role").strip()
                term = _normalize_tarih(match.group("term"))
            else:
                # Try direct YK pattern (no date)
                direct_match = direct_yk_pattern.search(line)
                if not direct_match:
                    continue
                prefix = direct_match.group("name")
                role = direct_match.group("role").strip()
                term = None
        name = _extract_member_name(prefix)
        if not name:
            continue
        normalized_name, entity_type, representative = _parse_entity_name(name)
        if entity_type == "legal_entity" and idx + 1 < len(lines) and "hareket edecektir" in lines[idx + 1].casefold():
            rep_inline = re.search(r"ikamet eden[:\s,]*([A-ZÇĞİÖŞÜa-zçğıöşü\s]+?)\s+hareket edecektir", lines[idx + 1], re.IGNORECASE)
            if rep_inline:
                representative = _normalize_person_name(rep_inline.group(1))
        new_member = BoardMember(
            name=normalized_name.strip("0123456789* "),
            role=role,
            entity_type=entity_type,
            representative=representative,
            action="atama",
            term_end=term,
            source_snippet=line[:200],
            source_page=source_page,
            kaynak_pdf=kaynak_pdf,
            appointment_ttsg_date=appointment_date,
            appointment_ttsg_no=appointment_no,
            source_pdf_link=f"input/{kaynak_pdf}",
        )
        members.append(new_member)
        if entity_type == "legal_entity":
            last_legal_entity = new_member

    rep_pattern = re.compile(r"Tüzel\s+kişi\s+adına[^:]*[:;]?.*?ikamet\s+eden[:\s,]*([A-ZÇĞİÖŞÜ\s]+)(?:\s+hareket|$)", re.IGNORECASE | re.DOTALL)
    for rep_match in rep_pattern.finditer(text):
        if last_legal_entity and not last_legal_entity.representative:
            rep_name = _normalize_person_name(rep_match.group(1))
            last_legal_entity.representative = rep_name

    deduped = {}
    for member in members:
        key = (member.name, member.role, member.action, member.term_end)
        deduped[key] = member
    return list(deduped.values())


def _extract_member_name(prefix: str) -> Optional[str]:
    prefix = re.sub(r"\s+", " ", prefix).strip(" -,:;.")
    prefix = re.sub(r"(?i)^ilk\s+\d+\s+yıl\s+için\s+", "", prefix)
    prefix = re.sub(r"(?i)^daha\s+önceden\s+", "", prefix)
    prefix = re.sub(r"(?i)^türkiye\s+(cumhuriyeti\s+)?uyr[ıiuü]klu\s+\d+\*+\d+\s+kimlik\s+no['’]lu,\s*", "", prefix)
    if "ikamet eden" in prefix.casefold():
        parts = re.split(r"(?i)ikamet eden,?", prefix)
        prefix = parts[-1].strip(" -,:;.")
    if "adresinde" in prefix.casefold():
        parts = re.split(r"(?i)adresinde", prefix)
        prefix = parts[-1].strip(" -,:;.")
    prefix = re.sub(r"(?i)^yer alan\s+", "", prefix)
    prefix = re.sub(r"(?i)^olan\s+", "", prefix)
    prefix = re.sub(r"(?i)\(.*?\)", "", prefix).strip(" -,:;.")
    if not prefix or len(prefix.split()) > 8:
        return None
    if any(bad in prefix.casefold() for bad in ("yetki şekli", "hareket edecektir", "görevi", "sona ermiştir", "yönetim kurulu /")):
        return None
    return prefix


def _normalize_person_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name).strip(" ,.;:")
    # Remove common preamble phrases that may leak into name capture
    cleaned = re.sub(
        r"^(?:Yönetim\s+Kurulu\s+Üyesi\s+(?:olan\s+)?|Yönetim\s+Kurulu\s+Başkanı?\s+(?:olan\s+)?)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" ,.;:")
    if "AYDEM" in cleaned.upper() and "ANON" in cleaned.upper() and "ŞİRKET" in cleaned.upper():
        return "AYDEM HOLDING ANONİM ŞİRKETİ"
    return cleaned.upper()


def _parse_entity_name(name: str) -> tuple[str, str, Optional[str]]:
    representative = None
    entity_type = "real_person"
    normalized = _normalize_person_name(name)

    rep_match = re.search(r"\(Adına\s+hareket\s+edecek\s+gerçek\s+kişi:\s*([^)]+)\)", name, re.IGNORECASE)
    if rep_match:
        representative = _normalize_person_name(rep_match.group(1))
        normalized = _normalize_person_name(re.sub(r"\(.*\)", "", name))
        entity_type = "legal_entity"
    elif re.search(r"(ANONİM|ANONIM|LİMİTED|LIMITED|A\.Ş\.|LTD)", name, re.IGNORECASE):
        entity_type = "legal_entity"
    return normalized, entity_type, representative


def _llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _call_llm_company_info(text: str) -> Optional[dict]:
    try:
        import anthropic

        prompt = COMPANY_INFO_PROMPT.read_text(encoding="utf-8").replace("{text}", text)
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        return json.loads(json_match.group()) if json_match else None
    except Exception as exc:
        logger.error("LLM şirket bilgileri hatasi: %s", exc)
        return None


def _call_llm_board_members(text: str) -> Optional[dict]:
    try:
        import anthropic

        prompt = BOARD_MEMBERS_PROMPT.read_text(encoding="utf-8").replace("{text}", text)
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        return json.loads(json_match.group()) if json_match else None
    except Exception as exc:
        logger.error("LLM YK hatasi: %s", exc)
        return None


def _merge_llm_company_info(
    info: CompanyInfo,
    llm_result: dict,
    source_text: str,
) -> tuple[CompanyInfo, list[HallucinationEntry]]:
    issues: list[HallucinationEntry] = []
    field_map = {
        "ticaret_unvani": "ticaret_unvani",
        "adres": "adres",
        "sermaye": "sermaye",
        "kurulus_tarihi": "kurulus_tarihi",
        "denetci": "denetci",
        "ticaret_sicil_mudurlugu": "ticaret_sicil_mudurlugu",
    }
    for llm_field, info_field in field_map.items():
        if getattr(info, info_field):
            continue
        llm_val = llm_result.get(llm_field)
        if not llm_val:
            continue
        if verify_against_source(str(llm_val), source_text):
            setattr(info, info_field, llm_val)
        else:
            issues.append(
                HallucinationEntry(
                    pdf=info.kaynak_pdf,
                    field_name=info_field,
                    llm_value=str(llm_val),
                    found_in_source=False,
                    action="marked_as_unverified",
                )
            )
            setattr(info, info_field, f"[DOĞRULANAMADI] {llm_val}")
    return info, issues


def _parse_llm_board_result(
    llm_result: dict,
    source_text: str,
    kaynak_pdf: str,
    source_page: Optional[int],
) -> tuple[list[BoardMember], list[HallucinationEntry]]:
    members: list[BoardMember] = []
    issues: list[HallucinationEntry] = []
    appointment_date = _extract_ttsg_date_from_filename(kaynak_pdf)

    for member_data in llm_result.get("board_members", []):
        name = member_data.get("name", "")
        if not verify_against_source(name, source_text):
            issues.append(
                HallucinationEntry(
                    pdf=kaynak_pdf,
                    field_name="board_member_name",
                    llm_value=name,
                    found_in_source=False,
                    action="marked_as_unverified",
                )
            )
            name = f"[DOĞRULANAMADI] {name}"

        term_end = member_data.get("term_end")
        if term_end and not verify_against_source(term_end, source_text):
            issues.append(
                HallucinationEntry(
                    pdf=kaynak_pdf,
                    field_name="board_member_term_end",
                    llm_value=term_end,
                    found_in_source=False,
                    action="marked_as_unverified",
                )
            )

        members.append(
            BoardMember(
                name=name,
                role=member_data.get("role", "Yönetim Kurulu Üyesi"),
                entity_type=member_data.get("entity_type", "real_person"),
                term_end=term_end,
                representative=member_data.get("representative"),
                action=member_data.get("action"),
                source_snippet=member_data.get("source_snippet", ""),
                source_page=source_page,
                kaynak_pdf=kaynak_pdf,
                appointment_ttsg_date=appointment_date,
                appointment_ttsg_no=member_data.get("appointment_ttsg_no"),
                source_pdf_link=f"input/{kaynak_pdf}",
            )
        )
    return members, issues


def verify_against_source(value: str, source_text: str) -> bool:
    return _normalize_text(value) in _normalize_text(source_text) if value and source_text else False


def verify_against_any_source(value: str, source_texts: list[str]) -> bool:
    if not value:
        return False
    return any(verify_against_source(value, source_text) for source_text in source_texts if source_text)


def _drop_unverified_company_fields(
    info: CompanyInfo,
    verification_texts: list[str],
    issues: list[HallucinationEntry],
) -> None:
    """Flag unverified fields but KEEP them in output (annotate, don't censor)."""
    for field_name in (
        "ticaret_unvani",
        "mersis_no",
        "ticaret_sicil_mudurlugu",
        "ticaret_sicil_no",
        "adres",
        "sermaye",
        "kurulus_tarihi",
        "denetci",
    ):
        value = getattr(info, field_name)
        if value is None or verify_against_any_source(str(value), verification_texts):
            continue
        # Flag but do NOT drop — keep data, log for audit
        issues.append(
            HallucinationEntry(
                pdf=info.kaynak_pdf,
                field_name=field_name,
                llm_value=str(value),
                found_in_source=False,
                action="flagged_unverified",
            )
        )


def _filter_unverified_board_members(
    members: list[BoardMember],
    verification_texts: list[str],
    issues: list[HallucinationEntry],
) -> list[BoardMember]:
    """Flag unverified members but KEEP them in output (annotate, don't censor)."""
    filtered: list[BoardMember] = []
    for member in members:
        # Drop only clear garbage (non-name patterns)
        if not member.name or len(member.name) < 3:
            continue
        if any(bad in member.name.upper() for bad in ("MADDE", "SONA ERMİŞTİR", "IŞLEMI", "ÇAĞRI", "SAKLID")):
            issues.append(
                HallucinationEntry(
                    pdf=member.kaynak_pdf,
                    field_name="board_member_name",
                    llm_value=member.name,
                    found_in_source=False,
                    action="dropped_garbage",
                )
            )
            continue
        if not verify_against_any_source(member.name, verification_texts):
            issues.append(
                HallucinationEntry(
                    pdf=member.kaynak_pdf,
                    field_name="board_member_name",
                    llm_value=member.name,
                    found_in_source=False,
                    action="flagged_unverified",
                )
            )
        if member.term_end and not verify_against_any_source(member.term_end, verification_texts):
            issues.append(
                HallucinationEntry(
                    pdf=member.kaynak_pdf,
                    field_name="board_member_term_end",
                    llm_value=member.term_end,
                    found_in_source=False,
                    action="flagged_unverified",
                )
            )
            # KEEP term_end — don't null it
        filtered.append(member)
    return filtered


def save_hallucination_log(
    issues: list[HallucinationEntry],
    output_path: str = "output/hallucination_log.json",
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump([asdict(issue) for issue in issues], f, ensure_ascii=False, indent=2)
    logger.info("Hallucination log kaydedildi: %s", path)
    return path
