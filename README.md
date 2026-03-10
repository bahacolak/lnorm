# LexNorm - Ticaret Sicil Gazetesi Analiz Pipeline

Bu proje, Parla Enerji Yatırımları A.Ş. için ticaret sicil gazetesi ilanlarını okuyup güncel şirket bilgisini, yönetim kurulunu ve esas sözleşmeyi daha güvenli şekilde üretmek için hazırlandı.

Buradaki hedef sadece PDF içinden metin çekmek değil. Asıl amaç:

- hedef şirkete ait ilan kısmını doğru ayıklamak
- birden fazla ilanı tarih sırasına koyup güncel durumu oluşturmak
- her çıktının kaynağını görünür tutmak
- emin olunmayan alanları zorla doğruymuş gibi publish etmemek

## Genel Yaklaşım

- Primary OCR olarak `Mistral OCR` kullanılıyor.
- Kritik belgelerde ikinci kaynak olarak `tesseract` ile çapraz kontrol yapılıyor.
- Hedef şirketin metni önce filtreleniyor, sonra extraction ve konsolidasyon yapılıyor.
- Esas sözleşme tarafında tablo ve bozuk OCR parçaları için yapı korumaya odaklanılıyor.
- Sistem emin olmadığı alanları review queue'ya bırakabiliyor veya belirsiz olarak işaretleyebiliyor.
- LLM opsiyonel. İstenirse daha zor maddelerde ve normalize etmede devreye giriyor; istenirse tamamen kapatılabiliyor.

## Değerlendirme Kriterlerine Göre Ne Yaptık?

### Doğru Filtreleme

İlanların tamamını körlemesine işlemiyoruz. Önce hedef şirkete ait bölümün sınırlarını bulup sadece ilgili parçayı alıyoruz. Böylece aynı belgede geçen başka şirketlerin bilgileri yanlışlıkla sonuca karışmıyor.

### Güncel Bilgiyi Konsolide Etme

Belgeleri tarih sırasına koyup şirketin son halini çıkarmaya çalışıyoruz. Amaç tek tek belgeleri listelemek değil, şirket bilgileri, yönetim kurulu ve esas sözleşme için güncel durumu üretmek.

### Kaynak İzlenebilirliği

Çıktılarda hangi bilginin hangi ilan ve hangi tarih üzerinden geldiği görülebiliyor. Yani sistem sadece sonuç vermiyor, o sonucun nereden geldiğini de görünür bırakıyor.

### Halüsinasyon Kontrolü

Sistem bir şeyi emin değilse uydurmuyor. OCR kaynakları birbiriyle çelişiyorsa veya içerik güven vermiyorsa bunu review queue'ya alıyor ya da belirsiz olarak işaretliyor. Amaç, yanlış bilgiyi temiz görünse bile kullanıcıya vermemek.

### Token Maliyeti Optimizasyonu

Her şeyi LLM'e bırakmıyoruz. Önce kural tabanlı ve daha ucuz yöntemlerle çözmeye çalışıyoruz. LLM daha çok zor, bozuk ya da yapısal olarak problemli alanlarda opsiyonel bir kalite katmanı olarak devreye giriyor.

### Mimari Tasarım Kalitesi

OCR, filtreleme, doğrulama, extraction, konsolidasyon ve çıktı üretimi ayrı katmanlar halinde ilerliyor. Bu sayede bir sorun olduğunda hangi aşamada çıktığını görmek ve sistemi parça parça geliştirmek daha kolay oluyor.

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
- `ANTHROPIC_API_KEY`: opsiyonel vision re-OCR ve article normalization desteği için

## Çalıştırma

```bash
# Case study için önerilen temel çalışma
# Bu modda LLM kullanmadan da sistemin çalıştığı görülebilir.
python3 -m src.main \
  --input input/ \
  --output output/ \
  --ocr-provider mistral \
  --verification-ocr-provider tesseract \
  --strict \
  --fail-on-unsafe-filter \
  --emit-review-queue \
  --allow-ocr-fallback

# LLM tamamen kapalı çalıştırma
# Şirket/YK extraction fallback'leri ve article normalization devre dışı kalır.
python3 -m src.main \
  --input input/ \
  --output output/ \
  --ocr-provider mistral \
  --verification-ocr-provider tesseract \
  --strict \
  --fail-on-unsafe-filter \
  --emit-review-queue \
  --allow-ocr-fallback \
  --no-llm

# LLM destekli esas sözleşme normalize etme
# Daha zor maddelerde article normalization için LLM devreye girer.
python3 -m src.main \
  --input input/ \
  --output output/ \
  --ocr-provider mistral \
  --verification-ocr-provider tesseract \
  --strict \
  --fail-on-unsafe-filter \
  --emit-review-queue \
  --allow-ocr-fallback \
  --llm-article-normalization

# Sadece tesseract kullan
python3 -m src.main --input input/ --output output/ --ocr-provider tesseract --no-llm

# Yalnızca OCR + filtreleme
python3 -m src.main --input input/ --output output/ --only-ocr --ocr-provider mistral --allow-ocr-fallback
```

Not:

- `--llm-article-normalization` verilmezse esas sözleşme maddeleri kural tabanlı normalize edilir.
- `--no-llm` verilirse LLM tabanlı extraction fallback'leri de kapanır.
- Yani kod değiştirmeden hem `LLM kapalı` hem `LLM açık` demo almak mümkün.

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
- `output/article_normalization_audit.json` article normalization açıksa
- `output/article_normalization_diff.json` article normalization açıksa

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

## Güvenlik ve Audit Yaklaşımı

- Rule-based first yaklaşımı kullanılır.
- LLM zorunlu değildir; ister tamamen kapalı çalıştırılabilir, ister zor maddelerde destekleyici olarak kullanılabilir.
- Filter katmanı `ok`, `partial`, `unsafe`, `not_found` statüsü üretir.
- Esas sözleşme maddeleri verifier sonucuna göre kabul edilir; disputed içerik review queue'ya düşer.
- Secondary OCR boşsa `primary_only` statüsü üretilir; bu maddeler case study sürümünde tutulur, production ortamında daha sert gate uygulanmalıdır.
- OCR ve extraction anomalleri `output/extraction_audit.json` ile izlenebilir.
- Review queue ve field confidence artefact'ları teknik kararları görünür kılar.
- Doğrulanamayan alanlar final çıktıda bastırılabilir veya belirsiz olarak işaretlenebilir.

## Bilinen Sınırlamalar

- `MISTRAL_API_KEY` yoksa Mistral OCR test edilemez; fallback `tesseract` devreye girer.
- Gerçek PDF kalitesi çok düşükse bazı maddeler strict modda bloke edilir ve manuel doğrulama gerekebilir.
- `verification_ocr_provider=vision` secondary OCR olarak desteklenmez; vision yalnızca re-OCR aşamasında kullanılır.
- YK ilan formatları heterojen olduğu için bazı kayıtlar bilinçli olarak boş/hariç bırakılabilir.

## Testler

```bash
python3 -m pytest -q
```

Testler sentetik örnekler üzerinden filtreleme, article parsing, extraction, tablo render ve konsolidasyon davranışını doğrular.
