"""
article_normalizer.py - Rule-based and LLM-assisted normalization for articles.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Literal

from .articles_parser import Article
from .ocr_verifier import LEGAL_TERM_EXPECTED, detect_legal_term_anomalies
from .persistence import write_json

logger = logging.getLogger(__name__)

DEFAULT_ARTICLE_NORMALIZATION_MODEL = "claude-sonnet-4-20250514"
MAX_NEW_TOKEN_RATIO = 0.20
MIN_SIMILARITY_RATIO = 0.70
MAX_UNCERTAIN_SPANS = 10
BLOCK_SIMILARITY_RATIO = 0.55
TABLE_CELL_HEADERS = [
    "Sayı No",
    "Adı Soyadı / Unvanı",
    "Adres",
    "Uyruğu",
    "Kimlik / Vergi No",
]
HEADER_EQUIVALENTS = {
    "sayi no": "Sayı No",
    "sira no": "Sayı No",
    "sıra no": "Sayı No",
    "no": "Sayı No",
    "kurucu": "Adı Soyadı / Unvanı",
    "adi soyadi unvani": "Adı Soyadı / Unvanı",
    "adı soyadı unvanı": "Adı Soyadı / Unvanı",
    "adi soyadi / unvani": "Adı Soyadı / Unvanı",
    "adı soyadı / unvanı": "Adı Soyadı / Unvanı",
    "karari": "Adı Soyadı / Unvanı",
    "karar": "Adı Soyadı / Unvanı",
    "kararı": "Adı Soyadı / Unvanı",
    "adres": "Adres",
    "uyrugu": "Uyruğu",
    "uyruğu": "Uyruğu",
    "uyruk": "Uyruğu",
    "gerak": "Uyruğu",
    "kimlik no": "Kimlik / Vergi No",
    "kimlik / vergi no": "Kimlik / Vergi No",
    "vergi no": "Kimlik / Vergi No",
    "mersis no": "Kimlik / Vergi No",
    "kendik no": "Kimlik / Vergi No",
}


@dataclass
class ArticleBlock:
    type: Literal["paragraph", "bullet_list", "table", "raw"]
    text: str | None = None
    rows: list[list[str]] | None = None
    cell_statuses: list[list[str]] | None = None
    note: str | None = None


@dataclass
class StructuredArticleDraft:
    madde_no: int
    title: str
    blocks: list[ArticleBlock]
    raw_text: str
    issues: list[str] = field(default_factory=list)
    needs_llm: bool = False


@dataclass
class NormalizedArticleResult:
    madde_no: int
    title: str
    blocks: list[ArticleBlock]
    source_mode: Literal["raw", "rule_based", "llm_normalized"]
    verification_status: Literal["accepted", "fallback_rule_based", "fallback_raw"]
    change_flags: list[str] = field(default_factory=list)
    uncertain_spans: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    llm_attempted: bool = False
    llm_blocks_accepted: int = 0
    llm_blocks_rejected: int = 0
    table_cells_published: int = 0
    table_cells_suppressed: int = 0
    publish_mode: Literal["llm_first", "hybrid_fallback", "rule_based_only"] = "rule_based_only"
    decision_entries: list[dict] = field(default_factory=list)


def build_article_draft(article: Article) -> StructuredArticleDraft:
    body = _strip_duplicate_heading(article.icerik, article.madde_no, article.baslik)
    raw_lines = [line.strip() for line in body.splitlines() if line.strip()]
    blocks: list[ArticleBlock] = []
    issues: list[str] = []
    current_paragraph: list[str] = []
    current_list: list[str] = []
    table_rows: list[list[str]] = []

    for line in raw_lines:
        normalized_line = _normalize_inline_noise(line)
        if _is_table_rule_line(normalized_line):
            issues.append("table_rule_line")
            continue
        if _looks_like_table_row(normalized_line):
            if current_paragraph:
                blocks.append(ArticleBlock(type="paragraph", text=" ".join(current_paragraph)))
                current_paragraph = []
            if current_list:
                blocks.append(ArticleBlock(type="bullet_list", text="\n".join(current_list)))
                current_list = []
            table_rows.append(_split_table_row(normalized_line))
            continue
        if _looks_like_noise(normalized_line):
            issues.append("noise_line")
            continue
        if table_rows:
            blocks.append(ArticleBlock(type="table", rows=table_rows))
            table_rows = []
        if _looks_like_list_item(normalized_line):
            if current_paragraph:
                blocks.append(ArticleBlock(type="paragraph", text=" ".join(current_paragraph)))
                current_paragraph = []
            current_list.append(normalized_line)
            continue
        if current_list:
            blocks.append(ArticleBlock(type="bullet_list", text="\n".join(current_list)))
            current_list = []
        if blocks and blocks[-1].type == "paragraph" and _should_merge_with_previous_paragraph(normalized_line):
            blocks[-1].text = f"{blocks[-1].text} {normalized_line}".strip()
        else:
            current_paragraph.append(normalized_line)

    if current_paragraph:
        blocks.append(ArticleBlock(type="paragraph", text=" ".join(current_paragraph)))
    if current_list:
        blocks.append(ArticleBlock(type="bullet_list", text="\n".join(current_list)))
    if table_rows:
        blocks.append(ArticleBlock(type="table", rows=table_rows))

    if not blocks:
        blocks = [ArticleBlock(type="raw", text=body.strip())]
        issues.append("raw_fallback")

    if detect_legal_term_anomalies(body):
        issues.append("legal_term_anomaly")
    if "|" in body:
        issues.append("table_like_content")
    if re.search(r"[�▪▫◊◦●○]", body):
        issues.append("unknown_glyph")

    needs_llm = any(
        issue in {"legal_term_anomaly", "table_like_content", "unknown_glyph", "raw_fallback"}
        for issue in issues
    ) or _count_suspicious_tokens(body) >= 3
    return StructuredArticleDraft(
        madde_no=article.madde_no,
        title=article.baslik,
        blocks=blocks,
        raw_text=article.icerik,
        issues=issues,
        needs_llm=needs_llm,
    )


def normalize_article(
    article: Article,
    secondary_text: str | None = None,
    use_llm: bool = False,
    model: str = DEFAULT_ARTICLE_NORMALIZATION_MODEL,
) -> NormalizedArticleResult:
    draft = build_article_draft(article)
    if use_llm and draft.needs_llm:
        return normalize_article_with_llm(draft, secondary_text=secondary_text, model=model)
    return _build_rule_based_result(draft, secondary_text=secondary_text)


def normalize_article_with_llm(
    draft: StructuredArticleDraft,
    secondary_text: str | None = None,
    model: str = DEFAULT_ARTICLE_NORMALIZATION_MODEL,
) -> NormalizedArticleResult:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _build_rule_based_result(draft, secondary_text=secondary_text, reason="missing_anthropic_key")

    payload = _call_anthropic_normalizer(draft, model=model)
    if payload is None:
        return _build_rule_based_result(draft, secondary_text=secondary_text, reason="llm_error")

    candidate = _candidate_from_payload(payload, draft)
    return _merge_llm_candidate(candidate, draft, secondary_text)


def save_article_normalization_audit(entries: list[dict], output_path: str = "output/article_normalization_audit.json"):
    return write_json(output_path, entries, logger=logger, message="Article normalization audit kaydedildi")


def save_article_normalization_diff(entries: list[dict], output_path: str = "output/article_normalization_diff.json"):
    return write_json(output_path, entries, logger=logger, message="Article normalization diff kaydedildi")


def _call_anthropic_normalizer(draft: StructuredArticleDraft, model: str) -> dict | None:
    try:
        import anthropic

        client = anthropic.Anthropic()
        prompt = (
            "Aşağıdaki Türkçe hukuki metni YENİDEN YAZMADAN normalize et. "
            "İçerik ekleme, yorum yapma, anlam değiştirme. "
            "Yalnızca JSON dön. JSON alanları: madde_no, title, blocks, uncertain_spans, notes. "
            "blocks elemanları type=paragraph|bullet_list|table|raw olabilir. "
            "table için `rows` dizisi kullan (örn: [[\"A\", \"B\"], [\"1\", \"2\"]]).\n"
            "KESİNLİKLE markdown tablo (örn: | A | B |) veya tireli satır (örn: |---|---|) KULLANMA.\n"
            "Eğer tabloda başlık veya tire varsa JSON 'rows' listesine sadece saf veriyi koy.\n"
            "Tablo başlıklarını mümkünse canonical hale getir: Sayı No, Adı Soyadı / Unvanı, Adres, Uyruğu, Kimlik / Vergi No.\n"
            "Belirsiz hücreleri tahmin etme; notlara yaz ve ilgili hücreyi boş ya da kısa bırak.\n"
            "Gereksiz ham OCR çöpünü taşımamaya çalış. Emin değilsen raw veya paragraph dön. Markdown tablo yapısı GÖNDERME.\n\n"
            f"Madde no: {draft.madde_no}\n"
            f"Başlık: {draft.title}\n"
            f"Sorunlar: {', '.join(draft.issues) or 'yok'}\n"
            f"Ham metin:\n{draft.raw_text}"
        )
        message = client.messages.create(
            model=model,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        return json.loads(json_match.group(0)) if json_match else None
    except Exception as exc:
        logger.warning("Article normalization LLM hatasi: %s", exc)
        return None


def _candidate_from_payload(payload: dict, draft: StructuredArticleDraft) -> NormalizedArticleResult:
    blocks: list[ArticleBlock] = []
    for block in payload.get("blocks", []):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type not in {"paragraph", "bullet_list", "table", "raw"}:
            continue
        rows = block.get("rows")
        if block_type == "table":
            if not isinstance(rows, list) or not rows:
                continue
            normalized_rows = []
            for row in rows:
                if isinstance(row, list) and row:
                    normalized_rows.append([str(cell).strip() for cell in row])
            if not normalized_rows:
                continue
            blocks.append(ArticleBlock(type="table", rows=normalized_rows))
        else:
            text = str(block.get("text", "")).strip()
            if text:
                blocks.append(ArticleBlock(type=block_type, text=text))

    if not blocks:
        blocks = draft.blocks
    return NormalizedArticleResult(
        madde_no=int(payload.get("madde_no", draft.madde_no)),
        title=str(payload.get("title", draft.title)).strip() or draft.title,
        blocks=blocks,
        source_mode="llm_normalized",
        verification_status="accepted",
        change_flags=["llm_normalized"],
        uncertain_spans=[str(item).strip() for item in payload.get("uncertain_spans", []) if str(item).strip()],
        notes=[str(item).strip() for item in payload.get("notes", []) if str(item).strip()],
        llm_attempted=True,
        publish_mode="llm_first",
    )


def _build_rule_based_result(
    draft: StructuredArticleDraft,
    *,
    secondary_text: str | None,
    reason: str | None = None,
) -> NormalizedArticleResult:
    blocks, decision_entries, published_cells, suppressed_cells = _clean_blocks(
        draft.blocks,
        secondary_text=secondary_text,
        source="rule_based",
    )
    return NormalizedArticleResult(
        madde_no=draft.madde_no,
        title=draft.title,
        blocks=blocks or [ArticleBlock(type="raw", text=_safe_paragraph_fallback(_strip_duplicate_heading(draft.raw_text, draft.madde_no, draft.title)))],
        source_mode="rule_based",
        verification_status="fallback_rule_based" if reason else "accepted",
        change_flags=sorted(set(draft.issues + ([reason] if reason else []))),
        notes=["clean_fallback"] if reason else [],
        llm_attempted=reason in {"missing_anthropic_key", "llm_error"},
        table_cells_published=published_cells,
        table_cells_suppressed=suppressed_cells,
        publish_mode="rule_based_only",
        decision_entries=decision_entries,
    )


def _merge_llm_candidate(
    candidate: NormalizedArticleResult,
    draft: StructuredArticleDraft,
    secondary_text: str | None,
) -> NormalizedArticleResult:
    fallback = _build_rule_based_result(draft, secondary_text=secondary_text)
    candidate_blocks, candidate_decisions, candidate_published, candidate_suppressed = _clean_blocks(
        candidate.blocks,
        secondary_text=secondary_text,
        source="llm",
    )

    accepted_blocks: list[ArticleBlock] = []
    accepted_count = 0
    rejected_count = 0
    max_len = max(len(candidate_blocks), len(fallback.blocks))
    for idx in range(max_len):
        candidate_block = candidate_blocks[idx] if idx < len(candidate_blocks) else None
        fallback_block = fallback.blocks[idx] if idx < len(fallback.blocks) else None
        if candidate_block and _accept_block(candidate_block, fallback_block, secondary_text):
            accepted_blocks.append(candidate_block)
            accepted_count += 1
            continue
        if fallback_block:
            accepted_blocks.append(fallback_block)
            if candidate_block:
                rejected_count += 1

    if not accepted_blocks:
        return _build_rule_based_result(draft, secondary_text=secondary_text, reason="no_publishable_blocks")

    final_text = _flatten_blocks(accepted_blocks)
    if detect_legal_term_anomalies(final_text):
        candidate.change_flags.append("legal_term_anomaly_remaining")

    publish_mode: Literal["llm_first", "hybrid_fallback", "rule_based_only"]
    source_mode: Literal["raw", "rule_based", "llm_normalized"]
    verification_status: Literal["accepted", "fallback_rule_based", "fallback_raw"]
    if accepted_count and rejected_count:
        publish_mode = "hybrid_fallback"
        source_mode = "llm_normalized"
        verification_status = "fallback_rule_based"
    elif accepted_count:
        publish_mode = "llm_first"
        source_mode = "llm_normalized"
        verification_status = "accepted"
    else:
        publish_mode = "rule_based_only"
        source_mode = "rule_based"
        verification_status = "fallback_rule_based"

    return NormalizedArticleResult(
        madde_no=candidate.madde_no,
        title=candidate.title,
        blocks=accepted_blocks,
        source_mode=source_mode,
        verification_status=verification_status,
        change_flags=sorted(set(draft.issues + candidate.change_flags + (["partial_llm_reject"] if rejected_count else []))),
        uncertain_spans=candidate.uncertain_spans,
        notes=candidate.notes + (["some_blocks_fell_back_to_rule_based"] if rejected_count else []),
        llm_attempted=True,
        llm_blocks_accepted=accepted_count,
        llm_blocks_rejected=rejected_count,
        table_cells_published=max(candidate_published, fallback.table_cells_published),
        table_cells_suppressed=max(candidate_suppressed, fallback.table_cells_suppressed),
        publish_mode=publish_mode,
        decision_entries=fallback.decision_entries + candidate_decisions,
    )


def _strip_duplicate_heading(text: str, madde_no: int, title: str) -> str:
    heading_patterns = [
        rf"^\s*#*\s*(?:MADDE|Madde)?\s*{madde_no}\s*[-:\.—]?\s*{re.escape(title)}\s*$",
        rf"^\s*#*\s*{madde_no}\.\s*{re.escape(title)}\s*$",
    ]
    lines = text.splitlines()
    cleaned_lines = []
    skipped_heading = False
    for line in lines:
        stripped = line.strip()
        if not skipped_heading and any(re.match(pattern, stripped, re.IGNORECASE) for pattern in heading_patterns if title):
            skipped_heading = True
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def _normalize_inline_noise(line: str) -> str:
    normalized = line.replace("ŞIŞLİASTANBUL", "ŞİŞLİ / İSTANBUL")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _looks_like_noise(line: str) -> bool:
    if len(line) <= 2:
        return True
    if re.search(r"[|]{2,}", line):
        return False
    if re.search(r"[a-zçğıöşüA-ZÇĞİÖŞÜ0-9]\)[A-ZÇĞİÖŞÜa-zçğıöşü]", line):
        return False
    if re.search(r"[^\w\sçğıöşüÇĞİÖŞÜ/\-:;,.()#%]", line):
        weird = re.findall(r"[^\w\sçğıöşüÇĞİÖŞÜ/\-:;,.()#%]", line)
        if len(weird) >= 6:
            return True
    return False


def _looks_like_list_item(line: str) -> bool:
    return bool(re.match(r"^(?:\d+\)|[a-zçğıöşü]\)|[a-zçğıöşü]\.)\s+", line, re.IGNORECASE))


def _looks_like_table_row(line: str) -> bool:
    if "|" in line and len(line.split("|")) >= 3:
        return True
    return False


def _is_table_rule_line(line: str) -> bool:
    return bool(re.fullmatch(r"[\|\-\s:]+", line))


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip("|").split("|") if cell.strip()]


def _should_merge_with_previous_paragraph(line: str) -> bool:
    return not _looks_like_list_item(line) and not _looks_like_table_row(line)


def _count_suspicious_tokens(text: str) -> int:
    patterns = [
        r"\bEidroelektrik\b",
        r"\bUşruklu\b",
        r"\btestiyar\b",
        r"\bihtiya\b",
        r"\btribünleri\b",
    ]
    return sum(len(re.findall(pattern, text, re.IGNORECASE)) for pattern in patterns)


def _flatten_blocks(blocks: list[ArticleBlock]) -> str:
    parts = []
    for block in blocks:
        if block.type == "table":
            for row in block.rows or []:
                parts.append(" ".join(row))
        elif block.text:
            parts.append(block.text)
    return "\n".join(parts)


def _normalize_text(text: str) -> str:
    text = text.casefold()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _new_token_ratio(candidate_text: str, raw_text: str) -> float:
    candidate_tokens = [token for token in re.split(r"\W+", candidate_text) if token]
    raw_tokens = set(token for token in re.split(r"\W+", raw_text) if token)
    if not candidate_tokens:
        return 1.0
    new_tokens = [token for token in candidate_tokens if token not in raw_tokens]
    return len(new_tokens) / len(candidate_tokens)


def _clean_blocks(
    blocks: list[ArticleBlock],
    *,
    secondary_text: str | None,
    source: Literal["rule_based", "llm"],
) -> tuple[list[ArticleBlock], list[dict], int, int]:
    cleaned_blocks: list[ArticleBlock] = []
    decision_entries: list[dict] = []
    published_cells = 0
    suppressed_cells = 0

    for block in blocks:
        if block.type == "table" and block.rows:
            cleaned_block, block_decisions, block_published, block_suppressed = _clean_table_block(
                block,
                secondary_text=secondary_text,
                source=source,
            )
            cleaned_blocks.append(cleaned_block)
            decision_entries.extend(block_decisions)
            published_cells += block_published
            suppressed_cells += block_suppressed
            continue

        if block.type == "bullet_list":
            items = []
            for item in [line.strip() for line in (block.text or "").splitlines() if line.strip()]:
                items.append(_apply_safe_legal_term_corrections(item))
            cleaned_blocks.append(ArticleBlock(type="bullet_list", text="\n".join(items)))
            continue

        text = _apply_safe_legal_term_corrections(block.text or "")
        cleaned_blocks.append(ArticleBlock(type="paragraph" if block.type == "raw" else block.type, text=_safe_paragraph_fallback(text)))

    return cleaned_blocks, decision_entries, published_cells, suppressed_cells


def _clean_table_block(
    block: ArticleBlock,
    *,
    secondary_text: str | None,
    source: Literal["rule_based", "llm"],
) -> tuple[ArticleBlock, list[dict], int, int]:
    if not block.rows:
        return ArticleBlock(type="table", rows=[["Belirsiz"]], cell_statuses=[["suppressed_uncertain"]], note="Bazı hücreler bastırıldı."), [], 0, 1

    rows = [[_normalize_inline_noise(str(cell)) for cell in row] for row in block.rows if row]
    header = [_canonicalize_table_header(cell) for cell in rows[0]]
    normalized_rows = [header]
    status_rows = [["accepted_from_llm" if source == "llm" else "accepted_from_rule_based" for _ in header]]
    decisions: list[dict] = []
    published_cells = len(header)
    suppressed_cells = 0

    for row_index, row in enumerate(rows[1:], start=1):
        normalized_row: list[str] = []
        status_row: list[str] = []
        for col_index, header_name in enumerate(header):
            raw_value = row[col_index] if col_index < len(row) else ""
            value, status, decision_reason = _normalize_table_cell(
                raw_value,
                header_name,
                secondary_text=secondary_text,
                source=source,
            )
            normalized_row.append(value)
            status_row.append(status)
            if status == "suppressed_uncertain":
                suppressed_cells += 1
            else:
                published_cells += 1
            decisions.append(
                {
                    "row_index": row_index,
                    "column_name": header_name,
                    "raw_value": raw_value,
                    "published_value": value,
                    "auto_corrected": status == "auto_corrected",
                    "suppressed": status == "suppressed_uncertain",
                    "decision_reason": decision_reason,
                }
            )
        normalized_rows.append(normalized_row)
        status_rows.append(status_row)

    note = "Bazı hücreler OCR/LLM belirsizliği nedeniyle bastırıldı." if suppressed_cells else None
    return ArticleBlock(type="table", rows=normalized_rows, cell_statuses=status_rows, note=note), decisions, published_cells, suppressed_cells


def _normalize_table_cell(
    raw_value: str,
    header_name: str,
    *,
    secondary_text: str | None,
    source: Literal["rule_based", "llm"],
) -> tuple[str, str, str]:
    text = _apply_safe_legal_term_corrections(_normalize_inline_noise(raw_value))
    if not text:
        return "—", "suppressed_uncertain", "empty_cell"

    default_status = "accepted_from_llm" if source == "llm" else "accepted_from_rule_based"

    if header_name == "Sayı No":
        digits = "".join(ch for ch in text if ch.isdigit())
        return (digits or "—", default_status if digits else "suppressed_uncertain", "sequence_number")

    if header_name == "Uyruğu":
        normalized = _normalize_lookup(text)
        if normalized in {"turkiye", "turk", "tc", "t c"} or SequenceMatcher(None, normalized, "turkiye").ratio() >= 0.75:
            return "TÜRKİYE", "auto_corrected" if text != "TÜRKİYE" else default_status, "nationality_normalized"
        return "Belirsiz", "suppressed_uncertain", "unsupported_nationality"

    if header_name == "Kimlik / Vergi No":
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 6:
            return digits, default_status, "identifier_verified"
        return "—", "suppressed_uncertain", "masked_or_unverifiable_identifier"

    if secondary_text and not _value_supported_by_secondary(text, secondary_text):
        if header_name == "Adres" and _looks_ocr_noise(text):
            return "Belirsiz adres", "suppressed_uncertain", "address_not_supported_by_secondary"
        if header_name == "Adı Soyadı / Unvanı" and _looks_ocr_noise(text):
            return "Belirsiz unvan", "suppressed_uncertain", "entity_not_supported_by_secondary"

    if _looks_ocr_noise(text) and header_name in {"Adres", "Adı Soyadı / Unvanı"}:
        fallback = "Belirsiz adres" if header_name == "Adres" else "Belirsiz unvan"
        return fallback, "suppressed_uncertain", "ocr_noise_suppressed"

    return text, default_status, "kept_clean_value"


def _accept_block(
    candidate_block: ArticleBlock,
    fallback_block: ArticleBlock | None,
    secondary_text: str | None,
) -> bool:
    if candidate_block.type == "table":
        return bool(candidate_block.rows)
    if candidate_block.type == "raw":
        return False

    candidate_text = _normalize_text(candidate_block.text or "")
    fallback_text = _normalize_text((fallback_block.text or "") if fallback_block else "")
    if not candidate_text:
        return False
    if fallback_text:
        similarity = SequenceMatcher(None, fallback_text, candidate_text).ratio()
        if similarity < BLOCK_SIMILARITY_RATIO:
            return False
        if _new_token_ratio(candidate_text, fallback_text) > MAX_NEW_TOKEN_RATIO:
            return False
    if secondary_text:
        secondary_ratio = SequenceMatcher(None, _normalize_text(secondary_text), candidate_text).ratio()
        if secondary_ratio < 0.10 and len(candidate_text.split()) > 6:
            return False
    return True


def _apply_safe_legal_term_corrections(text: str) -> str:
    corrected = text
    for expected, variants in LEGAL_TERM_EXPECTED.items():
        for variant in variants:
            corrected = re.sub(
                rf"(?<!\w){re.escape(variant)}(?!\w)",
                expected,
                corrected,
                flags=re.IGNORECASE,
            )
    return corrected


def _canonicalize_table_header(value: str) -> str:
    normalized = _normalize_lookup(value)
    if normalized in HEADER_EQUIVALENTS:
        return HEADER_EQUIVALENTS[normalized]
    best_match = value.strip() or "Belirsiz Kolon"
    best_ratio = 0.0
    for canonical in TABLE_CELL_HEADERS:
        ratio = SequenceMatcher(None, normalized, _normalize_lookup(canonical)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = canonical
    return best_match if best_ratio >= 0.62 else "Belirsiz Kolon"


def _normalize_lookup(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"[^a-z0-9\s/]", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def _value_supported_by_secondary(value: str, secondary_text: str) -> bool:
    candidate = _normalize_lookup(value)
    secondary = _normalize_lookup(secondary_text)
    if not candidate or not secondary:
        return False
    if candidate in secondary:
        return True
    return SequenceMatcher(None, candidate, secondary).ratio() >= 0.35


def _looks_ocr_noise(value: str) -> bool:
    normalized = _normalize_lookup(value)
    if not normalized:
        return True
    if re.search(r"\*{2,}", value):
        return True
    tokens = normalized.split()
    if len(tokens) >= 2 and any(token in {"anonim", "sirketi"} for token in tokens):
        return True
    weird_tokens = sum(1 for token in tokens if len(token) >= 6 and token not in {"turkiye", "adres", "anonim", "sirketi", "limited", "vergi", "kimlik"})
    return weird_tokens >= 2 and value.upper() == value


def _safe_paragraph_fallback(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return "Belirsiz içerik"
    if "|" in cleaned and cleaned.count("|") >= 4:
        return "Tablo içeriği ayrı yapılandırılmış olarak işlendi."
    return cleaned
