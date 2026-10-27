"""
Günlük e-posta bülteni — kullanıcının o gün aldığı bildirimlerin TEK e-postada özeti.

Kaynak, uygulama-içi `Notification` satırlarıdır: e-posta yeni bir eşleştirme YAPMAZ,
push'un söylediğini söyler. Günün bildirimlerini (08:00 filtre özeti, 08:30 favori
idare, 09:00 alarm + asistan önerisi, önceki günün 12:00 firma bildirimi …) tek
e-postada toplamak bilinçli: aynı günün 3-5 bildirimini ayrı e-posta yapmak spam olurdu.

**Kimler alır**: aktif, e-postası dolu, `User.email_notifications=True` ve penceresinde
en az bir bildirimi olan kullanıcı. Free/Pro farkı YOKTUR — kim hangi bildirimi aldıysa
e-postası da onu anlatır (kapılama bildirim görevlerinde zaten yapıldı).

⚠️⚠️ **Liste, uygulamanın açtığı listeyle AYNI fonksiyondan kurulur**
(`kayitli_filtre_birlesimi`, `idare_ozeti_dali` + bildirimin `pencere_bas/bit`'i).
Ayrı bir sorgu yazmak, bu kod tabanında üç kez düzeltilen "bildirimdeki sayı ekrandaki
listeyle tutmuyor" arızasını e-postaya taşırdı. Sayı ise bildirimin kendi gövdesinden
gelir (push'ta görülen sayı); listede yalnızca ilk `ILK_N` ihale gösterilir ve "N daha"
DENMEZ — o arada durumu değişen ihale iki sayıyı ayrıştırırdı.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

ILK_N = 8
ABONELIK_SALT = "eposta-bulten-v1"
_AYLAR = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos",
          "Eylül", "Ekim", "Kasım", "Aralık")
_GUNLER = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")


def tr_tarih(gun) -> str:
    return f"{gun.day} {_AYLAR[gun.month - 1]} {_GUNLER[gun.weekday()]}"


def pencere(gun=None):
    """Bültenin kapsadığı `(taban, tavan]` — günün bülten saatinden geriye 24 saat.

    ⚠️ Pencere `now`dan DEĞİL takvimden kurulur: görev aynı sabah birkaç kez koşar
    (süre bütçesi dolarsa kalan kullanıcılar sonraki tetikte gönderilir) ve hepsi
    AYNI pencereyi görmeli. Ardışık günlerin pencereleri boşluksuz/örtüşmesiz bölümler.
    """
    gun = gun or timezone.localdate()
    tavan = timezone.make_aware(datetime.combine(gun, time(settings.EPOSTA_BULTEN_SAAT)))
    return tavan - timedelta(hours=24), tavan


def _url(yol: str = "", **ek) -> str:
    taban = settings.EPOSTA_LINK_TABANI.rstrip("/")
    q = urlencode({"utm_source": "eposta", "utm_medium": "bulten", **ek})
    return f"{taban}{yol}?{q}"


def ihale_url(ikn: str) -> str:
    # Web portalı İKN'deki `/`'ı `-` yapar (`ihaletakip-web/src/features/tenders/slug.ts`).
    return _url(f"/ihale/{(ikn or '').strip().replace('/', '-', 1)}")


def abonelik_token(user) -> str:
    return signing.dumps({"u": user.pk}, salt=ABONELIK_SALT, compress=True)


def abonelik_coz(token: str):
    """Token → kullanıcı id (süresiz: e-postadaki bağlantı aylar sonra da çalışmalı)."""
    try:
        return int(signing.loads(token, salt=ABONELIK_SALT)["u"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None


def abonelik_url(user) -> str:
    taban = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{taban}/eposta/abonelik/?{urlencode({'t': abonelik_token(user)})}"


def _iller():
    from ekap.models import City

    return dict(City.objects.values_list("ekap_il_id", "ad"))


def _satirlar(qs, iller):
    alanlar = ("pk", "ikn", "ihale_adi", "idare_adi", "il_id", "ihale_tarihi",
               "ihale_tipi_aciklama", "created_at")
    sonuc = []
    for t in qs.order_by("-created_at").only(*alanlar)[:ILK_N]:
        tarih = timezone.localtime(t.ihale_tarihi) if t.ihale_tarihi else None
        sonuc.append({
            "ad": (t.ihale_adi or "").strip() or t.ikn,
            "idare": t.idare_adi,
            "il": iller.get(t.il_id, ""),
            "tarih": f"{tarih.day} {_AYLAR[tarih.month - 1]} {tarih:%H:%M}" if tarih else "",
            "tur": t.ihale_tipi_aciklama,
            "ikn": t.ikn,
            "url": ihale_url(t.ikn),
        })
    return sonuc


def _bolum(n, user, iller, simdi):
    """Bir bildirimi e-posta bölümüne çevirir (başlık, metin, ihaleler, bağlantı)."""
    from assistant.models import TenderRecommendation
    from ekap.models import Tender
    from ekap.views import idare_ozeti_dali, kayitli_filtre_birlesimi

    from ..models import Notification, SavedFilter

    b = {"baslik": n.title, "metin": n.body, "ihaleler": [], "etiket": "Bildirim",
         "url": _url("/bildirimler"), "dugme": "Uygulamada görüntüle"}
    if n.filtre_idler and n.pencere_bas and n.pencere_bit:
        ids = [int(x) for x in n.filtre_idler.split(",") if x.strip().isdigit()]
        filtreler = [sf.filters or {} for sf in SavedFilter.objects.filter(user=user, id__in=ids)]
        if filtreler:
            qs = kayitli_filtre_birlesimi(filtreler, simdi=simdi,
                                          pencere=(n.pencere_bas, n.pencere_bit))
            b["ihaleler"] = _satirlar(qs, iller)
        b.update(etiket="Kayıtlı filtreler", dugme="Tüm ihaleleri gör")
    elif n.idare_detsis_liste and n.pencere_bas and n.pencere_bit:
        detsis = [x for x in n.idare_detsis_liste.split(",") if x.strip()]
        qs = idare_ozeti_dali(Tender.objects.all(), detsis, simdi=simdi).filter(
            created_at__gte=n.pencere_bas, created_at__lte=n.pencere_bit)
        b["ihaleler"] = _satirlar(qs, iller)
        b.update(etiket="Takip ettiğiniz idareler", dugme="Tüm ihaleleri gör")
    elif n.type == Notification.Type.CHAT:
        oneriler = (TenderRecommendation.objects
                    .filter(user=user, date=timezone.localdate(n.created_at))
                    .values_list("tender_id", flat=True))
        b["ihaleler"] = _satirlar(Tender.objects.filter(pk__in=list(oneriler)), iller)
        b.update(etiket="İhale Asistanı", dugme="Önerileri incele")
    elif n.type == Notification.Type.ALARM:
        b["etiket"] = "İhale alarmı"
        if n.tender_ikn:
            b.update(url=ihale_url(n.tender_ikn), dugme="İhaleyi aç")
    elif n.contractor_id:
        b.update(etiket="Takip ettiğiniz firma", url=_url(f"/yuklenici/{n.contractor_id}"),
                 dugme="Firmayı aç")
    elif n.tender_ikn:
        b.update(url=ihale_url(n.tender_ikn), dugme="İhaleyi aç")
    return b


def bulten_kur(user, taban, tavan, *, iller=None):
    """Kullanıcının penceredeki bildirimlerinden e-posta bağlamı; bildirim yoksa None."""
    from ..models import Notification

    bildirimler = list(Notification.objects.filter(
        user=user, created_at__gt=taban, created_at__lte=tavan).order_by("created_at"))
    if not bildirimler:
        return None
    iller = _iller() if iller is None else iller
    simdi = timezone.now()
    bolumler = []
    for n in bildirimler:
        try:
            bolumler.append(_bolum(n, user, iller, simdi))
        except Exception:  # noqa: BLE001 — tek bozuk bölüm e-postayı düşürmesin
            logger.exception("bülten bölümü kurulamadı: bildirim=%s", n.pk)
            bolumler.append({"baslik": n.title, "metin": n.body, "ihaleler": [],
                             "etiket": "Bildirim", "url": _url("/bildirimler"),
                             "dugme": "Uygulamada görüntüle"})
    gun = timezone.localdate(tavan)
    ad = (user.first_name or user.display_name or "").strip()
    return {
        "ad": ad,
        "tarih": tr_tarih(gun),
        "kisa_tarih": f"{gun.day} {_AYLAR[gun.month - 1]}",
        "bolumler": bolumler,
        "adet": len(bolumler),
        "ozet": bolumler[0]["metin"] or bolumler[0]["baslik"],
        "email": user.email,
        "anasayfa": _url("/"),
        "ayarlar": _url("/hesabim"),
        "abonelik": abonelik_url(user),
    }


def eposta_olustur(user, baglam, connection=None) -> EmailMultiAlternatives:
    adet = baglam["adet"]
    konu = (f"{baglam['kisa_tarih']} özetiniz: {adet} yeni gelişme · İhale Takip" if adet > 1
            else f"{baglam['bolumler'][0]['baslik']} · İhale Takip")
    mesaj = EmailMultiAlternatives(
        subject=konu[:180],
        body=render_to_string("eposta/bulten.txt", baglam),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
        connection=connection,
        headers={
            # Gmail/Yahoo toplu gönderici kuralı: tek tıkla abonelikten çıkma (RFC 8058).
            "List-Unsubscribe": f"<{baglam['abonelik']}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    )
    mesaj.attach_alternative(render_to_string("eposta/bulten.html", baglam), "text/html")
    return mesaj
