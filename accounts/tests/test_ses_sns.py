"""SES bounce/şikâyet → SNS → `/eposta/ses-bildirim/` (accounts/services/ses_sns.py).

Gerçek bir RSA anahtarı + öz-imzalı sertifikayla SNS imzası üretilir; sertifika indirme
`requests.get` taklidiyle verilir. Asıl korunan şey: imzasız/yanlış imzalı/izinsiz
topic'ten ya da AWS dışı sertifika adresinden gelen istek kullanıcıyı ETKİLEYEMEZ.
"""
import base64
import datetime
import json
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from django.core.cache import cache
from django.test import TestCase, override_settings

from accounts.models import EpostaGeriBildirim, User
from accounts.services.ses_sns import _ALANLAR

YOL = "/eposta/ses-bildirim/"
TOPIC = "arn:aws:sns:eu-north-1:932617936970:ses-geri-bildirim"
SERT_URL = "https://sns.eu-north-1.amazonaws.com/SimpleNotificationService-abc.pem"

_ANAHTAR = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ad = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
_simdi = datetime.datetime.now(datetime.timezone.utc)
_PEM = (x509.CertificateBuilder().subject_name(_ad).issuer_name(_ad)
        .public_key(_ANAHTAR.public_key()).serial_number(1)
        .not_valid_before(_simdi).not_valid_after(_simdi + datetime.timedelta(days=1))
        .sign(_ANAHTAR, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))


def _imzala(m, surum="2", anahtar=_ANAHTAR):
    m = {"SignatureVersion": surum, "SigningCertURL": SERT_URL, "TopicArn": TOPIC,
         "MessageId": "m-1", "Timestamp": "2026-10-27T10:00:00.000Z", **m}
    metin = "".join(f"{k}\n{m[k]}\n" for k in _ALANLAR[m["Type"]] if k in m)
    ozet = hashes.SHA1() if surum == "1" else hashes.SHA256()
    m["Signature"] = base64.b64encode(
        anahtar.sign(metin.encode(), padding.PKCS1v15(), ozet)).decode()
    return m


def _bildirim(olay, **kw):
    return _imzala({"Type": "Notification", "Message": json.dumps(olay)}, **kw)


def _bounce(email, tur="Permanent"):
    return {"notificationType": "Bounce", "mail": {"messageId": "ses-1"},
            "bounce": {"bounceType": tur, "bounceSubType": "General",
                       "bouncedRecipients": [{"emailAddress": email,
                                              "diagnosticCode": "550 user unknown"}]}}


@override_settings(SES_SNS_TOPIC_ARNS=[TOPIC])
class SesSnsTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("a", email="Ahmet@Firma.com.tr", password="x")
        yama = mock.patch("requests.get")
        self.get = yama.start()
        self.addCleanup(yama.stop)
        self.get.return_value.content = _PEM
        self.get.return_value.raise_for_status.return_value = None

    def _post(self, mesaj):
        # SNS gövdeyi text/plain gönderir.
        return self.client.post(YOL, json.dumps(mesaj), content_type="text/plain")

    def _acik(self):
        self.user.refresh_from_db()
        return self.user.email_notifications

    def test_kalici_bounce_bulteni_kapatir(self):
        r = self._post(_bildirim(_bounce("ahmet@firma.com.tr")))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self._acik())
        k = EpostaGeriBildirim.objects.get()
        self.assertEqual((k.tur, k.bulten_kapatildi), ("bounce", True))

    def test_imza_surumu_1_de_kabul(self):
        self.assertEqual(self._post(_bildirim(_bounce("ahmet@firma.com.tr"), surum="1"))
                         .status_code, 200)
        self.assertFalse(self._acik())

    def test_gecici_bounce_kaydedilir_ama_kapatmaz(self):
        self._post(_bildirim(_bounce("ahmet@firma.com.tr", tur="Transient")))
        self.assertTrue(self._acik())
        self.assertEqual(EpostaGeriBildirim.objects.get().tur, "gecici")

    def test_sikayet_kapatir_ad_bicimli_adres(self):
        olay = {"notificationType": "Complaint", "mail": {"messageId": "x"},
                "complaint": {"complaintFeedbackType": "abuse",
                              "complainedRecipients": [
                                  {"emailAddress": "Ahmet <ahmet@firma.com.tr>"}]}}
        self._post(_bildirim(olay))
        self.assertFalse(self._acik())

    def test_yapilandirma_kumesi_olayi_eventType(self):
        olay = _bounce("ahmet@firma.com.tr")
        olay["eventType"] = olay.pop("notificationType")
        self._post(_bildirim(olay))
        self.assertFalse(self._acik())

    def test_yanlis_imza_reddedilir(self):
        baska = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        r = self._post(_bildirim(_bounce("ahmet@firma.com.tr"), anahtar=baska))
        self.assertEqual(r.status_code, 403)
        self.assertTrue(self._acik())

    def test_imzadan_sonra_degistirilen_mesaj_reddedilir(self):
        m = _bildirim(_bounce("baska@x.com"))
        m["Message"] = json.dumps(_bounce("ahmet@firma.com.tr"))
        self.assertEqual(self._post(m).status_code, 403)
        self.assertTrue(self._acik())

    def test_aws_disi_sertifika_adresi_reddedilir(self):
        for url in ("https://evil.com/cert.pem", "http://sns.eu-north-1.amazonaws.com/a.pem",
                    "https://sns.eu-north-1.amazonaws.com.evil.com/a.pem"):
            with self.subTest(url=url):
                m = _bildirim(_bounce("ahmet@firma.com.tr"))
                m["SigningCertURL"] = url
                self.assertEqual(self._post(m).status_code, 403)
        self.assertTrue(self._acik())

    def test_izinsiz_topic_reddedilir(self):
        m = _imzala({"Type": "Notification", "Message": json.dumps(_bounce("ahmet@firma.com.tr")),
                     "TopicArn": "arn:aws:sns:eu-north-1:1:baskasi"})
        self.assertEqual(self._post(m).status_code, 403)
        self.assertTrue(self._acik())

    @override_settings(SES_SNS_TOPIC_ARNS=[])
    def test_ayar_bossa_uc_kapali(self):
        self.assertEqual(self._post(_bildirim(_bounce("ahmet@firma.com.tr"))).status_code, 403)

    def test_abonelik_onayi_imzadan_sonra_cagrilir(self):
        url = "https://sns.eu-north-1.amazonaws.com/?Action=ConfirmSubscription&Token=t"
        m = _imzala({"Type": "SubscriptionConfirmation", "Message": "onayla",
                     "SubscribeURL": url, "Token": "t"})
        self.assertEqual(self._post(m).status_code, 200)
        self.get.assert_any_call(url, timeout=5)

    def test_get_izinli_degil(self):
        self.assertEqual(self.client.get(YOL).status_code, 405)
