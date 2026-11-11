"""
Asistan ekleri — uçlar, kota, tekilleştirme, görevler, temizlik (docs/plan-asistan-ekler.md).

Hazırlama dönüşümleri `test_ek_hazirla.py`'de. Burada Anthropic tamamen taklit edilir.
"""
import io
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from assistant.models import AssistantAttachment, ChatConversation, CompanyProfile
from assistant.tests.test_ek_hazirla import metinli_pdf

_SAYFA = "\n".join(f"Madde {i}. Yuklenici isi teknik sartnameye uygun yapacaktir." for i in range(8))


def png_bayt(w=40, h=30):
    from PIL import Image

    tampon = io.BytesIO()
    Image.new("RGB", (w, h), "blue").save(tampon, "PNG")
    return tampon.getvalue()


class _EkTest(TestCase):
    URL = "/api/v1/assistant/attachments/"

    def setUp(self):
        from django.core.cache import cache

        cache.clear()  # günlük kota sayacı Redis'te; testler arasında taşınmasın
        self.dizin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dizin, ignore_errors=True)
        ayar = override_settings(ASSISTANT_EK_KAPSAM="herkes", ASSISTANT_EK_DIR=self.dizin)
        ayar.enable()
        self.addCleanup(ayar.disable)
        User = get_user_model()
        self.user = User.objects.create_user(username="pro", email="pro@x.com", password="x",
                                             subscription_tier=User.Tier.PRO)
        CompanyProfile.objects.create(user=self.user, company_name="Firma")
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        # Görev kuyruğa atılır ama burada koşturulmaz (on_commit testte tetiklenmez).
        p = patch("assistant.tasks.ek_hazirla_task.delay")
        self.delay = p.start()
        self.addCleanup(p.stop)

    def yukle(self, ad="ts.pdf", bayt=None, **alanlar):
        bayt = metinli_pdf([_SAYFA]) if bayt is None else bayt
        with self.captureOnCommitCallbacks(execute=True):
            return self.api.post(self.URL, {"dosya": SimpleUploadedFile(ad, bayt), "ad": ad,
                                            **alanlar}, format="multipart")

    def hazir_ek(self, **alanlar):
        varsayilan = dict(user=self.user, tur="dokuman", kaynak="cihaz", ad="a.pdf",
                          durum="hazir", mod="metin", metin="içerik", token_sayisi=100,
                          sha256="x" * 64, expires_at=timezone.now() + timedelta(days=30))
        varsayilan.update(alanlar)
        return AssistantAttachment.objects.create(**varsayilan)


