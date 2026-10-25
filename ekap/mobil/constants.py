"""
EKAP Mobil API sabitleri — uç yolları, istek gövdesi ve metin→kod haritaları.

⚠️ Mobil API **enum kodu değil METİN** döndürüyor (`ihaleDurumu`,
`ihaleKapsamTurUsul`). Bu dosyadaki haritalar o metinleri `ekap/constants.py`'deki
sayısal kodlara çevirir. Eşleşmeyen metin **koda çevrilmez** (bkz. `adapt.py`) —
uydurma bir kod, `DURUM_SONUCLANMIS` üzerine kurulu her şeyi (sonuçlanmış filtresi,
alarm geçişleri, tazeleme kuralı) sessizce bozardı.
"""
from ..utils import normalize_tr

# ── Uçlar ──────────────────────────────────────────────
# Tümü POST; her istekte `?api-version=1.0`.
KOK = "/KikMobilServicesApi/api/Mobil/v1"

PATH_LISTE = f"{KOK}/IhaleArama/Liste"
PATH_IHALE = f"{KOK}/IhaleArama/Ihale"
PATH_SONUC_ILANLARI = f"{KOK}/IhaleArama/SonucIlanlari/Ilan"
PATH_DOKUMAN_LISTE = f"{KOK}/IhaleArama/IhaleDokumani/Liste"
PATH_DOKUMAN_INDIR = f"{KOK}/IhaleArama/IhaleDokumani/Indir"
PATH_TEKNIK_SARTNAME = f"{KOK}/IhaleArama/TeknikSartname/Bilgiler"
# ⚠️ Teknik şartname **ihale dokümanı ZIP'inin İÇİNDE DEĞİL**, ayrı bir dosyadır
# (üretimde ölçüldü: 15 MB PDF). İndirme ucu keşif notlarında yoktu; 2026-09-10'da
# bulundu — bilinmeyen yollar `404`, bu yol CAPTCHA duvarına takıldığında `300`
# döndü, yani var. Doğrulandı: `%PDF-1.6` baytlarıyla dönüyor.
PATH_TEKNIK_SARTNAME_INDIR = f"{KOK}/IhaleArama/TeknikSartname/Indir"
PATH_IDARI_SARTNAME = f"{KOK}/IhaleArama/IdariSartname/Bilgiler"

# ⚠️ Captcha uçları `Captcha/` kökündedir, `IhaleArama/` altında DEĞİL.
PATH_CAPTCHA_GETIR = f"{KOK}/Captcha/Getir"
PATH_CAPTCHA_SONUC = f"{KOK}/Captcha/Sonuc"

API_VERSION = "1.0"

# ── Liste gövdesi ──────────────────────────────────────
# Mobil uygulamanın gönderdiğinin birebir aynısı. ⚠️ Alan çıkarmayın: eksik alan
# gönderilen isteklerde uç bazen boş liste döndürüyor.
#
# Ölçülmüş parametre davranışı (bkz. docs/ekap-mobil-api.md):
#   çalışan   : ihaleTarihiBaslangic/Bitis, ilKod, ihaleTuru, icerik, yasaKapsam, aramaTuru
#   YOK SAYILAN: ilanTarihiBaslangic/Bitis, orderBy, iknYili/iknSayi,
#                ihaleBilgiSecim/ihaleBilgiOpsiyon
LISTE_GOVDESI = {
    "iknYili": 0,
    "iknSayi": 0,
    "ihaleTuru": -1,
    "ihaleTarihiBaslangic": "",
    "ihaleTarihiBitis": "",
    "ilanTarihiBaslangic": "",
    "ilanTarihiBitis": "",
    "ilKod": 0,
    "icerik": "",
    "ihaleBilgiSecim": 0,
    "ihaleBilgiOpsiyon": 0,
    "aramaTuru": 2,
    "ihaleUsulu": 0,
    "ihaleDurumu": 0,
    "orderBy": "IKN Desc",
    "yasaKapsam": 1,
}

# ⚠️ Liste ucu istek başına en çok **250 kayıt** döndürür ve sayfalama parametresi
# YOKTUR. Tavana takılan bir sorgu sessizce kesilir → gün × tür dilimlemesi şart.
LISTE_TAVAN = 250

