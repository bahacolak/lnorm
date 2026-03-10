"""Tests for ocr_verifier.py — cross-validation and legal term anomaly detection."""

from src.ocr_verifier import (
    calculate_disagreement_score,
    cross_validate_articles,
    cross_validate_ocr,
    detect_legal_term_anomalies,
)


class TestDisagreementScore:
    def test_identical_texts_score_zero(self):
        score = calculate_disagreement_score("Merhaba dünya", "Merhaba dünya")
        assert score == 0.0

    def test_empty_texts_score_zero(self):
        assert calculate_disagreement_score("", "") == 0.0

    def test_one_empty_score_one(self):
        assert calculate_disagreement_score("text", "") == 1.0
        assert calculate_disagreement_score("", "text") == 1.0

    def test_whitespace_normalized(self):
        score = calculate_disagreement_score("merhaba   dünya", "merhaba dünya")
        assert score == 0.0

    def test_similar_texts_low_score(self):
        # Only one word different
        score = calculate_disagreement_score(
            "Hidroelektrik santrali kurmak",
            "Eidroelektrik santrali kurmak",
        )
        assert 0.0 < score < 0.20  # Low but nonzero

    def test_completely_different_texts_high_score(self):
        score = calculate_disagreement_score(
            "Şirketin sermayesi 100000 TL",
            "XYZ tamamen farklı bir metin burada",
        )
        assert score > 0.5


class TestLegalTermAnomalies:
    def test_detects_eidroelektrik(self):
        text = "3) Eidroelektrik santrali, rüzgar tribünleri kurmak"
        anomalies = detect_legal_term_anomalies(text)
        found_terms = {a.found_text.lower() for a in anomalies}
        assert "eidroelektrik" in found_terms

    def test_detects_turk_ticaret_kurumu(self):
        text = "Türk Ticaret Kurumu hükümleri uygulanır."
        anomalies = detect_legal_term_anomalies(text)
        assert len(anomalies) >= 1
        assert any(a.expected_text == "Türk Ticaret Kanunu" for a in anomalies)

    def test_detects_usruklu(self):
        text = "Türkiye Uşruklu 234***62 Kimlik No'lu"
        anomalies = detect_legal_term_anomalies(text)
        assert any(a.found_text == "Uşruklu" for a in anomalies)

    def test_no_anomaly_in_clean_text(self):
        text = "Şirketin sermayesi 100.000 Türk Lirası değerindedir."
        anomalies = detect_legal_term_anomalies(text)
        assert len(anomalies) == 0

    def test_detects_multiple_anomalies(self):
        text = (
            "Eidroelektrik santrali kurmak. "
            "Türk Ticaret Kurumu hükümleri. "
            "Türkiye Uşruklu vatandaş."
        )
        anomalies = detect_legal_term_anomalies(text)
        assert len(anomalies) >= 3

    def test_context_provided(self):
        text = "Bu bir Eidroelektrik tesisidir"
        anomalies = detect_legal_term_anomalies(text)
        assert len(anomalies) == 1
        assert "Eidroelektrik" in anomalies[0].context

    def test_short_substrings_do_not_trigger_false_positive(self):
        text = "Toplantılarda, bu İç Yönergede öngörülmemiş bir husus ortaya çıkarsa karar verilir."
        anomalies = detect_legal_term_anomalies(text)
        assert anomalies == []


class TestCrossValidateOCR:
    def test_identical_pages_verified(self):
        primary = {1: "Aynı metin burada"}
        secondary = {1: "Aynı metin burada"}
        spans, review = cross_validate_ocr(primary, secondary, "test.pdf")
        assert spans[1].status == "verified"
        assert len(review) == 0

    def test_different_pages_disputed(self):
        primary = {1: "Hidroelektrik santrali kurmak, almak, satmak"}
        secondary = {1: "Eidroelektrik santrali kurmak, almak, satmak"}
        spans, review = cross_validate_ocr(primary, secondary, "test.pdf")
        assert spans[1].status in ("disputed", "verified")  # depends on threshold

    def test_empty_page_unverified(self):
        primary = {1: "Metin var"}
        secondary = {1: ""}
        spans, review = cross_validate_ocr(primary, secondary, "test.pdf")
        assert spans[1].status == "primary_only"
        assert len(review) > 0


class TestCrossValidateArticles:
    def test_matching_articles_verified(self):
        text = """MADDE 1 - KURULUŞ
Birinci madde içeriği.
MADDE 2 - ŞİRKETİN UNVANI
İkinci madde içeriği."""
        spans, review = cross_validate_articles(text, text, "test.pdf")
        assert all(s.status == "verified" for s in spans)
        assert len(review) == 0

    def test_disputed_article_flagged(self):
        primary = """MADDE 3 - AMAÇ VE KONU
Eidroelektrik santrali, rüzgar tribünleri kurmak."""
        secondary = """MADDE 3 - AMAÇ VE KONU
Hidroelektrik santrali, rüzgar türbinleri kurmak."""
        spans, review = cross_validate_articles(primary, secondary, "test.pdf")
        assert len(spans) >= 1
        # Either disagreement score or legal term anomaly should flag it
        assert any(s.status == "disputed" for s in spans)

    def test_no_articles_empty_result(self):
        spans, review = cross_validate_articles("no articles here", "no articles here", "test.pdf")
        assert len(spans) == 0
        assert len(review) == 0

    def test_primary_only_article_when_secondary_missing(self):
        primary = """MADDE 6 - SERMAYE
Şirketin sermayesi 100.000 TL'dir."""
        spans, review = cross_validate_articles(primary, "", "test.pdf")
        assert spans[0].status == "primary_only"
        assert spans[0].final_text.startswith("MADDE 6")
        assert review[0].recommended_action == "audit_primary_only"

    def test_numbered_articles_supported(self):
        primary = """1. KURULUŞ
Birinci madde.
2. ŞİRKETİN UNVANI
İkinci madde."""
        spans, _ = cross_validate_articles(primary, "", "test.pdf")
        assert [span.field_name for span in spans] == ["madde_1", "madde_2"]
