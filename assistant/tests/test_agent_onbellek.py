"""
Asistan araç döngüsünün önbellek davranışı (ek planı 1.6, ön koşul).

Sohbete doküman eklenmeden ÖNCE bu üç şey doğru olmalı; yoksa aynı 50 bin tokenlık
doküman bir turda 8 kez tam fiyattan okunur:
- her çağrıda istek düzeyi `cache_control` (otomatik nokta);
- kapanış çağrısı döngüdeki çağrıyla BİREBİR aynı parametreler (`tools=[]` araç +
  system + mesaj önbelleğini, `tool_choice` değişikliği mesaj önbelleğini bozar);
- ek dosyasını işaret eden 400/404 ayrı bir hata sınıfıyla yükselir.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from assistant.services.agent import EkDosyaHatasi, onbellek_isareti, sohbet_turu
from assistant.tools import TOOL_SPECS
from assistant.tools.context import ToolContext


def _yanit(stop_reason="end_turn", icerik=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=icerik if icerik is not None else [SimpleNamespace(type="text", text="Tamam.")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                              cache_read_input_tokens=0, cache_creation_input_tokens=0),
    )


def _arac_istegi():
    return _yanit("tool_use", [SimpleNamespace(type="tool_use", id="t1", name="ihale_ara", input={})])


class _Kayit:
    """`messages.create` çağrılarını o anki mesaj listesinin KOPYASIYLA kaydeder."""

    def __init__(self, yanitlar):
        self.yanitlar = list(yanitlar)
        self.cagrilar = []

    def __call__(self, **kw):
        kw = dict(kw)
        kw["messages"] = list(kw["messages"])
        self.cagrilar.append(kw)
        return self.yanitlar.pop(0)


def _calistir(yanitlar, context_text="Bugünün tarihi: 11.11.2026"):
    kayit = _Kayit(yanitlar)
    istemci = MagicMock()
    istemci.messages.create.side_effect = kayit
    with patch("anthropic.Anthropic", return_value=istemci), \
            patch("assistant.services.agent.get_api_key", return_value="k"):
        sonuc = sohbet_turu(ToolContext(user=None), {}, context_text,
                            [{"role": "user", "content": "merhaba"}])
    return sonuc, kayit.cagrilar


class OnbellekNoktasiTest(TestCase):
    def test_her_cagrida_istek_duzeyi_cache_control_var(self):
        _, cagrilar = _calistir([_yanit()])
        self.assertEqual(cagrilar[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(cagrilar[0]["system"][0]["cache_control"], {"type": "ephemeral"})

    def test_varsayilan_ttl_anahtari_yazilmaz(self):
        # system bloğu bugünkü gibi byte-aynı kalsın: 5 dk'da `ttl` yazılmaz.
        self.assertEqual(onbellek_isareti(), {"type": "ephemeral"})

    @override_settings(ASSISTANT_EK_CACHE_TTL="1h")
    def test_bir_saatte_tum_noktalar_bir_saat(self):
        _, cagrilar = _calistir([_yanit()])
        beklenen = {"type": "ephemeral", "ttl": "1h"}
        self.assertEqual(cagrilar[0]["cache_control"], beklenen)
        self.assertEqual(cagrilar[0]["system"][0]["cache_control"], beklenen)

    def test_bos_baglam_system_blogu_gondermez(self):
        _, cagrilar = _calistir([_yanit()], context_text="")
        self.assertEqual(len(cagrilar[0]["system"]), 1)

    def test_dolu_baglam_ikinci_system_blogu(self):
        _, cagrilar = _calistir([_yanit()])
        self.assertEqual(cagrilar[0]["system"][1]["text"], "Bugünün tarihi: 11.11.2026")


@override_settings(ASSISTANT_MAX_TURNS=0)
class KapanisTest(TestCase):
    def test_kapanis_dongudeki_cagriyla_ayni_parametreler(self):
        sonuc, cagrilar = _calistir([_arac_istegi(), _yanit()])
        self.assertEqual(len(cagrilar), 2)
        kapanis = cagrilar[1]
        self.assertEqual(kapanis["tools"], TOOL_SPECS)
        self.assertNotIn("tool_choice", kapanis)
        self.assertEqual(kapanis["system"], cagrilar[0]["system"])
        self.assertEqual(sonuc["metin"], "Tamam.")

    def test_kapanista_yine_arac_istenirse_tool_choice_none(self):
        sonuc, cagrilar = _calistir([_arac_istegi(), _arac_istegi(), _yanit()])
        self.assertEqual(len(cagrilar), 3)
        self.assertEqual(cagrilar[2]["tool_choice"], {"type": "none"})
        # Sonuçsuz tool_use mesaja EKLENMEZ (API reddederdi): iki kapanış aynı mesajları görür.
        self.assertEqual(cagrilar[2]["messages"], cagrilar[1]["messages"])
        self.assertEqual(sonuc["metin"], "Tamam.")


class DosyaHatasiTest(TestCase):
    def _hata(self, kod, mesaj):
        e = anthropic.APIError(mesaj, request=None, body=None)
        e.status_code = kod
        return e

    DOSYALI = [{"role": "user", "content": [
        {"type": "document", "source": {"type": "file", "file_id": "file_011"}},
        {"type": "text", "text": "x"}]}]

    def _dene(self, hata, mesajlar=None):
        istemci = MagicMock()
        istemci.messages.create.side_effect = hata
        with patch("anthropic.Anthropic", return_value=istemci), \
                patch("assistant.services.agent.get_api_key", return_value="k"):
            sohbet_turu(ToolContext(user=None), {}, "", mesajlar or self.DOSYALI)

    def test_dosyayi_isaret_eden_404_ek_hatasi(self):
        with self.assertRaises(EkDosyaHatasi):
            self._dene(self._hata(404, "File `file_011` not found."))

    def test_dosyayi_isaret_eden_400_ek_hatasi(self):
        with self.assertRaises(EkDosyaHatasi):
            self._dene(self._hata(400, "file_id file_011 has expired"))

    def test_dosyasiz_400_eski_davranis(self):
        from ai.services.claude import AnalysisError

        with self.assertRaises(AnalysisError) as ctx:
            self._dene(self._hata(400, "invalid model"))
        self.assertNotIsInstance(ctx.exception, EkDosyaHatasi)

    def test_dosya_kaynagi_yoksa_file_gecen_hata_ek_hatasi_degil(self):
        # "profile" da "file" içerir: eksiz bir istek ek hatası sayılmamalı.
        from ai.services.claude import AnalysisError

        with self.assertRaises(AnalysisError) as ctx:
            self._dene(self._hata(400, "invalid profile field"),
                       [{"role": "user", "content": "x"}])
        self.assertNotIsInstance(ctx.exception, EkDosyaHatasi)


class AgentYanitiDonusTest(TestCase):
    def test_mesajsiz_sohbette_dort_deger_doner(self):
        # Eskiden 3 değer dönüyordu, çağıran 4 açıyordu → ValueError ("çökme").
        from assistant.models import ChatConversation, ChatMessage, CompanyProfile
        from assistant.tasks import _agent_yaniti

        user = get_user_model().objects.create_user(username="u1", email="u1@x.com",
                                                    password="x")
        profil = CompanyProfile.objects.create(user=user, company_name="Firma")
        bos = ChatConversation.objects.create(user=user, title="boş")
        diger = ChatConversation.objects.create(user=user, title="dolu")
        msg = ChatMessage.objects.create(user=user, conversation=diger,
                                         role=ChatMessage.Role.USER, content="selam")
        sonuc = _agent_yaniti(profil, msg, bos, timezone.localdate())
        self.assertEqual(len(sonuc), 4)
        self.assertIsNone(sonuc[0])
