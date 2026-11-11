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
