"""
Araç kataloğu — Anthropic `tools` şemaları + uygulama eşlemesi.

⚠️ `TOOL_SPECS` SIRASI DETERMİNİSTİK OLMALI. `tools` bloğu prompt cache'inin önekinde
render edilir (sıra: tools → system → messages); sıra kullanıcıdan kullanıcıya ya da
istekten isteğe değişirse cache havuzu bölünür ve her mesaj tam fiyattan ödenir.
Yeni araç EKLERKEN listenin SONUNA ekle, araya sokma.

Parametre adları bilerek `ekap.views.apply_tender_filters` ile birebir aynıdır: modelin
ürettiği sözlük çevrilmeden geçirilir. Ara çeviri katmanı = zamanla ayrışan ikinci
gerçek kaynağı.
"""
from . import read

# Yalnızca sık kullanılan filtreler şemada; tamamı ~40 parametre ve hepsini yazmak
# cache önekini gereksiz şişirir. Model bilmediği bir filtreye ihtiyaç duyarsa
# `q`/`ihale_adi` ile ilerler.
_IHALE_ARA = {
    "name": "ihale_ara",
    "description": (
        "EKAP ihale veritabanında arama yapar (1 milyondan fazla ihale). "
        "Konu araması için ÖNCE okas_ara ile kod bul, sonra okas_kod ile filtrele; "
        "kod bulunamazsa ihale_adi/q kullan. Metin terimleri en az 3 karakter olmalı."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "Serbest metin: ihale adı + idare adı + İKN'de arar."},
            "ihale_adi": {"type": "string", "description": "Yalnızca ihale adında arar."},
            "idare_adi": {"type": "string", "description": "Yalnızca idare adında arar."},
            "ikn": {"type": "string", "description": "İhale Kayıt Numarası, ör. 2026/1473120."},
            "il_id": {"type": "array", "items": {"type": "integer"},
                      "description": "EKAP il id'si (245-325 aralığı). PLAKA KODU DEĞİLDİR."},
            "ihale_tip": {"type": "array", "items": {"type": "integer", "enum": [1, 2, 3, 4]},
                          "description": "1 Mal Alımı, 2 Yapım, 3 Hizmet, 4 Danışmanlık."},
            "ihale_usul": {"type": "array", "items": {"type": "integer"},
                           "description": "1 Açık, 2 Belli İstekliler, 3 Pazarlık, 4 Doğrudan Temin."},
            "ihale_durum": {"type": "array", "items": {"type": "integer"},
                            "description": "2 Katılıma Açık, 3 Teklif Değerlendirme, 5/20 Sözleşme İmzalanmış, 6/10 İptal, 15 Sonuç İlanı."},
            "idare_id": {"type": "array", "items": {"type": "string"}},
            "idare_detsis": {"type": "array", "items": {"type": "string"},
                             "description": "DETSIS düğümü; alt birimlere genişletilir."},
            "okas_kod": {"type": "array", "items": {"type": "string"},
                         "description": "OKAS kod ÖNEKİ (başına göre eşleşir). okas_ara ile bulunur."},
            "okas_adi": {"type": "array", "items": {"type": "string"},
                         "description": "OKAS kalem adında arar; birden çok terim OR'lanır."},
            "yuklenici": {"type": "string", "description": "Yüklenici firma adı (serbest metin)."},
            "yuklenici_id": {"type": "array", "items": {"type": "integer"}},
            "ihale_tarihi_min": {"type": "string", "description": "GG.AA.YYYY veya YYYY-AA-GG."},
            "ihale_tarihi_max": {"type": "string"},
            "ilan_tarihi_min": {"type": "string"},
            "ilan_tarihi_max": {"type": "string"},
            "sonuclanmis": {"type": "boolean", "description": "PRO. true=sözleşmesi olanlar."},
            "iptal": {"type": "boolean", "description": "PRO."},
            "yaklasik_maliyet_min": {"type": "number", "description": "PRO. TL."},
            "yaklasik_maliyet_max": {"type": "number", "description": "PRO. TL."},
            "sozlesme_bedeli_min": {"type": "number", "description": "PRO. TL."},
            "sozlesme_bedeli_max": {"type": "number", "description": "PRO. TL."},
            "indirim_orani_min": {"type": "number", "description": "PRO. ORAN (0.20 = %20), yüzde değil."},
            "indirim_orani_max": {"type": "number", "description": "PRO. ORAN."},
            "okas_ana_kod": {"type": "array", "items": {"type": "string"}, "description": "PRO. Tam eşleşme."},
            "en_ust_idare_kod": {"type": "array", "items": {"type": "string"}, "description": "PRO. Bakanlık kodu."},
            "order": {"type": "string", "enum": ["ihale_tarihi", "ilan_tarihi"]},
            "siralamaTipi": {"type": "string", "enum": ["asc", "desc"]},
            "page": {"type": "integer"},
            "page_size": {"type": "integer", "description": "En çok 20."},
        },
    },
}

