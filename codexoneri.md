Mistral OCR Çıktısının LexNorm Case’ine Göre Değerlendirilmesi ve Aksiyon Planı
Özet
Mevcut durumda Mistral OCR çıktısı case’in en kritik şartını ihlal ediyor: belgede yer aldığı şekliyle birebir aktarım ve sıfır halüsinasyon. Örnek çıktıdaki Eidroelektrik santrali ifadesi büyük olasılıkla Hidroelektrik santrali olmalı; ayrıca rüzgar tribünleri de muhtemelen rüzgar tribünleri/türbinleri benzeri bir OCR sapması içeriyor. Bu tip hata, sadece kozmetik bir OCR problemi değil; case kapsamında doğrudan ürün başarısızlığıdır, çünkü:

esas sözleşme maddeleri birebir aktarılmalı,
hedef şirket metni eksiksiz ve doğru çıkarılmalı,
yanlış OCR metni şu an “doğru kaynak metin” gibi downstream katmanlara taşınıyor.
Mevcut pipeline mimarisi OCR hatasını tespit etmekte zayıf, ama hatalı metni durdurmakta daha da zayıf. En büyük sorun, sistemin “confidence/audit” üretmesine rağmen doğrulanmamış metni yine de nihai çıktıya koymasıdır.

Mevcut Durum Analizi
1. Case’e göre mevcut sonuç neden yetersiz
Case dokümanındaki ana beklentiler:

yalnızca hedef şirkete ait ilanların eksiksiz çıkarılması,
güncel bilgilerin doğru konsolide edilmesi,
esas sözleşmenin birebir, güncel ve kaynaklı verilmesi,
sıfır halüsinasyon.
Bu beklentilere karşı mevcut örnek çıktı şu nedenle riskli:

OCR hatası madde içeriğinin kendisinde.
Sistem metni normalize etmiyor ama doğrulamadan da yayımlıyor.
“OCR QA” yalnızca karakter seviyesi bazı anomalileri kontrol ediyor; semantik yanlış kelimeyi yakalamıyor.
yanlış OCR ile çıkarılan esas sözleşme maddesi, case’te istenen “birebir doğru metin” yerine “birebir yanlış OCR” oluyor.
2. Kod seviyesinde temel zafiyetler
main.py
Pipeline, filter_result.status != ok olsa bile extraction ve consolidation’a devam ediyor.
Bu, partial veya unsafe extraction’ların nihai DOCX’e girmesine izin veriyor.
filter.py
Filtreleme sadece hedef şirket sınırını bulmaya odaklı.
Boundary çözülemezse unsafe üretiyor, fakat bu sonuç downstream’de bloklayıcı değil.
Yani “güvensiz metin” ile üretim devam ediyor.
articles_parser.py
QA kontrolleri çok yüzeysel:
madde sayısı,
sıra,
çok kısa içerik,
tanınmayan karakter.
Ama Eidroelektrik gibi gerçek kelimeye benzeyen OCR bozulmalarını yakalayamıyor.
Bu yüzden “parse oldu” sanılan metin case açısından yine başarısız olabilir.
extractor.py
Şirket bilgileri ve kurul üyeleri için rule-based + LLM fallback var.
verify_against_source, LLM çıktısını sadece OCR metninin içinde geçiyor mu diye kontrol ediyor.
Bu mekanizma gerçek kaynağa değil, zaten hatalı olabilecek OCR metnine doğrulama yapıyor.
Sonuç: “hallucination kontrolü” var gibi görünüyor, fakat gerçekte OCR-source-grounded, document-truth-grounded değil.
3. Kök neden
Asıl problem Mistral kullanılması değil; single-source OCR truth varsayımı. Şu an sistem:

Mistral metnini alıyor,
hedef şirketi filtreliyor,
bu metni canonical source kabul ediyor,
rule-based/LLM extraction’ı bunun üstünde yapıyor.
Bu yaklaşım, OCR düşük hata oranlı bile olsa case için yetmez. Çünkü burada başarı metriği “yaklaşık doğru” değil, hukuki metin doğruluğu.

