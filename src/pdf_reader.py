"""
pdf_reader.py - OCR orchestration layer with dual OCR support.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ocr_providers import (
    OCRDocumentResult,
    run_mistral_ocr,
    run_tesseract_ocr,
    run_vision_llm_ocr,
)

logger = logging.getLogger(__name__)


@dataclass
class DualOCRResult:
    """Holds primary and secondary OCR outputs for cross-validation."""
    primary: OCRDocumentResult
    secondary: OCRDocumentResult
    primary_provider: str = ""
    secondary_provider: str = ""

    def __post_init__(self) -> None:
        self.primary_provider = self.primary.provider
        self.secondary_provider = self.secondary.provider


def extract_text(
    pdf_path: str,
    provider: str = "mistral",
    dpi: int = 300,
    allow_fallback: bool = True,
) -> dict[int, str]:
    """
    Extract page texts from a PDF using the selected OCR provider.
    """
    return extract_document(
        pdf_path=pdf_path,
        provider=provider,
        dpi=dpi,
        allow_fallback=allow_fallback,
    ).as_page_texts()


def extract_document(
    pdf_path: str,
    provider: str = "mistral",
    dpi: int = 300,
    allow_fallback: bool = True,
) -> OCRDocumentResult:
    """
    Extract OCR output plus provider metadata.
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF bulunamadi: {pdf_path}")

    chosen = provider.lower()
    if chosen not in {"mistral", "tesseract", "vision"}:
        raise ValueError(f"Desteklenmeyen OCR provider: {provider}")

    if chosen == "mistral":
        try:
            return run_mistral_ocr(str(path))
        except Exception as exc:
            logger.warning("Mistral OCR basarisiz oldu: %s", exc)
            if not allow_fallback:
                raise
            fallback = run_tesseract_ocr(str(path), dpi=dpi)
            fallback.warnings.append(f"Mistral OCR basarisiz oldu, tesseract fallback kullanildi: {exc}")
            return fallback

    if chosen == "vision":
        return run_vision_llm_ocr(str(path), dpi=dpi)

    return run_tesseract_ocr(str(path), dpi=dpi)


def extract_dual(
    pdf_path: str,
    primary_provider: str = "mistral",
    secondary_provider: str = "tesseract",
    dpi: int = 300,
    allow_fallback: bool = True,
) -> DualOCRResult:
    """
    Run two OCR providers on the same PDF for cross-validation.

    Args:
        pdf_path: Path to the PDF file.
        primary_provider: Primary OCR provider (default: mistral).
        secondary_provider: Secondary OCR provider for verification (default: tesseract).
        dpi: DPI for image rendering.
        allow_fallback: Allow fallback if primary fails.

    Returns:
        DualOCRResult with both OCR outputs.
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF bulunamadi: {pdf_path}")

    logger.info("Dual OCR: primary=%s, secondary=%s — %s", primary_provider, secondary_provider, path.name)

    primary = extract_document(
        str(path),
        provider=primary_provider,
        dpi=dpi,
        allow_fallback=allow_fallback,
    )

    try:
        secondary = extract_document(
            str(path),
            provider=secondary_provider,
            dpi=dpi,
            allow_fallback=False,
        )
    except Exception as exc:
        logger.warning("Secondary OCR (%s) basarisiz: %s", secondary_provider, exc)
        # Return an empty secondary result rather than failing
        secondary = OCRDocumentResult(
            provider=secondary_provider,
            warnings=[f"Secondary OCR basarisiz: {exc}"],
        )

    return DualOCRResult(primary=primary, secondary=secondary)


def reocr_pages(
    pdf_path: str,
    pages: list[int],
    provider: str = "vision",
    dpi: int = 300,
) -> OCRDocumentResult:
    """
    Re-OCR specific pages using a different provider (typically Vision LLM).

    Used for disputed pages identified by the cross-validator.
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF bulunamadi: {pdf_path}")

    logger.info("Re-OCR sayfalari: %s pages=%s provider=%s", path.name, pages, provider)

    if provider == "vision":
        return run_vision_llm_ocr(str(path), dpi=dpi, pages=pages)
    elif provider == "tesseract":
        result = run_tesseract_ocr(str(path), dpi=dpi)
        result.pages = [p for p in result.pages if p.page_num in pages]
        return result
    elif provider == "mistral":
        result = run_mistral_ocr(str(path))
        result.pages = [p for p in result.pages if p.page_num in pages]
        return result
    else:
        raise ValueError(f"Desteklenmeyen re-OCR provider: {provider}")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) < 2:
        print("Kullanim: python pdf_reader.py <pdf_dosyasi> [provider]")
        raise SystemExit(1)

    provider = sys.argv[2] if len(sys.argv) > 2 else "mistral"
    pages = extract_text(sys.argv[1], provider=provider)
    for page_num, text in pages.items():
        print(f"\n--- Sayfa {page_num} ---\n")
        print(text[:3000])
