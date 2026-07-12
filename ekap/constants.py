"""
EKAP sabitleri — DEFAULT_SEARCH_BODY, id→isim eşlemeleri, CITIES seed verisi.

Kaynak: mobil `src/api/v1/api.js` (DEFAULT_SEARCH_BODY) ve `src/api/filterData.js`.
"""

# ── EKAP v2 arama gövdesi şablonu (api.js:8-56 birebir) ─
DEFAULT_SEARCH_BODY = {
    "searchText": "",
    "filterType": None,
    "ikNdeAra": True,
    "ihaleAdindaAra": True,
    "searchType": "GirdigimGibi",
    "iknYili": None,
    "iknSayi": None,
    "ihaleTarihSaatBaslangic": None,
    "ihaleTarihSaatBitis": None,
    "ilanTarihSaatBaslangic": None,
    "ilanTarihSaatBitis": None,
    "yasaKapsami4734List": [],
    "ihaleTuruIdList": [],
    "ihaleUsulIdList": [],
    "ihaleUsulAltIdList": [],
    "ihaleIlIdList": [],
    "ihaleDurumIdList": [],
    "ihaleIlanTuruIdList": [],
    "teklifTuruIdList": [],
    "asiriDusukTeklifIdList": [],
    "istisnaMaddeIdList": [],
    "okasBransKodList": [],
    "okasBransAdiList": [],
    "titubbKodList": [],
    "gmdnKodList": [],
    "idareKodList": [],
    "eIhale": None,
    "eEksiltmeYapilacakMi": None,
    "ortakAlimMi": None,
    "kismiTeklifMi": None,
    "fiyatDisiUnsurVarmi": None,
    "ekonomikVeMaliYeterlilikBelgeleriIsteniyorMu": None,
    "meslekiTeknikYeterlilikBelgeleriIsteniyorMu": None,
    "isDeneyimiGosterenBelgelerIsteniyorMu": None,
    "yerliIstekliyeFiyatAvantajiUgulaniyorMu": None,
    "yabanciIsteklilereIzinVeriliyorMu": None,
    "alternatifTeklifVerilebilirMi": None,
    "konsorsiyumKatilabilirMi": None,
    "altYukleniciCalistirilabilirMi": None,
    "fiyatFarkiVerilecekMi": None,
    "avansVerilecekMi": None,
    "cerceveAnlasmaMi": None,
    "personelCalistirilmasinaDayaliMi": None,
    "orderBy": "ihaleTarihi",
    "siralamaTipi": "asc",
    "paginationSkip": 0,
    "paginationTake": 10,
}

# ── id → isim eşlemeleri (constants/maps.js) ────────────
IHALE_TURU = {1: "Mal Alımı", 2: "Yapım", 3: "Hizmet", 4: "Danışmanlık"}

IHALE_USUL = {
    1: "Açık İhale Usulü",
    2: "Belli İstekliler Arasında",
    3: "Pazarlık (MD 21 F)",
    4: "Doğrudan Temin",
}

# İhale durum kodları (detay STATUS_MAP + liste DURUM_MAP birleşimi)
IHALE_DURUM = {
    1: "Taslak",
    2: "Katılıma Açık",
    3: "Katılıma Açık",
    4: "Değerlendirme Tamamlanmış",
    5: "Değerlendirmede",
    6: "İptal Edilmiş",
    10: "İptal Edilmiş",
    15: "Sonuç İlanı Yayımlanmış",
    20: "Sözleşme İmzalanmış",
}

# Sonuçlanmış/kapanmış sayılan durumlar (refresh politikası için)
DURUM_SONUCLANMIS = {10, 15, 20}

ILAN_TIP = {
    1: "İhale İlanı",
    2: "Düzeltme İlanı",
    3: "İptal İlanı",
    4: "Sonuç İlanı",
    5: "Ön İlan",
    10: "Ön Yeterlik İlanı",
}

