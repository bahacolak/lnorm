"""main.py - CLI entrypoint for the LexNorm pipeline."""

from __future__ import annotations

import argparse
from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--llm-article-normalization",
        action="store_true",
        help="Esas sozlesme maddeleri icin Anthropic tabanli normalize etme",
    )
    parser.add_argument(
        "--article-normalization-model",
        default="claude-sonnet-4-20250514",
        help="Esas sozlesme normalization modeli",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
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
        llm_article_normalization=args.llm_article_normalization,
        article_normalization_model=args.article_normalization_model,
    )


if __name__ == "__main__":
    main()
