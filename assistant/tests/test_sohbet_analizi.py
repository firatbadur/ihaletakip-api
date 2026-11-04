"""
Üretim sohbet analizinden (2026-11-04) çıkan düzeltmelerin testleri — LLM ÇAĞIRMAZ.

Her test, kullanıcıya YANLIŞ bilgi veren bir vakayı tutar:
- "Bu işi kim aldı" → sözleşme DB'deyken "yüklenici adını göremiyorum".
- "11 belediye için kart çıkardım" → kullanıcı 3 kart gördü, 12 satır bekliyor kaldı.
- "Avcılar Fen İşleri kaydı yok" → ihaleleri `idare_id=575` ile duruyordu.
- "Doküman sayısı 0, idare yüklememiş" → mobil hat bu sayıyı hiç doldurmuyor.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from assistant.tools import TOOL_IMPL
from assistant.tools.context import ToolContext
from ekap.models import Authority, Contract, Contractor, Tender


def _ctx(user=None):
    return ToolContext(user=user, premium=True)


class KazananFirmaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tender = Tender.objects.create(
            ekap_id="sa-1", ikn="2025/888001", ihale_adi="Malzemeli Yemek Hizmeti",
            ihale_adi_norm="malzemeli yemek hizmeti", idare_adi="TEST POMEM",
            idare_id="8801", il_id=251, ihale_tip=3, ihale_durum=15, sozlesme_sayisi=1,
            dokuman_sayisi=0,
        )
        cls.firma = Contractor.objects.create(
            kanonik_ad="TAŞHAN YEMEK LTD ŞTİ", kanonik_anahtar="tashan yemek ltd sti",
            arama_norm="tashan yemek ltd sti",
        )
        Contract.objects.create(
            tender=cls.tender, yuklenici=cls.firma, yuklenici_adi="TAŞHAN YEMEK",
            sozlesme_bedeli_num=Decimal("58832499.11"), indirim_orani=Decimal("0.4283"),
        )
        cls.acik = Tender.objects.create(
            ekap_id="sa-2", ikn="2026/888002", ihale_adi="Açık İş", idare_adi="X",
            ihale_durum=2,
        )

    def test_ihale_detay_kazanan_firmayi_doner(self):
        sonuc = TOOL_IMPL["ihale_detay"](_ctx(), key="2025/888001")
        self.assertTrue(sonuc["ok"])
        self.assertEqual(len(sonuc["sozlesmeler"]), 1)
        s = sonuc["sozlesmeler"][0]
        self.assertEqual(s["yuklenici"], "TAŞHAN YEMEK LTD ŞTİ")
        self.assertEqual(s["yuklenici_id"], self.firma.pk)
        self.assertEqual(s["sozlesme_bedeli"], "58832499.11")   # string kalmalı
        self.assertEqual(s["indirim_orani"], "0.4283")          # ORAN, yüzde değil

    def test_sonuclanmamis_ihalede_liste_bos(self):
        self.assertEqual(TOOL_IMPL["ihale_detay"](_ctx(), key="2026/888002")["sozlesmeler"], [])

    def test_dokuman_sayisi_modele_VERILMEZ(self):
        """0 değeri "idare yüklememiş" diye okunuyordu; mobil hatta sayı hiç dolmuyor."""
        self.assertNotIn("dokuman_sayisi", TOOL_IMPL["ihale_detay"](_ctx(), key="2025/888001"))

    def test_secili_ihale_baglaminda_kazanan_ve_EKAP_yonlendirmesi_yok(self):
        from assistant.tasks import _selected_tender_context

        metin = _selected_tender_context(self.tender, date(2026, 11, 4))
        self.assertIn("TAŞHAN YEMEK LTD ŞTİ", metin)
        self.assertNotIn("Doküman sayısı", metin)
        self.assertNotIn("EKAP dokümanlarına yönlendir", metin)


class KartSiniriTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create(email="kart@test.local", username="kart")
        for i in range(5):
            Authority.objects.create(detsis_no=f"9900{i}", ad=f"TEST BELEDİYE {i}",
                                     ad_norm=f"test belediye {i}")

    def test_sinir_asilinca_satir_ACILMAZ_ve_model_uyarilir(self):
        from assistant.models import AssistantAction
        from assistant.tools.write import AZAMI_KART

        ctx = _ctx(user=self.user)
        sonuclar = [TOOL_IMPL["idare_favori_oner"](ctx, detsis_no=f"9900{i}") for i in range(5)]
        self.assertTrue(all(s["ok"] for s in sonuclar[:AZAMI_KART]))
        for s in sonuclar[AZAMI_KART:]:
            self.assertFalse(s["ok"])
            self.assertTrue(s["kart_siniri_doldu"])
        # Gösterilmeyecek kart için "bekliyor" satırı kalmamalı.
        self.assertEqual(AssistantAction.objects.filter(user=self.user).count(), AZAMI_KART)
        self.assertEqual(len(ctx.oneriler), AZAMI_KART)


class IdareAraYedekTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Authority.objects.create(detsis_no="77001", idare_id="35120",
                                 ad="AVCILAR BELEDİYESİ ORKESTRA MÜDÜRLÜĞÜ",
                                 ad_norm="avcilar belediyesi orkestra mudurlugu")
        for i in range(3):
            Tender.objects.create(
                ekap_id=f"av-{i}", ikn=f"2026/77700{i}", ihale_adi="Yol İşi",
                idare_adi="AVCILAR BELEDİYE BAŞKANLIĞI FEN İŞLERİ MÜDÜRLÜĞÜ",
                idare_adi_norm="avcilar belediye baskanligi fen isleri mudurlugu",
                idare_id="575",
            )
        # DETSIS'te zaten olan idare ihale kayıtlarından ikinci kez gelmemeli.
        Tender.objects.create(ekap_id="av-x", ikn="2026/777999", ihale_adi="Konser",
                              idare_adi="AVCILAR BELEDİYESİ ORKESTRA MÜDÜRLÜĞÜ",
                              idare_adi_norm="avcilar belediyesi orkestra mudurlugu",
                              idare_id="35120")

    def test_detsiste_olmayan_idare_ihale_kayitlarindan_bulunur(self):
        sonuc = TOOL_IMPL["idare_ara"](_ctx(), q="avcılar")
        self.assertTrue(sonuc["ok"])
        ek = [x for x in sonuc["liste"] if x.get("kaynak") == "ihale_kayitlari"]
        self.assertEqual([x["idare_id"] for x in ek], ["575"])
        self.assertIsNone(ek[0]["detsis_no"])     # favori kartı bunu istemez → model uyarılır
        self.assertIn("not_ihale_kayitlari", sonuc)
        self.assertEqual([x["idare_id"] for x in sonuc["liste"]].count("35120"), 1)

    def test_tam_ad_ile_de_bulunur(self):
        sonuc = TOOL_IMPL["idare_ara"](_ctx(), q="Avcılar Belediye Başkanlığı Fen İşleri")
        self.assertIn("575", [x["idare_id"] for x in sonuc["liste"]])


class PromptEnvanteriTests(TestCase):
    """Ürün envanterinden düşen bir satır, asistanı yanlış yönlendirmeye geri döndürür."""

    def test_kullanicinin_sordugu_ekranlar_envanterde(self):
        from assistant.prompts import AGENT_PERSONA_PROMPT, VERI_TUZAKLARI

        for ifade in ("Doğrudan Teminler", "KİK Kurul Kararları", "ihaletakip.com",
                      "Şifremi unuttum", "Yardım & Destek", "firma adına (kalem simgesi)"):
            self.assertIn(ifade, AGENT_PERSONA_PROMPT, ifade)
        self.assertIn("Doküman sayısı", VERI_TUZAKLARI)
        self.assertIn("sozlesmeler", VERI_TUZAKLARI)


class OlcumTests(TestCase):
    """Faz 0: araç izi + cevapsız işareti + rapor komutu."""

    @classmethod
    def setUpTestData(cls):
        from assistant.models import ChatConversation, ChatMessage

        cls.user = get_user_model().objects.create(email="olc@test.local", username="olc")
        konu = ChatConversation.objects.create(user=cls.user)
        cls.soru = ChatMessage.objects.create(user=cls.user, conversation=konu, role="user",
                                              content="Rize POMEM yemek işini kim yapıyor")
        ChatMessage.objects.create(
            user=cls.user, conversation=konu, role="assistant",
            content="Yüklenici adını bulamadım.",
            usage={"tur_sayisi": 7, "araclar": [{"arac": "ihale_ara", "ok": True, "n": 0}]},
        )
        ChatMessage.objects.create(user=cls.user, conversation=konu, role="user",
                                   content="Bilgisayardan nasıl giriş yaparım")

    def test_iz_ozeti_uzun_metni_keser_buyuk_yapilari_atar(self):
        from assistant.tasks import _iz_ozeti

        iz = _iz_ozeti([{"arac": "ihale_ara", "ok": True, "n": 0,
                         "param": {"q": "x" * 300, "filtreler": {"a": "y" * 400}, "il_id": [251]}}])
        self.assertEqual(len(iz[0]["param"]["q"]), 80)
        self.assertNotIn("filtreler", iz[0]["param"])
        self.assertEqual(iz[0]["param"]["il_id"], [251])
        self.assertEqual(iz[0]["n"], 0)

    def test_hata_isareti_payloada_yazilir(self):
        from assistant.tasks import _hatayi_isaretle

        _hatayi_isaretle(self.soru, "cokme")
        self.soru.refresh_from_db()
        self.assertEqual(self.soru.payload["hata"], "cokme")

    def test_rapor_cevapsiz_bilgisiz_ve_tavani_sayar(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("asistan_rapor", gun=7, stdout=out)
        metin = out.getvalue()
        self.assertIn("2 kullanıcı mesajı", metin)
        self.assertIn("cevapsız: 1", metin)
        self.assertIn("'bilgi yok' kalıplı cevap: 1", metin)
        self.assertIn("tur tavanı: 1", metin)
        self.assertIn("kim_aldi", metin)


class DogrudanTeminAraciTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from datetime import timedelta

        from django.utils import timezone

        from ekap.models import DogrudanTemin

        simdi, bugun = timezone.now(), timezone.localdate()
        ortak = dict(idare_adi="ERZURUM ATATÜRK ÜNİVERSİTESİ", idare_adi_norm="erzurum ataturk universitesi",
                     il_id=269, tur=1, ilan_tarihi=bugun)
        DogrudanTemin.objects.create(dtn="26DT1", ad="Tavuk Eti Alımı", ad_norm="tavuk eti alimi",
                                     durum=202, son_teklif=simdi + timedelta(days=2), **ortak)
        # Süresi geçmiş — açık listede OLMAMALI (tanım dt.acik_q'dan gelir).
        DogrudanTemin.objects.create(dtn="26DT2", ad="Tavuk Alımı Eski", ad_norm="tavuk alimi eski",
                                     durum=202, son_teklif=simdi - timedelta(days=1), **ortak)

    def test_acik_dt_bulunur_suresi_gecen_dusulur(self):
        sonuc = TOOL_IMPL["dogrudan_temin_ara"](_ctx(), q="tavuk", il_id=[269])
        self.assertTrue(sonuc["ok"])
        self.assertEqual([x["dtn"] for x in sonuc["liste"]], ["26DT1"])
        self.assertEqual(sonuc["liste"][0]["tur"], "Mal")

    def test_sadece_acik_false_hepsini_getirir(self):
        sonuc = TOOL_IMPL["dogrudan_temin_ara"](_ctx(), q="tavuk", sadece_acik=False)
        self.assertEqual(sonuc["toplam"], 2)

    def test_acik_tanimi_liste_ucuyla_ayni(self):
        from ekap import dt_sorgu

        beklenen = set(dt_sorgu.acik_qs().values_list("dtn", flat=True))
        sonuc = TOOL_IMPL["dogrudan_temin_ara"](_ctx())
        self.assertEqual({x["dtn"] for x in sonuc["liste"]}, beklenen)


class SonucAlarmiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create(email="al@test.local", username="al")
        cls.tender = Tender.objects.create(ekap_id="al-1", ikn="2026/666001", ihale_adi="104 Adet İşyeri")

    def test_alarm_varsayilan_olarak_SONUCU_bildirir(self):
        from assistant.models import AssistantAction

        ctx = _ctx(user=self.user)
        ctx.kart_ekle(self.tender)
        sonuc = TOOL_IMPL["alarm_kur_oner"](ctx, ikn="2026/666001")
        eylem = AssistantAction.objects.get(id=sonuc["action_id"])
        self.assertTrue(eylem.params["completed"])
        self.assertFalse(eylem.params["kurul_karar"])
        self.assertIn("sonuçlanınca", eylem.ozet)

    def test_alarm_parametreleri_modelde_gecerli_alan(self):
        """Yürütme `TenderAlarm.update_or_create(**params)` yapar; alan adı yanlışsa çöker."""
        from tenders.models import TenderAlarm

        alanlar = {f.name for f in TenderAlarm._meta.get_fields()}
        ctx = _ctx(user=self.user)
        ctx.kart_ekle(self.tender)
        from assistant.models import AssistantAction

        eylem = AssistantAction.objects.get(
            id=TOOL_IMPL["alarm_kur_oner"](ctx, ikn="2026/666001", kurul_karari=True)["action_id"])
        self.assertTrue(set(eylem.params) <= alanlar, set(eylem.params) - alanlar)


class IdareYuklenicileriTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from datetime import datetime, timezone as tz

        eski = Tender.objects.create(ekap_id="iy-1", ikn="2024/1", ihale_adi="Yemek Hizmeti 2024",
                                     ihale_adi_norm="yemek hizmeti 2024", idare_id="501")
        yeni = Tender.objects.create(ekap_id="iy-2", ikn="2025/2", ihale_adi="Yemek Hizmeti 2025",
                                     ihale_adi_norm="yemek hizmeti 2025", idare_id="501")
        temizlik = Tender.objects.create(ekap_id="iy-3", ikn="2025/3", ihale_adi="Temizlik",
                                         ihale_adi_norm="temizlik", idare_id="501")
        for t, ad, yil in ((eski, "ESKİ YEMEK", 2024), (yeni, "BEŞTEPE YEMEK", 2025),
                           (temizlik, "TEMİZ AŞ", 2025)):
            Contract.objects.create(tender=t, idare_id="501", yuklenici_adi=ad,
                                    sozlesme_tarihi=datetime(yil, 6, 1, tzinfo=tz.utc),
                                    sozlesme_bedeli_num=Decimal("100"))

    def test_en_yeni_sozlesme_basta_konu_suzulur(self):
        ctx = _ctx()
        sonuc = TOOL_IMPL["idare_yuklenicileri"](ctx, idare_id="501", konu="yemek")
        self.assertTrue(sonuc["ok"])
        self.assertEqual([x["yuklenici"] for x in sonuc["liste"]], ["BEŞTEPE YEMEK", "ESKİ YEMEK"])
        self.assertIn("2025/2", ctx.card_pool)

    def test_idare_verilmezse_duzgun_hata(self):
        self.assertFalse(TOOL_IMPL["idare_yuklenicileri"](_ctx())["ok"])


class OncekiOturumTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from assistant.models import ChatConversation, ChatMessage

        cls.user = get_user_model().objects.create(email="hf@test.local", username="hf")
        cls.tender = Tender.objects.create(ekap_id="hf-1", ikn="2026/444001",
                                           ihale_adi="Ambar Binası Yapım İşi", idare_adi="MARDİN")
        Tender.objects.create(ekap_id="hf-2", ikn="2026/444002", ihale_adi="Öneri İhalesi")
        eski = ChatConversation.objects.create(user=cls.user)
        ChatMessage.objects.create(user=cls.user, conversation=eski, role="user",
                                   content="Ambar yapım işi var mı")
        ChatMessage.objects.create(user=cls.user, conversation=eski, role="assistant",
                                   content="2026/444001 ...",
                                   payload={"kind": "text", "tender_cards": [{"ikn": "2026/444001"}]})
        ozet = ChatConversation.objects.create(user=cls.user, kind="digest")
        ChatMessage.objects.create(user=cls.user, conversation=ozet, role="assistant", content="Günaydın",
                                   payload={"kind": "digest", "tender_cards": [{"ikn": "2026/444002"}]})
        cls.yeni = ChatConversation.objects.create(user=cls.user)

    def test_baska_oturumdaki_ihale_ve_soru_baglama_girer_oneri_GIRMEZ(self):
        from assistant.tasks import _onceki_oturum_baglami

        metin, ihaleler = _onceki_oturum_baglami(self.user, self.yeni)
        self.assertIn("2026/444001", metin)
        self.assertIn("Ambar yapım işi var mı", metin)
        self.assertNotIn("2026/444002", metin)
        self.assertEqual([t.ikn for t in ihaleler], ["2026/444001"])

    def test_bos_gecmiste_none(self):
        from assistant.tasks import _onceki_oturum_baglami

        kisi = get_user_model().objects.create(email="bos@test.local", username="bos")
        self.assertIsNone(_onceki_oturum_baglami(kisi, None))


class KelimeKelimeAramaTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()   # `_cached_count` sayısı süreç içinde testler arası sızar

    @classmethod
    def setUpTestData(cls):
        Tender.objects.create(
            ekap_id="kk-1", ikn="2026/1752648",
            ihale_adi="Afyonkarahisar İli Emirdağ İlçesi 104 Adet İşyeri İnşaatı",
            ihale_adi_norm="afyonkarahisar ili emirdag ilcesi 104 adet isyeri insaati",
            idare_adi="TOKİ", idare_adi_norm="toki",
        )

    def test_kelime_sirasi_fark_etmez(self):
        ctx = _ctx()
        sonuc = TOOL_IMPL["ihale_ara"](ctx, q="Emirdağ Afyonkarahisar 104 işyeri")
        self.assertEqual(sonuc["toplam"], 1)
        self.assertEqual(sonuc["kelime_eslesmesi"], ["emirdag", "afyonkarahisar", "104", "isyeri"])
        self.assertIn("2026/1752648", ctx.card_pool)

    def test_tam_ifade_tutarsa_yedek_devreye_GIRMEZ(self):
        sonuc = TOOL_IMPL["ihale_ara"](_ctx(), q="emirdağ ilçesi")
        self.assertEqual(sonuc["toplam"], 1)
        self.assertNotIn("kelime_eslesmesi", sonuc)

    def test_eksik_kelimede_sonuc_yok_ve_esanlam_onerilir(self):
        sonuc = TOOL_IMPL["ihale_ara"](_ctx(), q="Emirdağ 104 dükkan")
        self.assertEqual(sonuc["toplam"], 0)
        self.assertIn("dükkan → işyeri", sonuc["not"])


class TekrarEdenTekIhaleTests(TestCase):
    """Seriye ait ihalede tek-ihale kipi KeyError ile düşüyordu (üretim log'u 2026-11-03)."""

    @classmethod
    def setUpTestData(cls):
        from datetime import date as d

        ortak = dict(seri_anahtar="s" * 40, idare_id="777", idare_adi="PENDİK BELEDİYESİ")
        cls.bu_yil = Tender.objects.create(ekap_id="te-1", ikn="2026/1916586",
                                           ihale_adi="Taziye İkram Seti Alımı", **ortak)
        gecen = Tender.objects.create(ekap_id="te-2", ikn="2025/1668453", ihale_adi="Taziye İkram Seti",
                                      ilan_tarihi=d(2025, 11, 6), yaklasik_maliyet_num=Decimal("4278750"),
                                      toplam_sozlesme_bedeli=Decimal("3850000"), **ortak)
        Contract.objects.create(tender=gecen, yuklenici_adi="İKRAM GIDA LTD",
                                sozlesme_bedeli_num=Decimal("3850000"))

    def test_gecen_yilin_ym_si_ve_kazanani_doner(self):
        ctx = _ctx()
        sonuc = TOOL_IMPL["tekrar_eden_ihaleler"](ctx, key="2026/1916586")
        self.assertTrue(sonuc["ok"], sonuc)
        g = sonuc["gecmis"][0]
        self.assertEqual(g["ikn"], "2025/1668453")
        self.assertEqual(g["yaklasik_maliyet"], "4278750.00")
        self.assertEqual(g["sozlesme_bedeli"], "3850000.00")
        self.assertEqual(g["kazanan"], ["İKRAM GIDA LTD"])
        self.assertIn("2025/1668453", ctx.card_pool)
