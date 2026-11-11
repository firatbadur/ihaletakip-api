"""
Ek hazırlama servisi (`assistant/services/attachments.py`) — docs/plan-asistan-ekler.md 1.5.

Örnek dosyalar test içinde üretilir (repo'ya ikili dosya konmaz). Files API ve
`count_tokens` taklit edilir; gerçek ağ çağrısı yok.
"""
import io
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from assistant.services import attachments as ek_servis
from assistant.services.attachments import EkHatasi, donustur, imza_tani, uzanti_tutarli_mi


def metinli_pdf(sayfalar, sifre=None) -> bytes:
    """Her sayfası verilen metni taşıyan geçerli bir PDF (pypdf ile okunur)."""
    nesneler = ["<< /Type /Catalog /Pages 2 0 R >>", None,
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    sayfa_refleri = []
    for metin in sayfalar:
        satirlar = " ".join(f"({s}) Tj 0 -14 Td" for s in metin.split("\n"))
        akis = f"BT /F1 10 Tf 40 800 Td {satirlar} ET"
        nesneler.append(f"<< /Length {len(akis)} >>\nstream\n{akis}\nendstream")
        icerik_no = len(nesneler)
        nesneler.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                        f"/Resources << /Font << /F1 3 0 R >> >> /Contents {icerik_no} 0 R >>")
        sayfa_refleri.append(f"{len(nesneler)} 0 R")
    nesneler[1] = f"<< /Type /Pages /Kids [{' '.join(sayfa_refleri)}] /Count {len(sayfalar)} >>"
    govde, ofsetler = b"%PDF-1.4\n", []
    for i, n in enumerate(nesneler, 1):
        ofsetler.append(len(govde))
        govde += f"{i} 0 obj\n{n}\nendobj\n".encode("latin-1")
    xref = len(govde)
    govde += f"xref\n0 {len(nesneler) + 1}\n0000000000 65535 f \n".encode()
    for o in ofsetler:
        govde += f"{o:010d} 00000 n \n".encode()
    govde += (f"trailer\n<< /Size {len(nesneler) + 1} /Root 1 0 R >>\n"
              f"startxref\n{xref}\n%%EOF\n").encode()
    if sifre is None:
        return govde
    from pypdf import PdfReader, PdfWriter

    yazici = PdfWriter(clone_from=PdfReader(io.BytesIO(govde)))
    yazici.encrypt(user_password=sifre, owner_password="sahip")
    tampon = io.BytesIO()
    yazici.write(tampon)
    return tampon.getvalue()


def taranmis_pdf(sayfa: int) -> bytes:
    from PIL import Image

    resimler = [Image.new("RGB", (200, 280), "white") for _ in range(sayfa)]
    tampon = io.BytesIO()
    resimler[0].save(tampon, "PDF", save_all=True, append_images=resimler[1:])
    return tampon.getvalue()


_SAYFA = "\n".join(f"Madde {i}. Yuklenici isi teknik sartnameye uygun yapacaktir." for i in range(8))


class _Dizin(SimpleTestCase):
    def setUp(self):
        self.dizin = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dizin, ignore_errors=True)

    def yaz(self, ad, bayt):
        yol = self.dizin / ad
        yol.write_bytes(bayt)
        return yol

    def donustur(self, ad, bayt):
        yol = self.yaz(ad, bayt)
        return donustur(yol, imza_tani(yol.read_bytes()[:8192]), yol.suffix.lstrip("."))


