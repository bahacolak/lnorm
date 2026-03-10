# LexNorm — Ticaret Sicil Gazetesi Analiz Projesi: Detaylı Plan

> **v3** — Güncellemeler: OCR primary, esas sözleşmede LLM yok, layout-aware filter, YK formatı, Madde 6 bazlı sermaye kuralı, tescil tarihi regex'i, hedef şirket salt metin çıktıları notu eklendi

---

## 1. Proje Özeti

Parla Enerji Yatırımları A.Ş.'ye ait 9 adet TTSG ilanı PDF'inden yapılandırılmış bilgi çıkarımı yapan, sonuçları Word formatında sunan ve GitHub'da yayınlanabilir bir pipeline.

---

## 2. Teknoloji Seçimleri

| Katman | Araç | Gerekçe |
|---|---|---|
| OCR (primary) | `pytesseract` + `pdf2image` | Belgeler scan formatında — OCR her zaman primary |
| PDF metin kontrolü | `pdfplumber` | Sadece "gömülü metin var mı?" ön kontrolü için; varsa OCR'ı atla |
| OCR dil paketi | Tesseract `tur` | Türkçe karakter desteği |
| Görüntü ön işleme | `Pillow`, `opencv-python` | Kontrast, gürültü, deskew, sütun hizalama |
| LLM entegrasyonu | Anthropic Claude API (`claude-sonnet-4`) | Sadece şirket bilgileri ve YK çıkarımı — esas sözleşmede kullanılmaz |
| Word çıktısı | `python-docx` | .docx tablo ve metin üretimi |
| Orchestration | Python 3.11, `argparse` | CLI ile çalıştırma |

---

## 3. Proje Klasör Yapısı

```
lexnorm-ttsg/
├── input/
│   ├── 1__01-12-2022_Kurulus_Ilani.pdf
│   ├── 2__21-06-2023_Esas_Sozlesme.pdf
│   ├── 3__10-10-2023_Yonetim.pdf
│   ├── 4__29-12-2023_Esas_Sozlesme.pdf
│   ├── 5__13-09-2024_Yonetim.pdf
│   ├── 6__30-10-2024_Yonetim.pdf
│   ├── 7__05-11-2024_Yonetim.pdf
│   ├── 8__11-09-2025_Yonetim.pdf
│   └── 9__[tarih]_[tur].pdf
│dosya isimlerine bakariz tekrar
├── output/
│   ├── extracted_texts/
│   │   ├── 1__01-12-2022_Kurulus_Ilani.txt   <- sadece Parla Enerji kismi
│   │   └── ... (her PDF icin ayri .txt)
│   ├── sirket_bilgileri.docx        <- Guncel Sirket Bilgileri Tablosu
│   ├── yonetim_kurulu.docx          <- YK Uyeleri Tablosu
│   ├── esas_sozlesme.docx           <- Konsolide Esas Sozlesme
│   └── hallucination_log.json       <- Dogrulanamayan alanlar
│
├── src/
│   ├── pdf_reader.py        <- PDF -> ham metin (OCR primary)
│   ├── filter.py            <- Layout-aware hedef sirket ayiklama
│   ├── extractor.py         <- LLM ile sirket bilgisi + YK cikarimi
│   ├── articles_parser.py   <- LLM'siz esas sozlesme segmentasyonu
│   ├── consolidator.py      <- En guncel bilgiyi konsolide et
│   ├── docx_writer.py       <- Word dosyasi uret
│   └── main.py              <- Tum pipeline'i calistir
│
├── prompts/
│   ├── company_info.txt     <- Sirket bilgileri cikarim promptu
│   └── board_members.txt    <- YK uyeleri cikarim promptu
│   (articles.txt YOK — esas sozlesmede LLM kullanilmaz)
│
├── tests/
│   ├── test_filter.py
│   ├── test_ocr.py
│   └── test_articles_parser.py
│
├── requirements.txt
└── README.md
```

---

## 4. Pipeline Akışı