Karar
Mistral mevcut mimaride tek başına yeterli kabul edilmemeli. Primary OCR olabilir, ama authoritative source olamaz. Case’e uygun çözüm, Mistral-first, verification-gated, multi-evidence extraction yaklaşımı olmalı.

Uygulanacak Yaklaşım
1. Nihai çıktı üretmeden önce doğrulama kapısı eklenmeli
Sistem şu kuralı uygulamalı:

ok olmayan filter sonucu ile nihai çıktı üretilmemeli.
kuruluş ilanı ve esas sözleşme değişiklik ilanları için madde bazlı güven skoru zorunlu olmalı.
düşük güvenli madde varsa DOCX üretimi:
ya bloklanmalı,
ya da belge “manual review required” statüsüne alınmalı.
Varsayılan tercih:

Case gereği bloklayıcı davranış önerilir. Sessizce yanlış çıktı vermek kabul edilemez.
2. OCR çapraz doğrulama katmanı eklenmeli
Her kritik belge için iki bağımsız metin çıkarımı tutulmalı:

Mistral OCR
Tesseract OCR
Madde/paragraf bazında karşılaştırma yapılmalı.

Kurallar:

İki OCR aynıysa yüksek güven.
Ufak whitespace/punctuation farkı varsa normalize edilmiş eşleşme ile kabul.
Kelime farkı varsa madde “review_required”.
Özellikle kuruluş ilanı ve esas sözleşme değişikliklerinde bu kural zorunlu.
Bu sayede Eidroelektrik gibi tek OCR kaynağına özgü bozulmalar işaretlenebilir.

3. Görselden hedefli re-OCR / snippet doğrulama yapılmalı
Tam belge OCR yerine kritik alanlar için ikinci aşama çalışmalı:

ticaret unvanı
adres
MERSİS
ticaret sicil no
sermaye maddesi
denetçi bilgisi
esas sözleşme maddeleri
yönetim kurulu atama/görev bitiş cümleleri
Akış:

Filtrelenen metinden aday span bulunur.
İlgili span’in sayfa ve yaklaşık konumu belirlenir.
Sayfadan crop/snippet alınır.
Snippet üzerinde ikinci OCR veya vision doğrulama yapılır.
Son metin ancak bu kontrol geçerse authoritative kabul edilir.
Not:

Bu katman özellikle “birebir esas sözleşme” için gereklidir.
Tüm belgeye değil sadece kritik alanlara uygulanacağı için token/maliyet yönetilebilir.
4. “Doğrulanamayanı yazma” politikası getirilmeli
Şu an sistem bazı yerlerde [DOĞRULANAMADI] işaretli değer üretebiliyor. Case açısından bu yaklaşım sınırlı kabul edilebilir, ama nihai teslim dosyalarında hukuki metin içine karışmamalı.

Politika:

esas sözleşme maddelerinde doğrulanamayan hiçbir kelime yayınlanmamalı,
şirket bilgileri tablosunda doğrulanamayan alan boş bırakılmalı ve audit’e düşmeli,
yönetim kurulu tablosunda doğrulanamayan kayıt nihai tabloya alınmamalı; review queue’ya atılmalı.
5. OCR anomaly detection daha akıllı hale getirilmeli
Yeni QA kontrolleri eklenmeli:

sözlük/domain lexicon tabanlı uyarılar:
hidroelektrik
rüzgar
türbin/türbini/türbinleri
güneş enerjisi
TETAŞ, TEDAŞ, TEİAŞ
şirket hukuku kalıpları
yakın eşleşme alarmı:
eidroelektrik -> hidroelektrik
eneryisi -> enerjisi
tribünleri / tribüsleri gibi sapmalar
madde bazlı OCR disagreement skoru
beklenen hukuki kalıp kontrolleri
sayı/tarih/para formatı anomaly kontrolü
Bu katman düzeltme yapmak için değil, risk işaretlemek için kullanılmalı. Otomatik düzeltme yalnızca görüntü doğrulaması sonrası yapılmalı.

6. LLM kullanımının rolü yeniden tanımlanmalı
Mevcut fallback mantığı case için riskli. LLM şu işlerde kullanılmalı:

