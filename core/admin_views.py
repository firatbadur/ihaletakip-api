"""
Admin ekranı: Güvenlik Paneli (`/admin/guvenlik/`).

Bugün en çok istek atan kimlikler (Redis izleme sayaçları — `core.guvenlik_izleme`), şüphe
puanı + gerekçeleri, EKAP ortak bütçe durumu, son güvenlik olayları, etkin engeller ve tek
tıkla hesap askıya alma / IP engelleme.

Şüphe puanı `guvenlik.supheli_puani`'dan gelir (otomatik askı aynı fonksiyonu kullanır).
⚠️ Görüntüleme staff'a, **engelleme yalnızca superuser'a** açıktır (POST + CSRF).
⚠️ Ekran ağır sorgu atmaz: kimlik listesi Redis'ten, kullanıcı bilgileri tek `IN` sorgusuyla.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Count
from django.shortcuts import redirect, render
from django.utils import timezone

from . import guvenlik, guvenlik_izleme
from .models import ErisimEngeli, GuvenlikOlayi

_SURELER = {"24s": timedelta(hours=24), "7g": timedelta(days=7), "suresiz": None}


def _eylem(request):
    if not request.user.is_superuser:
        messages.error(request, "Engelleme yalnızca süper kullanıcıya açıktır.")
        return
    tur = request.POST.get("tur")
    sure = _SURELER.get(request.POST.get("sure"), timedelta(hours=24))
    sebep = (request.POST.get("sebep") or "Güvenlik panelinden").strip()[:500]
    try:
        if tur == "hesap":
            user = get_user_model().objects.get(pk=int(request.POST.get("uid")))
            guvenlik.hesap_engelle(user, sebep=sebep, sure=sure, olusturan=request.user)
            messages.success(request, f"{user.email or user.username} askıya alındı.")
        elif tur == "ip":
            e = guvenlik.ip_engelle(request.POST.get("ip"), sebep=sebep, sure=sure,
                                    olusturan=request.user)
            messages.success(request, f"{e.deger} engellendi.")
        elif tur == "kaldir":
            e = ErisimEngeli.objects.get(pk=int(request.POST.get("engel")))
            guvenlik.engeli_kaldir(e, kaldiran=request.user)
            messages.success(request, f"Engel kaldırıldı: {e}")
    except (ValueError, ErisimEngeli.DoesNotExist, get_user_model().DoesNotExist) as e:
        messages.error(request, str(e) or "İşlem yapılamadı.")


def guvenlik_ekrani(request):
    from ekap.mobil import kota, throttle

    if request.method == "POST":
        _eylem(request)
        return redirect("core_guvenlik")

    gun = timezone.localdate()
    satirlar = guvenlik_izleme.en_yogun(adet=50)
    uidler = [int(s["kimlik"][2:]) for s in satirlar
              if s["kimlik"].startswith("u:") and s["kimlik"][2:].isdigit()]
    kullanicilar = {u.pk: u for u in get_user_model().objects.filter(pk__in=uidler)}
    engelli_uid = set(ErisimEngeli.objects.filter(
        tur=ErisimEngeli.TUR_KULLANICI, aktif=True, kullanici_id__in=uidler)
        .values_list("kullanici_id", flat=True))
    for s in satirlar:
        uid = int(s["kimlik"][2:]) if s["kimlik"].startswith("u:") else None
        u = kullanicilar.get(uid) if uid else None
        s["user"] = u
        s["engelli"] = uid in engelli_uid
        s["ip_engelli"] = bool(guvenlik.ip_engeli(s.get("ip") or ""))
        s["kota_gun"], s["kota_saat"] = kota.ozet(s["kimlik"])
        s["puan"], s["nedenler"] = guvenlik.supheli_puani(s, u, s["kota_gun"])
        try:
            s["son"] = datetime.fromtimestamp(
                int(s.get("zaman") or 0), tz=timezone.get_current_timezone())
        except (TypeError, ValueError, OSError):
            s["son"] = None
        s["istemci"] = {"portal-girisli": "web", "portal-anonim": "web (anonim)",
                        "dogrudan-girisli": "mobil/API", "dogrudan-anonim":
                        "mobil/API (anonim)", "portal-dolum": "web önbellek"}.get(
                            s.get("tur", ""), s.get("tur", ""))

    bugun_bas = timezone.make_aware(datetime.combine(gun, datetime.min.time()))
    olay_sayilari = {r["tur"]: r["n"] for r in GuvenlikOlayi.objects.filter(
        zaman__gte=bugun_bas).values("tur").annotate(n=Count("id")).order_by()}
    supheli = sum(1 for s in satirlar if s["puan"] >= 5)

    return render(request, "admin/core/guvenlik.html", {
        "title": "Güvenlik Paneli",
        "gun": gun,
        "satirlar": satirlar,
        "toplam": guvenlik_izleme.toplamlar(),
        "butce": throttle.butce_ozet(),
        "kota_tablo": kota.tablo(),
        "olay_sayilari": olay_sayilari,
        "supheli": supheli,
        "olaylar": GuvenlikOlayi.objects.select_related("kullanici")[:40],
        "engeller": ErisimEngeli.objects.filter(aktif=True).select_related(
            "kullanici", "olusturan")[:100],
        "superuser": request.user.is_superuser,
        "oto": {"acik": getattr(settings, "GUVENLIK_OTOMATIK_ASKI", True),
                "puan": getattr(settings, "GUVENLIK_OTOMATIK_ASKI_PUAN", 5),
                "istek": getattr(settings, "GUVENLIK_OTOMATIK_ASKI_ISTEK", 500),
                "yeni_saat": getattr(settings, "GUVENLIK_OTOMATIK_ASKI_YENI_SAAT", 24),
                "sure_saat": getattr(settings, "GUVENLIK_OTOMATIK_ASKI_SURE_SAAT", 24)},
        "an": int(time.time()),
    })
