r"""
`OkasItem.adi_norm` — Türkçe-güvenli OKAS adı araması (kolon ekleme adımı).

⚠️ **Neden gerekliydi:** `okas_adi` filtresi `adi__icontains` kullanıyordu ve bunun
İKİ ayrı hatası vardı:

1. **Türkçe katlama yok.** DB `icontains`/ILIKE İ↔i, ş↔s katlamasını yapmaz →
   kullanıcı küçük harf Türkçe yazınca ("işlem") sonuç **boş** dönüyordu. Kardeş
   alanların (`Tender.ihale_adi_norm`, `Authority.ad_norm`, `OkasCode.adi_norm`)
   hepsinde normalize sütun vardı; burası atlanmıştı.
2. **İndeks ölüydü.** Django `icontains`'i `UPPER(adi) LIKE UPPER(%s)` diye derler;
   kolonun üstündeki `UPPER()` trigram indeksini kullanılamaz kılar. Prod denetimi
   (`scripts/index_audit.sql`, 2026-08-13) `ekap_okasitem_adi_trgm`'i **75 MB /
   idx_scan = 0** ölü indeks olarak gösterdi. Doğru çözüm indeksi silmek değil,
   filtreyi normalize desenine çevirmekti — indeks o zaman gerçekten çalışır.

⚠️ **Bu migration yalnızca boş kolon ekler** (nullable/blank → metadata-only, 1,2M
satır yeniden yazılmaz). Sıra: kolon → `manage.py backfill_okas_norm` → indeks (`0019`).
Boş kolona trigram kurup ardından 1,2M satır UPDATE etmek indeksi şişirirdi.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ekap", "0017_drop_dead_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="okasitem",
            name="adi_norm",
            field=models.CharField(blank=True, max_length=500),
        ),
    ]
