"""
main.py - CLI orchestrator for LexNorm pipeline with verification-gated OCR.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))

from articles_parser import Article, parse_articles, parse_changed_articles, save_ocr_qa_log
from consolidator import (
    consolidate_articles,
    consolidate_board_members,
    consolidate_company_info,
    parse_type_from_filename,
    sort_pdfs_by_date,
)
from docx_writer import write_esas_sozlesme, write_sirket_bilgileri, write_yonetim_kurulu
from extractor import extract_board_members, extract_company_info, save_hallucination_log
from filter import FilterResult, filter_result_to_dict, filter_target_company, save_extracted_text
from ocr_verifier import (
    ReviewQueueEntry,
    VerifiedSpan,
    cross_validate_articles,
    cross_validate_ocr,
    detect_legal_term_anomalies,
    save_article_comparison,
    save_field_confidence,
    save_review_queue,
    calculate_field_confidence,
)
from pdf_reader import extract_document, extract_dual, reocr_pages

logger = logging.getLogger(__name__)


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
) -> None:
    setup_logging(verbose)
    output_dir = Path(output_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    pdf_files = get_pdf_files(input_path)
    if not pdf_files:
        logger.error("Hiç PDF bulunamadı")
        return

    filenames = [Path(item).name for item in pdf_files]
    sorted_pdfs = sort_pdfs_by_date(filenames)

    all_ocr_texts: dict[str, dict[int, str]] = {}
    all_secondary_texts: dict[str, dict[int, str]] = {}
    all_filtered: dict[str, FilterResult] = {}
    all_secondary_filtered: dict[str, FilterResult] = {}
    all_company_infos = []
    all_board_members = []
    all_articles = []
    all_qa_issues = []
    all_hallucination_issues = []
    all_review_queue: list[ReviewQueueEntry] = []
    all_verified_spans: list[VerifiedSpan] = []
    audit_entries = []
    pipeline_blocked = False

    if verification_ocr_provider == "vision":
        raise ValueError("verification_ocr_provider=vision desteklenmiyor; vision yalnızca re-OCR aşamasında kullanılabilir")

    # ── Phase 1: OCR + Cross-Validation ──────────────────────────
    for pdf_file in pdf_files:
        filename = Path(pdf_file).name
        ftype = parse_type_from_filename(filename)
        is_critical = ftype in ("kurulus", "esas_sozlesme")
        logger.info("İşleniyor: %s (type=%s, critical=%s)", filename, ftype, is_critical)

        # Decide whether to run dual OCR
        run_dual = (is_critical or not verify_critical_only) and verification_ocr_provider != "none"

        try:
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
                all_secondary_texts[filename] = secondary_texts

                # Cross-validate pages
                page_spans, page_review = cross_validate_ocr(
                    page_texts, secondary_texts, pdf_name=filename,
                )
                all_review_queue.extend(page_review)

                # Log cross-validation results
                for page_num, span in page_spans.items():
                    all_verified_spans.append(span)
                    if span.status != "verified":
                        logger.warning(
                            "Sayfa %d disputed/unverified: %s (score=%.2f)",
                            page_num, span.evidence, span.disagreement_score,
                        )
            else:
                document = extract_document(
                    pdf_file,
                    provider=ocr_provider,
                    allow_fallback=allow_ocr_fallback,
                )
                page_texts = document.as_page_texts()

        except Exception as exc:
            logger.error("OCR hatası: %s", exc)
            audit_entries.append({"pdf": filename, "stage": "ocr", "status": "error", "detail": str(exc)})
            continue

        all_ocr_texts[filename] = page_texts

        # ── Legal term anomaly check ──
        full_text = "\n".join(t for _, t in sorted(page_texts.items()))
        anomalies = detect_legal_term_anomalies(full_text)
        if anomalies:
            logger.warning(
                "%s: %d hukuki terim anomalisi bulundu",
                filename, len(anomalies),
            )
            for anomaly in anomalies[:5]:
                logger.warning(
                    "  '%s' → olması gereken: '%s' (bağlam: ...%s...)",
                    anomaly.found_text, anomaly.expected_text, anomaly.context[:60],
                )
                all_review_queue.append(
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

        # ── Filter ──
        filtered = filter_target_company(page_texts, pdf_path=pdf_file)
        all_filtered[filename] = filtered
        secondary_filtered = None
        if filename in all_secondary_texts:
            secondary_filtered = filter_target_company(all_secondary_texts[filename], pdf_path=pdf_file)
            all_secondary_filtered[filename] = secondary_filtered
        audit_entries.append(
            {
                "pdf": filename,
                "stage": "filter",
                "provider": document.provider,
                "warnings": document.warnings + filtered.warnings,
                "filter": filter_result_to_dict(filtered),
                "secondary_filter": filter_result_to_dict(secondary_filtered) if secondary_filtered else None,
            }
        )

        if filtered.status == "unsafe" and fail_on_unsafe_filter:
            logger.error("BLOK: %s filter sonucu 'unsafe' — --fail-on-unsafe-filter aktif", filename)
            pipeline_blocked = True
        if strict and is_critical and filtered.status != "ok":
            logger.error("BLOK: %s kritik belge filter sonucu '%s' — strict mod", filename, filtered.status)
            pipeline_blocked = True

        if filtered.text:
            save_extracted_text(filtered, filename, output_dir=str(output_dir / "extracted_texts"))
        else:
            logger.warning("Filtre metni yok: %s", filename)

    # ── Check pipeline gate ──
    if pipeline_blocked:
        logger.error("Pipeline durduruldu: unsafe filter sonucu nedeniyle")
        _save_audit_log(audit_entries, output_dir / "extraction_audit.json")
        if emit_review_queue and all_review_queue:
            save_review_queue(all_review_queue, str(output_dir / "review_queue.json"))
        return

    if only_ocr:
        _save_audit_log(audit_entries, output_dir / "extraction_audit.json")
        if emit_review_queue and all_review_queue:
            save_review_queue(all_review_queue, str(output_dir / "review_queue.json"))
        if all_verified_spans:
            save_field_confidence(
                calculate_field_confidence(all_verified_spans),
                str(output_dir / "field_confidence.json"),
            )
        return

    # ── Phase 2: Extraction ──────────────────────────────────────
    disputed_article_pdfs: list[str] = []

    for filename, date, ftype in sorted_pdfs:
        filtered = all_filtered.get(filename)
        if not filtered or not filtered.text:
            audit_entries.append({"pdf": filename, "stage": "extract", "status": "skipped_no_filtered_text"})
            continue

        page_texts = all_ocr_texts.get(filename, {})
        is_kurulus = ftype == "kurulus"

        verification_texts = [filtered.text]
        secondary_filtered = all_secondary_filtered.get(filename)
        if secondary_filtered and secondary_filtered.text:
            verification_texts.append(secondary_filtered.text)

        info, company_issues = extract_company_info(
            filtered.text,
            page_texts,
            filename,
            is_kurulus=is_kurulus,
            allow_llm=not no_llm,
            filter_result=filtered,
            verification_texts=verification_texts,
        )
        all_company_infos.append((filename, info))
        all_hallucination_issues.extend(company_issues)

        members, board_issues = extract_board_members(
            filtered.text,
            page_texts,
            filename,
            allow_llm=not no_llm,
            verification_texts=verification_texts,
        )
        all_board_members.append((filename, date, members))
        all_hallucination_issues.extend(board_issues)

        if ftype in ("kurulus", "esas_sozlesme"):
            parser = parse_articles if is_kurulus else parse_changed_articles
            articles, qa = parser(filtered.text, filename)
            all_qa_issues.extend(qa)

            accepted_articles = articles
            if secondary_filtered and secondary_filtered.text:
                secondary_articles, secondary_qa = parser(secondary_filtered.text, filename)
                all_qa_issues.extend(secondary_qa)
                article_spans, article_review = cross_validate_articles(
                    filtered.text, secondary_filtered.text, pdf_name=filename,
                )
                all_verified_spans.extend(article_spans)
                all_review_queue.extend(article_review)

                # Try re-OCR for disputed articles (for quality improvement)
                disputed = [s for s in article_spans if s.status in {"disputed", "unverified"}]
                if disputed:
                    recovered_articles, recovered_spans, recovered_review = _attempt_reocr_recovery(
                        pdf_file=str(Path(input_path) / filename) if Path(input_path).is_dir() else input_path,
                        pdf_name=filename,
                        parser=parser,
                        secondary_text=secondary_filtered.text,
                        disputed_spans=disputed,
                        pages=sorted(page_texts),
                    )
                    if recovered_spans:
                        all_verified_spans.extend(recovered_spans)
                    if recovered_review:
                        all_review_queue.extend(recovered_review)
                    # Merge recovered articles over primary (better quality)
                    accepted_articles = _merge_articles_by_number(accepted_articles, recovered_articles)

                    unresolved = [
                        span for span in disputed
                        if span.field_name.split("_")[-1].isdigit()
                        and int(span.field_name.split("_")[-1]) not in {a.madde_no for a in recovered_articles}
                    ]
                    if unresolved:
                        disputed_article_pdfs.append(filename)
                        logger.warning("%s: %d disputed madde (kalite uyarısı, çıktıda mevcut)", filename, len(unresolved))
            all_articles.append((filename, date, accepted_articles))

    # ── Strict mode check ──
    if strict and disputed_article_pdfs:
        logger.error(
            "STRICT MOD: %d PDF'te disputed maddeler var, DOCX üretimi bloklanıyor: %s",
            len(disputed_article_pdfs), disputed_article_pdfs,
        )
        _save_audit_log(audit_entries, output_dir / "extraction_audit.json")
        save_ocr_qa_log(all_qa_issues, str(output_dir / "ocr_qa_log.json"))
        if all_hallucination_issues:
            save_hallucination_log(all_hallucination_issues, str(output_dir / "hallucination_log.json"))
        if emit_review_queue:
            save_review_queue(all_review_queue, str(output_dir / "review_queue.json"))
        if all_verified_spans:
            save_field_confidence(
                calculate_field_confidence(all_verified_spans),
                str(output_dir / "field_confidence.json"),
            )
            save_article_comparison(
                [s for s in all_verified_spans if s.field_name.startswith("madde_")],
                str(output_dir / "article_comparison.json"),
            )
        logger.error("DOCX dosyaları ÜRETİLMEDİ. Review queue'yu inceleyin.")
        return

    # ── Phase 3: Consolidate & Write ─────────────────────────────
    consolidated_info = consolidate_company_info(all_company_infos)
    consolidated_board = consolidate_board_members(all_board_members)
    consolidated_articles, article_sources = consolidate_articles(all_articles)

    write_sirket_bilgileri(consolidated_info, output_path=str(output_dir / "sirket_bilgileri.docx"))
    write_yonetim_kurulu(consolidated_board, output_path=str(output_dir / "yonetim_kurulu.docx"))
    write_esas_sozlesme(consolidated_articles, article_sources, output_path=str(output_dir / "esas_sozlesme.docx"))

    save_ocr_qa_log(all_qa_issues, str(output_dir / "ocr_qa_log.json"))
    if all_hallucination_issues:
        save_hallucination_log(all_hallucination_issues, str(output_dir / "hallucination_log.json"))

    # ── Save verification artifacts ──
    _save_audit_log(audit_entries, output_dir / "extraction_audit.json")
    if emit_review_queue and all_review_queue:
        save_review_queue(all_review_queue, str(output_dir / "review_queue.json"))
    if all_verified_spans:
        save_field_confidence(
            calculate_field_confidence(all_verified_spans),
            str(output_dir / "field_confidence.json"),
        )
        article_spans = [s for s in all_verified_spans if s.field_name.startswith("madde_")]
        if article_spans:
            save_article_comparison(article_spans, str(output_dir / "article_comparison.json"))

    logger.info("Pipeline tamamlandi. Cikti: %s", output_dir)


def _save_audit_log(entries: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _select_verified_articles(articles: list[Article], spans: list[VerifiedSpan]) -> list[Article]:
    if not spans:
        return articles
    allowed = {
        int(span.field_name.split("_")[-1])
        for span in spans
        if span.status in {"verified", "primary_only"} and span.field_name.split("_")[-1].isdigit()
    }
    return [article for article in articles if article.madde_no in allowed]


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
) -> tuple[list[Article], list[VerifiedSpan], list[ReviewQueueEntry]]:
    reocr_provider = "vision" if "ANTHROPIC_API_KEY" in os.environ else "tesseract"
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


def main() -> None:
    parser = argparse.ArgumentParser(description="LexNorm TTSG pipeline")
    parser.add_argument("--input", "-i", required=True, help="Input PDF veya klasor")
    parser.add_argument("--output", "-o", default="output", help="Cikti klasoru")
    parser.add_argument("--only-ocr", action="store_true", help="Yalnizca OCR ve filtreleme")
    parser.add_argument("--verbose", "-v", action="store_true", help="Detayli log")
    parser.add_argument(
        "--ocr-provider",
        default="mistral",
        choices=["mistral", "tesseract", "vision"],
        help="Primary OCR provider",
    )
    parser.add_argument("--no-llm", action="store_true", help="LLM fallback kapali")
    parser.add_argument(
        "--allow-ocr-fallback",
        action="store_true",
        default=False,
        help="Mistral OCR basarisiz olursa tesseract fallback kullan",
    )
    # ── New verification arguments ──
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Dogrulanamayan kritik icerik varsa DOCX uretme",
    )
    parser.add_argument(
        "--fail-on-unsafe-filter",
        action="store_true",
        help="Unsafe filter sonucunda pipeline'i durdur",
    )
    parser.add_argument(
        "--emit-review-queue",
        action="store_true",
        help="review_queue.json uret",
    )
    parser.add_argument(
        "--verification-ocr-provider",
        default="tesseract",
        choices=["tesseract", "vision", "none"],
        help="Ikincil dogrulama OCR provider (none = devre disi)",
    )
    parser.add_argument(
        "--verify-critical-fields-only",
        action="store_true",
        help="Sadece kritik belgelerde (kurulus, esas sozlesme) capraz dogrulama",
    )

    args = parser.parse_args()

    run_pipeline(
        input_path=args.input,
        output_path=args.output,
        only_ocr=args.only_ocr,
        verbose=args.verbose,
        ocr_provider=args.ocr_provider,
        no_llm=args.no_llm,
        allow_ocr_fallback=args.allow_ocr_fallback or args.ocr_provider == "mistral",
        strict=args.strict,
        fail_on_unsafe_filter=args.fail_on_unsafe_filter,
        emit_review_queue=args.emit_review_queue,
        verification_ocr_provider=args.verification_ocr_provider,
        verify_critical_only=args.verify_critical_fields_only,
    )


if __name__ == "__main__":
    main()
