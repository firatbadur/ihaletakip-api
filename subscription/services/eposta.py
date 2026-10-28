"""
Web ödemesi e-postaları (SES, `accounts/services/eposta_kod.py` deseni: HTML + düz metin).

| tür        | ne zaman                         |
|------------|----------------------------------|
| `alindi`   | ödeme `success` (+ işlem kaydı ekleri: ön bilgilendirme + mesafeli satış) |
| `fatura`   | fatura PDF'i hazır olunca (ayrı gelirse) |
| `hatirlat` | web Pro bitişine 30/7/1 gün kala |
| `bitti`    | web Pro bitince                   |
| `fesih`    | fesih talebi alındı               |
| `iade`     | istisnai iade yapıldı (admin)     |

⚠️ `alindi`/`fatura`/`fesih`/`iade` İŞLEM e-postasıdır: `email_notifications` kapalı
olsa da gider (ödeme kaydı kalıcı veri saklayıcı şartıdır). Hatırlatmalar tercihe uyar.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

SATIN_AL_URL = "/pro/satin-al"
HESABIM_URL = "/hesabim"


def _tarih(dt) -> str:
    return timezone.localtime(dt).strftime("%d.%m.%Y") if dt else "—"


def _tl(tutar) -> str:
    tam, _, kurus = f"{tutar:.2f}".partition(".")
    gruplu = f"{int(tam):,}".replace(",", ".")
    return f"{gruplu},{kurus}"


def _link(yol: str) -> str:
    return settings.EPOSTA_LINK_TABANI.rstrip("/") + yol


def _ad(user) -> str:
    return user.display_name or user.first_name or (user.email or "").split("@")[0]


def _gonder(user, konu: str, baglam: dict, ekler=()) -> None:
    baglam = {"ad": _ad(user), "paragraflar": [], "satirlar": [], "notlar": [], **baglam,
              "baslik": baglam.get("baslik") or konu}
    mesaj = EmailMultiAlternatives(
        subject=konu,
        body=render_to_string("eposta/odeme.txt", baglam),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    mesaj.attach_alternative(render_to_string("eposta/odeme.html", baglam), "text/html")
    for ad, icerik, tur in ekler:
        mesaj.attach(ad, icerik, tur)
    mesaj.send(fail_silently=False)


def _islem_kaydi(odeme, belge_adi: str) -> str:
    g = getattr(odeme, "grant", None)
    return render_to_string("sozlesme/islem_kaydi.html", {
        "belge_adi": belge_adi,
        "odeme": odeme,
        "fatura": odeme.billing_snapshot or {},
        "eposta": odeme.user.email,
        "tutar": _tl(odeme.amount),
        "donem": f"{_tarih(g.starts_at)} – {_tarih(g.ends_at)}" if g else "—",
        "onay_zamani": (odeme.consents or {}).get("at", ""),
        "satici": settings.SATICI_BILGI,
    })


def _fatura_eki(odeme):
    if not odeme.invoice_pdf:
        return None
    with odeme.invoice_pdf.open("rb") as f:
        return (f"fatura-{odeme.invoice_number or odeme.pk}.pdf", f.read(), "application/pdf")


def odeme_alindi(odeme) -> None:
    g = getattr(odeme, "grant", None)
    satirlar = [
        ("Tutar", f"{_tl(odeme.amount)} TL (KDV dahil)"),
        ("Pro başlangıç", _tarih(g.starts_at) if g else "—"),
        ("Pro bitiş", _tarih(g.ends_at) if g else "—"),
    ]
    if odeme.card_last4:
        satirlar.append(("Kart", f"•••• {odeme.card_last4}"))
    ekler = [
        (f"on-bilgilendirme-{odeme.pk}.html",
         _islem_kaydi(odeme, "Ön Bilgilendirme Formu"), "text/html"),
        (f"mesafeli-satis-sozlesmesi-{odeme.pk}.html",
         _islem_kaydi(odeme, "Mesafeli Satış Sözleşmesi"), "text/html"),
    ]
    fatura = _fatura_eki(odeme)
    if fatura:
        ekler.append(fatura)
    _gonder(odeme.user, "İhale Takip Pro ödemeniz alındı", {
        "paragraflar": [
            "Ödemeniz başarıyla alındı, Pro üyeliğiniz açıldı. Teşekkür ederiz!",
            "Bu bir tek seferlik ödemedir; otomatik yenileme yoktur, kartınızdan "
            "yeniden ücret alınmaz.",
        ],
        "satirlar": satirlar,
        "buton_metin": "Hesabıma git", "buton_url": _link(HESABIM_URL),
        "notlar": [
            "Ön bilgilendirme formu ve mesafeli satış sözleşmesinin işlem kaydı ektedir.",
            "Faturanız ekte." if fatura else "Faturanız hazır olunca ayrıca gönderilecek.",
        ],
    }, ekler)


def fatura_hazir(odeme) -> None:
    fatura = _fatura_eki(odeme)
    if not fatura:
        return
    _gonder(odeme.user, "İhale Takip faturanız", {
        "paragraflar": ["İhale Takip Pro ödemenizin faturası ektedir."],
        "satirlar": [("Fatura no", odeme.invoice_number or "—"),
                     ("Tutar", f"{_tl(odeme.amount)} TL")],
    }, [fatura])


def sure_hatirlat(user, bitis, kalan_gun: int) -> None:
    _gonder(user, f"Pro süreniz {_tarih(bitis)} tarihinde bitiyor", {
        "paragraflar": [
            (f"İhale Takip Pro üyeliğiniz {kalan_gun} gün sonra, {_tarih(bitis)} "
             "tarihinde sona eriyor." if kalan_gun > 0 else
             f"İhale Takip Pro üyeliğiniz bugün ({_tarih(bitis)}) sona eriyor."),
            "Web'den alınan Pro otomatik yenilenmez. Kesintisiz devam etmek için süreyi "
            "uzatabilirsiniz; yeni yıl mevcut sürenizin üstüne eklenir.",
        ],
        "buton_metin": "Süreyi uzat", "buton_url": _link(SATIN_AL_URL),
    })


def sure_bitti(user, bitis) -> None:
    _gonder(user, "Pro süreniz sona erdi", {
        "paragraflar": [
            f"İhale Takip Pro üyeliğiniz {_tarih(bitis)} tarihinde sona erdi. Kayıtlı "
            "ihaleleriniz ve filtreleriniz duruyor; Pro özellikleri yeniden açmak için "
            "süreyi uzatabilirsiniz.",
        ],
        "buton_metin": "Pro'yu yeniden aç", "buton_url": _link(SATIN_AL_URL),
    })


def fesih_alindi(odeme, pro_until) -> None:
    _gonder(odeme.user, "Fesih talebiniz alındı", {
        "paragraflar": [
            f"Fesih talebiniz {_tarih(odeme.cancel_requested_at)} tarihinde kaydedildi.",
            f"Pro üyeliğiniz {_tarih(pro_until)} tarihine kadar açık kalacak; sonrasında "
            "otomatik olarak yenilenmez.",
            "Hizmet satın alma anında ifa edilen dijital hizmet olduğundan, satın alma "
            "sırasında onayladığınız cayma hakkı istisnası gereği ödeme iade edilmez.",
        ],
    })


def iade_yapildi(odeme, pro_until) -> None:
    _gonder(odeme.user, "Ödemeniz iade edildi", {
        "paragraflar": [
            f"{_tarih(odeme.paid_at)} tarihli {_tl(odeme.amount)} TL ödemeniz iade edildi. "
            "Tutarın kartınıza yansıması bankanıza göre birkaç iş günü sürebilir.",
            (f"Pro üyeliğiniz {_tarih(pro_until)} tarihine kadar açık." if pro_until
             else "Bu ödemeyle açılan Pro süresi kapatıldı."),
        ],
    })
