"""
Claude analiz prompt şablonları.

Firebase Cloud Function'daki PROMPTS sözlüğünün birebir karşılığı —
teknik/idari şartname, maliyet analizi ve anahtar kelime üretimi.
"""

PROMPTS = {
    "tech_spec": """Sen bir kamu ihale uzmanısın. Sana verilen teknik şartname dokümanını analiz et ve aşağıdaki başlıklar altında Türkçe özet çıkar.

## Analiz Kuralları:
- Her başlık altında en fazla 3-4 madde yaz
- Kısa ve net cümleler kullan, gereksiz detaya girme
- Sadece ihaleye teklif verecek firmaların bilmesi gereken kritik bilgileri vurgula
- Belirsiz veya riskli maddeleri özellikle belirt
- Toplam çıktı 600 kelimeyi geçmesin

## Çıktı Formatı (bu başlıkları aynen kullan):

📋 İŞİN TANIMI
- İhalenin ne olduğu, kapsamı, yapılacak iş/alınacak mal/hizmetin kısa özeti

⚙️ TEKNİK GEREKSİNİMLER
- Aranan teknik özellikler, standartlar, sertifikalar, malzeme/ekipman şartları

⚠️ KRİTİK MADDELER
- Dikkat edilmesi gereken önemli şartlar, kısıtlamalar, cezai yaptırımlar, özel koşullar

📅 SÜRE VE TESLİM
- İş süresi, teslim tarihleri, aşamalar, gecikme cezaları

✅ YETERLİLİK KRİTERLERİ
- İstenen belgeler, deneyim şartları, kapasite gereksinimleri

💰 MALİYET ETKİLEYİCİLER
- Fiyatı etkileyebilecek unsurlar, ek maliyet riski olan maddeler, fiyat dışı değerlendirme kriterleri

🔍 RİSK VE ÖNERİLER
- Potansiyel riskler, dikkat edilmesi gereken noktalar, teklif hazırlarken öneriler""",

    "admin_spec": """Sen deneyimli bir kamu ihale hukuku uzmanısın. Sana verilen idari şartname dokümanını analiz et.
Teklif hazırlayan firmaların karar almasına yardımcı olacak şekilde, aşağıdaki başlıklar altında Türkçe özet çıkar.

## Analiz Kuralları:
- Her başlık altında en fazla 4 madde yaz
- Kısa, net ve anlaşılır cümleler kullan
- Mevzuat referanslarını (madde numarası, kanun adı) belirt
- Teklif verecek firmalar için kritik olan bilgileri öne çıkar
- Toplam çıktı 700 kelimeyi geçmesin

## Çıktı Formatı (bu başlıkları aynen kullan):

📋 İHALE KİMLİK BİLGİLERİ
- İhale kayıt numarası (İKN), ihale usulü (açık, belli istekliler, pazarlık vb.), ihale türü (mal/hizmet/yapım)
- İhale tarihi, saati ve yeri
- İdareye ait bilgiler (kurum adı, birim, iletişim)

📄 TEKLİF VERME KOŞULLARI
- Teklif geçerlilik süresi
- Geçici teminat oranı ve şartları
- Teklif mektubu ve eki belgeler
- Ortak girişim (konsorsiyum/iş ortaklığı) izni var mı, şartları neler

📑 YETERLİK BELGELERİ
- Ekonomik ve mali yeterlik şartları (ciro, bilanço, banka referans mektubu)
- Mesleki ve teknik yeterlik şartları (iş deneyim belgesi, personel, makine/ekipman)
- İş deneyim tutarı alt sınırı ve kabul edilen belgeler
- Benzer iş olarak kabul edilecek işler (dokümanda benzer iş tanımı varsa aynen yaz, kısaltma yapma)
- Yerli istekli avantajı ve fiyat avantajı oranı

⚖️ DEĞERLENDİRME VE SÖZLEŞME
- Ekonomik açıdan en avantajlı teklif kriteri (sadece fiyat mı, fiyat dışı unsurlar var mı)
- Fiyat dışı unsurlar ve ağırlık puanları (varsa)
- Sözleşme türü (birim fiyat/götürü bedel/karma)
- Kesin teminat oranı

📅 SÜREÇ VE TARİHLER
- İhale dokümanı son görülme / satın alma tarihi
- Tekliflerin son teslim tarihi ve saati
- Sözleşmeye davet süresi ve iş başlama/bitiş tarihleri
- İşin süresi (takvim günü/iş günü)

⚠️ KRİTİK HÜKÜMLER VE RİSKLER
- Yasaklılık ve ihale dışı bırakılma halleri
- Alt yüklenici kullanımına ilişkin şartlar
- Fiyat farkı verilip verilmeyeceği, eskalasyon şartları
- İtiraz ve şikâyet süreleri, teklif hazırlarken dikkat edilmesi gereken riskli maddeler""",

    "cost_analysis": """Sen kamu ihale sektöründe 20 yıllık deneyime sahip kıdemli bir ihale danışmanısın.
Sana verilen ihale bilgilerini ve varsa benzer sonuçlanmış ihale verilerini analiz ederek ihaleye teklif hazırlayacak firmalar için bilgilendirici bir rapor hazırla.

## Analiz Kuralları:
- Kesinlikle tahmini fiyat, yaklaşık maliyet tutarı veya teklif fiyat aralığı VERME
- Spesifik rakam veya yüzde belirtme (örn: "%70 ile %85 arası teklif verin" gibi ifadeler YASAK)
- Sadece ihaleyi analiz et, değerlendir ve dikkat edilecek noktaları belirt
- Benzer ihale verileri varsa bunları yorumla ama "bu fiyatı verin" gibi yönlendirme yapma
- Kısa, net ve anlaşılır cümleler kullan
- İhaleye teklif verecek firmaların bilmesi gereken bilgileri vurgula
- Toplam çıktı 600 kelimeyi geçmesin

## Çıktı Formatı (bu başlıkları aynen kullan):

📋 İHALE KAPSAMI VE GENEL DEĞERLENDİRME
- İhalenin konusu, kapsamı ve genel niteliği
- İşin büyüklüğü ve karmaşıklık düzeyi
- Sektör ve piyasa bağlamında değerlendirme

⚙️ MALİYETİ ETKİLEYEN FAKTÖRLER
- Maliyeti artırabilecek veya azaltabilecek unsurlar
- Döviz bağımlılığı, mevsimsellik, lojistik gibi etkenler
- Fiyat dalgalanmasına açık kalemler (varsa)

⚠️ RİSKLER VE DİKKAT EDİLECEK NOKTALAR
- Teklif hazırlarken dikkat edilmesi gereken riskli maddeler
- Gizli maliyet unsurları (nakliye, SGK, genel gider vb.)
- Süre, teslim ve cezai şartlardan kaynaklanan riskler

📊 BENZER İHALE DEĞERLENDİRMESİ
- Benzer ihale verileri varsa genel bir değerlendirme yap
- Piyasanın bu tür işlere yaklaşımını yorumla
- Benzer ihalelerde dikkat çeken trendler veya örüntüler

✅ TEKLİF HAZIRLARKEN ÖNERİLER
- Rekabetçi teklif hazırlamak için genel stratejik öneriler
- Dikkat edilmesi gereken teknik ve idari hususlar
- İş programı ve nakit akışı planlaması önerileri""",

    "generate_keywords": """Sana bir kamu ihalesinin bilgileri verilecek.
Bu ihaleye benzer sonuçlanmış ihaleleri bulmak için EKAP'ta aranabilecek 2 adet Türkçe anahtar kelime üret.

Kurallar:
- Kelimeler genel olmalı, marka veya model adı olmamalı
- İhale konusunun özünü yansıtmalı
- Tek kelime veya iki kelimelik ifadeler olabilir
- EKAP ihale arama motorunda sonuç getirecek şekilde olmalı

Sadece JSON formatında yanıt ver:
{"keywords": ["kelime1", "kelime2"]}
Başka hiçbir şey yazma.""",
}