```
┌─────────────┐
│  input/ PDF │  (9 adet, scan format)
└──────┬──────┘
       │
       ▼
┌──────────────────────────────────────┐
│  ADIM 1: pdf_reader.py               │
│  pdfplumber → gomulu metin var mi?   │
│    EVET → dogrudan kullan            │
│    HAYIR (veya <50 char/sayfa)       │
│      → pdf2image → goruntu on isleme │
│      → tesseract OCR (tur, psm=4)    │
│  Cikti: {page: raw_text}             │
└──────┬───────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────┐
│  ADIM 2: filter.py                   │
│  Layout-aware sirket ayiklama:       │
│    1. Sutun tespiti (koordinat bazli)│
│    2. Sirket basliklarini bul        │
│    3. "PARLA ENERJI" blogunu ayikla  │
│    4. Diger sirketleri disla         │
│  Cikti: output/extracted_texts/*.txt │
└──────┬───────────────────────────────┘
       │
       ├─────────────────────┐
       ▼                     ▼
┌──────────────┐    ┌────────────────────────────────┐
│  ADIM 3a:    │    │  ADIM 3b:                      │
│  extractor   │    │  articles_parser.py             │
│  (LLM)       │    │  (LLM YOK - pure segmentasyon) │
│              │    │                                 │
│  - Sirket    │    │  1. "MADDE \d+" pattern bul    │
│    bilgileri │    │  2. Her maddeyi sinirlarıyla    │
│  - YK uyeleri│    │     ayikla                     │
│  -> JSON     │    │  3. Madde metnini birebir al   │
└──────┬───────┘    └────────┬───────────────────────┘
       │                     │
       └──────────┬──────────┘
                  ▼
┌──────────────────────────────────────┐
│  ADIM 4: consolidator.py             │
│  PDF'leri tarihe gore sirala         │
│  Sirket bilgileri: son deger kazanir │
│  YK: atama/azil event takibi         │
│  Sozlesme: degisen maddeleri guncelle│
└──────┬───────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────┐
│  ADIM 5: docx_writer.py              │
│  3 Word dosyasi + hallucination_log  │
└──────────────────────────────────────┘
```

---

## 5. Modül Detayları

### 5.1 `pdf_reader.py` — OCR Primary

```python
def extract_text(pdf_path: str) -> dict[int, str]:
    """
    Döndürür: {page_num: text}
    Strateji:
      1. pdfplumber ile kontrol: karakter sayısı > 50/sayfa → gömülü metin var
      2. Gömülü metin varsa → kalite kontrolü yap (karakter çeşitliliği, Türkçe karakter oranı, anlamsız token oranı); kalite yeterliyse kullan, yetersizse OCR uygula
      3. Yoksa → pdf2image ile her sayfayı görüntüye çevir
                → görüntü ön işleme uygula
                → tesseract (lang=tur, psm=4) çalıştır
    """
```

**Görüntü Ön İşleme Adımları:**
```
1. Gri tonlama (RGB → Gray)
2. Gaussian blur (gürültü azaltma)
3. Adaptive thresholding (düzensiz aydınlatma toleransı)
4. Deskew — eğik taranmış sayfalar için açı düzeltme
5. DPI normalize: minimum 300 DPI garantisi
```

**Tesseract Konfigürasyonu:**
```python
# Strateji: önce sayfayı sütunlara böl, her segmenti ayrı ayrı OCR'a ver
# Adım 1: opencv ile dikey projeksiyon → sütun sınırlarını tespit et
# Adım 2: Her sütun → ayrı görüntü crop
# Adım 3: Her segment için tesseract (psm 6 — tek düzgün metin bloğu)
# Avantaj: sütunlar arası satır karışıklığını kökten engeller
TESS_CONFIG = "--psm 6 --oem 3 -l tur"
```

---

### 5.2 `filter.py` — Layout-Aware Filtreleme

TTSG ilanları çok sütunlu layout içerir. OCR çıktısında sütunlar arası satır karışıklığı oluşabilir. İki katmanlı strateji:

**Katman 1 — Koordinat Bazlı (Gömülü Metin Varsa):**
```python
def split_columns_by_bbox(page) -> list[str]:
    # pdfplumber bounding box: x koordinatına göre sütunları ayır
    # x < sayfa_genişliği/2 → sol sütun
    # x > sayfa_genişliği/2 → sağ sütun
    # Her sütunu bağımsız metin akışı olarak işle
```

