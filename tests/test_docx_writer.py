from zipfile import ZipFile

from src.article_normalizer import ArticleBlock, NormalizedArticleResult
from src.docx_writer import write_esas_sozlesme


def test_write_esas_sozlesme_renders_block_types(tmp_path):
    article = NormalizedArticleResult(
        madde_no=3,
        title="AMAÇ VE KONU",
        blocks=[
            ArticleBlock(type="paragraph", text="Şirketin amacı budur."),
            ArticleBlock(type="bullet_list", text="a) Birinci bent\nb) İkinci bent"),
            ArticleBlock(
                type="table",
                rows=[["Kolon 1", "Kolon 2"], ["Belirsiz", "B"]],
                cell_statuses=[["accepted_from_llm", "accepted_from_llm"], ["suppressed_uncertain", "accepted_from_llm"]],
                note="Bazı hücreler OCR/LLM belirsizliği nedeniyle bastırıldı.",
            ),
        ],
        source_mode="llm_normalized",
        verification_status="accepted",
    )
    output_path = tmp_path / "esas.docx"
    write_esas_sozlesme([article], {3: {"tarih": "01.01.2025", "kaynak_pdf": "x.pdf"}}, str(output_path))
    xml = ZipFile(output_path).read("word/document.xml").decode("utf-8", errors="ignore")
    assert "Şirketin amacı budur." in xml
    assert "Birinci bent" in xml
    assert "Kolon 1" in xml
    assert "Bazı hücreler OCR/LLM belirsizliği nedeniyle bastırıldı." in xml
    assert "Kaynak modu: llm_normalized" in xml


def test_write_esas_sozlesme_rule_based_table_creates_word_table(tmp_path):
    article = NormalizedArticleResult(
        madde_no=1,
        title="Kuruluş",
        blocks=[
            ArticleBlock(
                type="table",
                rows=[["Sayı No", "Uyruğu"], ["1", "TÜRKİYE"]],
                cell_statuses=[["accepted_from_rule_based", "accepted_from_rule_based"], ["accepted_from_rule_based", "auto_corrected"]],
            )
        ],
        source_mode="rule_based",
        verification_status="accepted",
    )
    output_path = tmp_path / "esas_rule.docx"
    write_esas_sozlesme([article], {1: {"tarih": "01.01.2025", "kaynak_pdf": "x.pdf"}}, str(output_path))
    xml = ZipFile(output_path).read("word/document.xml").decode("utf-8", errors="ignore")
    assert "<w:tbl" in xml
    assert "Sayı No" in xml
    assert "TÜRKİYE" in xml
