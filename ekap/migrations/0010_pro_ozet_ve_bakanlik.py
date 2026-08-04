"""
Pro veri katmanı — Adım 0: sözleşme özeti + bakanlık kolonları.

⚠️ **Kolonlar burada, indeksler BURADA DEĞİL.** Sıra bilinçlidir:

    kolon ekle (bu migration)  →  arka planda doldur  →  AddIndexConcurrently (sonra)

- Nullable / sabit-default kolon eklemek PG11+'da **metadata-only**'dir: 1M satırlık
  `ekap_tender` yeniden yazılmaz, işlem milisaniyeler sürer.
- İndeks bu aşamada kurulsaydı, sonraki doldurma ~1M satırı UPDATE ederken her indeks
  girdisini yeniden yazıp indeksi şişirirdi. Doldurma bitince kurulan indeks hem kompakt
  olur hem de iş bir kez yapılır.
- `db_index=True` bu yüzden model alanlarında da YOK (Django onu `AddField` içinde düz
  `CREATE INDEX` olarak üretir → dolu tabloda ACCESS EXCLUSIVE kilidi).

**`RunPython` yoktur.** Kod tabanının kuralı: doldurma migration'da değil, zaman bütçeli
görev/komutta yapılır — deploy'daki healthcheck penceresi riske girmesin
(bkz. `ekap/management/commands/rebuild_contractors.py` docstring'i).

Doldurma nasıl olacak (ek maliyet YOK):
- `sozlesme_sayisi` / `toplam_sozlesme_bedeli`: `sync_contracts_from_raw` zaten aynı
  satırı güncelliyor ve değerler elinde → hâlihazırda arşivi gezen `sync_contractors`
  süpürmesi bunları kendiliğinden dolduruyor. Ek `detail_raw` okuması yok.
- `en_ust_idare_*`: `upsert_tender_detail` yeni/tazelenen kayıtlarda dolduruyor;
  arşivin kalanı Adım 1'in `detail_raw` taramasında yakalanacak.
"""
from django.db import migrations, models

from ._pg_ops import lock_timeout


class Migration(migrations.Migration):

    # `SET LOCAL` transaction kapsamlı → atomic ŞART.
    atomic = True

    dependencies = [
        ("ekap", "0009_missing_detail_partial_idx"),
    ]

    operations = [
        lock_timeout("5s"),
        migrations.AddField(
            model_name="tender",
            name="sozlesme_sayisi",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="tender",
            name="toplam_sozlesme_bedeli",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=20, null=True
            ),
        ),
        migrations.AddField(
            model_name="tender",
            name="en_ust_idare_kod",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="tender",
            name="en_ust_idare_adi",
            field=models.CharField(blank=True, max_length=500),
        ),
    ]