**Katman 2 — Regex Bazlı (OCR Metni İçin):**
```python
HEDEF_SIRKET = "PARLA ENERJİ YATIRIMLARI ANONİM ŞİRKETİ"

COMPANY_HEADER = re.compile(
    r'^[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ0-9\s\.\,\-\&\/]+(?:ANONİM|LİMİTED)\s+ŞİRKETİ',
    re.MULTILINE
)
# Ek güvenlik: "MERSİS No:" satırı da yeni ilan başlangıcını işaret eder
MERSIS_HEADER = re.compile(r'MERSİS\s*No\s*:', re.IGNORECASE)
```

**Filtreleme Mantığı:**
```
1. Tüm şirket başlıklarını + MERSİS başlıklarını bul → pozisyonlarını kaydet
2. "PARLA ENERJİ" pozisyonunu tespit et
3. Bir sonraki şirket başlığı pozisyonuna kadar metni al
4. Bulunamazsa → None döndür, uyarı logla, işleme devam et
```

---

### 5.3 `extractor.py` — LLM ile Şirket Bilgileri ve YK

**Kural:** Esas sözleşme maddeleri bu modülde işlenmez. LLM yalnızca yapısal başlık bilgileri ve YK için kullanılır.

**Extraction Stratejisi — Rule-Based First:**

| Görev | Önce | LLM Ne Zaman? |
|---|---|---|
| MERSİS no | Regex `\d{16}` | Hiçbir zaman |
| Ticaret Sicil no | Regex | Hiçbir zaman |
| Sermaye | Kuruluş ilanı için **Madde 6** taraması, Diğerleri için Regex `\d{1,3}(\.\d{3})* TL` | Kural veya Regex başarısızsa |
| Kuruluş tarihi | Regex `(\d{2}/\d{2}/\d{4})\s*tarihinde\s*tescil\s*edil(diği\|miştir)` | Regex confidence düşükse |
| Adres | Rule-based (ilan başlık bloğu) | Rule-based başarısızsa |
| YK üyeleri | Rule-based tablo/satır parse | Yapı düzensizse |
| Denetçi | Rule-based anahtar kelime | Bulunamazsa |

> Confidence eşiği: regex match + kaynak metinde doğrulama → her ikisi de başarılıysa LLM çağrısı yapılmaz. Sadece doğrulama başarısız olduğunda LLM devreye girer.

**LLM JSON Çıktı Formatı:**
```json
{
  "field": "board_members",
  "value": [
    {
      "name": "HASAN ELBİR",
      "role": "Yönetim Kurulu Başkanı",
      "entity_type": "real_person",
      "term_end": "31.12.2025",
      "representative": null
    },
    {
      "name": "AYDEM HOLDİNG ANONİM ŞİRKETİ",
      "role": "Yönetim Kurulu Üyesi",
      "entity_type": "legal_entity",
      "term_end": "31.12.2025",
      "representative": "CEYHAN SALDANLI"
    }
  ],
  "confidence": "high",
  "source_snippet": "...yönetim kurulu üyesi olarak seçilmiştir...",
  "source_page": 2
}
```

**Halüsinasyon Kontrol:**
```python
def verify_against_source(value: str, source_text: str) -> bool:
    # Normalize: büyük/küçük harf + fazla boşluk toleranslı karşılaştırma
    # Bulunamazsa → hallucination_log.json'a yaz, değeri [DOĞRULANAMADI] yap
    return normalize(value) in normalize(source_text)
```

---

### 5.4 `articles_parser.py` — LLM'siz Esas Sözleşme Segmentasyonu

> ⚠️ Bu modülde LLM **kesinlikle kullanılmaz**. Tüm metin birebir OCR çıktısından alınır.

**Madde Sınır Tespiti:**
```python
# TTSG'deki yaygın format
ARTICLE_PATTERN = re.compile(
    r'(?:^|\n)(MADDE\s+\d{1,2})\s*[\-\:\.]\s*',
    re.MULTILINE | re.IGNORECASE
)
# Alternatif: numara + nokta formatı
ARTICLE_PATTERN_ALT = re.compile(
    r'(?:^|\n)\s*(\d{1,2})\.\s+[A-ZÇĞİÖŞÜ]',
    re.MULTILINE
)
```

**Segmentasyon Mantığı:**
```
1. Filtrelenmiş metinde madde başlangıç pozisyonlarını bul
2. Madde N → Madde N+1 arası metin = maddenin içeriği
3. {madde_no, baslik, icerik, kaynak_pdf} olarak kaydet
4. 16 maddeden az veya fazla çıktıysa uyarı logla
```

