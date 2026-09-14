"""
`_cached_rapor` — ağır rapor uçlarının önbelleği.

Bu testler önbelleğin **hızını** değil, sessizce yanlış veri servis etmesini
engelleyen üç davranışı korur. Üçü de üretimde fark edilmesi zor hatalardır.
"""

import threading
import time

from django.core.cache import cache
from django.http import QueryDict
from django.test import TestCase

from ekap.views import _cached_rapor


class RaporCacheTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_ikinci_cagri_hesaplamaz(self):
        """Önbelleğin asıl işi: aynı imza ikinci kez hesaplanmamalı."""
        cagri = []

        def hesapla():
            cagri.append(1)
            return {"n": 5}, None

        p = QueryDict("idare_id=42")
        self.assertEqual(_cached_rapor("t", p, hesapla)[0], {"n": 5})
        self.assertEqual(_cached_rapor("t", p, hesapla)[0], {"n": 5})
        self.assertEqual(len(cagri), 1, "ikinci çağrı önbellekten gelmeliydi")

    def test_maskeleme_onbellegi_kirletmez(self):
        """
        Free isteğinin maskelemesi önbellekteki kaydı bozmamalı.

        ⚠️ **Bu bir SÖZLEŞME testidir, regresyon testi değil**: bugün geçmesi
        `_cached_rapor`ın kopyasından değil, yapılandırılmış arka uçların
        serileştirmesinden geliyor (`RedisCache` serileştirir, `LocMemCache` de
        `pickle.dumps` ile saklar — ölçüldü). Kopyayı kaldırınca test **yeşil kalır**;
        dolayısıyla onu "kopya çalışıyor" kanıtı sanmayın.
        Değeri ileriye dönüktür: süreç-içi bir memo katmanı ya da serileştirmeyen bir
        önbellek arka ucu girdiği anda kırılır — ki o durumda ilk Free isteği kaydı
        kalıcı olarak maskeler ve **Pro kullanıcılar ödedikleri veriyi göremez**.
        """
        def hesapla():
            return {"indirim_orani": "0.21", "n": 7}, None

        p = QueryDict("x=1")

        veri, _ = _cached_rapor("b", p, hesapla)       # Free isteği
        veri["indirim_orani"] = None                    # view'daki maskeleme
        veri["kilitli"] = True

        veri2, _ = _cached_rapor("b", p, hesapla)       # Pro isteği
        self.assertEqual(veri2["indirim_orani"], "0.21", "Pro maskeli veri gördü")
        self.assertNotIn("kilitli", veri2)

    def test_hata_onbellege_alinmaz(self):
        """
        ⚠️ Geçici bir hata TTL boyunca kalıcı hataya dönüşmemeli.

        `documents/` ucunda yaşanan "bilmiyorum"u "yok" diye saklama arızasının
        (2026-09-11) aynısı olurdu.
        """
        durum = {"basarisiz": True}

        def hesapla():
            if durum["basarisiz"]:
                return None, "geçici hata"
            return {"n": 1}, None

        p = QueryDict("y=2")
        self.assertEqual(_cached_rapor("t", p, hesapla)[1], "geçici hata")
        durum["basarisiz"] = False
        veri, hata = _cached_rapor("t", p, hesapla)
        self.assertIsNone(hata)
        self.assertEqual(veri, {"n": 1})

    def test_ekstra_anahtari_ayirir(self):
        """
        ⚠️ Fiyat analizinde ihale **yol parametresidir**, query string'de yoktur.
        `ekstra` olmasa tüm ihaleler aynı anahtarı paylaşır ve kullanıcı başkasının
        ihalesinin analizini görürdü.
        """
        def yap(deger):
            return lambda: ({"ad": deger}, None)

        p = QueryDict("kapsam=auto")
        self.assertEqual(_cached_rapor("b", p, yap("A"), ekstra="1:20")[0], {"ad": "A"})
        self.assertEqual(_cached_rapor("b", p, yap("B"), ekstra="2:20")[0], {"ad": "B"})

    def test_farkli_param_farkli_anahtar(self):
        """Anahtar TÜM param'lardan üretilir; atlanan param yanlış rapor demekti."""
        def yap(deger):
            return lambda: ({"ad": deger}, None)

        self.assertEqual(
            _cached_rapor("t", QueryDict("idare_id=1"), yap("bir"))[0], {"ad": "bir"}
        )
        self.assertEqual(
            _cached_rapor("t", QueryDict("idare_id=2"), yap("iki"))[0], {"ad": "iki"}
        )

    def test_scope_anahtari_ayirir(self):
        """Aynı query string'le çağrılan iki ayrı uç aynı kaydı paylaşmamalı."""
        def yap(deger):
            return lambda: ({"ad": deger}, None)

        p = QueryDict("a=1")
        self.assertEqual(_cached_rapor("benchmark", p, yap("x"))[0], {"ad": "x"})
        self.assertEqual(_cached_rapor("idare_profil", p, yap("y"))[0], {"ad": "y"})


