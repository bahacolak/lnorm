"""
filter.py - Target company extraction with explicit safety status.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HEDEF_SIRKET_PATTERNS = [
    re.compile(r"PARLA\s+ENERJ[İI]\s+YATIRIMLARI\s+ANON[İI]M\s+Ş[İI]RKET[İI]", re.IGNORECASE),
    re.compile(r"PARLA\s+ENERJ[İI]\s+YATIRIM", re.IGNORECASE),
]

COMPANY_HEADER = re.compile(
    r"^(?:#+\s*)?[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ0-9\s\.,\-/&]+(?:ANON[İI]M|L[İI]M[İI]TED)\s+Ş[İI]RKET[İI]\s*$",
    re.MULTILINE,
)

END_SIGNALS = [
    re.compile(r"^(?:#+\s*)?T\.C\.\s+.*T[İI]CARET\s+S[İI]C[İI]L[İI]\s+M[ÜU]D[ÜU]RL[ÜU][ĞG][ÜU]'?NDEN", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^(?:#+\s*)?İlan\s+Sıra\s+No\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^(?:#+\s*)?[A-ZÇĞİÖŞÜ\s]+\s+T[İI]CARET\s+S[İI]C[İI]L[İI]\s+M[ÜU]D[ÜU]RL[ÜU][ĞG][ÜU]'?NDEN", re.IGNORECASE | re.MULTILINE),
]

START_SIGNALS = [
    re.compile(r"^(?:#+\s*)?T\.C\.\s+.*T[İI]CARET\s+S[İI]C[İI]L[İI]\s+M[ÜU]D[ÜU]RL[ÜU][ĞG][ÜU]'?NDEN", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^(?:#+\s*)?İlan\s+Sıra\s+No\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^(?:#+\s*)?MERS[İI]S\s+No\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^(?:#+\s*)?Ticaret\s+Sicil(?:/Dosya)?\s+No\s*:", re.IGNORECASE | re.MULTILINE),
]

NOISE_PATTERNS = [
    re.compile(r"^\(Devam[ıi]\s+\d+\.?\s*Sayfada\)\s*$", re.IGNORECASE),
    re.compile(r"^\(Baştaraf[ıi]\s+\d+\.?\s*Sayfada\)\s*$", re.IGNORECASE),
    re.compile(r"^\(Başaraf[ıi]\s+\d+\.?\s*Sayfada\)\s*$", re.IGNORECASE),  # Mistral OCR typo variant
    re.compile(r"^[Iİ1l]{1,3}\s+[A-ZÇĞİÖŞÜ]+\s+\d{4}\s+SAYI[:;]?\s+\d+\s+T[ÜU]RK[İI]YE\s+T[İI]CAR.*$", re.IGNORECASE),
    re.compile(r"^\d{1,2}\s+[A-ZÇĞİÖŞÜ]+\s+\d{4}\s+SAYI[:;]?\s*\d+\s*$", re.IGNORECASE),  # "1 ARALIK 2022 SAYI: 10716"
    re.compile(r"^TÜRKİYE\s+TİCARET\s+SİCİLİ\s+GAZETESİ\s*$", re.IGNORECASE),
    re.compile(r"^SAYFA[:;]?\s*\d+\s*$", re.IGNORECASE),
]


@dataclass
class FilterResult:
    text: Optional[str]
    status: str
    start_anchor: Optional[str] = None
    end_anchor: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


def filter_target_company(
    ocr_texts: dict[int, str],
    pdf_path: Optional[str] = None,
) -> FilterResult:
    full_text = "\n\n".join(text for _, text in sorted(ocr_texts.items()) if text)
    if not full_text.strip():
        return FilterResult(text=None, status="not_found", warnings=["empty_ocr_text"])

    normalized = _clean_common_noise(full_text)
    target_match = _find_target_match(normalized)
    if not target_match:
        return FilterResult(text=None, status="not_found", warnings=["target_company_not_found"])

    start_anchor = target_match.group(0)
    start = _resolve_start(normalized, target_match.start())
    end, end_anchor, end_warning = _resolve_end(normalized, target_match.end())
    warnings = []
    if end_warning:
        warnings.append(end_warning)

    extracted = normalized[start:end].strip()
    extracted = _trim_trailing_noise(extracted)
    if not extracted:
        return FilterResult(text=None, status="not_found", warnings=["empty_extract_after_trim"])

    status = "ok"
    if warnings:
        status = "partial"
    if end == len(normalized):
        if "boundary_unresolved" not in warnings:
            warnings.append("boundary_unresolved")
        
        # Eğer tek uyarı tipi boundary_unresolved ise, bunu normal kabul et ve statüyü ok olarak bırak.
        if all(w == "boundary_unresolved" for w in warnings):
            status = "ok"

    return FilterResult(
        text=extracted,
        status=status,
        start_anchor=start_anchor,
        end_anchor=end_anchor,
        warnings=warnings,
    )


def _find_target_match(text: str) -> Optional[re.Match[str]]:
    for pattern in HEDEF_SIRKET_PATTERNS:
        match = pattern.search(text)
        if match:
            return match
    return None


def _resolve_start(text: str, target_pos: int) -> int:
    line_start = text.rfind("\n", 0, target_pos)
    line_start = 0 if line_start == -1 else line_start + 1

    best_start = line_start
    for match in COMPANY_HEADER.finditer(text):
        if match.start() <= target_pos:
            best_start = match.start()
        else:
            break

    window_start = max(0, best_start - 1200)
    for pattern in START_SIGNALS:
        matches = list(pattern.finditer(text, window_start, target_pos))
        if matches:
            best_start = min(best_start, matches[-1].start())
    return best_start


def _resolve_end(text: str, target_end: int) -> tuple[int, Optional[str], Optional[str]]:
    candidate_positions: list[tuple[int, str]] = []

    for match in COMPANY_HEADER.finditer(text):
        if match.start() > target_end:
            candidate_positions.append((match.start(), match.group(0)))

    for pattern in END_SIGNALS:
        match = pattern.search(text, pos=target_end)
        if match:
            candidate_positions.append((match.start(), match.group(0)))

    if not candidate_positions:
        return len(text), None, "boundary_unresolved"

    boundary_pos, anchor = min(candidate_positions, key=lambda item: item[0])
    return boundary_pos, anchor, None


def _trim_trailing_noise(text: str) -> str:
    lines = text.splitlines()
    kept = []
    for line in lines:
        stripped = line.strip()
        if any(pattern.match(stripped) for pattern in NOISE_PATTERNS):
            continue
        if re.match(r"^\(\d{6,}\)$", stripped):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _clean_common_noise(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if any(pattern.match(stripped) for pattern in NOISE_PATTERNS):
            continue
        lines.append(line.rstrip())
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def save_extracted_text(
    result: FilterResult,
    pdf_filename: str,
    output_dir: str = "output/extracted_texts",
) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    txt_filename = Path(pdf_filename).stem + ".txt"
    file_path = output_path / txt_filename
    payload = result.text or ""
    file_path.write_text(payload, encoding="utf-8")
    logger.info("Kaydedildi: %s", file_path)
    return file_path


def filter_result_to_dict(result: FilterResult) -> dict:
    return asdict(result)


if __name__ == "__main__":
    import sys
    from .pdf_reader import extract_text

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) < 2:
        print("Kullanim: python filter.py <pdf_dosyasi>")
        raise SystemExit(1)

    ocr_texts = extract_text(sys.argv[1], provider="tesseract")
    filtered = filter_target_company(ocr_texts, pdf_path=sys.argv[1])
    print(filtered.status)
    print(filtered.text[:2000] if filtered.text else "No extract")
