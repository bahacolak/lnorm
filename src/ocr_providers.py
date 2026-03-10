"""
ocr_providers.py - OCR provider abstraction for Mistral and Tesseract.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

import httpx
import numpy as np
import pytesseract
from pdf2image import convert_from_path
from PIL import Image

import cv2

logger = logging.getLogger(__name__)

TESS_CONFIG = "--psm 6 --oem 3 -l tur"
DEFAULT_MISTRAL_MODEL = "mistral-ocr-latest"
DEFAULT_MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"


@dataclass
class OCRPageResult:
    page_num: int
    text: str
    provider: str
    confidence: Optional[str] = None
    raw_blocks: list[dict[str, Any]] | None = None


@dataclass
class OCRDocumentResult:
    provider: str
    pages: list[OCRPageResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_page_texts(self) -> dict[int, str]:
        return {page.page_num: page.text for page in self.pages}


def run_mistral_ocr(pdf_path: str) -> OCRDocumentResult:
    """
    Run Mistral OCR against a PDF.

    The parser is intentionally tolerant because the API payload can evolve.
    """
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY tanimli degil")

    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF bulunamadi: {pdf_path}")

    logger.info("Mistral OCR deneniyor: %s", path.name)
    url = os.environ.get("MISTRAL_OCR_URL", DEFAULT_MISTRAL_OCR_URL)
    model = os.environ.get("MISTRAL_OCR_MODEL", DEFAULT_MISTRAL_MODEL)

    with path.open("rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")

    payload = {
        "model": model,
        "document": {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{encoded}",
        },
        "include_image_base64": False,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=120.0) as client:
        response = client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    pages = _parse_mistral_pages(data)
    if not pages:
        raise RuntimeError("Mistral OCR cevabindan sayfa metni parse edilemedi")

    return OCRDocumentResult(provider="mistral", pages=pages)


def _parse_mistral_pages(payload: dict[str, Any]) -> list[OCRPageResult]:
    page_candidates = payload.get("pages") or payload.get("document", {}).get("pages") or []
    results: list[OCRPageResult] = []

    if page_candidates:
        for idx, page in enumerate(page_candidates, start=1):
            text = _extract_text_from_page_payload(page)
            blocks = _extract_blocks(page)
            if not text and blocks:
                text = "\n".join(
                    block.get("text", "").strip()
                    for block in blocks
                    if block.get("text")
                )
            p_num = page.get("index")
            if p_num is None:
                p_num = page.get("page")
            if p_num is None:
                p_num = idx

            results.append(
                OCRPageResult(
                    page_num=p_num,
                    text=_normalize_ocr_text(text),
                    provider="mistral",
                    confidence=str(page.get("confidence")) if page.get("confidence") is not None else None,
                    raw_blocks=blocks,
                )
            )
        return results

    text = payload.get("text") or payload.get("markdown")
    if text:
        return [OCRPageResult(page_num=1, text=_normalize_ocr_text(text), provider="mistral")]

    return []


def _extract_text_from_page_payload(page: dict[str, Any]) -> str:
    for key in ("text", "markdown", "content"):
        value = page.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _extract_blocks(page: dict[str, Any]) -> list[dict[str, Any]] | None:
    blocks = page.get("blocks") or page.get("lines")
    if isinstance(blocks, list):
        normalized = []
        for block in blocks:
            if isinstance(block, dict):
                normalized.append(block)
        return normalized or None
    return None


def run_tesseract_ocr(pdf_path: str, dpi: int = 300) -> OCRDocumentResult:
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF bulunamadi: {pdf_path}")

    logger.info("Tesseract OCR calisiyor: %s", path.name)
    images = convert_from_path(str(path), dpi=dpi)
    pages: list[OCRPageResult] = []

    for page_num, pil_image in enumerate(images, start=1):
        processed = _preprocess_image(pil_image)
        columns = _detect_columns(processed)

        page_parts = []
        for col_img in columns:
            col_pil = Image.fromarray(col_img)
            page_parts.append(pytesseract.image_to_string(col_pil, config=TESS_CONFIG))

        pages.append(
            OCRPageResult(
                page_num=page_num,
                text=_normalize_ocr_text("\n\n".join(page_parts)),
                provider="tesseract",
            )
        )

    return OCRDocumentResult(provider="tesseract", pages=pages)


def run_vision_llm_ocr(
    pdf_path: str,
    dpi: int = 300,
    pages: list[int] | None = None,
    model: str | None = None,
) -> OCRDocumentResult:
    """
    Run Vision LLM OCR against specific pages of a PDF.

    Sends each page as an image to Claude's vision API for high-quality
    text extraction with semantic understanding of Turkish legal documents.

    Args:
        pdf_path: Path to the PDF file.
        dpi: DPI for page rendering.
        pages: Optional list of 1-based page numbers to process. If None, all pages.
        model: Optional model override (default: claude-sonnet-4-20250514).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY tanimli degil (Vision LLM OCR icin)")

    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF bulunamadi: {pdf_path}")

    logger.info("Vision LLM OCR deneniyor: %s (pages=%s)", path.name, pages or "all")

    images = convert_from_path(str(path), dpi=dpi)
    results: list[OCRPageResult] = []
    chosen_model = model or os.environ.get("VISION_LLM_MODEL", "claude-sonnet-4-20250514")

    try:
        import anthropic
        client = anthropic.Anthropic()
    except ImportError:
        raise RuntimeError("anthropic paketi gerekli: pip install anthropic")

    system_prompt = (
        "Bu bir Türk Ticaret Sicil Gazetesi (TTSG) sayfasıdır. "
        "Sayfadaki metni birebir oku. Yorum, özet, düzeltme yapma. "
        "Türkçe hukuki terimleri ve şirket isimlerini doğru yaz. "
        "Varsa tabloları düz metin olarak koru. "
        "Sayfa üstündeki tarih/sayı bilgilerini ve sütun yapısını koru."
    )

    for page_num, pil_image in enumerate(images, start=1):
        if pages is not None and page_num not in pages:
            continue

        # Convert to base64
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        try:
            message = client.messages.create(
                model=chosen_model,
                max_tokens=4096,
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": img_b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": "Bu sayfadaki tüm metni birebir oku ve yaz.",
                            },
                        ],
                    }
                ],
            )
            page_text = message.content[0].text
        except Exception as exc:
            logger.warning("Vision LLM OCR sayfa %d hatasi: %s", page_num, exc)
            page_text = ""

        results.append(
            OCRPageResult(
                page_num=page_num,
                text=_normalize_ocr_text(page_text),
                provider="vision_llm",
            )
        )

    if not results:
        raise RuntimeError("Vision LLM OCR sonuc uretmedi")

    return OCRDocumentResult(provider="vision_llm", pages=results)


