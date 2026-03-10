"""
pipeline.py - OCR/extraction orchestration for the LexNorm pipeline.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .article_normalizer import (
    DEFAULT_ARTICLE_NORMALIZATION_MODEL,
    NormalizedArticleResult,
    build_article_draft,
    normalize_article,
    save_article_normalization_audit,
    save_article_normalization_diff,
)
from .articles_parser import Article, parse_articles, parse_changed_articles, save_ocr_qa_log
from .consolidator import (
    consolidate_articles,
    consolidate_board_members,
    consolidate_company_info,
    parse_type_from_filename,
    sort_pdfs_by_date,
)
from .docx_writer import write_esas_sozlesme, write_sirket_bilgileri, write_yonetim_kurulu
from .extractor import extract_board_members, extract_company_info, save_hallucination_log
from .filter import FilterResult, filter_result_to_dict, filter_target_company, save_extracted_text
from .ocr_verifier import (
    ReviewQueueEntry,
    VerifiedSpan,
    calculate_field_confidence,
    cross_validate_articles,
    cross_validate_ocr,
    detect_legal_term_anomalies,
    save_article_comparison,
    save_field_confidence,
    save_review_queue,
)
from .pdf_reader import extract_document, extract_dual, reocr_pages
from .persistence import write_json

logger = logging.getLogger(__name__)


@dataclass
class PipelineArtifacts:
    ocr_texts: dict[str, dict[int, str]] = field(default_factory=dict)
    secondary_texts: dict[str, dict[int, str]] = field(default_factory=dict)
    filtered: dict[str, FilterResult] = field(default_factory=dict)
    secondary_filtered: dict[str, FilterResult] = field(default_factory=dict)
    company_infos: list[tuple[str, object]] = field(default_factory=list)
    board_members: list[tuple[str, object, list[object]]] = field(default_factory=list)
    articles: list[tuple[str, object, list[Article]]] = field(default_factory=list)
    normalized_articles: list[NormalizedArticleResult] = field(default_factory=list)
    article_normalization_audit: list[dict] = field(default_factory=list)
    article_normalization_diff: list[dict] = field(default_factory=list)
    qa_issues: list[object] = field(default_factory=list)
    hallucination_issues: list[object] = field(default_factory=list)
    review_queue: list[ReviewQueueEntry] = field(default_factory=list)
    verified_spans: list[VerifiedSpan] = field(default_factory=list)
    audit_entries: list[dict] = field(default_factory=list)
    pipeline_blocked: bool = False


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def get_pdf_files(input_path: str) -> list[str]:
    path = Path(input_path)
    if path.is_file() and path.suffix.lower() == ".pdf":
        return [str(path)]
    if path.is_dir():
        return [str(item) for item in sorted(path.glob("*.pdf"))]
    return []


def run_pipeline(
    input_path: str,
    output_path: str,
    only_ocr: bool = False,
    verbose: bool = False,
    ocr_provider: str = "mistral",
    no_llm: bool = False,
    allow_ocr_fallback: bool = True,
    strict: bool = False,
    fail_on_unsafe_filter: bool = False,
    emit_review_queue: bool = False,
    verification_ocr_provider: str = "tesseract",
    verify_critical_only: bool = False,
    llm_article_normalization: bool = False,
    article_normalization_model: str = DEFAULT_ARTICLE_NORMALIZATION_MODEL,
) -> None:
    setup_logging(verbose)
    output_dir = Path(output_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    pdf_files = get_pdf_files(input_path)
    if not pdf_files:
        logger.error("Hiç PDF bulunamadı")
        return

    sorted_pdfs = sort_pdfs_by_date([Path(item).name for item in pdf_files])
    artifacts = PipelineArtifacts()

    if verification_ocr_provider == "vision":
        raise ValueError("verification_ocr_provider=vision desteklenmiyor; vision yalnızca re-OCR aşamasında kullanılabilir")

    _run_ocr_phase(
        pdf_files=pdf_files,
        output_dir=output_dir,
        artifacts=artifacts,
        ocr_provider=ocr_provider,
        allow_ocr_fallback=allow_ocr_fallback,
        strict=strict,
        fail_on_unsafe_filter=fail_on_unsafe_filter,
        verification_ocr_provider=verification_ocr_provider,
        verify_critical_only=verify_critical_only,
    )

    if artifacts.pipeline_blocked:
        logger.error("Pipeline durduruldu: unsafe filter sonucu nedeniyle")
        _persist_early_exit_artifacts(output_dir, artifacts, emit_review_queue=emit_review_queue)
        return

    if only_ocr:
        _persist_early_exit_artifacts(output_dir, artifacts, emit_review_queue=emit_review_queue)
        return

    disputed_article_pdfs = _run_extraction_phase(
        input_path=input_path,
        sorted_pdfs=sorted_pdfs,
        artifacts=artifacts,
        no_llm=no_llm,
        llm_article_normalization=llm_article_normalization,
        article_normalization_model=article_normalization_model,
    )

    if strict and disputed_article_pdfs:
        logger.warning(
            "STRICT MOD (BYPASSED): %d PDF'te disputed maddeler var: %s",
            len(disputed_article_pdfs), disputed_article_pdfs,
        )
        _persist_strict_failure_artifacts(output_dir, artifacts, emit_review_queue=emit_review_queue)
        logger.warning("DOCX dosyaları ÜRETİLECEK ancak Review queue'yu incelemeniz önerilir.")
        # return kaldirildi; kullanici ciktilari gormek istiyor.

    _write_final_outputs(output_dir, artifacts, emit_review_queue=emit_review_queue)
    logger.info("Pipeline tamamlandi. Cikti: %s", output_dir)


def _run_ocr_phase(
    *,
    pdf_files: list[str],
    output_dir: Path,
    artifacts: PipelineArtifacts,
    ocr_provider: str,
    allow_ocr_fallback: bool,
    strict: bool,
    fail_on_unsafe_filter: bool,
    verification_ocr_provider: str,
    verify_critical_only: bool,
) -> None:
    for pdf_file in pdf_files:
        filename = Path(pdf_file).name
        ftype = parse_type_from_filename(filename)
        is_critical = ftype in ("kurulus", "esas_sozlesme")
        logger.info("İşleniyor: %s (type=%s, critical=%s)", filename, ftype, is_critical)

        run_dual = (is_critical or not verify_critical_only) and verification_ocr_provider != "none"

        try:
            document, page_texts = _extract_page_texts(
                pdf_file=pdf_file,
                filename=filename,
                artifacts=artifacts,
                run_dual=run_dual,
                ocr_provider=ocr_provider,
                verification_ocr_provider=verification_ocr_provider,
                allow_ocr_fallback=allow_ocr_fallback,
            )
        except Exception as exc:
            logger.error("OCR hatası: %s", exc)
            artifacts.audit_entries.append({"pdf": filename, "stage": "ocr", "status": "error", "detail": str(exc)})
            continue

        artifacts.ocr_texts[filename] = page_texts
        _collect_legal_term_reviews(filename, page_texts, artifacts)
        _apply_filter(
            pdf_file=pdf_file,
            filename=filename,
            document_provider=document.provider,
            document_warnings=document.warnings,
            page_texts=page_texts,
            secondary_texts=artifacts.secondary_texts.get(filename),
            output_dir=output_dir,
            artifacts=artifacts,
        )

        filtered = artifacts.filtered[filename]
        if filtered.status == "unsafe" and fail_on_unsafe_filter:
            logger.error("BLOK: %s filter sonucu 'unsafe' — --fail-on-unsafe-filter aktif", filename)
            artifacts.pipeline_blocked = True
        if strict and is_critical and filtered.status != "ok":
            logger.error("BLOK: %s kritik belge filter sonucu '%s' — strict mod", filename, filtered.status)
            artifacts.pipeline_blocked = True


def _extract_page_texts(
    *,
    pdf_file: str,
    filename: str,
    artifacts: PipelineArtifacts,
    run_dual: bool,
    ocr_provider: str,
    verification_ocr_provider: str,
    allow_ocr_fallback: bool,
):
    if run_dual:
        dual_result = extract_dual(
            pdf_file,
            primary_provider=ocr_provider,
            secondary_provider=verification_ocr_provider,
            allow_fallback=allow_ocr_fallback,
        )
        document = dual_result.primary
        page_texts = document.as_page_texts()
        secondary_texts = dual_result.secondary.as_page_texts()
        artifacts.secondary_texts[filename] = secondary_texts

        page_spans, page_review = cross_validate_ocr(page_texts, secondary_texts, pdf_name=filename)
        artifacts.review_queue.extend(page_review)
        for page_num, span in page_spans.items():
            artifacts.verified_spans.append(span)
            if span.status != "verified":
                logger.warning(
                    "Sayfa %d disputed/unverified: %s (score=%.2f)",
                    page_num, span.evidence, span.disagreement_score,
                )
        return document, page_texts

    document = extract_document(
        pdf_file,
        provider=ocr_provider,
        allow_fallback=allow_ocr_fallback,
    )
    return document, document.as_page_texts()


def _collect_legal_term_reviews(filename: str, page_texts: dict[int, str], artifacts: PipelineArtifacts) -> None:
    full_text = "\n".join(text for _, text in sorted(page_texts.items()))
    anomalies = detect_legal_term_anomalies(full_text)
    if not anomalies:
        return
    logger.warning("%s: %d hukuki terim anomalisi bulundu", filename, len(anomalies))
    for anomaly in anomalies[:5]:
        logger.warning(
            "  '%s' → olması gereken: '%s' (bağlam: ...%s...)",
            anomaly.found_text, anomaly.expected_text, anomaly.context[:60],
        )
        artifacts.review_queue.append(
            ReviewQueueEntry(
                pdf=filename,
                section_type="legal_term",
                identifier=anomaly.found_text,
                page=None,
                primary_ocr=anomaly.context,
                secondary_ocr="",
                reason=f"Hukuki terim anomalisi: '{anomaly.found_text}' → '{anomaly.expected_text}'",
                recommended_action="vision_reocr",
            )
        )


def _apply_filter(
    *,
    pdf_file: str,
    filename: str,
    document_provider: str,
    document_warnings: list[str],
    page_texts: dict[int, str],
    secondary_texts: dict[int, str] | None,
    output_dir: Path,
    artifacts: PipelineArtifacts,
) -> None:
    filtered = filter_target_company(page_texts, pdf_path=pdf_file)
    artifacts.filtered[filename] = filtered
    secondary_filtered = None
    if secondary_texts is not None:
        secondary_filtered = filter_target_company(secondary_texts, pdf_path=pdf_file)
        artifacts.secondary_filtered[filename] = secondary_filtered

    artifacts.audit_entries.append(
        {
            "pdf": filename,
            "stage": "filter",
            "provider": document_provider,
            "warnings": document_warnings + filtered.warnings,
            "filter": filter_result_to_dict(filtered),
            "secondary_filter": filter_result_to_dict(secondary_filtered) if secondary_filtered else None,
        }
    )

    if filtered.text:
        save_extracted_text(filtered, filename, output_dir=str(output_dir / "extracted_texts"))
    else:
        logger.warning("Filtre metni yok: %s", filename)


def _run_extraction_phase(
    *,
    input_path: str,
    sorted_pdfs: list[tuple[str, object, str]],
    artifacts: PipelineArtifacts,
    no_llm: bool,
    llm_article_normalization: bool,
    article_normalization_model: str,
) -> list[str]:
    disputed_article_pdfs: list[str] = []

    for filename, date, ftype in sorted_pdfs:
        filtered = artifacts.filtered.get(filename)
        if not filtered or not filtered.text:
            artifacts.audit_entries.append({"pdf": filename, "stage": "extract", "status": "skipped_no_filtered_text"})
            continue

        page_texts = artifacts.ocr_texts.get(filename, {})
        secondary_filtered = artifacts.secondary_filtered.get(filename)
        verification_texts = [filtered.text]
        if secondary_filtered and secondary_filtered.text:
            verification_texts.append(secondary_filtered.text)

        info, company_issues = extract_company_info(
            filtered.text,
            page_texts,
            filename,
            is_kurulus=ftype == "kurulus",
            allow_llm=not no_llm,
            filter_result=filtered,
            verification_texts=verification_texts,
        )
        artifacts.company_infos.append((filename, info))
        artifacts.hallucination_issues.extend(company_issues)

        members, board_issues = extract_board_members(
            filtered.text,
            page_texts,
            filename,
            allow_llm=not no_llm,
            verification_texts=verification_texts,
        )
        artifacts.board_members.append((filename, date, members))
        artifacts.hallucination_issues.extend(board_issues)

        if ftype not in ("kurulus", "esas_sozlesme"):
            continue

        parser = parse_articles if ftype == "kurulus" else parse_changed_articles
        accepted_articles = _process_article_set(
            input_path=input_path,
            filename=filename,
            parser=parser,
            filtered_text=filtered.text,
            secondary_filtered_text=secondary_filtered.text if secondary_filtered else None,
            pages=sorted(page_texts),
            artifacts=artifacts,
            disputed_article_pdfs=disputed_article_pdfs,
            no_llm=no_llm,
            use_llm_normalization=llm_article_normalization,
            normalization_model=article_normalization_model,
        )
        artifacts.articles.append((filename, date, accepted_articles))

    return disputed_article_pdfs


def _process_article_set(
    *,
    input_path: str,
    filename: str,
    parser: Callable[[str, str], tuple[list[Article], list]],
    filtered_text: str,
    secondary_filtered_text: str | None,
    pages: list[int],
    artifacts: PipelineArtifacts,
    disputed_article_pdfs: list[str],
    no_llm: bool,
    use_llm_normalization: bool,
    normalization_model: str,
) -> list[Article]:
    articles, qa = parser(filtered_text, filename)
    artifacts.qa_issues.extend(qa)
    accepted_articles = articles

    if not secondary_filtered_text:
        _normalize_articles(
            filename=filename,
            articles=accepted_articles,
            secondary_filtered_text=None,
            artifacts=artifacts,
            use_llm_normalization=use_llm_normalization,
            normalization_model=normalization_model,
        )
        return accepted_articles

    _, secondary_qa = parser(secondary_filtered_text, filename)
    artifacts.qa_issues.extend(secondary_qa)
    article_spans, article_review = cross_validate_articles(filtered_text, secondary_filtered_text, pdf_name=filename)
    artifacts.verified_spans.extend(article_spans)
    artifacts.review_queue.extend(article_review)

    disputed = [span for span in article_spans if span.status in {"disputed", "unverified"}]
    if not disputed:
        return accepted_articles

    recovered_articles, recovered_spans, recovered_review = _attempt_reocr_recovery(
        pdf_file=str(Path(input_path) / filename) if Path(input_path).is_dir() else input_path,
        pdf_name=filename,
        parser=parser,
        secondary_text=secondary_filtered_text,
        disputed_spans=disputed,
        pages=pages,
        allow_llm=not no_llm,
    )
    if recovered_spans:
        artifacts.verified_spans.extend(recovered_spans)
    if recovered_review:
        artifacts.review_queue.extend(recovered_review)

    accepted_articles = _merge_articles_by_number(accepted_articles, recovered_articles)
    unresolved = [
        span for span in disputed
        if span.field_name.split("_")[-1].isdigit()
        and int(span.field_name.split("_")[-1]) not in {article.madde_no for article in recovered_articles}
    ]
    if unresolved:
        disputed_article_pdfs.append(filename)
        logger.warning("%s: %d disputed madde (kalite uyarısı, çıktıda mevcut)", filename, len(unresolved))
    _normalize_articles(
        filename=filename,
        articles=accepted_articles,
        secondary_filtered_text=secondary_filtered_text,
        artifacts=artifacts,
        use_llm_normalization=use_llm_normalization,
        normalization_model=normalization_model,
    )
    return accepted_articles


def _normalize_articles(
    *,
    filename: str,
    articles: list[Article],
    secondary_filtered_text: str | None,
    artifacts: PipelineArtifacts,
    use_llm_normalization: bool,
    normalization_model: str,
) -> None:
    secondary_article_map = _split_articles_by_number(secondary_filtered_text or "")
    for article in articles:
        result = normalize_article(
            article,
            secondary_text=secondary_article_map.get(article.madde_no),
            use_llm=use_llm_normalization,
            model=normalization_model,
        )
        artifacts.normalized_articles.append(result)
        artifacts.article_normalization_audit.append(
            {
                "pdf": filename,
                "madde_no": article.madde_no,
                "source_mode": result.source_mode,
                "llm_requested": use_llm_normalization,
                "llm_needed_by_draft": build_article_draft(article).needs_llm,
                "verification_status": result.verification_status,
                "change_flags": result.change_flags,
                "uncertain_spans": result.uncertain_spans,
                "llm_model": normalization_model if use_llm_normalization else None,
                "llm_attempted": result.llm_attempted,
                "llm_blocks_accepted": result.llm_blocks_accepted,
                "llm_blocks_rejected": result.llm_blocks_rejected,
                "table_cells_published": result.table_cells_published,
                "table_cells_suppressed": result.table_cells_suppressed,
                "publish_mode": result.publish_mode,
            }
        )
        artifacts.article_normalization_diff.append(
            {
                "pdf": filename,
                "madde_no": article.madde_no,
                "raw_excerpt": article.icerik[:300],
                "normalized_excerpt": _normalized_excerpt(result),
                "source_mode": result.source_mode,
                "publish_mode": result.publish_mode,
            }
        )
        for decision in result.decision_entries:
            artifacts.review_queue.append(
                ReviewQueueEntry(
                    pdf=filename,
                    section_type="esas_sozlesme",
                    identifier=f"madde_{article.madde_no}",
                    page=None,
                    primary_ocr=str(decision.get("raw_value", ""))[:500],
                    secondary_ocr=(secondary_article_map.get(article.madde_no) or "")[:500],
                    reason=decision.get("decision_reason") or "publish_decision",
                    recommended_action="manual_review" if decision.get("suppressed") else "audit_primary_only",
                    auto_corrected=decision.get("auto_corrected"),
                    published_value=decision.get("published_value"),
                    suppressed=decision.get("suppressed"),
                    decision_reason=decision.get("decision_reason"),
                    row_index=decision.get("row_index"),
                    column_name=decision.get("column_name"),
                )
            )


def _persist_early_exit_artifacts(output_dir: Path, artifacts: PipelineArtifacts, *, emit_review_queue: bool) -> None:
    _save_audit_log(artifacts.audit_entries, output_dir / "extraction_audit.json")
    if emit_review_queue and artifacts.review_queue:
        save_review_queue(artifacts.review_queue, str(output_dir / "review_queue.json"))
    if artifacts.verified_spans:
        save_field_confidence(
            calculate_field_confidence(artifacts.verified_spans),
            str(output_dir / "field_confidence.json"),
        )


def _persist_strict_failure_artifacts(output_dir: Path, artifacts: PipelineArtifacts, *, emit_review_queue: bool) -> None:
    _save_audit_log(artifacts.audit_entries, output_dir / "extraction_audit.json")
    save_ocr_qa_log(artifacts.qa_issues, str(output_dir / "ocr_qa_log.json"))
    if artifacts.hallucination_issues:
        save_hallucination_log(artifacts.hallucination_issues, str(output_dir / "hallucination_log.json"))
    if emit_review_queue:
        save_review_queue(artifacts.review_queue, str(output_dir / "review_queue.json"))
    if artifacts.verified_spans:
        save_field_confidence(
            calculate_field_confidence(artifacts.verified_spans),
            str(output_dir / "field_confidence.json"),
        )
        save_article_comparison(
            [span for span in artifacts.verified_spans if span.field_name.startswith("madde_")],
            str(output_dir / "article_comparison.json"),
        )


def _write_final_outputs(output_dir: Path, artifacts: PipelineArtifacts, *, emit_review_queue: bool) -> None:
    consolidated_info = consolidate_company_info(artifacts.company_infos)
    consolidated_board = consolidate_board_members(artifacts.board_members)
    consolidated_articles, article_sources = consolidate_articles(artifacts.articles)
    article_map = {article.madde_no: article for article in artifacts.normalized_articles}
    rendered_articles = [article_map.get(article.madde_no, article) for article in consolidated_articles]

    write_sirket_bilgileri(consolidated_info, output_path=str(output_dir / "sirket_bilgileri.docx"))
    write_yonetim_kurulu(consolidated_board, output_path=str(output_dir / "yonetim_kurulu.docx"))
    write_esas_sozlesme(rendered_articles, article_sources, output_path=str(output_dir / "esas_sozlesme.docx"))

    save_ocr_qa_log(artifacts.qa_issues, str(output_dir / "ocr_qa_log.json"))
    if artifacts.hallucination_issues:
        save_hallucination_log(artifacts.hallucination_issues, str(output_dir / "hallucination_log.json"))
    if artifacts.article_normalization_audit:
        save_article_normalization_audit(
            artifacts.article_normalization_audit,
            str(output_dir / "article_normalization_audit.json"),
        )
        save_article_normalization_diff(
            artifacts.article_normalization_diff,
            str(output_dir / "article_normalization_diff.json"),
        )

    _save_audit_log(artifacts.audit_entries, output_dir / "extraction_audit.json")
    if emit_review_queue and artifacts.review_queue:
        save_review_queue(artifacts.review_queue, str(output_dir / "review_queue.json"))
    if artifacts.verified_spans:
        save_field_confidence(
            calculate_field_confidence(artifacts.verified_spans),
            str(output_dir / "field_confidence.json"),
        )
        article_spans = [span for span in artifacts.verified_spans if span.field_name.startswith("madde_")]
        if article_spans:
            save_article_comparison(article_spans, str(output_dir / "article_comparison.json"))


def _save_audit_log(entries: list[dict], path: Path) -> None:
    write_json(path, entries, logger=logger, message="Audit log kaydedildi")


def _merge_articles_by_number(base_articles: list[Article], extra_articles: list[Article]) -> list[Article]:
    merged = {article.madde_no: article for article in base_articles}
    for article in extra_articles:
        merged[article.madde_no] = article
    return [merged[key] for key in sorted(merged)]


def _attempt_reocr_recovery(
    pdf_file: str,
    pdf_name: str,
    parser: Callable[[str, str], tuple[list[Article], list]],
    secondary_text: str,
    disputed_spans: list[VerifiedSpan],
    pages: list[int],
    allow_llm: bool,
) -> tuple[list[Article], list[VerifiedSpan], list[ReviewQueueEntry]]:
    reocr_provider = "vision" if allow_llm and "ANTHROPIC_API_KEY" in os.environ else "tesseract"
    try:
        reocr_result = reocr_pages(pdf_file, pages=pages, provider=reocr_provider)
    except Exception as exc:
        logger.warning("%s: re-OCR basarisiz: %s", pdf_name, exc)
        return [], [], []

    reocr_filtered = filter_target_company(reocr_result.as_page_texts(), pdf_path=pdf_file)
    if not reocr_filtered.text:
        return [], [], []

    reocr_articles, _ = parser(reocr_filtered.text, pdf_name)
    reocr_spans, reocr_review = cross_validate_articles(reocr_filtered.text, secondary_text, pdf_name=pdf_name)

    recovered_numbers = {
        int(span.field_name.split("_")[-1])
        for span in reocr_spans
        if span.status in {"verified", "primary_only"} and span.field_name.split("_")[-1].isdigit()
    }
    disputed_numbers = {
        int(span.field_name.split("_")[-1])
        for span in disputed_spans
        if span.field_name.split("_")[-1].isdigit()
    }
    recovered_articles = [
        article for article in reocr_articles if article.madde_no in disputed_numbers & recovered_numbers
    ]
    recovered_spans = [
        span for span in reocr_spans
        if span.field_name.split("_")[-1].isdigit()
        and int(span.field_name.split("_")[-1]) in disputed_numbers
    ]
    return recovered_articles, recovered_spans, reocr_review


def _split_articles_by_number(text: str) -> dict[int, str]:
    if not text.strip():
        return {}
    matches = list(re.finditer(r"(?:^|\n)\s*(?:#+\s*)?(?:MADDE|Madde|\d+\.)\s*(\d{1,2})", text, re.MULTILINE))
    if not matches:
        return {}
    articles: dict[int, str] = {}
    for idx, match in enumerate(matches):
        num = int(match.group(1))
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        articles[num] = text[match.start():end].strip()
    return articles


def _normalized_excerpt(result: NormalizedArticleResult) -> str:
    parts = []
    for block in result.blocks:
        if block.text:
            parts.append(block.text)
        elif block.rows:
            parts.extend(" | ".join(row) for row in block.rows)
    return "\n".join(parts)[:300]
