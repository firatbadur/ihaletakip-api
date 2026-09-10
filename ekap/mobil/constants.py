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
})

# ⚠️ Yedek eşleştirme: EKAP metni ufak farklarla döndürebiliyor (noktalama, ek).
# Sıra ÖNEMLİ — daha belirgin olan önce denenir. Yine de eşleşme yoksa kod
# yazılmaz; "bilmiyoruz" demek, yanlış kod yazmaktan iyidir.
DURUM_PARCA = [
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
KAPSAM_ACIKLAMA = {1: "4734 Kapsamında", 2: "Kapsam Dışı", 3: "İstisna"}

# `ihaleKapsamTurUsul` üçlüsünün parçaları:
#   "4734 Kapsamında - Mal - Açık"  ·  "İstisna - Hizmet - 4734 / 3-g"
KAPSAM_METIN = _n({
    "4734 Kapsamında": 1,
    "4734 Kapsamı Dışında": 2,
    "Kapsam Dışı": 2,
    "İstisna": 3,
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