def _normalize_ocr_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _preprocess_image(pil_image: Image.Image) -> np.ndarray:
    img = np.array(pil_image)
    if len(img.shape) == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    else:
        gray = img

    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    thresh = cv2.adaptiveThreshold(
        blurred,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=15,
        C=10,
    )
    return _deskew(thresh)


def _deskew(image: np.ndarray, max_angle: float = 5.0) -> np.ndarray:
    coords = np.column_stack(np.where(image < 128))
    if len(coords) < 100:
        return image

    angle = cv2.minAreaRect(coords)[-1]
    angle = -(90 + angle) if angle < -45 else -angle
    if abs(angle) < 0.1 or abs(angle) > max_angle:
        return image

    h, w = image.shape[:2]
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(
        image,
        matrix,
        (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _detect_columns(image: np.ndarray, min_gap_ratio: float = 0.02) -> list[np.ndarray]:
    h, w = image.shape[:2]
    inverted = 255 - image
    vertical_proj = np.sum(inverted, axis=0)

    if vertical_proj.max() <= 0:
        return [image]

    normalized = vertical_proj / vertical_proj.max()
    threshold = 0.05
    min_gap_width = int(w * min_gap_ratio)

    is_gap = normalized < threshold
    gaps = []
    gap_start = None

    for x in range(w):
        if is_gap[x] and gap_start is None:
            gap_start = x
        elif not is_gap[x] and gap_start is not None:
            if x - gap_start >= min_gap_width:
                gaps.append((gap_start, x))
            gap_start = None

    center_gaps = [(start, end) for start, end in gaps if start > w * 0.2 and end < w * 0.8]
    if not center_gaps:
        return [image]

    best_gap = max(center_gaps, key=lambda item: item[1] - item[0])
    split_x = (best_gap[0] + best_gap[1]) // 2
    left_col = image[:, :split_x]
    right_col = image[:, split_x:]

    if left_col.shape[1] < w * 0.15 or right_col.shape[1] < w * 0.15:
        return [image]

    return [left_col, right_col]
