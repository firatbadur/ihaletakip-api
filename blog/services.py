"""Web portalına yeniden doğrulama (revalidate) çağrısı.

Web blog yanıtlarını 1 saate kadar önbellekte tutar; yayımlanan ya da düzeltilen yazının
hemen görünmesi için ona haber veririz.

⚠️ **Bu çağrı asla yazı kaydını düşürmez**: web kapalıysa, sır yanlışsa ya da ağ
yavaşsa hata yalnızca loglanır. En kötü hâlde web'in 1 saatlik önbelleği dolunca yazı
yine görünür — kaydı başarısız saymak editöre yanlış bir şey söylemek olurdu.
⚠️ Ayar boşsa (yerel geliştirme, test) **hiç istek atılmaz**.
"""
import logging

from django.conf import settings

logger = logging.getLogger("ihaletakip")

ZAMAN_ASIMI_SN = 5


def web_revalidate(slug: str | None = None) -> bool:
    url = getattr(settings, "WEB_REVALIDATE_URL", "")
    sir = getattr(settings, "WEB_REVALIDATE_SECRET", "")
    if not url or not sir:
        return False
    import requests  # hafif ama yalnızca burada lazım

    govde = {"tag": "blog"}
    if slug:
        govde["slug"] = slug
    try:
        r = requests.post(url, json=govde, timeout=ZAMAN_ASIMI_SN,
                          headers={"X-Revalidate-Secret": sir})
    except Exception as e:  # noqa: BLE001 — ağ hatası kaydı düşürmemeli
        logger.warning("Blog revalidate gönderilemedi (%s): %s", slug or "-", str(e)[:150])
        return False
    if r.status_code != 200:
        # ⚠️ Sır loglanmaz; yalnızca durum kodu.
        logger.warning("Blog revalidate reddedildi (%s): HTTP %s", slug or "-",
                       r.status_code)
        return False
    return True