_IHALE_DETAY = {
    "name": "ihale_detay",
    "description": "Tek bir ihalenin ayrıntısı: usul, durum, yer, doküman sayısı, OKAS kalemleri, yaklaşık maliyet.",
    "input_schema": {
        "type": "object",
        "properties": {"key": {"type": "string", "description": "İKN (2026/1473120) veya ekap_id."}},
        "required": ["key"],
    },
}

_OKAS_ARA = {
    "name": "okas_ara",
    "description": (
        "Konu kelimesini EKAP'ın OKAS kategori kataloğuna oturtur. Eşanlamlıları TEK "
        "çağrıda gönder — ör. otomasyon için ['otomasyon','scada','plc','kontrol sistemi']. "
        "Dönen kodların önekini ihale_ara(okas_kod=...) ile kullan."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "terimler": {"type": "array", "items": {"type": "string"},
                         "description": "En az 3 karakterlik terimler, en çok 6 tane."},
        },
        "required": ["terimler"],
    },
}

_IDARE_PROFILI = {
    "name": "idare_profili",
    "description": (
        "PRO. Bir idarenin satın alma profili: toplam harcama, kimlere iş verdiği "
        "(yüklenici payları), yoğunlaşma (HHI), yıllara göre seyir, OKAS ve il dağılımı. "
        "'Şu idare kimlere iş vermiş' sorusunun cevabı budur."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "idare_id": {"type": "string", "description": "Yaprak idare (ihale kayıtlarındaki idare_id)."},
            "idare_detsis": {"type": "string", "description": "DETSIS düğümü; alt birimleri kapsar."},
            "en_ust_idare_kod": {"type": "string", "description": "Bakanlık geneli."},
            "detay": {"type": "boolean", "description": "false ise yalnızca toplamlar."},
        },
    },
}

_FIRMA_ARA = {
    "name": "firma_ara",
    "description": "Yüklenici firma arar (94 binden fazla firma). Türkçe arama güvenlidir; ünvanın bir parçası yeter.",
    "input_schema": {
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "Firma ünvanı, en az 3 harf."},
            "il_id": {"type": "array", "items": {"type": "integer"}},
            "kind": {"type": "array", "items": {"type": "string", "enum": ["firma", "sahis", "ortak_girisim"]}},
            "min_sozlesme": {"type": "integer"},
            "page_size": {"type": "integer", "description": "En çok 15."},
        },
        "required": ["q"],
    },
}

_FIRMA_PROFILI = {
    "name": "firma_profili",
    "description": (
        "Bir firmanın geçmişi: sözleşme/ihale/idare sayısı, toplam bedel, faaliyet aralığı, "
        "ihale türü + il + yıl + idare dağılımı. contractor_id firma_ara'dan gelir."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"contractor_id": {"type": "integer"}},
        "required": ["contractor_id"],
    },
}

_FIRMA_ISLERI = {
    "name": "firma_isleri",
    "description": (
        "Firmanın ALDIĞI işler (imzalanmış sözleşmeler), en yenisi başta. "
        "'En son ne iş almış' sorusunun cevabı. Kaybedilen teklifler EKAP'ta yoktur."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "contractor_id": {"type": "integer"},
            "il_id": {"type": "array", "items": {"type": "integer"}},
            "ihale_tip": {"type": "array", "items": {"type": "integer"}},
            "yil": {"type": "integer"},
            "order": {"type": "string", "enum": ["sozlesme_tarihi", "sozlesme_bedeli"]},
            "page_size": {"type": "integer", "description": "En çok 15."},
        },
        "required": ["contractor_id"],
    },
}

_KULLANICI_VERISI = {
    "name": "kullanicinin_verisi",
    "description": "Kullanıcının kendi kayıtları: kaydettiği ihaleler, kayıtlı filtreleri, alarmları, favori idare ve firmaları.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tur": {"type": "string", "enum": [
                "kayitli_ihaleler", "kayitli_filtreler", "alarmlar",
                "favori_idareler", "favori_firmalar",
            ]},
            "limit": {"type": "integer", "description": "En çok 20."},
        },
        "required": ["tur"],
    },
}

# ⚠️ SIRA SABİT — yeni araç SONA eklenir (bkz. modül docstring'i, prompt cache).
TOOL_SPECS = [
    _IHALE_ARA,
    _IHALE_DETAY,
    _OKAS_ARA,
    _IDARE_PROFILI,
    _FIRMA_ARA,
    _FIRMA_PROFILI,
    _FIRMA_ISLERI,
    _KULLANICI_VERISI,
]

TOOL_IMPL = {
    "ihale_ara": read.ihale_ara,
    "ihale_detay": read.ihale_detay,
    "okas_ara": read.okas_ara,
    "idare_profili": read.idare_profili,
    "firma_ara": read.firma_ara,
    "firma_profili": read.firma_profili,
    "firma_isleri": read.firma_isleri,
    "kullanicinin_verisi": read.kullanicinin_verisi,
}

assert {t["name"] for t in TOOL_SPECS} == set(TOOL_IMPL), "TOOL_SPECS ve TOOL_IMPL ayrıştı"