# İhale türü dilimleri (`ihaleTuru`) — 250 tavanını aşmanın birinci kırılımı.
IHALE_TURU_DILIMLERI = (1, 2, 3, 4)

# Mobil `ihaleTipi` == v2 `ihaleTip` (1 Mal, 2 Yapım, 3 Hizmet, 4 Danışmanlık).

# ── Metin → kod haritaları ─────────────────────────────
# Anahtarlar `normalize_tr` ile katlanmıştır (Türkçe-i güvenli).
def _n(harita):
    return {normalize_tr(k): v for k, v in harita.items()}


# `ihaleDurumu` metni → `ekap.constants.IHALE_DURUM` kodu.
DURUM_METIN = _n({
    "Taslak": 1,
    "İhale İlanı Yayımlanmış/İlansız, Katılıma Açık": 2,
    "Katılıma Açık": 2,
    "Teklif Değerlendirme": 3,
    "Değerlendirme Tamamlanmış": 4,
    "Sözleşme İmzalanmış": 5,
    "İptal Edilmiş": 6,
    "Sonuç İlanı Yayımlanmış": 15,
    # ⚠️ Ön yeterlik aşaması = **katılıma açık** (kullanıcı kararı, 2026-09-22).
    # "Belli İstekliler Arasında" usulünde ihale iki aşamalıdır: önce ön yeterlik
    # başvurusu alınır, yeterli bulunanlar teklife davet edilir. "henüz yapılmamış"
    # demek başvuruların **hâlâ alındığı** anlamına gelir → firma açısından bu ihale
    # katılıma açıktır ve arama sonuçlarında görünmelidir.
    # ⚠️ EKAP'ın bu aşama için ayrı bir sayısal kodu **arşivde hiç görülmedi**
    # (ölçüldü: v2 satırlarında "yeterlik" metni 0 kayıt) → kod türetilemiyor. Onu
    # boş bırakmak, 13 açık ihaleyi "Katılıma Açık" filtresinin dışında tutuyordu.
    # ⚠️ Bu eşleme YALNIZCA "henüz yapılmamış" hâli içindir. Ön yeterlik
    # **değerlendirmesi tamamlanmış** bir ihale artık katılıma açık DEĞİLDİR (davet
    # aşaması); o metin haritaya eklenmemeli — bilinmeyen olarak loglanıp fark
    # edilmesi doğrudur.
    "Ön yeterlik henüz yapılmamış": 2,
})

# ⚠️ Yedek eşleştirme: EKAP metni ufak farklarla döndürebiliyor (noktalama, ek).
# Sıra ÖNEMLİ — daha belirgin olan önce denenir. Yine de eşleşme yoksa kod
# yazılmaz; "bilmiyoruz" demek, yanlış kod yazmaktan iyidir.
DURUM_PARCA = [
    # ⚠️ **DAR tutulmalı**: yalnızca "henüz" içeren hâl katılıma açıktır. Geniş bir
    # ("on yeterlik", 2) girişi, ön yeterlik **değerlendirmesi tamamlanmış**
    # ihaleleri de açık gösterip filtreyi sessizce yanlışlardı.
    ("on yeterlik henuz", 2),
    ("sonuc ilani", 15),
    ("sozlesme imzalan", 5),
    ("iptal", 6),
    ("degerlendirme tamamlan", 4),
    ("teklif degerlendirme", 3),
    ("katilima acik", 2),
    ("taslak", 1),
]

