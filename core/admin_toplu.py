"""
Admin ekranı: Toplu Gönderim (`/admin/toplu-gonderim/`) — kullanıcılara toplu e-posta ya da
push bildirimi. Gönderim mantığı `core/toplu.py`'de; burası form, önizleme, test gönderimi,
kullanıcı arama ve ilerleme uçlarıdır.

⚠️ Tamamı yalnızca SÜPER KULLANICIYA açıktır (sayfa dahil): toplu gönderim geri alınamaz
ve e-posta içeriği ham HTML olabilir.
⚠️ Gönderim için istemci `onay: true` göndermelidir (ekran önce alıcı sayısını gösterip
onay ister); yanlışlıkla tek tıkla binlerce kişiye gitmesin.
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from . import toplu
from .eposta import TEMALAR, aktif_tema
from .models import TopluGonderim

_PUSH_BASLIK = 120
_PUSH_METIN = 500


def _yetki(request):
    if not request.user.is_superuser:
        raise PermissionDenied("Toplu gönderim yalnızca süper kullanıcıya açıktır.")


def _json(request) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return {}


def _hedef(veri: dict) -> dict:
    h = veri.get("hedef") or {}
    seg = h.get("segment")
    hedef = {"segment": seg}
    if seg == "secili":
        hedef["user_ids"] = sorted({int(x) for x in (h.get("user_ids") or [])
                                    if str(x).isdigit()})
    return hedef


def dogrula(veri: dict) -> tuple[dict, dict]:
    """Form verisini temizler. (temiz, hatalar) — hatalar alan → Türkçe mesaj."""
    hatalar: dict[str, str] = {}
    kanal = veri.get("kanal")
    if kanal not in TopluGonderim.Kanal.values:
        hatalar["kanal"] = "Kanal seçin."
    eposta = kanal == TopluGonderim.Kanal.EPOSTA
    konu = (veri.get("konu") or "").strip()
    icerik = (veri.get("icerik") or "").strip()
    sinir = 200 if eposta else _PUSH_BASLIK
    if not konu:
        hatalar["konu"] = "Konu zorunlu." if eposta else "Başlık zorunlu."
    elif len(konu) > sinir:
        hatalar["konu"] = f"En çok {sinir} karakter."
    if not icerik:
        hatalar["icerik"] = "İçerik zorunlu."
    elif not eposta and len(icerik) > _PUSH_METIN:
        hatalar["icerik"] = f"Bildirim metni en çok {_PUSH_METIN} karakter."
    icerik_turu = veri.get("icerik_turu") or TopluGonderim.IcerikTuru.METIN
    if icerik_turu not in TopluGonderim.IcerikTuru.values:
        icerik_turu = TopluGonderim.IcerikTuru.METIN
    dugme_metni = (veri.get("dugme_metni") or "").strip()[:60]
    dugme_url = (veri.get("dugme_url") or "").strip()
    if eposta and (dugme_metni or dugme_url):
        if not (dugme_metni and dugme_url):
            hatalar["dugme_url"] = "Düğme için hem metin hem adres girin."
        else:
            try:
                URLValidator(schemes=["https"])(dugme_url)
            except ValidationError:
                hatalar["dugme_url"] = "Adres https:// ile başlamalı."
    hedef = _hedef(veri)
    if hedef["segment"] not in toplu.SEGMENTLER:
        hatalar["hedef"] = "Alıcı grubu seçin."
    elif hedef["segment"] == "secili":
        if not hedef["user_ids"]:
            hatalar["hedef"] = "En az bir kullanıcı seçin."
        elif len(hedef["user_ids"]) > toplu.AZAMI_SECILI:
            hatalar["hedef"] = f"En çok {toplu.AZAMI_SECILI} kullanıcı seçilebilir."
    temiz = {
        "kanal": kanal, "konu": konu, "icerik": icerik, "icerik_turu": icerik_turu,
        "eposta_baslik": (veri.get("eposta_baslik") or "").strip()[:200] if eposta else "",
        "on_metin": (veri.get("on_metin") or "").strip()[:200] if eposta else "",
        "dugme_metni": dugme_metni if eposta else "",
        "dugme_url": dugme_url if eposta else "",
        "uygulama_ici": bool(veri.get("uygulama_ici", True)) and not eposta,
        "sessiz_saat_yok": bool(veri.get("sessiz_saat_yok")) and not eposta,
        "hedef": hedef,
    }
    return temiz, hatalar


def _taslak(temiz: dict, tema: str | None = None) -> TopluGonderim:
    """Kaydedilmemiş gönderim nesnesi (önizleme / test için)."""
    alanlar = {k: v for k, v in temiz.items() if k != "hedef"}
    return TopluGonderim(**alanlar, tema=tema or "")


def toplu_ekrani(request):
    _yetki(request)
    return render(request, "admin/core/toplu_gonderim.html", {
        "title": "Toplu Gönderim",
        "segmentler": toplu.SEGMENTLER,
        "ikon_url": "/static/ihaletakip/eposta/ikon.png",
        "tema_ad": f'{TEMALAR[aktif_tema()]["ad"]} ({aktif_tema().upper()})',
        "gonderimler": [toplu.ozet(g) for g in
                        TopluGonderim.objects.select_related("olusturan")[:15]],
        "has_permission": True,
    })


@require_GET
def kullanici_ara(request):
    _yetki(request)
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"sonuclar": []})
    User = get_user_model()
    qs = User.objects.filter(is_active=True)
    if q.isdigit():
        qs = qs.filter(Q(pk=int(q)) | Q(email__icontains=q))
    else:
        qs = qs.filter(Q(email__icontains=q) | Q(display_name__icontains=q)
                       | Q(first_name__icontains=q) | Q(last_name__icontains=q)
                       | Q(username__icontains=q))
    sonuc = []
    for u in qs.order_by("-last_seen_at", "-pk").prefetch_related("push_devices")[:20]:
        sonuc.append({"id": u.pk, "email": u.email, "ad": u.display_name or u.get_full_name(),
                      "pro": u.is_premium, "push": bool(u.push_devices.all()),
                      "eposta": bool(u.email) and u.email_notifications})
    return JsonResponse({"sonuclar": sonuc})


@require_POST
def sayim(request):
    _yetki(request)
    hedef = _hedef(_json(request))
    if hedef["segment"] not in toplu.SEGMENTLER:
        return JsonResponse({"toplam": 0, "eposta": 0, "bildirim": 0})
    return JsonResponse(toplu.sayim(hedef))


@require_POST
def onizle(request):
    _yetki(request)
    veri = _json(request)
    temiz, hatalar = dogrula({**veri, "hedef": {"segment": "hepsi"}})
    if temiz["kanal"] != TopluGonderim.Kanal.EPOSTA:
        return JsonResponse({"html": ""})
    temiz["konu"] = temiz["konu"] or "(konu)"
    temiz["icerik"] = temiz["icerik"] or "<p style='color:#999'>İçerik burada görünecek…</p>"
    tema = veri.get("tema") if veri.get("tema") in TEMALAR else None
    _, html = toplu.eposta_olustur(_taslak(temiz, tema), request.user)
    return JsonResponse({"html": html, "hatalar": hatalar})


@require_POST
def test_gonder(request):
    """Yalnızca giriş yapmış yöneticiye gönderir (e-posta adresi / kayıtlı cihazları)."""
    _yetki(request)
    from tenders.services.notify import push_to_user

    veri = _json(request)
    temiz, hatalar = dogrula({**veri, "hedef": {"segment": "hepsi"}})
    if hatalar:
        return JsonResponse({"ok": False, "hatalar": hatalar}, status=400)
    user = request.user
    if temiz["kanal"] == TopluGonderim.Kanal.EPOSTA:
        if not user.email:
            return JsonResponse({"ok": False, "mesaj": "Hesabınızda e-posta adresi yok."}, status=400)
        mesaj, _ = toplu.eposta_olustur(_taslak(temiz), user)
        mesaj.subject = f"[TEST] {mesaj.subject}"
        n = mesaj.send()
        return JsonResponse({"ok": bool(n), "mesaj": f"Test e-postası {user.email} adresine "
                             f"{'gönderildi' if n else 'GÖNDERİLEMEDİ (adres engelli olabilir)'}."})
    if not user.push_devices.exists():
        return JsonResponse({"ok": False, "mesaj": "Hesabınızda kayıtlı cihaz yok — uygulamaya "
                             "bu hesapla giriş yapıp bildirim izni verin."}, status=400)
    ok = push_to_user(user, title=toplu.kisisellestir(temiz["konu"], user),
                      body=toplu.kisisellestir(temiz["icerik"], user),
                      data={"type": "info", "toplu": "test"}, transactional=True)
    return JsonResponse({"ok": ok, "mesaj": "Test bildirimi cihazlarınıza gönderildi." if ok
                         else "Bildirim gönderilemedi (push tercihi kapalı ya da cihaz geçersiz)."})


@require_POST
def gonder(request):
    _yetki(request)
    veri = _json(request)
    temiz, hatalar = dogrula(veri)
    if hatalar:
        return JsonResponse({"ok": False, "hatalar": hatalar}, status=400)
    if veri.get("onay") is not True:
        return JsonResponse({"ok": False, "mesaj": "Gönderim onaylanmadı."}, status=400)
    g = toplu.olustur(temiz, request.user)
    return JsonResponse({"ok": True, "gonderim": toplu.ozet(g)})


@require_GET
def liste(request):
    _yetki(request)
    return JsonResponse({"gonderimler": [toplu.ozet(g) for g in
                                         TopluGonderim.objects.select_related("olusturan")[:15]]})


@require_GET
def detay(request, pk: int):
    _yetki(request)
    g = get_object_or_404(TopluGonderim, pk=pk)
    return JsonResponse({**toplu.ozet(g), "sebepler": toplu.atlanma_ozeti(g),
                         "icerik": g.icerik, "icerik_turu": g.icerik_turu,
                         "eposta_baslik": g.eposta_baslik, "on_metin": g.on_metin,
                         "dugme_metni": g.dugme_metni, "dugme_url": g.dugme_url})


@require_POST
def eylem(request, pk: int, ad: str):
    _yetki(request)
    g = get_object_or_404(TopluGonderim, pk=pk)
    if ad == "iptal":
        toplu.iptal_et(g)
    elif ad == "devam":
        toplu.devam_ettir(g)
    else:
        return JsonResponse({"ok": False}, status=404)
    g.refresh_from_db()
    return JsonResponse({"ok": True, "gonderim": toplu.ozet(g)})