# ── İhale özellik etiketleri (ihaleOzellikList) ─────────
# Mobil filtre boolean anahtarı → EKAP ihaleOzellik etiketi (TENDER_DETAIL. öneki atılmış).
# Gelişmiş boolean filtreleri Tender.ozellikler listesi üzerinden çalışır.
# (EKAP detayında bu 9 etiket mevcut; app'in diğer boolean'ları EKAP'ta öznitelik değil.)
OZELLIK_MAP = {
    "eIhale": "E_IHALE",
    "kismiTeklifMi": "KISMI_TEKLIF_VEREBILIR",
    "altYukleniciCalistirilabilirMi": "ALT_YUKLENICI",
    "fiyatFarkiVerilecekMi": "FIYAT_FARKI_VERILMESI",
    "isDeneyimiGosterenBelgelerIsteniyorMu": "IS_DENEYIM_BELGE",
    "meslekiTeknikYeterlilikBelgeleriIsteniyorMu": "MESLEKI_TEKNIK_YETERLIK",
    "yabanciIsteklilereIzinVeriliyorMu": "YABANCI_ISTEKLI_KATILIM",
    "yerliIstekliyeFiyatAvantajiUgulaniyorMu": "YERLI_ISTEKLI_AVANTAJ",
    "ekonomikVeMaliYeterlilikBelgeleriIsteniyorMu": "EKONOMIK_MALI_YETERLIK",
}

# ── Şehirler (filterData.js CITIES — 81 il) ─────────────
# (ekap_il_id, plaka, ad, is_big_city)
CITIES = [
    (251, 6, "ANKARA", True), (284, 34, "İSTANBUL", True), (285, 35, "İZMİR", True),
    (245, 1, "ADANA", True), (246, 2, "ADIYAMAN", False), (247, 3, "AFYONKARAHİSAR", False),
    (248, 4, "AĞRI", False), (249, 68, "AKSARAY", False), (250, 5, "AMASYA", False),
    (252, 7, "ANTALYA", True), (253, 75, "ARDAHAN", False), (254, 8, "ARTVİN", False),
    (255, 9, "AYDIN", True), (256, 10, "BALIKESİR", True), (257, 74, "BARTIN", False),
    (258, 72, "BATMAN", False), (259, 69, "BAYBURT", False), (260, 11, "BİLECİK", False),
    (261, 12, "BİNGÖL", False), (262, 13, "BİTLİS", False), (263, 14, "BOLU", False),
    (264, 15, "BURDUR", False), (265, 16, "BURSA", True), (266, 17, "ÇANAKKALE", False),
    (267, 18, "ÇANKIRI", False), (268, 19, "ÇORUM", False), (269, 20, "DENİZLİ", True),
    (270, 21, "DİYARBAKIR", True), (271, 81, "DÜZCE", False), (272, 22, "EDİRNE", False),
    (273, 23, "ELAZIĞ", False), (274, 24, "ERZİNCAN", False), (275, 25, "ERZURUM", True),
    (276, 26, "ESKİŞEHİR", True), (277, 27, "GAZİANTEP", True), (278, 28, "GİRESUN", False),
    (279, 29, "GÜMÜŞHANE", False), (280, 30, "HAKKARİ", False), (281, 31, "HATAY", True),
    (282, 76, "IĞDIR", False), (283, 32, "ISPARTA", False), (286, 70, "KARAMAN", False),
    (287, 36, "KARS", False), (288, 37, "KASTAMONU", False), (289, 38, "KAYSERİ", True),
    (290, 71, "KIRIKKALE", False), (291, 39, "KIRKLARELİ", False), (292, 40, "KIRŞEHİR", False),
    (293, 79, "KİLİS", False), (294, 41, "KOCAELİ", True), (295, 42, "KONYA", True),
    (296, 43, "KÜTAHYA", False), (297, 44, "MALATYA", True), (298, 45, "MANİSA", True),
    (299, 47, "MARDİN", True), (300, 46, "KAHRAMANMARAŞ", True), (301, 33, "MERSİN", True),
    (302, 48, "MUĞLA", True), (303, 49, "MUŞ", False), (304, 50, "NEVŞEHİR", False),
    (305, 51, "NİĞDE", False), (306, 52, "ORDU", True), (307, 80, "OSMANİYE", False),
    (308, 53, "RİZE", False), (309, 54, "SAKARYA", True), (310, 55, "SAMSUN", True),
    (311, 56, "SİİRT", False), (312, 57, "SİNOP", False), (313, 58, "SİVAS", False),
    (314, 63, "ŞANLIURFA", True), (315, 73, "ŞIRNAK", False), (316, 59, "TEKİRDAĞ", True),
    (317, 60, "TOKAT", False), (318, 61, "TRABZON", True), (319, 62, "TUNCELİ", False),
    (320, 64, "UŞAK", False), (321, 65, "VAN", True), (322, 77, "YALOVA", False),
    (323, 66, "YOZGAT", False), (324, 67, "ZONGULDAK", False), (325, 78, "KARABÜK", False),
]
