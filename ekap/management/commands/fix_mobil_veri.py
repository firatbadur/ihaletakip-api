"""
Mobil kaynaklı satırlarda **sonradan düzeltilen** iki alanı geriye dönük onarır.

1. **İlan HTML'i** — mobil API sayısal karakter referansı döndürüyor
   (`SA&#286;LIK` = "SAĞLIK"); v2 aynı belgeyi düz UTF-8 veriyor. Aynı kolonda iki
   kodlama, HTML olarak render etmeyen her yerde metni okunamaz gösteriyordu.
2. **Açıklama metinleri** — mobil ham metni v2'ninkinden farklı
   ("İhale İlanı Yayımlanmış/İlansız, Katılıma Açık" ↔ "İhale İlanı Yayımlanmış,
   Katılıma Açık"). Mobil uygulama bu alanı doğrudan gösterdiği için kaynağa göre
   değişmesi kullanıcıya görünen bir tutarsızlık.

3. **Durum/usul kolonları** — mobil liste yanıtı bu alanları hiç vermiyor ama
   `upsert_tender_from_list` onları koşulsuz yazdığı için her keşif turu, detay
   senkronunun yazdığı değeri siliyordu (1.991 mobil satırın 1.990'ında
   `ihale_durum` NULL'dı → "Katılıma Açık" filtresi bu ihaleleri göstermiyordu).

Üçü de ingest'te düzeltildi; bu komut **daha önce yazılmış** satırlar içindir.
Saf DB işidir: EKAP'a hiç gidilmez.
"""
from django.core.management.base import BaseCommand

from ekap.mobil import constants as C
from ekap.mobil.adapt import ILAN_ONEK, html_normalize
from ekap.models import Announcement, Tender


