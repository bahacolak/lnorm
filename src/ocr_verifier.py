"""
ocr_verifier.py - Cross-validation layer for dual OCR outputs.

Compares primary (Mistral) and secondary (Tesseract) OCR outputs at
paragraph/article level. Flags disagreements and produces a review queue.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from .persistence import write_json
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hukuki terim sözlüğü — bilinen OCR bozulmalarını flaglemek için.
# Bu sözlük otomatik düzeltme YAPMAZ; disputed durumu üretir.
# ---------------------------------------------------------------------------
LEGAL_TERM_EXPECTED: dict[str, list[str]] = {
    # Doğru terim  →  bilinen OCR bozulmaları
    "Hidroelektrik": ["Eidroelektrik", "Hidraelektrik", "Hirdoelektrik"],
    "türbinleri": ["tribünleri", "tribüsleri", "türbünleri"],
    "tüzel kişi": ["tüzeli kişi", "tüzeli kisi"],
    "mümessillik": ["mümesellik", "mümesillik"],
    "iştigal": ["işe gidiçel", "iştigâl"],
    "sınai mülkiyet": ["sınav mülkiyet", "sınav mükûret", "sınav mükûnî"],
    "sınai": ["sınav"],
    "Türk Ticaret Kanunu": ["Türk Ticaret Kurumu"],
    "Uyruklu": ["Uşruklu", "Uşrukİu"],
    "Vekâletnameler": ["Vekiletmeneler", "Vekîletmeneler"],
    "imtiyaz": ["testiyaç", "testiyar"],
    "ihtira beratı": ["ihtiya bensi", "ihtiya besin"],
    "paya ayrılmış": ["pay aşırmış", "paya aşırmış"],
    "itibari": ["biberi"],
    "pay senetlerini": ["pay sanatlerini", "pay satarlari"],
    "küpürler": ["kuptürler", "kuptolar"],
    "fıkrasının": ["fikrini", "fıkrını"],
    "haiz": ["huz"],
    "müteselsi̇l": ["mütevellal", "müteselid"],
    "edilecek": ["adilacık", "adilacik"],
    "çağrılmasına": ["dağıtılmasına"],
    "Ana sözleşme": ["Anaotvleşme"],
    "ESENTEPE": ["ENENTEPE"],
    "ilmühaberi": ["ilmihaberi", "ilmihabeti"],
}

# Flatten for fast lookup: {bozuk_kelime_lower: doğru_terim}
_ANOMALY_INDEX: dict[str, str] = {}
for correct, variants in LEGAL_TERM_EXPECTED.items():
    for variant in variants:
        if len(variant.strip()) >= 5:
            _ANOMALY_INDEX[variant.casefold()] = correct


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class VerifiedSpan:
    field_name: str
    primary_text: str
    secondary_text: str
    final_text: str
    status: str  # "verified" | "primary_only" | "disputed" | "unverified"
    disagreement_score: float
    evidence: str
    pdf: str = ""
    page: Optional[int] = None


@dataclass
class ReviewQueueEntry:
    pdf: str
    section_type: str  # "esas_sozlesme" | "sirket_bilgisi" | "yk"
    identifier: str  # "madde_3" or field name
    page: Optional[int]
    primary_ocr: str
    secondary_ocr: str
    reason: str
    recommended_action: str  # "vision_reocr" | "manual_review"


@dataclass
class LegalTermAnomaly:
    position: int
    found_text: str
    expected_text: str
    context: str  # surrounding text snippet


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------
DISAGREEMENT_THRESHOLD_VERIFIED = 0.05   # ≤5% → verified
DISAGREEMENT_THRESHOLD_DISPUTED = 0.30   # ≤30% → disputed, >30% → unverified


def cross_validate_ocr(
    primary: dict[int, str],
    secondary: dict[int, str],
    pdf_name: str = "",
) -> tuple[dict[int, VerifiedSpan], list[ReviewQueueEntry]]:
    """
    Compare primary and secondary OCR outputs page-by-page.

    Returns verified spans and review queue entries for disputed content.
    """
    spans: dict[int, VerifiedSpan] = {}
    review_queue: list[ReviewQueueEntry] = []

    all_pages = sorted(set(primary.keys()) | set(secondary.keys()))

    for page in all_pages:
        p_text = primary.get(page, "")
        s_text = secondary.get(page, "")

        if not p_text and not s_text:
            continue

        score = calculate_disagreement_score(p_text, s_text)
        status, evidence = _classify_score(score, p_text, s_text)

        span = VerifiedSpan(
            field_name=f"page_{page}",
            primary_text=p_text,
            secondary_text=s_text,
            final_text=p_text if status in {"verified", "primary_only"} else "",
            status=status,
            disagreement_score=score,
            evidence=evidence,
            pdf=pdf_name,
            page=page,
        )
        spans[page] = span

        if status in ("disputed", "unverified", "primary_only"):
            review_queue.append(
                ReviewQueueEntry(
                    pdf=pdf_name,
                    section_type="full_page",
                    identifier=f"page_{page}",
                    page=page,
                    primary_ocr=p_text[:500],
                    secondary_ocr=s_text[:500],
                    reason=evidence,
                    recommended_action="audit_primary_only" if status == "primary_only" else ("vision_reocr" if status == "disputed" else "manual_review"),
                )
            )

    return spans, review_queue


def cross_validate_articles(
    primary_text: str,
    secondary_text: str,
    pdf_name: str = "",
) -> tuple[list[VerifiedSpan], list[ReviewQueueEntry]]:
    """
    Compare article-level content between two OCR outputs.
    Splits texts by MADDE pattern and compares each article.
    """
    primary_articles = _split_into_articles(primary_text)
    secondary_articles = _split_into_articles(secondary_text)
    spans: list[VerifiedSpan] = []
    review_queue: list[ReviewQueueEntry] = []

    all_madde_nos = sorted(set(primary_articles.keys()) | set(secondary_articles.keys()))

    for madde_no in all_madde_nos:
        p_text = primary_articles.get(madde_no, "")
        s_text = secondary_articles.get(madde_no, "")

        score = calculate_disagreement_score(p_text, s_text)
        status, evidence = _classify_score(score, p_text, s_text)

        # Also check for known legal term anomalies in primary
        anomalies = detect_legal_term_anomalies(p_text)
        if anomalies and status == "verified":
            status = "disputed"
            evidence = f"Hukuki terim anomalileri bulundu: {', '.join(a.found_text for a in anomalies[:3])}"

        span = VerifiedSpan(
            field_name=f"madde_{madde_no}",
            primary_text=p_text,
            secondary_text=s_text,
            final_text=p_text if status in {"verified", "primary_only"} else "",
            status=status,
            disagreement_score=score,
            evidence=evidence,
            pdf=pdf_name,
        )
        spans.append(span)

        if status in ("disputed", "unverified", "primary_only"):
            review_queue.append(
                ReviewQueueEntry(
                    pdf=pdf_name,
                    section_type="esas_sozlesme",
                    identifier=f"madde_{madde_no}",
                    page=None,
                    primary_ocr=p_text[:500],
                    secondary_ocr=s_text[:500],
                    reason=evidence,
                    recommended_action="audit_primary_only" if status == "primary_only" else "vision_reocr",
                )
            )

    return spans, review_queue


def calculate_disagreement_score(text_a: str, text_b: str) -> float:
    """
    Normalized disagreement score between two texts.
    0.0 = identical, 1.0 = completely different.
    """
    if not text_a and not text_b:
        return 0.0
    if not text_a or not text_b:
        return 1.0

    norm_a = _normalize_for_comparison(text_a)
    norm_b = _normalize_for_comparison(text_b)

    if norm_a == norm_b:
        return 0.0

    ratio = SequenceMatcher(None, norm_a, norm_b).ratio()
    return round(1.0 - ratio, 4)


def detect_legal_term_anomalies(text: str) -> list[LegalTermAnomaly]:
    """
    Scan text for known OCR corruptions of legal terms.
    Does NOT auto-correct; only flags for review.
    """
    anomalies: list[LegalTermAnomaly] = []
    folded_text = text.casefold()

    for variant_folded, correct_term in _ANOMALY_INDEX.items():
        pattern = re.compile(rf"(?<!\w){re.escape(variant_folded)}(?!\w)")
        start = 0
        while True:
            match = pattern.search(folded_text, pos=start)
            if not match:
                break
            pos = match.start()
            # Extract context
            ctx_start = max(0, pos - 30)
            ctx_end = min(len(text), pos + len(variant_folded) + 30)
            context = text[ctx_start:ctx_end]

            anomalies.append(
                LegalTermAnomaly(
                    position=pos,
                    found_text=text[pos : pos + len(variant_folded)],
                    expected_text=correct_term,
                    context=context,
                )
            )
            start = pos + len(variant_folded)

    return anomalies


def calculate_field_confidence(
    verified_spans: list[VerifiedSpan],
) -> dict[str, dict]:
    """
    Produce a field-level confidence summary from verified spans.
    """
    result = {}
    for span in verified_spans:
        result[span.field_name] = {
            "status": span.status,
            "disagreement_score": span.disagreement_score,
            "evidence": span.evidence,
            "pdf": span.pdf,
        }
    return result


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def save_review_queue(
    entries: list[ReviewQueueEntry],
    output_path: str = "output/review_queue.json",
) -> Path:
    return write_json(
        output_path,
        [asdict(entry) for entry in entries],
        logger=logger,
        message="Review queue kaydedildi",
    )


def save_field_confidence(
    confidence: dict[str, dict],
    output_path: str = "output/field_confidence.json",
) -> Path:
    return write_json(
        output_path,
        confidence,
        logger=logger,
        message="Field confidence kaydedildi",
    )


def save_article_comparison(
    spans: list[VerifiedSpan],
    output_path: str = "output/article_comparison.json",
) -> Path:
    data = []
    for s in spans:
        data.append({
            "field_name": s.field_name,
            "status": s.status,
            "disagreement_score": s.disagreement_score,
            "evidence": s.evidence,
            "primary_text": s.primary_text[:300],
            "secondary_text": s.secondary_text[:300],
        })
    return write_json(
        output_path,
        data,
        logger=logger,
        message="Article comparison kaydedildi",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _classify_score(
    score: float,
    p_text: str,
    s_text: str,
) -> tuple[str, str]:
    """Return (status, evidence) based on disagreement score."""
    if p_text and not s_text.strip():
        return "primary_only", "Secondary OCR boş döndü, primary kabul edildi"
    if score <= DISAGREEMENT_THRESHOLD_VERIFIED:
        return "verified", f"Düşük fark skoru ({score:.2%}), iki OCR uyumlu"
    if score <= DISAGREEMENT_THRESHOLD_DISPUTED:
        # Find specific word-level differences
        diffs = _find_word_diffs(p_text, s_text)
        diff_summary = "; ".join(diffs[:5])
        return "disputed", f"Fark skoru {score:.2%}. Kelime farkları: {diff_summary}"
    return "unverified", f"Yüksek fark skoru ({score:.2%}), metin güvenilir değil"


def _find_word_diffs(text_a: str, text_b: str, max_diffs: int = 10) -> list[str]:
    """Find first N word-level differences between two texts."""
    words_a = _normalize_for_comparison(text_a).split()
    words_b = _normalize_for_comparison(text_b).split()
    diffs: list[str] = []

    sm = SequenceMatcher(None, words_a, words_b)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a_chunk = " ".join(words_a[i1:i2])
        b_chunk = " ".join(words_b[j1:j2])
        if tag == "replace":
            diffs.append(f"'{a_chunk}' ↔ '{b_chunk}'")
        elif tag == "delete":
            diffs.append(f"silindi: '{a_chunk}'")
        elif tag == "insert":
            diffs.append(f"eklendi: '{b_chunk}'")
        if len(diffs) >= max_diffs:
            break
    return diffs


def _normalize_for_comparison(text: str) -> str:
    """Normalize text for comparison: lowercase, collapse whitespace, strip punctuation noise."""
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    # Normalize common dash variants
    text = re.sub(r"[–—−]", "-", text)
    return text.strip()


_ARTICLE_SPLIT_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:#+\s*)?(?:MADDE|Madde)\s+(\d{1,2})\s*[-:\.—]",
    re.MULTILINE | re.IGNORECASE,
)
_ARTICLE_SPLIT_PATTERN_NUMBERED = re.compile(
    r"(?:^|\n)\s*(?:#+\s*)?(\d{1,2})\.\s+[A-ZÇĞİÖŞÜ]",
    re.MULTILINE,
)


def _split_into_articles(text: str) -> dict[int, str]:
    """Split text into article segments keyed by article number."""
    matches = list(_ARTICLE_SPLIT_PATTERN.finditer(text))
    if not matches:
        matches = list(_ARTICLE_SPLIT_PATTERN_NUMBERED.finditer(text))
    if not matches:
        return {}

    articles: dict[int, str] = {}
    for idx, match in enumerate(matches):
        madde_no = int(match.group(1))
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        articles[madde_no] = text[start:end].strip()
    return articles