class YuklemeTest(_EkTest):
    def test_202_ve_gorev_kuyrugu(self):
        r = self.yukle(kaynak="ihale_dokumani", ikn="2026/123456", parca="Teknik Şartname")
        self.assertEqual(r.status_code, 202, r.content)
        ek = r.json()["data"]["ek"]
        self.assertEqual(ek["durum"], "hazirlaniyor")
        self.assertEqual(ek["tur"], "dokuman")
        self.assertEqual(ek["parca"], "Teknik Şartname")
        self.assertNotIn("metin", ek)
        self.assertNotIn("anthropic_file_id", ek)
        self.assertEqual(r.json()["data"]["gunluk_kalan"]["dokuman"], 19)
        self.delay.assert_called_once_with(ek["id"])
        satir = AssistantAttachment.objects.get(id=ek["id"])
        self.assertTrue(Path(satir.depo_yolu).exists())

    def test_free_403_premium_required(self):
        self.user.subscription_tier = self.user.Tier.FREE
        self.user.save(update_fields=["subscription_tier"])
        r = self.yukle()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["errors"]["code"], "premium_required")

    def test_profil_yok_400_kodlu(self):
        CompanyProfile.objects.filter(user=self.user).delete()
        r = self.yukle()
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"]["code"], "profil_yok")

    @override_settings(ASSISTANT_EK_KAPSAM="kapali")
    def test_kapsam_kapali_503(self):
        r = self.yukle()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["errors"]["code"], "ek_gecici_kapali")

    @override_settings(ASSISTANT_EK_AZAMI_BAYT=1000)
    def test_boyut_400_dosya_buyuk(self):
        r = self.yukle(bayt=b"%PDF" + b"0" * 2000)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"]["code"], "dosya_buyuk")
        self.assertEqual(AssistantAttachment.objects.count(), 0)
        self.assertEqual(list(Path(self.dizin).iterdir()), [])  # ham dosya kalmadı

    @override_settings(ASSISTANT_EK_FOTO_AZAMI_BAYT=100)
    def test_fotograf_kendi_sinirina_tabi(self):
        r = self.yukle(ad="foto.png", bayt=png_bayt())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"]["code"], "dosya_buyuk")

    def test_uzanti_yanlis_imza_dogru_kabul_tur_duzeltilir(self):
        r = self.yukle(ad="resim.pdf.bin", bayt=png_bayt(), tur="dokuman")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["data"]["ek"]["tur"], "fotograf")

    def test_ici_html_olan_pdf_gecersiz(self):
        r = self.yukle(ad="ts.pdf", bayt=b"<html><body>captcha</body></html>")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"]["code"], "gecersiz_dosya")

    def test_heic_desteklenmez(self):
        r = self.yukle(ad="IMG_1.heic", bayt=b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"]["code"], "desteklenmeyen_tur")

    @override_settings(ASSISTANT_EK_GUNLUK_DOKUMAN=1)
    def test_kota_429_sifirlanma_ve_limit(self):
        self.assertEqual(self.yukle().status_code, 202)
        r = self.yukle(bayt=metinli_pdf([_SAYFA + " ikinci"]))
        self.assertEqual(r.status_code, 429)
        hata = r.json()["errors"]
        self.assertEqual(hata["code"], "ek_kotasi")
        self.assertEqual(hata["limit"], 1)
        self.assertIn("T00:00:00", hata["sifirlanma"])

    @override_settings(ASSISTANT_EK_GUNLUK_DOKUMAN=1)
    def test_iade_edilen_hak_sayilmaz(self):
        self.hazir_ek(kota_sayildi=False, sha256="y" * 64)
        self.assertEqual(self.yukle().status_code, 202)

    @override_settings(ASSISTANT_EK_GUNLUK_DOKUMAN=1)
    def test_sohbet_silmek_hakki_geri_vermez(self):
        # Satır sayılsaydı: yükle → bağla → sohbeti sil (CASCADE) → hak yeniden dolu.
        r = self.yukle()
        ek = AssistantAttachment.objects.get(id=r.json()["data"]["ek"]["id"])
        sohbet = ChatConversation.objects.create(user=self.user)
        AssistantAttachment.objects.filter(id=ek.id).update(conversation=sohbet, sira=1)
        with patch("assistant.tasks.ek_dosyalarini_sil_task.delay"):
            sohbet.delete()
        self.assertFalse(AssistantAttachment.objects.exists())
        r2 = self.yukle(bayt=metinli_pdf([_SAYFA + " yeni"]))
        self.assertEqual(r2.status_code, 429)

    @override_settings(ASSISTANT_EK_GUNLUK_DOKUMAN=1)
    def test_bagsiz_eki_silmek_hakki_geri_verir(self):
        r = self.yukle()
        ek_id = r.json()["data"]["ek"]["id"]
        with patch("assistant.tasks.ek_dosyalarini_sil_task.delay"), \
                self.captureOnCommitCallbacks(execute=True):
            self.api.delete(f"{self.URL}{ek_id}/")
        self.assertEqual(self.yukle(bayt=metinli_pdf([_SAYFA + " yeni"])).status_code, 202)

    def test_kuyruk_erisilemezse_503_satir_ve_dosya_kalmaz(self):
        self.delay.side_effect = ConnectionError("redis yok")
        with patch("assistant.tasks.ek_dosyalarini_sil_task.delay"):
            r = self.yukle()
        self.assertEqual((r.status_code, r.json()["errors"]["code"]), (503, "ek_gecici_kapali"))
        self.assertFalse(AssistantAttachment.objects.exists())
        self.assertEqual(list(Path(self.dizin).iterdir()), [])
        self.assertEqual(self.api.get(f"{self.URL}limits/").json()["data"]["gunluk_kalan"]
                         ["dokuman"], 20)

    def test_tekrar_bagsiz_ek_aynisi_200_hak_yok(self):
        import hashlib

        bayt = metinli_pdf([_SAYFA])
        mevcut = self.hazir_ek(sha256=hashlib.sha256(bayt).hexdigest())
        r = self.yukle(bayt=bayt)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"]["ek"]["id"], str(mevcut.id))
        self.assertEqual(r.json()["data"]["ek"]["durum"], "hazir")
        self.assertEqual(AssistantAttachment.objects.count(), 1)
        self.delay.assert_not_called()

    def test_tekrar_baska_sohbetteki_ek_kopyalanir_kota_sayilmaz(self):
        import hashlib

        bayt = metinli_pdf([_SAYFA])
        sohbet = ChatConversation.objects.create(user=self.user)
        kaynak = self.hazir_ek(sha256=hashlib.sha256(bayt).hexdigest(), conversation=sohbet,
                               sira=1, anthropic_file_id="")
        r = self.yukle(bayt=bayt, parca="İdari Şartname")
        self.assertEqual(r.status_code, 200)
        kopya = AssistantAttachment.objects.get(id=r.json()["data"]["ek"]["id"])
        self.assertNotEqual(kopya.id, kaynak.id)
        self.assertFalse(kopya.kota_sayildi)
        self.assertEqual(kopya.metin, kaynak.metin)
        self.assertEqual(kopya.expires_at, kaynak.expires_at)  # süre uzatılamaz
        self.assertEqual(kopya.parca, "İdari Şartname")
        self.assertIsNone(kopya.conversation_id)

    def test_omru_kisa_ek_kopyalanmaz_yeniden_yuklenir(self):
        import hashlib

        bayt = metinli_pdf([_SAYFA])
        self.hazir_ek(sha256=hashlib.sha256(bayt).hexdigest(),
                      expires_at=timezone.now() + timedelta(days=2))
        self.assertEqual(self.yukle(bayt=bayt).status_code, 202)


class DetaySilmeSinirTest(_EkTest):
    def test_baskasinin_eki_404(self):
        diger = get_user_model().objects.create_user(username="d", email="d@x.com", password="x")
        ek = self.hazir_ek(user=diger)
        r = self.api.get(f"{self.URL}{ek.id}/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["errors"]["code"], "ek_bulunamadi")

    def test_detay_hata_mesaji_dolu(self):
        ek = self.hazir_ek(durum="hata", hata_kodu="cok_uzun", sayfa_sayisi=180)
        ek_json = self.api.get(f"{self.URL}{ek.id}/").json()["data"]["ek"]
        self.assertEqual(ek_json["hata_kodu"], "cok_uzun")
        self.assertIn("≈180 sayfa", ek_json["hata_mesaji"])

    def test_bagsiz_ek_silinir_hak_doner(self):
        ek = self.hazir_ek()
        with self.captureOnCommitCallbacks():
            r = self.api.delete(f"{self.URL}{ek.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(AssistantAttachment.objects.filter(id=ek.id).exists())
        self.assertEqual(r.json()["data"]["gunluk_kalan"]["dokuman"], 20)

    def test_bagli_ek_409(self):
        sohbet = ChatConversation.objects.create(user=self.user)
        ek = self.hazir_ek(conversation=sohbet, sira=1)
        r = self.api.delete(f"{self.URL}{ek.id}/")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["errors"]["code"], "ek_bagli")

    def test_limits(self):
        d = self.api.get(f"{self.URL}limits/").json()["data"]
        self.assertTrue(d["etkin"])
        self.assertEqual(d["foto_azami_bayt"], 10 * 1024 * 1024)
        self.assertGreater(d["yoklama_azami_sn"], 90)  # görev süresinden uzun
        self.assertIn({"uzanti": "pdf", "tur": "dokuman"}, d["turler"])

    @override_settings(ASSISTANT_EK_KAPSAM="staff")
    def test_kapsam_staff_kullanici_bazinda(self):
        self.assertFalse(self.api.get(f"{self.URL}limits/").json()["data"]["etkin"])
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.assertTrue(self.api.get(f"{self.URL}limits/").json()["data"]["etkin"])


class HazirlaGoreviTest(_EkTest):
    def _ek(self, bayt, ad="ts.pdf"):
        yol = Path(self.dizin) / "ham"
        yol.write_bytes(bayt)
        return AssistantAttachment.objects.create(
            user=self.user, tur="dokuman", kaynak="cihaz", ad=ad, uzanti=ad.rsplit(".", 1)[-1],
            depo_yolu=str(yol))

    def _istemci(self, token=500):
        istemci = MagicMock()
        istemci.messages.count_tokens.return_value = SimpleNamespace(input_tokens=token)
        istemci.files.upload.return_value = SimpleNamespace(id="file_x", expires_at=None)
        return istemci

    def test_basari_metin_yazilir_ham_dosya_silinir(self):
        from assistant.tasks import ek_hazirla_task

        ek = self._ek(metinli_pdf([_SAYFA]))
        with patch("assistant.services.attachments._istemci", return_value=self._istemci()):
            ek_hazirla_task(str(ek.id))
        ek.refresh_from_db()
        self.assertEqual(ek.durum, "hazir")
        self.assertEqual(ek.mod, "metin")
        self.assertEqual(ek.token_sayisi, 500)
        self.assertEqual(ek.depo_yolu, "")
        self.assertIsNotNone(ek.expires_at)
        self.assertEqual(list(Path(self.dizin).iterdir()), [])

    def test_kullanici_hatasi_hak_iade_edilmez(self):
        from assistant.tasks import ek_hazirla_task

        ek = self._ek(metinli_pdf([_SAYFA]))
        with patch("assistant.services.attachments._istemci",
                   return_value=self._istemci(token=999_999)):
            ek_hazirla_task(str(ek.id))
        ek.refresh_from_db()
        self.assertEqual((ek.durum, ek.hata_kodu), ("hata", "cok_uzun"))
        self.assertTrue(ek.kota_sayildi)

    def test_beklenmeyen_hata_okunamadi_hak_iade(self):
        from assistant.tasks import ek_hazirla_task

        ek = self._ek(metinli_pdf([_SAYFA]))
        with patch("assistant.services.attachments.hazirla", side_effect=RuntimeError("x")):
            ek_hazirla_task(str(ek.id))
        ek.refresh_from_db()
        self.assertEqual((ek.durum, ek.hata_kodu), ("hata", "okunamadi"))
        self.assertFalse(ek.kota_sayildi)
        self.assertEqual(list(Path(self.dizin).iterdir()), [])

    def test_hazirlanirken_silinen_ek_dirilmez_dosyasi_silinir(self):
        from assistant.tasks import ek_hazirla_task

        ek = self._ek(png_bayt(), ad="f.png")
        istemci = self._istemci()

        def _hazirla_ve_sil(e):
            AssistantAttachment.objects.filter(id=e.id).delete()
            return {"mod": "gorsel", "anthropic_file_id": "file_x", "token_sayisi": 2,
                    "expires_at": timezone.now() + timedelta(days=30)}

        with patch("assistant.services.attachments.hazirla", side_effect=_hazirla_ve_sil), \
                patch("assistant.services.attachments._istemci", return_value=istemci):
            ek_hazirla_task(str(ek.id))
        self.assertFalse(AssistantAttachment.objects.filter(id=ek.id).exists())
        istemci.files.delete.assert_called_once_with("file_x")


class TemizlikTest(_EkTest):
    def test_sinyal_dosya_silme_gorevini_kuyruklar(self):
        ek = self.hazir_ek(anthropic_file_id="file_1", mod="pdf")
        with patch("assistant.tasks.ek_dosyalarini_sil_task.delay") as sil, \
                self.captureOnCommitCallbacks(execute=True):
            ek.delete()
        sil.assert_called_once_with(["file_1"], [])

    def test_paylasilan_files_api_dosyasi_silinmez(self):
        from assistant.tasks import ek_dosyalarini_sil_task

        self.hazir_ek(anthropic_file_id="file_ortak", mod="pdf")
        istemci = MagicMock()
        with patch("assistant.services.attachments._istemci", return_value=istemci):
            ek_dosyalarini_sil_task(["file_ortak"], [])
        istemci.files.delete.assert_not_called()

    def test_deactivate_ekleri_siler(self):
        self.hazir_ek()
        with patch("assistant.tasks.ek_dosyalarini_sil_task.delay"):
            r = self.api.post("/api/v1/auth/deactivate/")
        self.assertLess(r.status_code, 300, r.content)
        self.assertFalse(AssistantAttachment.objects.filter(user=self.user).exists())

    def test_yetim_supurme_ayar_kapaliyken_hicbir_dosyaya_dokunmaz(self):
        from assistant.tasks import ek_temizligi

        istemci = MagicMock()
        istemci.files.list.return_value = [SimpleNamespace(
            id="file_yetim", filename="ihaletakip-ek-1.pdf",
            created_at=timezone.now() - timedelta(days=3))]
        with patch("assistant.services.attachments._istemci", return_value=istemci):
            ek_temizligi()
        istemci.files.list.assert_not_called()
        istemci.files.delete.assert_not_called()

    @override_settings(ASSISTANT_EK_YETIM_SUPURME=True)
    def test_gunluk_temizlik(self):
        from assistant.tasks import ek_temizligi

        simdi = timezone.now()
        sohbet = ChatConversation.objects.create(user=self.user)
        dolan = self.hazir_ek(conversation=sohbet, sira=1, mod="pdf", metin="",
                              anthropic_file_id="file_dolan", expires_at=simdi - timedelta(hours=1))
        bagsiz = self.hazir_ek(sha256="b" * 64)
        AssistantAttachment.objects.filter(id=bagsiz.id).update(
            created_at=simdi - timedelta(hours=25))
        takili = self.hazir_ek(durum="hazirlaniyor", sha256="c" * 64)
        AssistantAttachment.objects.filter(id=takili.id).update(
            created_at=simdi - timedelta(hours=2))
        self.hazir_ek(conversation=sohbet, sira=2, mod="gorsel", anthropic_file_id="file_canli",
                      sha256="d" * 64)

        eski = simdi - timedelta(days=3)
        dosyalar = [
            SimpleNamespace(id="file_yetim", filename="ihaletakip-ek-1.pdf", created_at=eski),
            SimpleNamespace(id="file_yeni", filename="ihaletakip-ek-2.pdf", created_at=simdi),
            SimpleNamespace(id="file_baska", filename="rapor.pdf", created_at=eski),
            SimpleNamespace(id="file_canli", filename="ihaletakip-ek-3.jpg", created_at=eski),
        ]
        istemci = MagicMock()
        istemci.files.list.return_value = dosyalar
        with patch("assistant.services.attachments._istemci", return_value=istemci), \
                patch("assistant.tasks.ek_dosyalarini_sil_task.delay"):
            ek_temizligi()

        dolan.refresh_from_db()
        self.assertEqual((dolan.durum, dolan.anthropic_file_id), ("suresi_doldu", ""))
        self.assertFalse(AssistantAttachment.objects.filter(id=bagsiz.id).exists())
        takili.refresh_from_db()
        self.assertEqual((takili.durum, takili.hata_kodu, takili.kota_sayildi),
                         ("hata", "okunamadi", False))
        silinen = [c.args[0] for c in istemci.files.delete.call_args_list]
        self.assertIn("file_dolan", silinen)
        self.assertIn("file_yetim", silinen)
        # ⚠️ Öneksiz, yeni ya da kullanılan dosyaya dokunulmaz.
        for korunan in ("file_yeni", "file_baska", "file_canli"):
            self.assertNotIn(korunan, silinen)


# ── Sohbete gönderme (ChatSendView) ─────────────────────────────────────────

class SohbetGondermeTest(_EkTest):
    CHAT = "/api/v1/assistant/chat/"

    def setUp(self):
        super().setUp()
        p = patch("assistant.tasks.assistant_chat_task.delay", return_value=SimpleNamespace(id="t1"))
        self.chat_delay = p.start()
        self.addCleanup(p.stop)

    def gonder(self, **govde):
        return self.api.post(self.CHAT, govde, format="json")

    def test_bos_mesaj_ek_varsayilan_soru_sira_ve_payload(self):
        ek = self.hazir_ek(ad="TEKNİK ŞARTNAME.docx")
        r = self.gonder(message="", ekler=[str(ek.id)], yetenekler=["ek"])
        self.assertEqual(r.status_code, 202, r.content)
        from assistant.models import ChatMessage

        msg = ChatMessage.objects.get(role="user")
        self.assertEqual(msg.content, "Bu dokümanı özetler misin?")
        self.assertEqual(msg.payload["yetenekler"], ["ek"])
        self.assertEqual(msg.payload["ekler"][0]["id"], str(ek.id))
        self.assertNotIn("metin", msg.payload["ekler"][0])
        ek.refresh_from_db()
        self.assertEqual((ek.sira, ek.message_id), (1, msg.id))
        self.assertEqual(ek.conversation.title, "Doküman: TEKNİK ŞARTNAME.docx")
        self.chat_delay.assert_called_once()

    def test_ikinci_ek_sira_iki(self):
        ek1, ek2 = self.hazir_ek(), self.hazir_ek(sha256="2" * 64)
        conv = self.gonder(message="a", ekler=[str(ek1.id)]).json()["data"]["conversation_id"]
        self.assertEqual(self.gonder(message="b", conversation=conv,
                                     ekler=[str(ek2.id)]).status_code, 202)
        ek2.refresh_from_db()
        self.assertEqual(ek2.sira, 2)

    def test_bos_mesaj_eksiz_400(self):
        self.assertEqual(self.gonder(message="").status_code, 400)

    def test_hazir_degil_409(self):
        ek = self.hazir_ek(durum="hazirlaniyor")
        r = self.gonder(message="x", ekler=[str(ek.id)])
        self.assertEqual((r.status_code, r.json()["errors"]["code"]), (409, "ek_hazir_degil"))

    def test_suresi_dolan_410(self):
        ek = self.hazir_ek(expires_at=timezone.now() + timedelta(minutes=30))
        r = self.gonder(message="x", ekler=[str(ek.id)])
        self.assertEqual((r.status_code, r.json()["errors"]["code"]), (410, "ek_suresi_doldu"))

    def test_baskasinin_eki_404_ve_hicbir_sey_yazilmaz(self):
        from assistant.models import ChatMessage

        diger = get_user_model().objects.create_user(username="d2", email="d2@x.com", password="x")
        ek = self.hazir_ek(user=diger)
        r = self.gonder(message="x", ekler=[str(ek.id)])
        self.assertEqual((r.status_code, r.json()["errors"]["code"]), (404, "ek_bulunamadi"))
        # atomic: sohbet ve mesaj da geri alındı
        self.assertFalse(ChatMessage.objects.exists())
        self.assertFalse(ChatConversation.objects.exists())

    @override_settings(ASSISTANT_EK_MESAJ_DOKUMAN=1)
    def test_mesaj_siniri_400(self):
        a, b = self.hazir_ek(), self.hazir_ek(sha256="3" * 64)
        r = self.gonder(message="x", ekler=[str(a.id), str(b.id)])
        self.assertEqual((r.status_code, r.json()["errors"]["code"]), (400, "mesaj_ek_siniri"))

    @override_settings(ASSISTANT_EK_SOHBET_DOKUMAN=1)
    def test_sohbet_siniri_409(self):
        sohbet = ChatConversation.objects.create(user=self.user)
        self.hazir_ek(conversation=sohbet, sira=1)
        ek = self.hazir_ek(sha256="4" * 64)
        r = self.gonder(message="x", conversation=sohbet.id, ekler=[str(ek.id)])
        self.assertEqual((r.status_code, r.json()["errors"]["code"]), (409, "sohbet_ek_siniri"))

    @override_settings(ASSISTANT_AGENT_ENABLED=False)
    def test_eski_yol_ekli_mesaji_503(self):
        ek = self.hazir_ek()
        r = self.gonder(message="x", ekler=[str(ek.id)])
        self.assertEqual((r.status_code, r.json()["errors"]["code"]), (503, "ek_gecici_kapali"))

    def test_profil_yok_kodlu(self):
        CompanyProfile.objects.filter(user=self.user).delete()
        r = self.gonder(message="merhaba")
        self.assertEqual((r.status_code, r.json()["errors"]["code"]), (400, "profil_yok"))
        self.assertEqual(r.json()["message"], "Önce firma profilinizi oluşturun.")

    def test_es_zamanli_baglama_ikinci_sohbete_kopya(self):
        # İki sohbete aynı anda gönderilen bağsız ek: koşullu UPDATE yalnızca birini
        # bağlar, öteki kopya alır (son yazan kazansaydı X'in mesajı görünmeyen eki gösterirdi).
        from assistant.services import ekler as ek_kurallari

        ek = self.hazir_ek()
        diger = ChatConversation.objects.create(user=self.user)
        asil = ek_kurallari.kullanilabilir_mi

        def _araya_gir(e, simdi=None):
            AssistantAttachment.objects.filter(id=e.id).update(conversation=diger, sira=1)
            return asil(e, simdi)

        with patch.object(ek_kurallari, "kullanilabilir_mi", side_effect=_araya_gir):
            r = self.gonder(message="x", ekler=[str(ek.id)])
        self.assertEqual(r.status_code, 202, r.content)
        ek.refresh_from_db()
        self.assertEqual(ek.conversation_id, diger.id)
        kopya = AssistantAttachment.objects.exclude(id=ek.id).get()
        self.assertEqual(kopya.conversation_id, r.json()["data"]["conversation_id"])

    def test_baska_sohbetteki_ek_kopyalanir(self):
        eski = ChatConversation.objects.create(user=self.user)
        ek = self.hazir_ek(conversation=eski, sira=1)
        r = self.gonder(message="x", ekler=[str(ek.id)])
        self.assertEqual(r.status_code, 202)
        yeni = AssistantAttachment.objects.exclude(id=ek.id).get()
        self.assertEqual(yeni.conversation_id, r.json()["data"]["conversation_id"])
        self.assertFalse(yeni.kota_sayildi)
        ek.refresh_from_db()
        self.assertEqual(ek.conversation_id, eski.id)

    def test_sohbet_detayi_ekler_sohbet_kalan(self):
        ek = self.hazir_ek()
        conv = self.gonder(message="x", ekler=[str(ek.id)]).json()["data"]["conversation_id"]
        d = self.api.get(f"/api/v1/assistant/conversations/{conv}/").json()["data"]
        self.assertEqual(d["totalCount"], 1)
        self.assertEqual([e["id"] for e in d["ekler"]], [str(ek.id)])
        self.assertNotIn("metin", d["ekler"][0])
        self.assertIsNone(d["sohbet"])
        self.assertEqual(d["sohbet_kalan"], {"dokuman": 4, "fotograf": 12})
        d2 = self.api.get(f"/api/v1/assistant/conversations/{conv}/?page=2").json()["data"]
        self.assertNotIn("ekler", d2)


# ── Model bağlamı (_agent_yaniti) ───────────────────────────────────────────

class BaglamTest(_EkTest):
    def setUp(self):
        super().setUp()
        from assistant.models import ChatMessage

        self.ChatMessage = ChatMessage
        self.profil = CompanyProfile.objects.get(user=self.user)
        self.sohbet = ChatConversation.objects.create(user=self.user, title="t")

    def soru(self, metin, ek=None, yetenekler=("ek",)):
        msg = self.ChatMessage.objects.create(
            user=self.user, conversation=self.sohbet, role="user", content=metin,
            payload={"yetenekler": list(yetenekler)} if yetenekler else None)
        if ek is not None:
            sira = (AssistantAttachment.objects.filter(conversation=self.sohbet).count() + 1)
            AssistantAttachment.objects.filter(id=ek.id).update(
                conversation=self.sohbet, message=msg, sira=sira)
        return msg

    def calistir(self, msg, yanitlar=None, hata=None, metadata=None):
        from assistant.tasks import _agent_yaniti
        from assistant.tests.test_agent_onbellek import _Kayit, _yanit

        kayit = _Kayit(yanitlar or [_yanit()])
        istemci = MagicMock()
        istemci.messages.create.side_effect = hata or kayit
        if metadata is not None:
            istemci.files.retrieve_metadata.side_effect = metadata
        with patch("anthropic.Anthropic", return_value=istemci), \
                patch("assistant.services.agent.get_api_key", return_value="k"), \
                patch("assistant.services.attachments._istemci", return_value=istemci):
            sonuc = _agent_yaniti(self.profil, msg, self.sohbet, timezone.localdate())
        return sonuc, kayit.cagrilar

    def test_ekler_ilk_mesajin_basinda_tek_onbellek_noktasi(self):
        ek1 = self.hazir_ek(metin="Teknik şartname metni")
        foto = self.hazir_ek(sha256="f" * 64, tur="fotograf", mod="gorsel", metin="",
                             anthropic_file_id="file_foto")
        msg = self.soru("Teslim süresi ne?", ek1)
        AssistantAttachment.objects.filter(id=foto.id).update(
            conversation=self.sohbet, message=msg, sira=2)
        _, cagrilar = self.calistir(msg)
        c = cagrilar[0]
        ilk = c["messages"][0]["content"]
        self.assertEqual([b["type"] for b in ilk[:3]], ["document", "text", "image"])
        self.assertEqual(ilk[0]["source"]["data"], "Teknik şartname metni")
        self.assertEqual(ilk[2]["source"], {"type": "file", "file_id": "file_foto"})
        isaretli = [b for b in ilk if "cache_control" in b]
        self.assertEqual(isaretli, [ilk[2]])  # yalnızca son ek bloğu
        noktalar = (sum(1 for b in c["system"] if "cache_control" in b) + len(isaretli)
                    + (1 if c.get("cache_control") else 0))
        self.assertLessEqual(noktalar, 4)

    def test_ekli_sohbette_baglam_son_mesajda_eklerden_sonra(self):
        ek = self.hazir_ek()
        _, cagrilar = self.calistir(self.soru("Özetle", ek))
        c = cagrilar[0]
        self.assertEqual(len(c["system"]), 1)  # değişken bağlam system'de DEĞİL
        icerik = c["messages"][-1]["content"]
        self.assertEqual(icerik[0]["type"], "document")      # önce ek
        self.assertIn("EK GÖNDERME: AÇIK", icerik[-2]["text"])  # sonra bağlam
        self.assertTrue(icerik[-1]["text"].endswith("Özetle"))  # en son soru

    def test_iki_turda_system_ve_ekler_byte_ayni(self):
        ek = self.hazir_ek()
        _, ilk = self.calistir(self.soru("Özetle", ek))
        self.ChatMessage.objects.create(user=self.user, conversation=self.sohbet,
                                        role="assistant", content="Özet.")
        _, ikinci = self.calistir(self.soru("2026/123456 ihalesi bununla ilgili mi?"))
        self.assertEqual(ilk[0]["system"], ikinci[0]["system"])
        self.assertEqual(ilk[0]["messages"][0]["content"][0],
                         ikinci[0]["messages"][0]["content"][0])
        # Değişen İKN ipucu yalnızca son mesajda
        self.assertIn("2026/123456", ikinci[0]["messages"][-1]["content"][-2]["text"])

    def test_eksiz_sohbette_baglam_systemde_kapali(self):
        _, cagrilar = self.calistir(self.soru("Merhaba", yetenekler=()))
        c = cagrilar[0]
        self.assertEqual(len(c["system"]), 2)
        self.assertIn("EK GÖNDERME: KAPALI", c["system"][1]["text"])
        self.assertEqual(c["messages"][-1]["content"], "Merhaba")

    @override_settings(ASSISTANT_EK_KAPSAM="kapali")
    def test_kapatma_anahtari_bagli_ekleri_de_durdurur(self):
        ek = self.hazir_ek(metin="gizli içerik")
        _, cagrilar = self.calistir(self.soru("Özetle", ek))
        c = cagrilar[0]
        self.assertNotIn("gizli içerik", str(c["messages"]))
        self.assertIn("ekleri şu an kapalı", c["system"][1]["text"])

    def test_payload_none_cokmez(self):
        msg = self.ChatMessage.objects.create(user=self.user, conversation=self.sohbet,
                                              role="user", content="selam", payload=None)
        sonuc, _ = self.calistir(msg)
        self.assertEqual(sonuc[0], "Tamam.")

    def test_gecmiste_ek_etiketi(self):
        ek = self.hazir_ek()
        self.soru("İlk soru", ek)
        self.ChatMessage.objects.create(user=self.user, conversation=self.sohbet,
                                        role="assistant", content="Cevap.")
        _, cagrilar = self.calistir(self.soru("İkinci soru"))
        ilk_metin = cagrilar[0]["messages"][0]["content"][-1]["text"]
        self.assertTrue(ilk_metin.startswith("[Bu mesajla gönderilen ekler: Ek 1]"))

    def test_suresi_dolan_ek_gitmez_not_duser(self):
        ek = self.hazir_ek(expires_at=timezone.now() + timedelta(minutes=10))
        _, cagrilar = self.calistir(self.soru("Özetle", ek))
        c = cagrilar[0]
        self.assertEqual(c["messages"][0]["content"], "[Bu mesajla gönderilen ekler: Ek 1]\nÖzetle")
        self.assertIn("saklama süresi doldu", c["system"][1]["text"])

    def test_dosya_hatasinda_tek_yeniden_deneme(self):
        import anthropic
        import httpx2

        from assistant.tests.test_agent_onbellek import _Kayit, _yanit

        ek = self.hazir_ek(mod="pdf", metin="", anthropic_file_id="file_olu")
        msg = self.soru("Özetle", ek)
        hata = anthropic.APIError("File `file_olu` not found.", request=None, body=None)
        hata.status_code = 404
        kayit = _Kayit([_yanit()])
        cagri = {"n": 0}

        def _create(**kw):
            cagri["n"] += 1
            if cagri["n"] == 1:
                raise hata
            return kayit(**kw)

        yok = anthropic.NotFoundError(
            "yok", response=httpx2.Response(404, request=httpx2.Request("GET", "https://x")),
            body=None)
        sonuc, _ = self.calistir(msg, hata=_create, metadata=yok)
        self.assertEqual(sonuc[0], "Tamam.")
        self.assertEqual(cagri["n"], 2)
        ikinci = kayit.cagrilar[0]["messages"][0]["content"]
        self.assertNotIn("document", str(ikinci))
        ek.refresh_from_db()
        self.assertEqual((ek.durum, ek.anthropic_file_id), ("suresi_doldu", ""))


# ── Prompt ve araçlar (1.7) ─────────────────────────────────────────────────

class PromptAracTest(TestCase):
    def test_prompt_ek_gondermeyi_bayraga_baglar(self):
        from assistant.prompts import AGENT_PERSONA_PROMPT, PERSONA_PROMPT

        self.assertNotIn("sohbete fotoğraf/dosya gönderilemez", AGENT_PERSONA_PROMPT)
        self.assertIn("EK GÖNDERME: AÇIK", AGENT_PERSONA_PROMPT)
        self.assertIn("Kayıtlı dokümanlarım", AGENT_PERSONA_PROMPT)
        self.assertIn("## SOHBETE EKLENEN DOSYALAR", AGENT_PERSONA_PROMPT)
        self.assertIn("<bağlam>", AGENT_PERSONA_PROMPT)
        self.assertIn("tahmini bedel", AGENT_PERSONA_PROMPT)  # maliyet analizi kuralı
        # Eski yol ek okuyamaz → onun personasında ek bölümü yok.
        self.assertNotIn("## SOHBETE EKLENEN DOSYALAR", PERSONA_PROMPT)

    def _ctx(self, **kw):
        from assistant.tools.context import ToolContext

        return ToolContext(user=None, **kw)

    def test_dokuman_sec_yalnizca_yetenek_varken_ve_ek_yokken(self):
        from assistant.tools.read import dokuman_analizi

        acik = self._ctx(ek_acik=True)
        dokuman_analizi(acik, ikn="2026/111")
        dokuman_analizi(acik, ikn="2026/111")  # turda bir kez
        self.assertEqual([b["type"] for b in acik.gorsel_bloklar], ["dokuman_sec"])
        self.assertEqual(acik.gorsel_bloklar[0]["ikn"], "2026/111")

        kapali = self._ctx(ek_acik=False)
        dokuman_analizi(kapali, ikn="2026/111")
        self.assertEqual(kapali.gorsel_bloklar, [])

        ekli = self._ctx(ek_acik=True, ekler=[{"sira": 1, "ad": "ts.pdf", "tur": "dokuman",
                                               "parca": ""}])
        sonuc = dokuman_analizi(ekli, ikn="2026/111")
        self.assertIn("Ek 1 (ts.pdf)", sonuc["not"])
        self.assertEqual(ekli.gorsel_bloklar, [])

    def test_ekli_sohbette_arac_calismasa_da_oneri(self):
        from assistant.tools.oneri import soru_onerileri

        self.assertEqual(soru_onerileri(self._ctx(), []), [])
        ekli = self._ctx(ekler=[{"sira": 1, "ad": "a", "tur": "dokuman", "parca": ""}])
        self.assertTrue(soru_onerileri(ekli, []))


class AltinSetYardimciTest(TestCase):
    def test_fiyat_ozel_ad_once(self):
        from assistant.management.commands.asistan_altin_set import _fiyat

        # "opus-5" alt dizesi "claude-opus-5-5"'i de yakalardı.
        self.assertEqual(_fiyat("claude-opus-5-5"), (4.0, 20.0, 0.05))
        self.assertEqual(_fiyat("claude-opus-5"), (5.0, 25.0, 0.1))

    def test_fotograf_eki_gercek_yoldan_hazirlanir(self):
        from assistant.management.commands.asistan_altin_set import _ek_hazirla

        user = get_user_model().objects.create_user(username="g", email="g@x.com", password="x")
        istemci = MagicMock()
        istemci.files.upload.return_value = SimpleNamespace(id="file_g", expires_at=None)
        dizin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dizin, ignore_errors=True)
        with patch("assistant.services.attachments._istemci", return_value=istemci):
            ek = _ek_hazirla(user, {"tur": "fotograf", "ad": "s.png", "metin": "SAYAC 1"}, dizin)
        self.assertEqual((ek.mod, ek.anthropic_file_id, ek.durum), ("gorsel", "file_g", "hazir"))
