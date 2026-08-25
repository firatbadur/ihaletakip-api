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
from . import read, write

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
    "description": (
        "Kullanıcının kendi kayıtları: kaydettiği ihaleler, kayıtlı filtreleri, alarmları, "
        "favori idare ve firmaları. `yaklasan` = teklif tarihi yaklaşan kayıtları "
        "('bu hafta neyin son günü'), `onerilerim` = günlük öneriler ve GEREKÇELERİ "
        "('bunu neden önerdin')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tur": {"type": "string", "enum": [
                "kayitli_ihaleler", "kayitli_filtreler", "alarmlar",
                "favori_idareler", "favori_firmalar",
                # yaklasan: ihale tarihi bugün/sonrası olan kayıtlı ihale ve alarmlar
                # onerilerim: günlük eşleştirmenin ürettiği öneriler + gerekçeleri
                "yaklasan", "onerilerim",
            ]},
            "limit": {"type": "integer", "description": "En çok 20."},
        },
        "required": ["tur"],
    },
}

_IDARE_ARA = {
    "name": "idare_ara",
    "description": (
        "İdareyi/kurumu ADIYLA arar ve kimliğini (idare_id, detsis_no) döner. "
        "`idare_profili` ya da `ihale_ara(idare_id=...)` kullanmadan ÖNCE bunu çağır — "
        "idare adını doğrudan filtreye yazmak yerine kimliğe çevirmek çok daha isabetlidir."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "Kurum adı, en az 3 harf."},
            "take": {"type": "integer", "description": "En çok 15."},
        },
        "required": ["q"],
    },
}

_IHALE_KAYDET = {
    "name": "ihale_kaydet_oner",
    "description": (
        "Kullanıcıya 'bu ihaleyi kaydedeyim mi?' onay kartı gösterir. Kullanıcı açıkça "
        "istediğinde ya da profiline çok uygun bir ihale bulduğunda kullan. "
        "SEN KAYDETMEZSİN — kullanıcı butona basınca kaydedilir."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ikn": {"type": "string", "description": "Bu sohbette bir araçtan gelmiş İKN."},
            "klasor": {"type": "string", "description": "Klasör adı (opsiyonel)."},
        },
        "required": ["ikn"],
    },
}

_ALARM_KUR = {
    "name": "alarm_kur_oner",
    "description": (
        "Kullanıcıya 'bu ihaleye alarm kurayım mı?' onay kartı gösterir. Alarm PRO "
        "özelliğidir; onay anında kontrol edilir, sen kontrol etme."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ikn": {"type": "string"},
            "ihale_gunu": {"type": "boolean", "description": "İhale günü hatırlat."},
            "dokuman_degisikligi": {"type": "boolean", "description": "Doküman değişince bildir."},
        },
        "required": ["ikn"],
    },
}

_FILTRE_KAYDET = {
    "name": "filtre_kaydet_oner",
    "description": (
        "Yaptığın aramayı kayıtlı filtre olarak eklemeyi önerir. `filtreler` alanına "
        "`ihale_ara`'da KULLANDIĞIN parametreleri aynen koy. `alarm: true` ise yeni "
        "eşleşen ihalede bildirim gider (PRO)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ad": {"type": "string", "description": "Kullanıcının tanıyacağı kısa ad."},
            "filtreler": {"type": "object", "description": "ihale_ara parametreleri."},
            "alarm": {"type": "boolean"},
        },
        "required": ["ad", "filtreler"],
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# Faz 3 araçları
# ══════════════════════════════════════════════════════════════════════════════

_IHALE_BENCHMARK = {
    "name": "ihale_benchmark",
    "description": (
        "“Bu iş kaça kapanır / ne kadar indirim verilir / kaç kişi teklif verir?” "
        "Benzer İMZALANMIŞ sözleşmelerin bedel ve indirim dağılımı, rekabet ortalaması, "
        "yıllara göre seyir. Kullanıcı fiyat/teklif stratejisi sorduğunda İLK bunu çağır."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "İKN veya ekap_id."},
            "yil_geri": {"type": "integer", "description": "Kaç yıl geriye bakılsın (varsayılan 5)."},
            "kapsam": {"type": "string",
                       "description": "Karşılaştırma kümesi kademesi; boş bırak, otomatik genişler."},
        },
        "required": ["key"],
    },
}

_TEKRAR_EDEN = {
    "name": "tekrar_eden_ihaleler",
    "description": (
        "Her yıl/dönem tekrarlanan işler ve SIRADAKİ ilanın beklenen tarihi. "
        "`key` verirsen o ihalenin serisi + geçmiş örnekleri gelir "
        "(“bu iş geçen sene kaça verilmişti”); vermezsen idare/OKAS/il filtresiyle "
        "liste gelir (“bu idare önümüzdeki 60 günde ne açar”). "
        "beklenen_ilan_tarihi TAHMİNDİR, guven alanını mutlaka aktar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Tek ihale kipi: İKN veya ekap_id."},
            "idare_id": {"type": "array", "items": {"type": "string"}},
            "idare_detsis": {"type": "string"},
            "en_ust_idare_kod": {"type": "array", "items": {"type": "string"}},
            "okas_ana_kod": {"type": "array", "items": {"type": "string"}},
            "il_id": {"type": "array", "items": {"type": "integer"}},
            "ihale_tip": {"type": "array", "items": {"type": "integer"}},
            "guven": {"type": "array", "items": {"type": "string",
                                                 "enum": ["yuksek", "orta", "dusuk"]}},
            "beklenen_gun": {"type": "integer", "description": "Önümüzdeki N gün içinde beklenenler."},
            "page_size": {"type": "integer", "description": "En çok 10."},
        },
    },
}