alan adaylarını önermek,
iki OCR arasındaki farkları sınıflandırmak,
review önceliği vermek.
LLM şu işlerde kullanılmamalı:

esas sözleşme metnini “tamamlama”
OCR hatalı kelimeyi görüntü görmeden tahmin ederek düzeltme
kaynakta net olmayan yönetim kurulu kaydını uydurma
Temel ilke:

LLM extract edebilir, ama yalnızca source-backed value publish edilebilir.
7. Çıktı modeli iki katmanlı olmalı
Nihai output’a ek olarak makine okunur audit üretilmeli.

Önerilen yeni output’lar:

*.txt
extraction_audit.json
review_queue.json
field_confidence.json
article_comparison.json
review_queue.json içinde:

belge
madde/alan adı
mistral text
secondary OCR text
disagreement type
source page
action recommendation
Bu, kaynak izlenebilirliği ve manuel doğrulama akışını netleştirir.

Public API / Arayüz Değişiklikleri
CLI’ye şu opsiyonlar eklenmeli:

--verification-ocr-provider {tesseract}
--strict
doğrulanamayan kritik içerikte nihai çıktı üretimini durdurur
--emit-review-queue
--verify-critical-fields-only
--fail-on-unsafe-filter
Yeni veri yapıları:

VerifiedSpan

field_name
pdf
page
primary_text
secondary_text
final_text
status (verified, disputed, unverified)
evidence
ReviewQueueEntry

pdf
section_type
identifier
page
primary_ocr
secondary_ocr
reason
recommended_action
ArticleVerificationResult

madde_no
status
disagreement_score
verified_text
sources
Test Senaryoları
Aşağıdaki testler eklenmeli:

Kuruluş ilanında Hidroelektrik kelimesi Mistral’da bozulup Tesseract’ta doğruysa:

madde disputed işaretlenmeli
strict modda esas sözleşme DOCX üretilmemeli
Filter sonucu unsafe ise:

--fail-on-unsafe-filter ile pipeline fail etmeli
İki OCR aynı maddeyi aynı veriyorsa:

madde verified olmalı
normal çıktıya girmeli
Şirket bilgisi alanı yalnızca LLM ile bulunuyor ama OCR source’da doğrulanamıyorsa:

tablo alanı boş kalmalı
audit’e issue düşmeli
Yönetim kurulu üyesi adı iki OCR arasında ayrışıyorsa:

nihai tabloya alınmamalı
review queue’ya gitmeli
Esas sözleşme değişiklik ilanında yalnız değişen maddeler varsa:

yalnız o maddeler update edilmeli
her madde için kaynak ilan tarihi korunmalı
Varsayımlar ve Seçilen Varsayılanlar
Varsayılan birincil OCR mistral olarak kalır.
İkincil doğrulama OCR varsayılanı tesseract olur.
Kuruluş ilanı ve esas sözleşme değişiklikleri “kritik belge” kabul edilir.
strict mod üretim ortamında varsayılan olmalıdır.
Amaç otomatik düzeltme değil, doğrulanmış metin üretmektir.
Manual review kabul edilebilir; sessiz yanlış çıktı kabul edilemez.
Nihai Değerlendirme
Bu case’e göre mevcut çözüm “iyi bir OCR-first prototip”, ama henüz “teslim edilebilir hukuk-tech extraction sistemi” değil. En kritik eksik, OCR kalitesinden çok verification gate eksikliği. Mistral’ı bırakmak zorunlu değil; fakat Mistral çıktısını tek doğru kaynak gibi kullanmak bırakılmalı.

Doğru aksiyon sırası:

unsafe/partial metni blokla
kritik alanlar için çift OCR doğrulaması ekle
esas sözleşme maddelerini verification-gated hale getir
doğrulanamayan veriyi nihai çıktıya sokma
review queue ve evidence tabanlı audit üret
LLM’yi sadece yardımcı, asla authoritative olmayan katmana indir
Bu değişiklikler yapılmadan örnekteki gibi Eidroelektrik hataları case değerlendirmesinde doğrudan eksi yazar; özellikle birebir metin, halüsinasyon kontrolü, kaynak izlenebilirliği ve mimari tasarım kalitesi başlıklarında.