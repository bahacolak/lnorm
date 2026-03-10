"""
articles_parser.py - Rule based articles parser with OCR noise cleanup.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ARTICLE_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:#+\s*)?(MADDE\s+(\d{1,2}))\s*[-:\.—]+\s*(.*?)(?=\n|$)",
    re.MULTILINE | re.IGNORECASE,
)
ARTICLE_PATTERN_ALT = re.compile(
    r"(?:^|\n)\s*(?:#+\s*)?(Madde\s+(\d{1,2}))\s*[-:\.—]+\s*(.*?)(?=\n|$)",
    re.MULTILINE,
)
ARTICLE_PATTERN_NUMBERED = re.compile(
    r"(?:^|\n)\s*(?:#+\s*)?((\d{1,2})\.\s+([A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜa-zçğıöşü0-9\s\(\)\/,-]+?))\s*(?=\n|$)",
    re.MULTILINE,
)

NOISE_PATTERNS = [
    re.compile(r"^\(Devam[ıi]\s+\d+\.?\s*Sayfada\)\s*$", re.IGNORECASE),
    re.compile(r"^\(Baştaraf[ıi]\s+\d+\.?\s*Sayfada\)\s*$", re.IGNORECASE),
    re.compile(r"^[Iİ1l]{1,3}\s+[A-ZÇĞİÖŞÜ]+\s+\d{4}\s+SAYI[:;]?\s+\d+\s+T[ÜU]RK[İI]YE\s+T[İI]CAR.*$", re.IGNORECASE),
]


@dataclass
class Article:
    madde_no: int
    baslik: str
    icerik: str
    kaynak_pdf: str
    kaynak_tarih: Optional[str] = None
    kaynak_ttsg_sayi: Optional[str] = None


@dataclass
class OCRQAEntry:
    madde_no: int
    pozisyon: int
    sorun_tipi: str
    detay: str
    kaynak_pdf: str


def parse_articles(
    text: str,
    kaynak_pdf: str,
    expected_count: int = 16,
) -> tuple[list[Article], list[OCRQAEntry]]:
    cleaned = clean_article_text(text)
    articles: list[Article] = []
    qa_issues: list[OCRQAEntry] = []

    if not cleaned:
        qa_issues.append(
            OCRQAEntry(
                madde_no=0,
                pozisyon=0,
                sorun_tipi="madde_bulunamadi",
                detay="Temizlenmis metin bos",
                kaynak_pdf=kaynak_pdf,
            )
        )
        return articles, qa_issues

    matches = list(ARTICLE_PATTERN.finditer(cleaned))
    if not matches:
        matches = list(ARTICLE_PATTERN_ALT.finditer(cleaned))
    if not matches:
        matches = list(ARTICLE_PATTERN_NUMBERED.finditer(cleaned))

    if not matches:
        qa_issues.append(
            OCRQAEntry(
                madde_no=0,
                pozisyon=0,
                sorun_tipi="madde_bulunamadi",
                detay="Madde baslangici tespit edilemedi",
                kaynak_pdf=kaynak_pdf,
            )
        )
        return articles, qa_issues

    for idx, match in enumerate(matches):
        madde_no = int(match.group(2))
        baslik = (match.group(3) or "").strip()
        content_start = match.start()
        content_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(cleaned)
        icerik = cleaned[content_start:content_end].strip()
        article = Article(
            madde_no=madde_no,
            baslik=baslik,
            icerik=icerik,
            kaynak_pdf=kaynak_pdf,
        )
        articles.append(article)
        qa_issues.extend(_check_article_quality(article, match.start()))

    articles = _deduplicate_articles(articles)
    numbers = [article.madde_no for article in articles]

    if expected_count > 0 and len(articles) != expected_count:
        qa_issues.append(
            OCRQAEntry(
                madde_no=0,
                pozisyon=0,
                sorun_tipi="madde_sayisi_uyumsuz",
                detay=f"Beklenen: {expected_count}, Bulunan: {len(articles)}",
                kaynak_pdf=kaynak_pdf,
            )
        )

    if numbers != sorted(numbers):
        qa_issues.append(
            OCRQAEntry(
                madde_no=0,
                pozisyon=0,
                sorun_tipi="sira_anomali",
                detay=f"Madde numaralari: {numbers}",
                kaynak_pdf=kaynak_pdf,
            )
        )

    if expected_count > 0 and numbers:
        expected_series = list(range(numbers[0], numbers[0] + len(numbers)))
        if numbers != expected_series:
            qa_issues.append(
                OCRQAEntry(
                    madde_no=0,
                    pozisyon=0,
                    sorun_tipi="ardisiklik_bozuk",
                    detay=f"Madde sirasi beklenen ardisklilikta degil: {numbers}",
                    kaynak_pdf=kaynak_pdf,
                )
            )

    return articles, qa_issues


def parse_changed_articles(text: str, kaynak_pdf: str) -> tuple[list[Article], list[OCRQAEntry]]:
    # Filter out İç Yönerge sections — only keep Esas Sözleşme content
    text = _strip_ic_yonerge(text)
    return parse_articles(text, kaynak_pdf, expected_count=-1)


def _strip_ic_yonerge(text: str) -> str:
    """Remove İç Yönerge sections from the text, keeping only Esas Sözleşme content."""
    ic_yonerge_markers = [
        "İÇ YÖNERGESİ",
        "İÇ YÖNERGE",
        "İç Yönergesi",
        "İç Yönerge",
        "iç yönergesi",
        "Genel Kurul İç Yönerge",
    ]

    # Find the earliest İç Yönerge marker
    earliest_pos = len(text)
    for marker in ic_yonerge_markers:
        pos = text.find(marker)
        if pos != -1 and pos < earliest_pos:
            earliest_pos = pos

    if earliest_pos < len(text):
        # Check if there's Esas Sözleşme content BEFORE the İç Yönerge
        before = text[:earliest_pos].strip()
        if before:
            return before
        # If İç Yönerge starts at beginning, the entire text is İç Yönerge
        return ""

    return text


def clean_article_text(text: str) -> str:
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if any(pattern.match(stripped) for pattern in NOISE_PATTERNS):
            continue
        if re.match(r"^\(\d{6,}\)$", stripped):
            continue
        lines.append(line)

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _deduplicate_articles(articles: list[Article]) -> list[Article]:
    deduped: dict[int, Article] = {}
    for article in articles:
        existing = deduped.get(article.madde_no)
        if existing is None or len(article.icerik) > len(existing.icerik):
            deduped[article.madde_no] = article
    return [deduped[key] for key in sorted(deduped.keys())]


def _check_article_quality(article: Article, position: int) -> list[OCRQAEntry]:
    issues = []
    if len(article.icerik) < 30:
        issues.append(
            OCRQAEntry(
                madde_no=article.madde_no,
                pozisyon=position,
                sorun_tipi="dusuk_kelime_sayisi",
                detay=f"Madde icerigi cok kisa: {len(article.icerik)} karakter",
                kaynak_pdf=article.kaynak_pdf,
            )
        )

    unknown_chars = re.findall(r"[□■�▪▫◊◦●○]", article.icerik)
    if unknown_chars:
        issues.append(
            OCRQAEntry(
                madde_no=article.madde_no,
                pozisyon=position,
                sorun_tipi="taninamayan_karakter",
                detay=f"{len(unknown_chars)} adet taninamayan karakter var",
                kaynak_pdf=article.kaynak_pdf,
            )
        )

    # Legal term anomaly detection
    try:
        from ocr_verifier import detect_legal_term_anomalies

        anomalies = detect_legal_term_anomalies(article.icerik)
        for anomaly in anomalies:
            issues.append(
                OCRQAEntry(
                    madde_no=article.madde_no,
                    pozisyon=anomaly.position,
                    sorun_tipi="hukuki_terim_anomali",
                    detay=f"'{anomaly.found_text}' → olması gereken: '{anomaly.expected_text}'",
                    kaynak_pdf=article.kaynak_pdf,
                )
            )
    except ImportError:
        pass  # ocr_verifier not available

    return issues


def save_ocr_qa_log(qa_issues: list[OCRQAEntry], output_path: str = "output/ocr_qa_log.json") -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump([asdict(issue) for issue in qa_issues], f, ensure_ascii=False, indent=2)
    logger.info("OCR QA log kaydedildi: %s", path)
    return path


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if len(sys.argv) < 2:
        print("Kullanim: python articles_parser.py <txt_dosyasi>")
        raise SystemExit(1)

    text = Path(sys.argv[1]).read_text(encoding="utf-8")
    articles, qa = parse_articles(text, Path(sys.argv[1]).name)
    print(f"{len(articles)} madde")
    print(f"{len(qa)} QA issue")