# ── v2 ile BİREBİR aynı açıklama metinleri ─────────────
# ⚠️ **Mobil ham metni kullanılmaz.** Mobil "İhale İlanı Yayımlanmış/İlansız,
# Katılıma Açık" derken v2 "İhale İlanı Yayımlanmış, Katılıma Açık" diyor; mobil
# uygulama bu metni **doğrudan gösteriyor**, dolayısıyla kaynağa göre değişmesi
# kullanıcıya görünen bir tutarsızlıktır (üretimde bildirildi 2026-09-10).
# Aşağıdaki değerler üretim DB'sindeki v2 satırlarından **sayılarak** alındı.
DURUM_ACIKLAMA = {
    1: "İhale Onayı Verilmemiş",
    2: "İhale İlanı Yayımlanmış, Katılıma Açık",
    3: "İhale Tekliflere Kapalı, Teklifler Değerlendiriliyor",
    4: "Teklif Değerlendirme Tamamlanmış",
    5: "Sözleşme İmzalanmış",
    6: "İhale İptal Edilmiş",
    10: "İhale İptal Edilmiş",
    15: "Sonuç İlanı Yayımlanmış",
    20: "Sözleşme İmzalanmış",
}
TIP_ACIKLAMA = {1: "Mal", 2: "Yapım", 3: "Hizmet", 4: "Danışmanlık"}
USUL_ACIKLAMA = {
    1: "İhale Usulü: Açık",
    2: "İhale Usulü: Belli İstekliler Arasında",
    3: "İhale Usulü: Pazarlık",
    4: "İhale Usulü: Doğrudan Temin",
}
KAPSAM_ACIKLAMA = {1: "4734 Kapsamında", 2: "İstisna", 3: "Kapsam Dışı"}

# `ihaleKapsamTurUsul` üçlüsünün parçaları:
#   "4734 Kapsamında - Mal - Açık"  ·  "İstisna - Hizmet - 4734 / 3-g"
# ⚠️⚠️ **Kodlar ÜRETİM VERİSİNDEN alındı, model yorumundan DEĞİL.**
# `Tender.yasa_kapsami` yorumu "1=4734, 2=Dışı, 3=İstisna" diyor ama gerçek dağılım
# (1.050.157 ihale) bunun tersini gösteriyor:
#     1 → "4734 Kapsamında"  869.109  (%82,8)
#     2 → "İstisna"          178.120  (%17,0)
#     3 → "Kapsam Dışı"        2.736  (%0,3)
# İlk sürümde 2 ile 3 yer değiştirmişti; mobil kaynaklı kayıtlar v2 ile **farklı
# kod** yazıyordu ve `yasa_kapsami` filtresi iki kaynağı karıştırıyordu.
KAPSAM_METIN = _n({
    "4734 Kapsamında": 1,
    "İstisna": 2,
    "Kapsam Dışı": 3,
    "4734 Kapsamı Dışında": 3,
})
TUR_METIN = _n({
    "Mal": 1, "Mal Alımı": 1,
    "Yapım": 2, "Yapım İşi": 2,
    "Hizmet": 3, "Hizmet Alımı": 3,
    "Danışmanlık": 4, "Danışmanlık Hizmet Alımı": 4,
})
USUL_METIN = _n({
    "Açık": 1,
    "Belli İstekliler": 2, "Belli İstekliler Arasında İhale Usulü": 2,
    "Pazarlık": 3, "Pazarlık Usulü": 3,
    "Doğrudan Temin": 4,
})


# ══ Doğrudan Temin (DT) ════════════════════════════════
# Uçlar EKAP iOS 2.2.0 trafiğinden alındı (mitm, 2026-10-25). ⚠️⚠️ Bu ağaçta
# **yol tahmin edilmez**: bilinmeyen `DogrudanTemin/*` yolu 404 değil F5 WAF
# **406 + HTML engel sayfası** döndürüyor (3 tahminde ölçüldü) — WAF ihlali sayılıyor.
DT_KOK = f"{KOK}/DogrudanTemin"
PATH_DT_ENUM = f"{DT_KOK}/FiltreEnumlari"
PATH_DT_LISTE = f"{DT_KOK}/Liste"
# ⚠️ Detay ve doküman uçları `?api-version` TAŞIMAZ (uygulama göndermiyor).
PATH_DT_DETAY = f"{DT_KOK}/DogrudanTemin"
# DT dokümanı: tek dosya, tek istek (ihale dokümanının Liste+Indir zinciri yok).
PATH_DT_DOKUMAN = f"{DT_KOK}/TeknikSartnameIndir"
# Birim fiyat teklif cetveli eski EKAP portalından iner (mobil API değil).
DT_BIRIM_FIYAT_URL = (
    "https://ekap.kik.gov.tr/EKAP/YeniIhaleArama.aspx"
    "?qs=1&birimFiyatCetvelIndir=1&dogrudanTeminId={enc_id}"
)

