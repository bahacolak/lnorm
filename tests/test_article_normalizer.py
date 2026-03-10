from src.article_normalizer import (
    ArticleBlock,
    build_article_draft,
    normalize_article,
    normalize_article_with_llm,
)
from src.articles_parser import Article


def test_build_article_draft_marks_clean_article_without_llm():
    article = Article(madde_no=2, baslik="UNVAN", icerik="Şirketin unvanı Parla Enerji Yatırımları Anonim Şirketidir.", kaynak_pdf="x.pdf")
    draft = build_article_draft(article)
    assert draft.needs_llm is False
    assert draft.blocks[0].type == "paragraph"


def test_build_article_draft_detects_table_like_content():
    article = Article(
        madde_no=1,
        baslik="Kuruluş",
        icerik="# 1. Kuruluş\n| Ad | Adres |\n| --- | --- |\n| Aydem | Denizli |",
        kaynak_pdf="x.pdf",
    )
    draft = build_article_draft(article)
    assert draft.needs_llm is True
    assert any(block.type == "table" for block in draft.blocks)


def test_normalize_article_rule_based_strips_duplicate_heading():
    article = Article(
        madde_no=3,
        baslik="AMAÇ VE KONU",
        icerik="# 3. AMAÇ VE KONU\n3) Hidroelektrik santrali kurmak.\na) Proje geliştirmek.",
        kaynak_pdf="x.pdf",
    )
    result = normalize_article(article, use_llm=False)
    first_block = result.blocks[0]
    assert "# 3. AMAÇ VE KONU" not in (first_block.text or "")


def test_normalize_article_with_llm_falls_back_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    article = Article(
        madde_no=3,
        baslik="AMAÇ VE KONU",
        icerik="Eidroelektrik santrali kurmak.",
        kaynak_pdf="x.pdf",
    )
    draft = build_article_draft(article)
    result = normalize_article_with_llm(draft)
    assert result.source_mode == "rule_based"


def test_normalize_article_with_llm_accepts_valid_payload(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    article = Article(
        madde_no=3,
        baslik="AMAÇ VE KONU",
        icerik="Eidroelektrik santrali kurmak.\na) Proje geliştirmek.",
        kaynak_pdf="x.pdf",
    )
    from src import article_normalizer as mod

    monkeypatch.setattr(
        mod,
        "_call_anthropic_normalizer",
        lambda draft, model: {
            "madde_no": draft.madde_no,
            "title": draft.title,
            "blocks": [
                {"type": "paragraph", "text": "Eidroelektrik santrali kurmak."},
                {"type": "bullet_list", "text": "a) Proje geliştirmek."},
            ],
            "uncertain_spans": [],
            "notes": ["normalized"],
        },
    )
    result = normalize_article(article, use_llm=True)
    assert result.source_mode == "llm_normalized"
    assert any(block.type == "bullet_list" for block in result.blocks)


def test_normalize_article_with_llm_falls_back_on_large_diff(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    article = Article(madde_no=6, baslik="SERMAYE", icerik="Eidroelektrik santrali ve testiyar hakkı.", kaynak_pdf="x.pdf")
    from src import article_normalizer as mod

    monkeypatch.setattr(
        mod,
        "_call_anthropic_normalizer",
        lambda draft, model: {
            "madde_no": draft.madde_no,
            "title": draft.title,
            "blocks": [{"type": "paragraph", "text": "Tamamen farklı ve yeni bir metin eklendi."}],
            "uncertain_spans": [],
            "notes": [],
        },
    )
    result = normalize_article(article, use_llm=True)
    assert result.verification_status in {"fallback_rule_based", "fallback_raw"}
