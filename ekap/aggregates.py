"""
Yüzdelik (percentile) agregaları — Django 5.1'de yerleşik karşılığı yoktur.

PostgreSQL'in **ordered-set** agregaları `PERCENTILE_CONT` / `PERCENTILE_DISC`,
`WITHIN GROUP (ORDER BY ...)` sözdizimi ister; Django'nun `Aggregate` sınıfı bunu
`template` ile üretebilir.

## Hangisi ne zaman

| Veri | Kullan | Neden |
|---|---|---|
| Para (`NUMERIC`) | **`PercentileDisc`** | Girdi tipini korur ve **gerçekten gözlenmiş** bir değer döner |
| Oran/skor | `PercentileCont` | Ara değer üretmek anlamlı |

⚠️ **`PERCENTILE_CONT` para için KULLANILMAZ.** Yalnızca `double precision` kabul eder;
`NUMERIC(20,2)` bir sözleşme bedeli sessizce float'a çevrilir, iki değer arasında
enterpolasyon yapılır ve sonuçta **hiç var olmamış** bir tutar döner. Kullanıcıya
"tipik sözleşme bedeli" diye 1.234.567,89 gösterip o bedelde bir sözleşme
bulunmaması, ürün açısından da yanlıştır. `PERCENTILE_DISC` gerçek bir satırın
değerini döndürür.

## Dizi formu şart

Üç ayrı `PERCENTILE_CONT(0.25)` / `(0.5)` / `(0.75)` çağrısı, farklı *direct argument*
taşıdıkları için Postgres'te **üç bağımsız transition state** ve dolayısıyla **üç ayrı
sort** üretir. `ARRAY[0.25,0.5,0.75]` tek sortla üçünü birden verir.

## Güvenlik

⚠️ `%(percentile)s` şablona **ham** gömülür (ordered-set agregalarında direct argument
parametre olamaz). Bu yüzden yüzdelik listesi **yalnızca modül seviyesindeki sabitlerden**
gelir; **asla** `request.query_params`'tan türetilmez.
"""
from django.contrib.postgres.fields import ArrayField
from django.db.models import Aggregate, DecimalField, FloatField

# Ham SQL'e gömülecek tek yüzdelik kümesi. Kullanıcı girdisinden ASLA üretilmez.
QUARTILES = (0.25, 0.5, 0.75)
MEDIAN = (0.5,)


def _array_literal(yuzdelikler) -> str:
    """`(0.25, 0.5)` → `ARRAY[0.25,0.5]`. Girdi float'a zorlanır (enjeksiyon kapısı kapalı)."""
    return "ARRAY[" + ",".join(repr(float(p)) for p in yuzdelikler) + "]"


class _OrderedSetPercentile(Aggregate):
    """
    Ordered-set percentile agregası ortak tabanı.

    ⚠️ Şablona **`%(filter)s` YAZILMAZ**. Django bu yer tutucuyu `extra_context`'e
    yalnızca `filter=` verildiğinde koyar; şablonda koşulsuz durursa filtresiz her çağrı
    `KeyError: 'filter'` ile patlar. Doğru mekanizma `filter_template`'tir — Django taban
    şablonu onunla sarar ve `PERCENTILE_… WITHIN GROUP (…) FILTER (WHERE …)` geçerli
    SQL'dir, yani varsayılan sarmalayıcı olduğu gibi çalışır.
    """

    template = "%(function)s(%(percentile)s) WITHIN GROUP (ORDER BY %(expressions)s)"
    allow_distinct = False
    window_compatible = False

    def __init__(self, expression, yuzdelikler=QUARTILES, **extra):
        self.yuzdelikler = tuple(yuzdelikler)
        super().__init__(
            expression, percentile=_array_literal(self.yuzdelikler), **extra
        )


class PercentileDisc(_OrderedSetPercentile):
    """
    `PERCENTILE_DISC` — **gerçekten gözlenmiş** bir değer döner, girdi tipini korur.

    Para alanları için kullanın (bkz. modül docstring'i).
    """

    function = "PERCENTILE_DISC"

    @property
    def output_field(self):
        return ArrayField(DecimalField(max_digits=20, decimal_places=2))


class PercentileCont(_OrderedSetPercentile):
    """
    `PERCENTILE_CONT` — enterpolasyonlu, `double precision` döner.

    Oran/skor alanları için kullanın; **para için kullanmayın**.
    """

    function = "PERCENTILE_CONT"

    @property
    def output_field(self):
        return ArrayField(FloatField())


def percentile_destekleniyor() -> bool:
    """
    Ordered-set agregaları yalnızca PostgreSQL'de var.

    Yerel geliştirme `DATABASE_URL` yoksa SQLite'a düşüyor (bkz. settings). Orada
    `PERCENTILE_DISC` "no such function" ile patlardı → çağıranlar bunu kontrol edip
    yüzdelikleri atlar, sayım ve ortalamalar (her iki DB'de de çalışır) yine döner.
    Aynı `_PostgresOnly` yaklaşımı migration'larda da kullanılıyor.
    """
    from django.db import connection

    return connection.vendor == "postgresql"


def yuzdelik_sozluk(deger, yuzdelikler=QUARTILES, basamak=None):
    """
    Agrega çıktısı olan diziyi `{"p25":…, "medyan":…, "p75":…}` sözlüğüne çevirir.

    `deger` `None` ise (hiç satır yok) `None` döner — sıfırla karıştırılmaz.
    """
    if not deger:
        return None
    adlar = {0.25: "p25", 0.5: "medyan", 0.75: "p75"}
    out = {}
    for p, v in zip(yuzdelikler, deger):
        if v is None:
            continue
        ad = adlar.get(p, f"p{int(p * 100)}")
        out[ad] = str(round(v, basamak)) if basamak is not None else str(v)
    return out or None