class Command(BaseCommand):
    help = "Mobil kaynaklı ilan HTML'i ve açıklama metinlerini onarır"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--batch", type=int, default=500)
        # ⚠️ Varsayılan AÇIK: liste upsert'i kaynağa bakmadan siliyordu, dolayısıyla
        # onarımın da kaynağa bakmaması doğrudur. Bayrak yalnızca kapsamı daraltmak
        # (mobil satırlarla sınırlamak) isteyen için var.
        parser.add_argument("--yalniz-mobil", dest="tum_kaynaklar",
                            action="store_false", default=True,
                            help="yalnızca detay_kaynak='mobil' satırları onar")

    def handle(self, *args, **o):
        self._html(o)
        # ⚠️ SIRA ÖNEMLİ: `_aciklamalar` metinleri **koddan** türetiyor, dolayısıyla
        # kodlar geri yazılmadan çalıştırılırsa hiçbir şey onarmaz.
        self._durumlar(o)
        self._aciklamalar(o)
        self._bos_aciklamalar(o)

    def _html(self, o):
        qs = Announcement.objects.filter(
            ekap_ilan_id__startswith=ILAN_ONEK
        ).only("id", "veri_html").iterator(chunk_size=o["batch"])
        bakilan = onarilan = 0
        yigin = []
        for a in qs:
            bakilan += 1
            yeni = html_normalize(a.veri_html or "")
            if yeni != (a.veri_html or ""):
                a.veri_html = yeni
                yigin.append(a)
                onarilan += 1
            if len(yigin) >= o["batch"] and not o["dry_run"]:
                Announcement.objects.bulk_update(yigin, ["veri_html"])
                yigin = []
        if yigin and not o["dry_run"]:
            Announcement.objects.bulk_update(yigin, ["veri_html"])
        self.stdout.write(f"ilan html : bakılan={bakilan} onarılan={onarilan}")

    def _durumlar(self, o):
        """
        Liste upsert'inin sildiği kolonları `detail_raw`'dan geri yazar.

        ⚠️⚠️ Kök neden (üretimde ölçüldü 2026-09-22): mobil liste yanıtı **altı
        alan** doldurup gerisini hiç vermiyor; `upsert_tender_from_list` o alanları
        koşulsuz yazdığı için **her keşif turu** detay senkronunun yazdığı durumu ve
        usul kodunu siliyordu. Mobil kaynaklı 1.991 satırın 1.990'ında `ihale_durum`
        NULL kalmıştı → mobil uygulamanın "Katılıma Açık" filtresi bu ihaleleri hiç
        göstermiyor, `DURUM_SONUCLANMIS` üstüne kurulu tazeleme/alarm mantığı da
        kör kalıyordu. Kök neden `upsert_tender_from_list(koruyucu=True)` ile
        kapatıldı; bu bölüm **daha önce yazılmış** satırlar içindir.

        ⚠️ Değer yeniden ÇIKARILMAZ, `detail_raw`'dan **okunur**: adapter onu zaten
        oraya yazmış (`item.ihaleDurum`). İkinci bir çıkarım yolu yazmak, tek
        çıkarım kaynağı kuralının ihlali olurdu (bkz. CLAUDE.md, mobil adapter).
        ⚠️ Boş değer YAZILMAZ: ham gövdede anahtar yoksa kolona dokunulmaz —
        "bilmiyoruz" ile "yok" aynı şey değil.
        """
        from django.db.models import F, Q

        from ekap.mobil.adapt import durum_kodu
        from ekap.sync import _as_int, detay_govdesi

        alanlar = ("ihale_durum", "ihale_usul", "ilan_var_mi")
        # ⚠️⚠️ **`detay_kaynak='mobil'` FİLTRESİ YETMEZ — ilk sürüm burada eksikti.**
        # Silme mobil **liste** yolundan geliyor, ama dokunduğu kaydın detayı v2'den
        # gelmiş olabilir; o satırlarda `detay_kaynak` boştur. Ölçüldü (2026-09-22,
        # ilk onarım turundan sonra): `detay_kaynak` boş **4.163** satırda
        # `ihale_durum` NULL, üstelik hepsinde `detail_raw` durumu taşıyor ve
        # hepsinde `list_synced_at > detail_synced_at` — yani liste, detaydan SONRA
        # çalışıp değeri silmiş. Bunların 1.682'si **gelecek tarihli**, yani
        # kullanıcının en çok isteyeceği açık ihaleler (mobil ekip bildirdi).
        # ⚠️ Kapsam bu yüzden **kaynağa değil, ARIZANIN İZİNE** bakar.
        kapsam = Q(detay_kaynak="mobil") | Q(
            ihale_durum__isnull=True, detail_synced_at__isnull=False
        )
        if not o["tum_kaynaklar"]:
            kapsam = Q(detay_kaynak="mobil")
        qs = Tender.objects.filter(kapsam).only(
            "id", "detail_raw", *alanlar
        ).iterator(chunk_size=o["batch"])
        bakilan = onarilan = 0
        yigin = []
        for t in qs:
            bakilan += 1
            data = detay_govdesi(t.detail_raw or {}) or {}
            bilgi = data.get("ihaleBilgi") or {}
            degisti = False

            durum = _as_int(data.get("ihaleDurum") or bilgi.get("ihaleDurum"))
            # ⚠️ Sentetik gövdede kod YOKSA ham metinden çözülür. Gerekçe: adapter o
            # satırı yazdığı anda metni tanımıyordu (ör. "Ön yeterlik henüz
            # yapılmamış" haritaya sonradan eklendi) ve kod çözülemediği için anahtarı
            # hiç koymadı. Harita büyüdükçe bu satırlar onarılabilir hâle gelir.
            # ⚠️ Bu **ikinci bir çıkarım yolu DEĞİL**: adapter'ın kendi `durum_kodu`
            # fonksiyonu çağrılır, dolayısıyla tek çıkarım kaynağı korunur.
            if not durum:
                durum = durum_kodu((t.detail_raw or {}).get("_ham", {}).get("ihaleDurumu"))
            if durum and t.ihale_durum != durum:
                t.ihale_durum = durum
                degisti = True
            usul = _as_int(data.get("ihaleUsul"))
            if usul and t.ihale_usul != usul:
                t.ihale_usul = usul
                degisti = True
            # ⚠️ Yalnızca pozitif yön: `ilanList` yokluğu "ilan yok" demek değil.
            if data.get("ilanList") and not t.ilan_var_mi:
                t.ilan_var_mi = True
                degisti = True

            if degisti:
                onarilan += 1
                yigin.append(t)
            if len(yigin) >= o["batch"] and not o["dry_run"]:
                Tender.objects.bulk_update(yigin, list(alanlar))
                yigin = []
        if yigin and not o["dry_run"]:
            Tender.objects.bulk_update(yigin, list(alanlar))
        self.stdout.write(
            f"durum/usul: bakılan={bakilan} onarılan={onarilan}"
            + (" (dry-run, yazılmadı)" if o["dry_run"] else "")
        )

    def _bos_aciklamalar(self, o):
        """
        Kodu dolu ama açıklaması **boş** satırlarda metni koddan doldurur.

        ⚠️ Gerekçe (mobil ekip bildirdi, 2026-09-22): liste upsert'i açıklama
        kolonlarını `""` ile eziyordu ve bu, kaynağı v2 olan satırlarda da oldu —
        `_durumlar` kodu geri yazdıktan sonra **4.163 satırda kod dolu, açıklama
        boştu**. Mobil uygulama bu metni doğrudan gösterdiği için kart durum
        etiketsiz kalıyordu.
        ⚠️ `_aciklamalar`'dan farkı: o, **mobil** satırların metnini v2'nin kanonik
        metniyle hizalar (kaynaklar arası tutarlılık). Bu bölüm kaynağa bakmaz ama
        **yalnızca BOŞ** olanı doldurur — EKAP'tan gelmiş dolu bir metni kanonik
        metinle değiştirmek bu bölümün işi değil, o sessiz bir veri değişikliği olurdu.
        """
        esler = (
            ("ihale_durum", "ihale_durum_aciklama", C.DURUM_ACIKLAMA),
            ("ihale_tip", "ihale_tipi_aciklama", C.TIP_ACIKLAMA),
            ("ihale_usul", "ihale_usul_aciklama", C.USUL_ACIKLAMA),
        )
        toplam = 0
        for kod_kolonu, metin_kolonu, harita in esler:
            for kod, metin in harita.items():
                if not metin:
                    continue
                n = Tender.objects.filter(**{
                    kod_kolonu: kod, metin_kolonu: "",
                })
                toplam += n.count() if o["dry_run"] else n.update(**{
                    metin_kolonu: metin,
                })
        self.stdout.write(
            f"boş metin : doldurulan={toplam}"
            + (" (dry-run, yazılmadı)" if o["dry_run"] else "")
        )

    def _aciklamalar(self, o):
        # ⚠️ Yalnızca mobil kaynaklı satırlar: v2'nin metni zaten kanoniktir.
        temel = Tender.objects.filter(detay_kaynak="mobil")
        toplam = 0
        for kod, metin in C.DURUM_ACIKLAMA.items():
            n = temel.filter(ihale_durum=kod).exclude(ihale_durum_aciklama=metin)
            toplam += n.count() if o["dry_run"] else n.update(
                ihale_durum_aciklama=metin)
        for kod, metin in C.TIP_ACIKLAMA.items():
            n = temel.filter(ihale_tip=kod).exclude(ihale_tipi_aciklama=metin)
            toplam += n.count() if o["dry_run"] else n.update(
                ihale_tipi_aciklama=metin)
        for kod, metin in C.USUL_ACIKLAMA.items():
            n = temel.filter(ihale_usul=kod).exclude(ihale_usul_aciklama=metin)
            toplam += n.count() if o["dry_run"] else n.update(
                ihale_usul_aciklama=metin)
        # ⚠️ **Kapsam KODU da onarılır**, yalnızca metin değil: ilk sürümde
        # `İstisna` ↔ `Kapsam Dışı` kodları ters yazılmıştı (2 ↔ 3) ve mobil
        # kaynaklı kayıtlar v2'den farklı kod taşıyordu → `yasa_kapsami` filtresi
        # iki kaynağı karıştırıyordu. Doğru kod, kaydın kendi açıklama metninden
        # yeniden türetilir.
        for metin_norm, kod in C.KAPSAM_METIN.items():
            metin = C.KAPSAM_ACIKLAMA.get(kod)
            if not metin:
                continue
            n = temel.filter(ihale_kapsam_aciklama=metin).exclude(yasa_kapsami=kod)
            toplam += n.count() if o["dry_run"] else n.update(yasa_kapsami=kod)
        for kod, metin in C.KAPSAM_ACIKLAMA.items():
            n = temel.filter(yasa_kapsami=kod).exclude(ihale_kapsam_aciklama=metin)
            toplam += n.count() if o["dry_run"] else n.update(
                ihale_kapsam_aciklama=metin)
        self.stdout.write(
            f"açıklama  : güncellenen={toplam}"
            + (" (dry-run, yazılmadı)" if o["dry_run"] else "")
        )