_PAZAR_PANOSU = {
    "name": "pazar_panosu",
    "description": (
        "Bir iş kolunun pazar görünümü: yıllara göre hacim, en çok iş alan firmalar, "
        "il dağılımı ve yoğunlaşma (HHI). `okas_bucket` (4 haneli OKAS ön eki) verirsen "
        "o iş grubunun detayı, vermezsen yılın en büyük iş grupları gelir. "
        "“Bu alanda kimler var / rekabet nasıl” sorusunun karşılığı."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "okas_bucket": {"type": "string", "description": "4 haneli OKAS ön eki (okas_ara'dan)."},
            "yil": {"type": "integer"},
            "limit": {"type": "integer", "description": "En çok 20."},
        },
    },
}

_DOKUMAN_ANALIZI = {
    "name": "dokuman_analizi",
    "description": (
        "İhalenin şartnamesi için DAHA ÖNCE üretilmiş AI analizini getirir. "
        "Sen doküman İNDİREMEZSİN; hazır analiz yoksa kullanıcıyı uygulamadaki "
        "Dokümanlar → Analiz akışına yönlendir (EKAP'a ASLA yönlendirme)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ikn": {"type": "string"},
            "tur": {"type": "string",
                    "enum": ["tech_spec", "admin_spec", "cost_analysis", "generate_keywords"]},
        },
        "required": ["ikn"],
    },
}

_TOPLU_KAYDET = {
    "name": "toplu_ihale_kaydet_oner",
    "description": (
        "Birden çok ihaleyi TEK onay kartıyla kaydetmeyi önerir (“hepsini kaydet”). "
        "İhale başına ayrı kart çıkarma — bunu kullan."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "iknler": {"type": "array", "items": {"type": "string"},
                       "description": "Bu sohbette araçtan gelmiş İKN'ler, en çok 20."},
            "klasor": {"type": "string", "description": "Klasör adı (yoksa oluşturulur)."},
        },
        "required": ["iknler"],
    },
}

_IHALE_TASI = {
    "name": "ihale_tasi_oner",
    "description": "Kayıtlı bir ihaleyi başka klasöre taşımayı önerir. Klasör boşsa “Genel”e taşır.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ikn": {"type": "string"},
            "klasor": {"type": "string"},
        },
        "required": ["ikn"],
    },
}

_FIRMA_TAKIP = {
    "name": "firma_takip_oner",
    "description": (
        "Yüklenici firmayı takibe almayı önerir (rakip takibi). `firma_id` "
        "`firma_ara`/`firma_profili` sonucundaki `id`'dir."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "firma_id": {"type": "integer"},
            "alarm": {"type": "boolean", "description": "Yeni iş aldığında bildirim."},
        },
        "required": ["firma_id"],
    },
}

_IDARE_FAVORI = {
    "name": "idare_favori_oner",
    "description": (
        "İdareyi favorilere eklemeyi önerir. `detsis_no` `idare_ara` sonucundan gelir."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "detsis_no": {"type": "string"},
            "alarm": {"type": "boolean", "description": "Yeni ihale yayınlayınca bildirim."},
        },
        "required": ["detsis_no"],
    },
}

_KAYIT_SIL = {
    "name": "kayit_sil_oner",
    "description": (
        "Kullanıcının bir kaydını SİLMEYİ önerir (“yok sil onu”, “alarmı kaldır”, "
        "“bu firmayı takipten çıkar”). Silme geri alınamaz; kart kullanıcıya yıkıcı "
        "biçimde gösterilir. Anahtarı bilmiyorsan önce kullanicinin_verisi ile listele."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tur": {"type": "string", "enum": ["ihale", "alarm", "filtre", "firma", "idare"]},
            "anahtar": {"type": "string",
                        "description": "ihale→İKN, alarm→İKN/ekap_id, filtre→id, firma→firma id, idare→detsis_no."},
        },
        "required": ["tur", "anahtar"],
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
    _IDARE_ARA,
    _IHALE_KAYDET,
    _ALARM_KUR,
    _FILTRE_KAYDET,
    # ── Faz 3 (sona eklendi) ──
    _IHALE_BENCHMARK,
    _TEKRAR_EDEN,
    _PAZAR_PANOSU,
    _DOKUMAN_ANALIZI,
    _TOPLU_KAYDET,
    _IHALE_TASI,
    _FIRMA_TAKIP,
    _IDARE_FAVORI,
    _KAYIT_SIL,
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
    "idare_ara": read.idare_ara,
    "ihale_kaydet_oner": write.ihale_kaydet_oner,
    "alarm_kur_oner": write.alarm_kur_oner,
    "filtre_kaydet_oner": write.filtre_kaydet_oner,
    "ihale_benchmark": read.ihale_benchmark,
    "tekrar_eden_ihaleler": read.tekrar_eden_ihaleler,
    "pazar_panosu": read.pazar_panosu,
    "dokuman_analizi": read.dokuman_analizi,
    "toplu_ihale_kaydet_oner": write.toplu_ihale_kaydet_oner,
    "ihale_tasi_oner": write.ihale_tasi_oner,
    "firma_takip_oner": write.firma_takip_oner,
    "idare_favori_oner": write.idare_favori_oner,
    "kayit_sil_oner": write.kayit_sil_oner,
}

assert {t["name"] for t in TOOL_SPECS} == set(TOOL_IMPL), "TOOL_SPECS ve TOOL_IMPL ayrıştı"