class ImzaTest(SimpleTestCase):
    def test_imzalar(self):
        self.assertEqual(imza_tani(b"%PDF-1.7\n"), "pdf")
        self.assertEqual(imza_tani(b"\xff\xd8\xff\xe0"), "jpeg")
        self.assertEqual(imza_tani(b"\x89PNG\r\n\x1a\n"), "png")
        self.assertEqual(imza_tani(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "webp")
        self.assertEqual(imza_tani(b"\x00\x00\x00\x18ftypheic"), "heic")
        self.assertEqual(imza_tani(b"Rar!\x1a\x07\x01\x00"), "arsiv")
        self.assertEqual(imza_tani(b"{\\rtf1\\ansi"), "rtf")
        self.assertEqual(imza_tani(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), "ole")
        self.assertEqual(imza_tani(b"<html><body>captcha</body></html>"), "html")
        self.assertEqual(imza_tani("Düz metin satırı".encode("cp1254")), "metin")

    def test_pdf_uzantili_html_tutarsiz(self):
        # EKAP'ın captcha sayfası ".pdf" diye inebiliyor → gecersiz_dosya.
        self.assertFalse(uzanti_tutarli_mi("pdf", "html"))
        # ".doc" EKAP'ta sık sık HTML çıkar → imza kazanır, hata değil.
        self.assertTrue(uzanti_tutarli_mi("doc", "html"))

    def test_tur_secimi(self):
        self.assertEqual(ek_servis.tur_sec("png"), "fotograf")
        self.assertEqual(ek_servis.tur_sec("docx"), "dokuman")
        self.assertIsNone(ek_servis.tur_sec("heic"))
        self.assertIsNone(ek_servis.tur_sec("arsiv"))


class PdfTest(_Dizin):
    def test_metinli_pdf_sayfa_isaretli_metin(self):
        don = self.donustur("ts.pdf", metinli_pdf([_SAYFA, _SAYFA]))
        self.assertEqual(don.mod, "metin")
        self.assertEqual(don.sayfa_sayisi, 2)
        self.assertIn("[Sayfa 1]", don.metin)
        self.assertIn("[Sayfa 2]", don.metin)
        self.assertIn("Madde 3.", don.metin)

    def test_metinsiz_pdf_dosya_modu(self):
        don = self.donustur("taranmis.pdf", taranmis_pdf(3))
        self.assertEqual(don.mod, "pdf")
        self.assertEqual(don.sayfa_sayisi, 3)
        self.assertTrue(don.dosya_bayt.startswith(b"%PDF"))

    @override_settings(ASSISTANT_EK_TARANMIS_SAYFA=30)
    def test_31_sayfa_taranmis_reddedilir(self):
        with self.assertRaises(EkHatasi) as ctx:
            self.donustur("uzun.pdf", taranmis_pdf(31))
        self.assertEqual(ctx.exception.kod, "taranmis_cok_sayfa")
        self.assertEqual(ctx.exception.sayfa_sayisi, 31)

    def test_parolali_pdf_sifreli(self):
        with self.assertRaises(EkHatasi) as ctx:
            self.donustur("gizli.pdf", metinli_pdf([_SAYFA], sifre="gizli"))
        self.assertEqual(ctx.exception.kod, "sifreli")

    def test_bos_parolali_pdf_cozulur(self):
        don = self.donustur("izinli.pdf", metinli_pdf([_SAYFA], sifre=""))
        self.assertEqual(don.mod, "metin")
        self.assertIn("Madde 1.", don.metin)


class OfisTest(_Dizin):
    def _docx_bayt(self):
        import docx

        belge = docx.Document()
        belge.add_paragraph("1. Giriş paragrafı")
        tablo = belge.add_table(rows=2, cols=2)
        tablo.cell(0, 0).text, tablo.cell(0, 1).text = "Kalem", "Miktar"
        tablo.cell(1, 0).text, tablo.cell(1, 1).text = "Çimento", "40 ton"
        belge.add_paragraph("2. Tablodan sonraki madde")
        tampon = io.BytesIO()
        belge.save(tampon)
        return tampon.getvalue()

    def test_docx_tablo_govde_sirasinda(self):
        metin = self.donustur("ts.docx", self._docx_bayt()).metin
        self.assertIn("Çimento │ 40 ton", metin)
        # ⚠️ Tablo kendi maddesinin arasında kalmalı, sona atılmamalı.
        self.assertLess(metin.index("Giriş"), metin.index("Çimento"))
        self.assertLess(metin.index("Çimento"), metin.index("Tablodan sonraki"))

    def test_ici_docx_olan_doc(self):
        metin = self.donustur("eski.doc", self._docx_bayt()).metin
        self.assertIn("Çimento", metin)

    def test_ici_html_olan_doc(self):
        html = ("<html><head><meta charset='windows-1254'><style>p{}</style></head><body>"
                "<p>Teknik Şartname</p><table><tr><td>Kalem</td><td>Adet</td></tr>"
                "</table></body></html>").encode("cp1254")
        metin = self.donustur("ekap.doc", html).metin
        self.assertIn("Teknik Şartname", metin)
        self.assertIn("Kalem │ Adet", metin)
        self.assertNotIn("p{}", metin)

    def test_xlsx(self):
        import openpyxl

        kitap = openpyxl.Workbook()
        kitap.active.title = "Keşif"
        kitap.active.append(["Poz", "Tanım", "Miktar"])
        kitap.active.append(["15.150", "Beton", 120])
        tampon = io.BytesIO()
        kitap.save(tampon)
        metin = self.donustur("kesif.xlsx", tampon.getvalue()).metin
        self.assertIn("## Sayfa: Keşif", metin)
        self.assertIn("15.150 │ Beton │ 120", metin)

    def test_cp1254_txt(self):
        metin = self.donustur("not.txt", "Şartname ğüşıöç".encode("cp1254")).metin
        self.assertEqual(metin, "Şartname ğüşıöç")

    def test_arsiv_reddedilir(self):
        import zipfile

        tampon = io.BytesIO()
        with zipfile.ZipFile(tampon, "w") as z:
            z.writestr("a.txt", "x")
        with self.assertRaises(EkHatasi) as ctx:
            self.donustur("paket.zip", tampon.getvalue())
        self.assertEqual(ctx.exception.kod, "desteklenmeyen_tur")

    def test_antiword_yoksa_eski_word(self):
        with patch("shutil.which", return_value=None):
            with self.assertRaises(EkHatasi) as ctx:
                self.donustur("eski.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
        self.assertEqual(ctx.exception.kod, "eski_word")


class GorselTest(_Dizin):
    def test_exif_silinir_boyut_sinirlanir(self):
        from PIL import Image

        im = Image.new("RGB", (4000, 3000), "red")
        exif = Image.Exif()
        exif[0x010F] = "TelefonMarkasi"   # Make
        exif[0x0112] = 6                  # Orientation: 90° döndür
        tampon = io.BytesIO()
        im.save(tampon, "JPEG", exif=exif)
        don = self.donustur("foto.jpg", tampon.getvalue())

        self.assertEqual(don.mod, "gorsel")
        cikti = Image.open(io.BytesIO(don.dosya_bayt))
        self.assertEqual(len(cikti.getexif()), 0)
        w, h = cikti.size
        self.assertLessEqual(max(w, h), 2048)
        self.assertLessEqual(w * h, 3_750_000)
        self.assertGreater(h, w)  # yön uygulandı (yatay → dikey)
        self.assertTrue(don.kucuk_resim.startswith("data:image/jpeg;base64,"))
        self.assertLessEqual(len(don.kucuk_resim), 6 * 1024 * 4 // 3 + 40)

    def test_bozuk_gorsel(self):
        with self.assertRaises(EkHatasi) as ctx:
            self.donustur("bozuk.jpg", b"\xff\xd8\xff\xe0" + b"bozuk" * 10)
        self.assertEqual(ctx.exception.kod, "gecersiz_dosya")


class HazirlaTest(_Dizin):
    """`hazirla`: token tavanı, Files API, tahmin yedeği."""

    def _ek(self, yol, uzanti):
        return SimpleNamespace(id="11111111-2222-3333-4444-555555555555", depo_yolu=str(yol),
                               uzanti=uzanti)

    def _istemci(self, token=None, sayim_hatasi=False):
        istemci = MagicMock()
        if sayim_hatasi:
            istemci.messages.count_tokens.side_effect = RuntimeError("desteklenmiyor")
        else:
            istemci.messages.count_tokens.return_value = SimpleNamespace(input_tokens=token)
        istemci.files.upload.return_value = SimpleNamespace(id="file_abc", expires_at=None)
        return istemci

    def test_token_tavani_asilirsa_cok_uzun_kesme_yok(self):
        yol = self.yaz("ts.pdf", metinli_pdf([_SAYFA] * 3))
        with patch.object(ek_servis, "_istemci", return_value=self._istemci(token=999_999)):
            with self.assertRaises(EkHatasi) as ctx:
                ek_servis.hazirla(self._ek(yol, "pdf"))
        self.assertEqual(ctx.exception.kod, "cok_uzun")
        self.assertEqual(ctx.exception.sayfa_sayisi, 3)

    def test_metin_modu_alanlari(self):
        yol = self.yaz("ts.pdf", metinli_pdf([_SAYFA]))
        with patch.object(ek_servis, "_istemci", return_value=self._istemci(token=900)):
            alanlar = ek_servis.hazirla(self._ek(yol, "pdf"))
        self.assertEqual(alanlar["mod"], "metin")
        self.assertEqual(alanlar["token_sayisi"], 900)
        self.assertIn("[Sayfa 1]", alanlar["metin"])
        self.assertNotIn("anthropic_file_id", alanlar)

    def test_taranmis_pdf_sentetik_adla_yuklenir_tahmin_yedegi(self):
        yol = self.yaz("TEKNİK ŞARTNAME: v2.pdf", taranmis_pdf(2))
        istemci = self._istemci(sayim_hatasi=True)
        with patch.object(ek_servis, "_istemci", return_value=istemci):
            alanlar = ek_servis.hazirla(self._ek(yol, "pdf"))
        self.assertEqual(alanlar["mod"], "pdf")
        self.assertEqual(alanlar["anthropic_file_id"], "file_abc")
        self.assertEqual(alanlar["token_sayisi"], 2 * 4800)
        ad, _bayt, mime = istemci.files.upload.call_args.kwargs["file"]
        # ⚠️ Türkçe/özel karakterli ad Files API'ye GİTMEZ (400 riski + süpürme öneki).
        self.assertEqual(ad, "ihaletakip-ek-11111111-2222-3333-4444-555555555555.pdf")
        self.assertEqual(mime, "application/pdf")
        self.assertEqual(istemci.files.upload.call_args.kwargs["expires_in_seconds"], 30 * 86400)

    def test_taranmis_pdf_tavani_asarsa_dosya_silinir(self):
        yol = self.yaz("t.pdf", taranmis_pdf(2))
        istemci = self._istemci(token=999_999)
        with patch.object(ek_servis, "_istemci", return_value=istemci):
            with self.assertRaises(EkHatasi):
                ek_servis.hazirla(self._ek(yol, "pdf"))
        istemci.files.delete.assert_called_once_with("file_abc")


class BlokTest(SimpleTestCase):
    def test_cache_control_yalnizca_son_blokta(self):
        ekler = [
            SimpleNamespace(mod="metin", metin="a", anthropic_file_id="", sira=1, ad="a.docx",
                            ikn="", dtn="", parca="", sayfa_sayisi=None, created_at=None,
                            etiket=lambda: "Ek 1 — a.docx"),
            SimpleNamespace(mod="gorsel", metin="", anthropic_file_id="file_2", sira=2, ad="f.jpg",
                            ikn="", dtn="", parca="", sayfa_sayisi=None, created_at=None,
                            etiket=lambda: "Ek 2 — f.jpg"),
        ]
        bloklar = ek_servis.ek_bloklari(ekler, {"type": "ephemeral"})
        self.assertEqual([b["type"] for b in bloklar], ["document", "text", "image"])
        self.assertEqual([("cache_control" in b) for b in bloklar], [False, False, True])
        self.assertEqual(bloklar[0]["context"], ek_servis.KULLANICI_VERISI)