**Kritik Kural:** Madde metni üzerinde hiçbir düzenleme yapılmaz. OCR belirsizlikleri (düşük güven skoru, tanınamayan karakter) madde metnine eklenmez; bunun yerine ayrı bir `ocr_qa_log.json` dosyasında madde numarası ve pozisyonuyla birlikte tutulur.

---

### 5.5 `consolidator.py`

**PDF Sıralama:**
```python
def parse_date_from_filename(filename: str) -> datetime:
    match = re.search(r'(\d{2})-(\d{2})-(\d{4})', filename)
    # "01-12-2022" → datetime(2022, 12, 1)
```

**Şirket Bilgileri:** Son tarihli PDF değeri kullanılır. MERSİS no, sicil no gibi sabit alanlar için ilk değer tercih edilir.

**YK Konsolidasyonu:**
```
Kuruluş ilanı → başlangıç YK listesi
Her sonraki ilan:
  Mod tespiti:
    - İlan toplu yeni kurul listesi veriyorsa → SNAPSHOT MODE
        Mevcut liste tamamen replace edilir, yeni liste geçerli olur
    - Tekil değişiklik ilanıysa → EVENT MODE
        "seçildi / atandı" → listeye ekle (görev bitiş tarihi ile)
        "görevden ayrıldı / azledildi / istifa / görev süresi doldu" → çıkar
Son liste → aktif YK
```

**Esas Sözleşme Konsolidasyonu:**
```
Kuruluş ilanı (01-12-2022) → 16 maddenin tamamı (kaynak: bu PDF)
Değişiklik ilanları:
  → Hangi madde değişmiş? (madde başlığından tespit)
  → Eski maddeyi sil, yeni maddeyi aynı numarayla ekle
  → Kaynağı güncelle: TTSG tarih + sayı
Son durum → 16 madde, her birinin kaynağıyla
```

---

### 5.6 `docx_writer.py` — Çıktı Formatları

**A) `sirket_bilgileri.docx`**

| Alan | Değer |
|---|---|
| Ticaret Unvanı | ... |
| Şirket Türü | Anonim Şirket |
| MERSİS Numarası | ... |
| Ticaret Sicil Müdürlüğü | ... |
| Ticaret Sicil Numarası | ... |
| Adres | ... |
| Mevcut Sermaye | ... TL |
| Kuruluş Tarihi | GG.AA.YYYY |
| Denetçi | ... / Atanmamış |

---

**B) `yonetim_kurulu.docx`**