class TekUcusTest(TestCase):
    """
    Tek-uçuş: aynı raporu isteyen eşzamanlı istekler bir kez hesaplamalı.

    Üretimde ölçülen senaryo (2026-09-14): mobil istemci aynı raporu **aynı saniyede
    altı kez** istiyor. Önbellek tek başına işe yaramıyor çünkü altısı da ıskalıyor;
    altısı birlikte hesaplanınca aynı soğuk sayfalar için diskte yarışıyor ve tek
    başına 1,2 sn olan iş 113 sn'ye çıkıyordu.
    """

    def setUp(self):
        cache.clear()

    def test_alti_eszamanli_istek_bir_kez_hesaplar(self):
        cagri = []
        basladi = threading.Event()

        def hesapla():
            cagri.append(1)
            basladi.set()
            time.sleep(0.4)          # yavaş rapor
            return {"n": 3}, None

        sonuclar = []
        lock = threading.Lock()

        def istek():
            v, h = _cached_rapor("b", QueryDict("idare_detsis=1"), hesapla)
            with lock:
                sonuclar.append((v, h))

        lider = threading.Thread(target=istek)
        lider.start()
        basladi.wait(2)              # lider kilidi almış olsun
        digerleri = [threading.Thread(target=istek) for _ in range(5)]
        for t in digerleri:
            t.start()
        for t in [lider, *digerleri]:
            t.join(15)

        self.assertEqual(len(sonuclar), 6, "altı istek de yanıt almalı")
        self.assertTrue(all(v == {"n": 3} and h is None for v, h in sonuclar))
        self.assertEqual(len(cagri), 1, f"hesap {len(cagri)} kez yapıldı, 1 olmalıydı")

    def test_kilit_serbestse_hesaplar(self):
        """Tek istek (yarış yok) normal yolda hesaplamalı — kilit onu engellemesin."""
        cagri = []

        def hesapla():
            cagri.append(1)
            return {"n": 1}, None

        veri, hata = _cached_rapor("b", QueryDict("x=9"), hesapla)
        self.assertEqual((veri, hata), ({"n": 1}, None))
        self.assertEqual(len(cagri), 1)

    def test_hatali_liderden_sonra_kilit_serbest(self):
        """
        ⚠️ Lider hata dönerse kilit BIRAKILMALI (finally) ve sonuç önbelleğe
        girmemeli; aksi halde geçici bir hata kilidi TTL boyunca tutar ve o rapor
        kimse için hesaplanamaz hâle gelirdi.
        """
        durum = {"basarisiz": True}

        def hesapla():
            if durum["basarisiz"]:
                return None, "geçici"
            return {"n": 2}, None

        p = QueryDict("z=1")
        self.assertEqual(_cached_rapor("b", p, hesapla)[1], "geçici")
        durum["basarisiz"] = False
        self.assertEqual(_cached_rapor("b", p, hesapla)[0], {"n": 2})