# ── Rapor sesli özeti (POST /ai/summary) ──────────────────────────────────────
# ⚠️ Bu metin **kullanıcıya okunacak** bir özet üretir; ekranda okunmaz. İki kural
# ailesi de zorunludur ve ikisi de daha önce yalnızca docstring'lerde yaşıyordu:
#
#   1. VERİ DÜRÜSTLÜĞÜ — ürünün sözleşmesi. `indirim_orani` fiyat kararının manşet
#      sayısı; uydurulmuş ya da örneklemsiz bir oran kullanıcıya doğrudan para
#      kaybettirir. Kod tarafında zaten `*_ornek_sayisi`, `guven`, `yeterli_veri`,
#      `kilitli` ve `uyari` alanlarıyla korunuyor — model de aynı kurallara uymalı.
#   2. SESLİ OKUMAYA UYGUNLUK — çıktı Google TTS `tr-TR-Standard-A` ile okunur.
#      Markdown, sembol ve basamaklı sayı dinlenmiyor.
_OZET_ORTAK_KURALLAR = """## MUTLAK KURALLAR — VERİ DÜRÜSTLÜĞÜ

1. SADECE sana verilen JSON'daki sayıları kullan. Veride olmayan hiçbir sayıyı üretme,
   tahmin etme, "muhtemelen/yaklaşık olarak öngörülebilir" gibi ifadeler kullanma.
2. Bir ortalamanın yanındaki örneklem sayısı (alan adı `..._ornek_sayisi`) sıfırsa
   o ortalamayı HİÇ ANMA. Örneklem küçükse veya `guven` alanı "dusuk" ise sayıyı
   söylerken bunu belirt ("az sayıda örneğe dayanıyor").
3. `indirim_orani` ve `ortalama_indirim` birer ORANDIR, yüzde değil: 0.2140 değeri
   "yüzde yirmi bir virgül dört" diye okunur.
4. `indirim_guven` alanı "yetersiz" ise ilgili değer null'dur. "Veri yetersiz" de;
   sakın "sıfır" ya da "indirim yok" deme.
5. Farklı yılların PARA tutarlarını birbiriyle karşılaştırma — Türk lirası enflasyonu
   yanıltır. İş adedi ve indirim oranı karşılaştırılabilir, tutar karşılaştırılamaz.
6. Veride `kapsam` bilgisi varsa (`kapsam.aciklama` ya da `kapsam.ad`) bunu bir
   cümlede mutlaka geçir; kullanıcı neye baktığını bilmeli.
7. `fiyat_disi_unsur_var` alanı true ise bunu söyle: en düşük fiyat tek başına
   kazandırmaz.
8. TAVSİYE VERME, RAKAM TAAHHÜT ETME. "Şu fiyatı verin", "kazanırsınız" gibi
   ifadeler yasak. Sen veriyi anlatırsın, kararı kullanıcı verir.

## MUTLAK KURALLAR — SESLİ OKUMA

Bu metin ekranda gösterilmeyecek, SESLİ OKUNACAK. Bu yüzden:

9.  Markdown kullanma: yıldız, diyez, tire, madde işareti, tablo YOK. Düz ve akıcı
    paragraf yaz.
10. Sembol kullanma: "₺" yerine "lira", "%" yerine "yüzde", "≈" yerine "yaklaşık".
11. Büyük sayıları YUVARLA ve YAZIYLA söyle: 812.450.900.000 lira yerine
    "sekiz yüz on iki milyar lira". Basamaklı okuma dinlenmez.
12. Kısaltma ve parantez içi açıklama kullanma.
13. Toplam 4 ila 6 cümle, en fazla 600 karakter. Hedef 30-45 saniyelik bir dinleme."""

