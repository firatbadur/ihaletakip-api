"""
İdari şartname HTML'inden **OKAS kalemi** çıkarma.

⚠️ Mobil API OKAS kalemlerini ayrı bir alanda vermiyor; tek kaynak idari şartnamenin
içindeki ihtiyaç kalemi tablosudur. Yöntem: metindeki 8-9 haneli sayılar **DB'deki
OKAS kataloğuyla** (`OkasCode.kod`) kesiştirilir.

Ölçüm (25 ihale, 2026): kesinlik **%100** (35/35), duyarlılık **%97,2** (35/36);
kaynak 24/24 ihalede **idari şartname**, ilan HTML'i değil.

⚠️ **Katalogla kesiştirme ŞART.** Kataloğa bakmadan "8-9 haneli sayı = OKAS" demek
tarih, tutar, telefon ve dosya numaralarını da kalem sanır. Kesiştirme yanlış
pozitifi sıfırlıyor.
⚠️ Katalog boşsa (henüz `sync_okas` çalışmamış) fonksiyon **boş liste** döner; çağıran
bunu "veri yok" sayıp alana hiç dokunmamalıdır (bkz. `adapt.detaydan`).
"""
import logging
import re

logger = logging.getLogger("ihaletakip")

# OKAS kod uzunluğu sabit değil: canlı veride 8 ve 9 hane bir arada.
_ADAY = re.compile(r"(?<!\d)(\d{8,9})(?!\d)")
# Bir şartnamede makul kalem sayısı; üstü büyük olasılıkla ayrıştırma kazası.
AZAMI_KALEM = 200


def okas_cikar(html: str) -> list[dict]:
    """
    `[{"kodu": "15100000", "adi": "Et ve et ürünleri"}, ...]` döner.

    ⚠️ **Sıra korunur** (metinde ilk geçen önce): `sync.apply_pro_fields` listenin
    **ilk** elemanını birincil OKAS (`okas_ana_kod`) sayıyor.
    """
    if not html:
        return []
    from ..models import OkasCode

    adaylar, gorulen = [], set()
    for m in _ADAY.finditer(html):
        kod = m.group(1)
        if kod not in gorulen:
            gorulen.add(kod)
            adaylar.append(kod)
    if not adaylar:
        return []

    katalog = dict(
        OkasCode.objects.filter(kod__in=adaylar).values_list("kod", "adi")
    )
    if not katalog:
        return []
    return [
        {"kodu": kod, "adi": katalog[kod]}
        for kod in adaylar if kod in katalog
    ][:AZAMI_KALEM]
