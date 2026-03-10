# LexNorm - Ticaret Sicil Gazetesi Analiz Pipeline

Parla Enerji Yatırımları A.Ş. için hazırlanmış TTSG analiz pipeline'ı. Çözüm, case study beklentisine uygun olarak tam ürünleşme yerine `doğru filtreleme`, `kaynak izlenebilirliği`, `verification-gated extraction` ve `yanlış metni publish etmeme` ilkelerine odaklanır.

## Yaklaşım

- Birincil OCR sağlayıcısı `Mistral OCR` olarak kullanılır.
- Kritik belgelerde ikinci bir OCR kaynağı (`tesseract`) ile çapraz doğrulama yapılır.
- `primary_only` durumunda, case study kapsam görünürlüğü için primary OCR metni korunur ve audit artefact'larına işaretlenir.
- Strict modda disputed/unverified kritik içerik varsa DOCX üretimi bloklanır.
- LLM zorunlu değildir; varsa yalnızca hedefli re-OCR için kullanılır.

## Neden Mistral OCR

- Çok sütunlu taranmış belgelerde Tesseract'a göre daha iyi OCR kalitesi hedeflenir.
- OCR çıktısındaki gürültü azaltıldığında filtreleme, esas sözleşme parse ve yönetim kurulu extraction katmanları daha stabil çalışır.
- Maliyet olarak Vision tabanlı genel LLM çözümlerinden düşüktür.

## Kurulum

```bash
pip install -r requirements.txt
brew install tesseract tesseract-lang poppler
```

## Ortam Değişkenleri

Değişkenleri sistem ortamına ekleyebilir veya proje kök dizininde bir `.env` dosyası oluşturarak tanımlayabilirsiniz (otomatik yüklenir).
Repo gerçek API anahtarı içermez. Başlamak için `.env.example` dosyasını kopyalayıp kendi anahtarlarınızı ekleyin:

```bash
cp .env.example .env
```

- `MISTRAL_API_KEY`: Mistral OCR erişimi için
- `MISTRAL_OCR_URL`: opsiyonel özel endpoint override
- `MISTRAL_OCR_MODEL`: opsiyonel model override
- `ANTHROPIC_API_KEY`: opsiyonel vision re-OCR desteği için

## Çalıştırma

```bash
# Önerilen case study çalıştırması
python -m src.main --input input/ --output output/ --ocr-provider mistral --verification-ocr-provider tesseract --strict --fail-on-unsafe-filter --emit-review-queue --allow-ocr-fallback

# Sadece tesseract kullan
python -m src.main --input input/ --output output/ --ocr-provider tesseract --no-llm

# Yalnızca OCR + filtreleme
python -m src.main --input input/ --output output/ --only-ocr --ocr-provider mistral --allow-ocr-fallback
```

## Çıktılar

- `output/sirket_bilgileri.docx`
- `output/yonetim_kurulu.docx`
- `output/esas_sozlesme.docx`
- `output/extracted_texts/*.txt`
- `output/ocr_qa_log.json`
- `output/extraction_audit.json`
- `output/review_queue.json`
- `output/field_confidence.json`
- `output/article_comparison.json`
- `output/hallucination_log.json` yalnızca LLM çağrıldıysa

## Mimari

```text
PDF -> Primary OCR -> Secondary OCR -> Verification Gate -> FilterResult -> Extraction / Articles Parse -> Consolidation -> DOCX
```

### Modüller

- `src/ocr_providers.py`: Mistral OCR ve Tesseract adaptörleri
- `src/pdf_reader.py`: OCR orchestration, dual OCR ve re-OCR
- `src/filter.py`: hedef şirket metni ve boundary güvenlik statüsü
- `src/articles_parser.py`: OCR noise temizleme ve esas sözleşme madde parse
- `src/extractor.py`: şirket bilgileri, denetçi ve YK extraction
- `src/ocr_verifier.py`: OCR disagreement tespiti, anomaly detection ve review queue
- `src/consolidator.py`: tarih bazlı konsolidasyon
- `src/pipeline.py`: pipeline orchestration
- `src/persistence.py`: ortak artifact persistence yardımcıları
- `src/docx_writer.py`: Word çıktıları
- `src/main.py`: CLI

## Halüsinasyon ve Audit Stratejisi

- Rule-based first yaklaşımı kullanılır.
- LLM zorunlu değildir; varsa yalnızca disputed sayfalar için re-OCR amaçlı kullanılır.
- Filter katmanı `ok`, `partial`, `unsafe`, `not_found` statüsü üretir.
- Esas sözleşme maddeleri verifier sonucuna göre kabul edilir; disputed içerik review queue'ya düşer.
- Secondary OCR boşsa `primary_only` statüsü üretilir; bu maddeler case study sürümünde tutulur, production ortamında daha sert gate uygulanmalıdır.
- OCR ve extraction anomalleri `output/extraction_audit.json` ile izlenebilir.
- Review queue ve field confidence artefact'ları teknik kararları görünür kılar.
- Doğrulanamayan alanlar final çıktıda boş/hariç bırakılır.

## Bilinen Sınırlamalar

- `MISTRAL_API_KEY` yoksa Mistral OCR test edilemez; fallback `tesseract` devreye girer.
- Gerçek PDF kalitesi çok düşükse bazı maddeler strict modda bloke edilir ve manuel doğrulama gerekebilir.
- `verification_ocr_provider=vision` secondary OCR olarak desteklenmez; vision yalnızca re-OCR aşamasında kullanılır.
- YK ilan formatları heterojen olduğu için bazı kayıtlar bilinçli olarak boş/hariç bırakılabilir.

## Testler

```bash
python3 -m pytest -q
```

Testler sentetik örnekler üzerinden filter, articles parser, extractor ve consolidator davranışını doğrular.