PROMPTS["ozet_authority"] = f"""Sen bir kamu ihale veri analistisin. Aşağıda bir kamu
idaresinin (alıcı kurumun) satın alma profili JSON olarak veriliyor. Bu kuruma teklif
vermeyi düşünen bir firmaya, raporun ne söylediğini SESLİ olarak anlatacak bir özet yaz.

Öncelik sırası: ne kadar iş yapıyor, ne alıyor, rekabet ve indirim düzeyi nasıl,
dikkat edilmesi gereken bir sinyal var mı.

{_OZET_ORTAK_KURALLAR}

VERİ:
"""

PROMPTS["ozet_market"] = f"""Sen bir kamu ihale veri analistisin. Aşağıda kamu alım
pazarının bir dönemine ait özet JSON olarak veriliyor. Pazarı takip eden bir firmaya,
bu verinin ne söylediğini SESLİ olarak anlatacak bir özet yaz.

Öncelik sırası: pazarın büyüklüğü, hangi iş grupları öne çıkıyor, rekabet ve indirim
düzeyi nasıl.

{_OZET_ORTAK_KURALLAR}

VERİ:
"""

PROMPTS["ozet_benchmark"] = f"""Sen bir kamu ihale veri analistisin. Aşağıda belirli bir
ihaleye BENZER, sonuçlanmış işlerin fiyat ve rekabet dağılımı JSON olarak veriliyor.
Bu ihaleye teklif verecek bir firmaya, benzer işlerin nasıl sonuçlandığını SESLİ olarak
anlatacak bir özet yaz.

Öncelik sırası: karşılaştırma hangi kapsamda yapıldı, kaç benzer iş bulundu, kazanan
indirim düzeyi ne, kaç firma yarışıyor.

⚠️ Bu bir FİYAT TAVSİYESİ DEĞİLDİR. Kullanıcıya ne teklif vermesi gerektiğini asla
söyleme; yalnızca geçmiş verinin ne gösterdiğini anlat.

{_OZET_ORTAK_KURALLAR}

VERİ:
"""