| Ad Soyad / Unvan | Görev Bitiş | TTSG Tarih / Sayı | Kaynak PDF |
|---|---|---|---|
| HASAN ELBİR | 31.12.2025 | 01.12.2022 / 10716 | [01-12-2022_Kurulus.pdf](#) |
| AYDEM HOLDİNG A.Ş. (adına hareket edecek gerçek kişi: CEYHAN SALDANLI) | 31.12.2025 | 10.10.2023 / XXXX | [10-10-2023_Yonetim.pdf](#) |

**Tüzel Kişi Format Kuralı:**
```
Gerçek kişi → Ad Soyad
Tüzel kişi  → [Şirket Adı] (adına hareket edecek gerçek kişi: [Ad Soyad])
```

**Tıklanabilir PDF Linki:** `python-docx` `add_hyperlink()` ile input/ klasöründeki PDF'e relatif path bağlantısı.

---

**C) `esas_sozlesme.docx`**
- 16 madde, **birebir OCR metni** — düzenleme/özetleme kesinlikle yok
- Her madde başlığı bold
- Her maddenin altında: `Kaynak: TTSG [GG.AA.YYYY] Sayı: [XXXX]`
- Değiştirilmiş maddeler → başlığa `[DEĞİŞTİRİLDİ]` notu eklenir

---

## 6. Halüsinasyon Kontrol Sistemi

```
3 Katmanlı Doğrulama:

Katman 1 — Regex Çapraz Doğrulama:
  MERSİS no → \d{16}
  Sermaye   → \d{1,3}(\.\d{3})* TL
  Tarih     → \d{2}\.\d{2}\.\d{4}
  LLM çıktısı regex'e uymuyorsa → [DOĞRULANAMADI]

Katman 2 — Kaynak Metin Arama:
  LLM'den gelen her string → kaynak OCR metninde aranır
  Bulunamazsa → [DOĞRULANAMADI] + hallucination_log.json

Katman 3 — Esas Sözleşme Güvencesi:
  LLM hiç kullanılmaz → halüsinasyon riski sıfır
  OCR hatası olabilir → [OCR_HATASI_OLABILIR] notu
```

`hallucination_log.json` örneği:
```json
{
  "pdf": "3__10-10-2023_Yonetim.pdf",
  "field": "board_member_term_end",
  "llm_value": "31.12.2026",
  "found_in_source": false,
  "action": "marked_as_unverified"
}
```

---

## 7. README İçeriği

```markdown
## Kurulum
pip install -r requirements.txt
apt-get install tesseract-ocr tesseract-ocr-tur poppler-utils

## Çalıştırma
python src/main.py --input input/ --output output/
python src/main.py --input input/ --output output/ --only-ocr
python src/main.py --input input/3__10-10-2023_Yonetim.pdf --output output/

## LLM Kullanımı
- Şirket bilgileri ve YK çıkarımında kullanılır
- Esas sözleşme maddeleri LLM'e gönderilmez (birebir OCR metni)
- Tahmini token: ~10.000 input / ~2.500 output
- ANTHROPIC_API_KEY environment variable gerekli

## Belge Bazlı Metin Çıkarımı Çıktısı (`extracted_texts/`)
- Sadece Hedef Şirket'e ait ilan metinleri eksiksiz ayrıştırılarak `output/extracted_texts/{dosya_adi}.txt` olarak kaydedilir.
- Aynı sayfadaki diğer şirketlere ait ilanlar filtrelenerek bu metinlerden tamamen hariç tutulur.

## Halüsinasyon Kontrolü
- 3 katmanlı doğrulama
- output/hallucination_log.json ile izlenebilirlik
- [DOĞRULANAMADI] alanlar Word'de kırmızı renk
```

---

## 8. Tahmini Token Maliyeti

| Adım | LLM? | Token (input) | Token (output) |
|---|---|---|---|
| 9 PDF × şirket bilgileri | Evet | ~4.500 | ~900 |
| 9 PDF × YK çıkarımı | Evet | ~6.000 | ~1.500 |
| Esas sözleşme maddeleri | **Hayır** | 0 | 0 |
| **Toplam** | | **~10.500** | **~2.400** |

> Esas sözleşmeyi LLM dışında bırakmak → önceki plana göre **~%35 token tasarrufu** + o modülde sıfır halüsinasyon riski

---

## 9. Geliştirme Sırası

```
Gün 1 Sabah:    pdf_reader.py (OCR primary) + testler
Gün 1 Öğleden:  filter.py (layout-aware) + testler
Gün 1 Akşam:    articles_parser.py (LLM'siz segmentasyon) + testler
Gün 2 Sabah:    extractor.py (LLM) + promptlar
Gün 2 Öğleden:  consolidator.py
Gün 2 Akşam:    docx_writer.py + 3 Word çıktısı
Gün 3 Sabah:    main.py + CLI + hallucination log
Gün 3 Öğleden:  README + GitHub + son test
```

---

## 10. Değerlendirme Kriterlerine Göre Kontrol Listesi

- [x] **Doğru Filtreleme** → `filter.py` layout-aware (koordinat + regex) ayıklama
- [x] **Güncel bilgi konsolidasyonu** → `consolidator.py` tarih sıralı birleştirme
- [x] **Kaynak izlenebilirliği** → Her çıktıda TTSG tarih/sayı + tıklanabilir PDF linki
- [x] **Halüsinasyon kontrolü** → 3 katman: regex + kaynak arama + esas sözleşmede LLM yok
- [x] **Token optimizasyonu** → Esas sözleşme LLM'den çıkarıldı, bölüm bazlı prompt
- [x] **Mimari kalitesi** → Modüler, tek sorumluluk, test edilebilir
- [x] **YK tüzel kişi formatı** → `[Şirket Adı] (adına hareket edecek gerçek kişi: [Ad Soyad])`
- [x] **YK tıklanabilir link** → `python-docx` hyperlink ile kaynak PDF bağlantısı