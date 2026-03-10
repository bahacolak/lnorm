"""
article_normalizer.py - Rule-based and LLM-assisted normalization for articles.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Literal

from .articles_parser import Article
from .ocr_verifier import detect_legal_term_anomalies
from .persistence import write_json

logger = logging.getLogger(__name__)

DEFAULT_ARTICLE_NORMALIZATION_MODEL = "claude-sonnet-4-20250514"
MAX_NEW_TOKEN_RATIO = 0.08
MIN_SIMILARITY_RATIO = 0.82
MAX_UNCERTAIN_SPANS = 3


@dataclass
class ArticleBlock:
    type: Literal["paragraph", "bullet_list", "table", "raw"]
    text: str | None = None
    rows: list[list[str]] | None = None


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
        if _looks_like_noise(normalized_line):
            issues.append("noise_line")
            continue
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
    return NormalizedArticleResult(
        madde_no=draft.madde_no,
        title=draft.title,
        blocks=draft.blocks,
        source_mode="rule_based",
        verification_status="accepted",
        change_flags=draft.issues.copy(),
    )


def normalize_article_with_llm(
    draft: StructuredArticleDraft,
    secondary_text: str | None = None,
    model: str = DEFAULT_ARTICLE_NORMALIZATION_MODEL,
) -> NormalizedArticleResult:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _fallback_rule_based(draft, "missing_anthropic_key")

    payload = _call_anthropic_normalizer(draft, model=model)
    if payload is None:
        return _fallback_rule_based(draft, "llm_error")

    candidate = _candidate_from_payload(payload, draft)
    decision = _verify_normalized_article(candidate, draft.raw_text, secondary_text)
    if decision is not None:
        return decision
    candidate.change_flags = sorted(set(draft.issues + candidate.change_flags))
    return candidate


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
            "table için rows kullan. Emin değilsen raw veya paragraph dön.\n\n"
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
    )


def _verify_normalized_article(
    candidate: NormalizedArticleResult,
    raw_text: str,
    secondary_text: str | None,
) -> NormalizedArticleResult | None:
    normalized_text = _flatten_blocks(candidate.blocks)
    raw_normalized = _normalize_text(raw_text)
    candidate_normalized = _normalize_text(normalized_text)
    similarity = SequenceMatcher(None, raw_normalized, candidate_normalized).ratio()
    if similarity < MIN_SIMILARITY_RATIO:
        return NormalizedArticleResult(
            madde_no=candidate.madde_no,
            title=candidate.title,
            blocks=[ArticleBlock(type="raw", text=raw_text.strip())],
            source_mode="raw",
            verification_status="fallback_raw",
            change_flags=candidate.change_flags + ["similarity_low"],
            uncertain_spans=candidate.uncertain_spans,
        )

    if _new_token_ratio(candidate_normalized, raw_normalized) > MAX_NEW_TOKEN_RATIO:
        return _fallback_rule_based_from_result(candidate, raw_text, "new_token_ratio")

    if len(candidate.uncertain_spans) > MAX_UNCERTAIN_SPANS:
        return _fallback_rule_based_from_result(candidate, raw_text, "too_many_uncertain_spans")

    if secondary_text:
        secondary_ratio = SequenceMatcher(None, _normalize_text(secondary_text), candidate_normalized).ratio()
        if secondary_ratio < 0.65:
            return _fallback_rule_based_from_result(candidate, raw_text, "secondary_mismatch")

    if detect_legal_term_anomalies(normalized_text):
        candidate.change_flags.append("legal_term_anomaly_remaining")
    return None


def _fallback_rule_based(draft: StructuredArticleDraft, reason: str) -> NormalizedArticleResult:
    return NormalizedArticleResult(
        madde_no=draft.madde_no,
        title=draft.title,
        blocks=draft.blocks,
        source_mode="rule_based",
        verification_status="fallback_rule_based" if reason != "missing_anthropic_key" else "accepted",
        change_flags=draft.issues + [reason],
    )


def _fallback_rule_based_from_result(candidate: NormalizedArticleResult, raw_text: str, reason: str) -> NormalizedArticleResult:
    draft_blocks = [ArticleBlock(type="raw", text=raw_text.strip())] if not candidate.blocks else [
        block for block in candidate.blocks if block.type != "table"
    ] or [ArticleBlock(type="raw", text=raw_text.strip())]
    return NormalizedArticleResult(
        madde_no=candidate.madde_no,
        title=candidate.title,
        blocks=draft_blocks,
        source_mode="rule_based" if draft_blocks[0].type != "raw" else "raw",
        verification_status="fallback_rule_based" if draft_blocks[0].type != "raw" else "fallback_raw",
        change_flags=candidate.change_flags + [reason],
        uncertain_spans=candidate.uncertain_spans,
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
