from src.main import build_parser


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