# Liste gövdesi — uygulamanın gönderdiğinin birebir aynısı (23 alan).
# ⚠️ Alan çıkarmayın (ihale listesindeki "eksik alan → boş liste" dersi).
# Ölçülmüş davranış (2026-10-25):
#   çalışan : dogrudanTeminDurumu (202 = açık), ilanTarihiBaslangic/Bitis
#             (dd.mm.yyyy — ihale listesinin AKSİNE çalışıyor), topRowCnt (250),
#             pageIndex (sayfalama VAR), orderBy (10 = dtn azalan)
#   yanıt   : {dtBilgiListesi, kod, mesaj, sonuc} — totalCount YOK
DT_LISTE_GOVDESI = {
    "arananIfade": "",
    "dogrudanTeminIstisnaAliminDayanagi": "",
    "dogrudanTeminKapsami": "101",
    "dogrudanTeminKayitTarihiBaslangic": "",
    "dogrudanTeminKayitTarihiBitis": "",
    "dogrudanTeminTarihiBaslangic": "",
    "dogrudanTeminTarihiBitis": "",
    "dogrudanTeminTuru": "",
    "dogrudanTeminYasaKapsamiTeminMadde": "",
    "dtnSayi": "",
    "dtnYil": "",
    "idareID": "",
    "enUstIdareKod": "",
    "ilanTarihiBaslangic": "",
    "ilanTarihiBitis": "",
    "ilanTipi": 0,
    "ilId": "",
    "okasListesi": "",
    "orderBy": 10,
    "pageIndex": 1,
    "topRowCnt": 10,
    "ustIdareKod": "",
    "dogrudanTeminDurumu": 0,
}
DT_SAYFA = 250
DT_ORDER_DTN_AZALAN = 10

# `FiltreEnumlari.dtDurumlari` — liste satırı durumu METİN olarak verir.
DT_DURUM_ACIK = 202
DT_DURUM = {
    202: "Doğrudan Temin Duyurusu Yayımlanmış",
    3: "Teklifler Değerlendiriliyor",
    4: "Doğrudan Temin Sonuçlandırıldı",
    5: "Sonuç Bilgileri Gönderildi",
    15: "Sonuç Duyurusu Yayımlanmış",
}
DT_DURUM_METIN = _n({v: k for k, v in DT_DURUM.items()})
DT_TUR = {1: "Mal", 2: "Yapım", 3: "Hizmet", 4: "Danışmanlık"}
DT_TUR_METIN = _n({v: k for k, v in DT_TUR.items()})
DT_KAPSAM = {101: "4734 Kapsamında", 102: "İstisna", 103: "Kapsam Dışı"}
# Detay "Doğrudan Temin 4734 Kapsamında" de diyebiliyor → önek atılıp eşlenir.
DT_KAPSAM_METIN = _n({v: k for k, v in DT_KAPSAM.items()})
DT_MADDE = {
    101: "22-a", 102: "22-b", 103: "22-c",
    104: "22-d* (Parasal Limit Kapsamında)",
    105: "22-d** (Temsil Ağırlama Kapsamında)",
    106: "22-e", 107: "22-f", 108: "22-g", 109: "22-h", 110: "22-ı", 111: "22-i",
}
# ⚠️ `normalize_tr` "22-ı" ile "22-i"yi AYNI dizgeye katlar → madde haritası
# katlanmadan, birebir metinle kurulur (yanlış madde kodu sessizce filtre bozardı).
DT_MADDE_METIN = {v: k for k, v in DT_MADDE.items()}

# ══ KİK Kurul Karar Tutanakları ════════════════════════
# ⚠️ Yazım EKAP'ınki: liste/tarih ucu "Tutunaklari", detay ucu "Tutanaklari".
PATH_KURUL_TARIHLER = f"{KOK}/KurulKararTutunaklari/Tarihler"
PATH_KURUL_LISTE = f"{KOK}/KurulKararTutunaklari/Liste"
# `kararNitelikKod`: 2 = Uyuşmazlık (UM/UH/UY), 3 = Düzenleyici (DK), 5 = Mahkeme (MK).
# Yalnızca uyuşmazlık kararları İKN taşıyor (3/5'te ikn null).
KURUL_NITELIK_UYUSMAZLIK = 2
