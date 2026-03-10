from src.main import build_parser
from src.pipeline import _attempt_reocr_recovery


def test_build_parser_preserves_cli_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--input",
            "input",
            "--output",
            "output",
            "--strict",
            "--emit-review-queue",
            "--verification-ocr-provider",
            "tesseract",
            "--llm-article-normalization",
        ]
    )
    assert args.input == "input"
    assert args.output == "output"
    assert args.strict is True
    assert args.emit_review_queue is True
    assert args.verification_ocr_provider == "tesseract"
    assert args.llm_article_normalization is True
    assert args.article_normalization_model == "claude-sonnet-4-20250514"


def test_attempt_reocr_recovery_uses_tesseract_when_no_llm(monkeypatch):
    from src import pipeline as mod

    captured = {}

    class DummyResult:
        def as_page_texts(self):
            return {1: "x"}

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    def fake_reocr_pages(pdf_file, pages, provider):
        captured["provider"] = provider
        return DummyResult()

    monkeypatch.setattr(mod, "reocr_pages", fake_reocr_pages)
    monkeypatch.setattr(mod, "filter_target_company", lambda *args, **kwargs: type("Filtered", (), {"text": "MADDE 1 - x"})())
    monkeypatch.setattr(mod, "cross_validate_articles", lambda *args, **kwargs: ([], []))

    _attempt_reocr_recovery(
        pdf_file="x.pdf",
        pdf_name="x.pdf",
        parser=lambda text, pdf: ([], []),
        secondary_text="MADDE 1 - x",
        disputed_spans=[],
        pages=[1],
        allow_llm=False,
    )

    assert captured["provider"] == "tesseract"
