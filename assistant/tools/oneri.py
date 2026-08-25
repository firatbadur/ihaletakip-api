"""
Sohbet sonrası SORU ÖNERİLERİ — deterministik üretilir, model karışmaz.

Neden modele yazdırmıyoruz:
· Model kendi yapamayacağı bir şeyi önerebilir ("teklifini hazırlayayım mı?") ve
  kullanıcı dokunduğunda hayal kırıklığı yaşar. Buradaki her öneri, gerçekten var
  olan bir araca birebir karşılık gelir.
· Öneri üretmek ek çıktı tokenı demektir; her mesajda ödenir.
· Öneriler ölçülebilir olmalı; sabit kurallar test edilebilir, model çıktısı edilemez.

Neden bu iş değerli: asistanın 21 aracının çoğunu kullanıcı SORMADIKÇA keşfedemez.
Statik çipler yalnızca sohbetin başında görünür; asıl fırsat, kullanıcı bir idareye
ya da firmaya baktıktan HEMEN SONRA "bunu takip edeyim mi" diye sorabilmektir.
"""

AZAMI_ONERI = 3


def _tek_ihale_odagi(ctx):
    """Sohbette TEK bir ihale odaktaysa onun İKN'si, değilse None.

    ⚠️ Liste dönen bir aramadan sonra "bu iş kaça kapanır?" önermek anlamsızdır —
    "bu" hangi iş belli değildir ve model yanlış ihaleyi seçebilir.
    """
    if getattr(ctx.conversation, "tender_ikn", ""):
        return ctx.conversation.tender_ikn
    return ctx.son_grup[0] if len(ctx.son_grup) == 1 else None


def _kullanici_verisi_turleri(arac_izi):
    """Bu turda `kullanicinin_verisi` hangi türlerle çağrıldı."""
    return {
        (a.get("param") or {}).get("tur")
        for a in (arac_izi or [])
        if a.get("ok") and a.get("arac") == "kullanicinin_verisi"
    }


def soru_onerileri(ctx, arac_izi):
    """Bu turda çalışan araçlara bakarak sıradaki soruları önerir (en çok 3)."""
    calisan = {a.get("arac") for a in (arac_izi or []) if a.get("ok")}
    if not calisan:
        return []          # araç çalışmadıysa (mevzuat sorusu) öneri gürültü olur

    oneriler = []

    def ekle(metin):
        if metin not in oneriler:
            oneriler.append(metin)

    tek = _tek_ihale_odagi(ctx)

    # ── Tek ihale odağı: teklif kararının asıl iki sorusu ──
    if tek:
        if "ihale_benchmark" not in calisan:
            ekle("Bu iş kaça kapanır, ne kadar kırım yapmalıyım?")
        if "tekrar_eden_ihaleler" not in calisan:
            ekle("Bu iş her yıl açılıyor mu?")
        if "alarm_kur_oner" not in calisan:
            ekle("Bu ihaleye alarm kur")

    # ── İdareye bakıldı → takvimi ve takibi ──
    if "idare_profili" in calisan:
        if "tekrar_eden_ihaleler" not in calisan:
            ekle("Bu idare her yıl aynı işleri açıyor mu?")
        if "idare_favori_oner" not in calisan:
            ekle("Bu idareyi favorilerime ekle")

    # ── Firmaya bakıldı → rakip takibi ──
    if calisan & {"firma_profili", "firma_isleri"}:
        if "firma_takip_oner" not in calisan:
            ekle("Bu firmayı takibe al")
        ekle("Bu firma en çok hangi idarelerden iş alıyor?")

    # ── Fiyat analizinden sonra rekabetin kendisi ──
    if "ihale_benchmark" in calisan:
        ekle("Bu alanda pazar nasıl, kimler iş alıyor?")

    # ── Pazar panosundan fırsata geçiş ──
    if "pazar_panosu" in calisan:
        ekle("Bu alanda bana uygun açık ihale var mı?")

    # ── Liste dönen arama → kaydetmeye ve elemeye ──
    if "ihale_ara" in calisan and not tek:
        if "filtre_kaydet_oner" not in calisan:
            ekle("Bu aramayı kaydet, yenisi çıkınca haber ver")
        ekle("Bunlardan hangisi firmama daha uygun?")

    # ── Kullanıcının kendi verisi ──
    # ⚠️ Bu iki soru BAŞLANGIÇ çipi olamaz: "Teklif tarihi yaklaşan ihalelerim"
    # kayıtlı ihalesi olmayanda boş döner, "bunları neden önerdin" ise henüz öneri
    # yokken anlamsızdır. Yerleri tam olarak burası — kullanıcı kendi verisine
    # baktıktan SONRA.
    if "kullanicinin_verisi" in calisan:
        turler = _kullanici_verisi_turleri(arac_izi)
        if "yaklasan" in turler:
            ekle("Bunlara alarm kur")
        else:
            ekle("Teklif tarihi yaklaşan ihalelerimi göster")
        if "onerilerim" in turler:
            ekle("Bunları neden önerdin?")

    return oneriler[:AZAMI_ONERI]


def oneri_blogu(ctx, arac_izi):
    """`payload.blocks` için `suggestions` bloğu (öneri yoksa None)."""
    sorular = soru_onerileri(ctx, arac_izi)
    return {"type": "suggestions", "sorular": sorular} if sorular else None
