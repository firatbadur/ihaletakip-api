# Pro paketlerinin mağaza fiyatları — `GET /api/v1/subscription/fiyatlar/`

Web Pro sihirbazının aylık / yıllık fiyat kartları için (portal isteği #18). Token'sız,
her zamanki zarfta döner.

```json
{
  "ulke": "TR",
  "guncellenme": "2026-10-29T04:20:12+03:00",
  "paketler": [
    {"kod": "aylik", "rc_paket": "$rc_monthly", "sure": "P1M", "magazalar": {
      "app_store":  {"fiyat": "249.99", "para_birimi": "TRY", "deneme": null},
      "play_store": {"fiyat": "249.99", "para_birimi": "TRY", "deneme": null}}}
  ]
}
```

- Paketler RevenueCat'in **current offering**'inden gelir (mobil ödeme ekranıyla aynı paketler,
  RC'deki `position` sırasıyla). `kod`: `haftalik` · `aylik` · `uc_aylik` · `alti_aylik` · `yillik`.
- Fiyat App Store Connect (TUR) ve Google Play (TR) API'lerinden okunur; KDV dahil müşteri
  fiyatı, **ondalık metin**. Okunamayan mağaza `null` — asla tahmin ya da `0`.
- `deneme`: yürürlükteki ücretsiz denemenin süresi (ISO 8601) ya da `null`.
- `guncellenme`: kaydın mağazadan en son **başarıyla** okunduğu an. Hiç kayıt yoksa
  `guncellenme: null`, `paketler: []`.
- Uç mağazaya istek anında **gitmez**; günlük görev (04:20) kaydı tazeler. Görev hata alırsa
  önceki kayıt ve `guncellenme` korunur.

## Kurulum (backend)

`.env.prod`:

| değişken | değer |
|---|---|
| `ASC_ISSUER_ID`, `ASC_KEY_ID` | App Store Connect → Users and Access → Integrations → App Store Connect API |
| `ASC_PRIVATE_KEY` | `.p8` dosyasının yolu (`/app/credentials/asc-api-key.p8`) ya da içeriği; rol App Manager veya Finance |
| `PLAY_SERVICE_ACCOUNT_JSON` | servis hesabı JSON yolu (`/app/credentials/play-service-account.json`) ya da içeriği; Play Console'da uygulamaya erişimi olmalı |
| `ASC_APP_ID` / `PLAY_PACKAGE_NAME` | varsayılanları `6761832614` / `com.envisoft.ihaletakip` |

Dosyalar `credentials/` dizinine konur (`web` ve `worker`'a salt okunur bağlı). Kimliği eksik
mağaza `null` döner, diğeri çalışmaya devam eder.

```bash
python manage.py magaza_fiyatlarini_guncelle --kuru    # oku + bas, kaydetme
python manage.py magaza_fiyatlarini_guncelle           # oku + kaydet (fiyat değişince)
python manage.py magaza_fiyatlarini_guncelle --durum   # saklanan kayıt + son deneme/hata
```
