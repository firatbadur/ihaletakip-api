"""
EKAP Mobil API HTTP istemcisi.

⚠️ **`requests` kullanılır, `curl_cffi` DEĞİL.** v2'deki TLS parmak izi engeli bu uçta
yok (ölçüldü: düz istemci çalışıyor) → tarayıcı taklidine gerek yok. `EKAP_IMPERSONATE`
bu yola uygulanmaz.

Katmanlar:
  `throttle` → zaman penceresi + günlük bütçe (IP tabanlı hız sınırına karşı)
  `session`  → F5 ASM çerezi (`TS015c8da3`) kalıcı kavanozu
  `captcha`  → `HTTP 300 CAPTCHA_REQUIRED` duvarını OCR/insan ile aşma

⚠️ **Başarı kontrolü yalnızca `status_code == 200` OLAMAZ**: captcha duvarı `HTTP 300`
ile gelir ve istemci kütüphaneleri bunu hata saymaz (bkz. `captcha.captcha_mi`).
"""
import logging
import uuid

import requests
from django.conf import settings

from . import captcha as captcha_mod
from . import constants as C
from . import session as mobil_session
from . import throttle

logger = logging.getLogger("ihaletakip")


class MobilError(Exception):
    """EKAP mobil isteği kalıcı olarak başarısız oldu."""


class MobilCaptchaError(MobilError):
    """CAPTCHA duvarı aşılamadı.

    ⚠️ **Retry edilmez** (v2'deki `EkapDogrulamaError` deseninin aynısı): engellenmiş
    bir IP'ye ısrarla istek atmak soğuma süresini uzatır. Çözüm ya OCR'ın bir sonraki
    turda tutması ya operatörün cevabı girmesidir.
    """


class MobilSlotError(MobilError):
    """Hız penceresi bu an için dolu — iş yapılmadı.

    ⚠️ Hata DEĞİL, **sıra beklemedir**: çekmeli tik görevi bunu görünce sessizce
    turu atlar. Ayrı sınıf olmasının sebebi, gerçek ağ/API hatalarıyla karışıp
    `SyncRun.errors`'a sayılmaması.
    """


class MobilButceError(MobilError):
    """Günlük istek bütçesi doldu — bu tur bedavaya çıkılır."""


def _cihaz_uuid() -> str:
    """
    `user-agent` içindeki cihaz kimliği.

    ⚠️ Her istekte yeni UUID üretmek bot imzasıdır (gerçek uygulamada kurulum başına
    tek UUID vardır) → ayar boşsa süreç ömrü boyunca sabit bir değer kullanılır ve
    ayarın doldurulması önerilir.
    """
    ayar = getattr(settings, "EKAP_MOBIL_CIHAZ_UUID", "") or ""
    if ayar:
        return ayar
    if not hasattr(_cihaz_uuid, "_deger"):
        _cihaz_uuid._deger = str(uuid.uuid4())
        logger.warning(
            "EKAP_MOBIL_CIHAZ_UUID boş — geçici UUID üretildi (%s). Kalıcı bir "
            "değer atayın, aksi hâlde her yeniden başlatmada kimlik değişir.",
            _cihaz_uuid._deger,
        )
    return _cihaz_uuid._deger


def _user_agent() -> str:
    return (
        f"EKAP/2.2.0 {_cihaz_uuid()} iOS 26.6.1 Darwin Kernel Version 25.6.0: "
        "Mon Jul 14 21:00:00 PDT 2025; root:xnu-12377.61.5~1/RELEASE_ARM64_T8130"
    )


class EkapMobilClient:
    """Mobil API POST istemcisi (throttle + kalıcı oturum + captcha)."""

    def __init__(self, base_url=None, timeout=None, butce="arka_plan"):
        self.base_url = (base_url or settings.EKAP_MOBIL_BASE_URL).rstrip("/")
        self.timeout = timeout or getattr(settings, "EKAP_MOBIL_TIMEOUT", 60)
        self.butce = butce
        self.session = requests.Session()
        self.son_istek_captcha = False

    # ── Düşük seviye ────────────────────────────────────
    def _basliklar(self, *, govdeli: bool, ek=None):
        h = {
            "user-agent": _user_agent(),
            "accept": "*/*",
            "accept-language": "en-us",
            "accept-encoding": "gzip, deflate, br",
            "connection": "keep-alive",
            # ⚠️ İçerik tipi uca göre değişir: gövdeli `Liste` JSON, gövdesiz
            # query-param'lı uçlar `text/plain`. Yanlış tip F5 ASM'de protokol
            # ihlali sayılabiliyor (v2'de GET'e Content-Type eklemek 406 veriyordu).
            "content-type": (
                "application/json; charset=utf-8" if govdeli
                else "text/plain; charset=utf-8"
            ),
        }
        if ek:
            h.update(ek)
        return h

    def _ham_istek(self, path, *, json_body=None, params=None, ek_baslik=None,
                   stream=False, throttle_uygula=True):
        """
        Tek POST — captcha çözümü YAPMAZ (sonsuz özyineleme olurdu).

        `captcha.py` ve `_post` bunun üzerine kurulur.
        """
        if throttle_uygula:
            if not throttle.butce_harca(self.butce):
                raise MobilButceError(
                    f"EKAP mobil günlük bütçe doldu ({self.butce}); tur atlandı."
                )
            # ⚠️ Kullanıcı istekleri ayrı (daha dar) pencereyi ve sınırlı beklemeyi
            # kullanır: karşıda bekleyen bir insan var, arka plan turu ise bekleyebilir.
            if self.butce == "kullanici":
                uygun = throttle.slot_al(
                    ad="kullanici", bekle=True,
                    azami_bekleme=getattr(settings, "EKAP_MOBIL_KULLANICI_BEKLEME", 20),
                )
            else:
                uygun = throttle.slot_al()
            if not uygun:
                raise MobilSlotError("EKAP mobil hız penceresi dolu (slot alınamadı).")

        url = f"{self.base_url}{path}"
        sorgu = {"api-version": C.API_VERSION}
        if params:
            sorgu.update({k: v for k, v in params.items() if v is not None})

        try:
            resp = self.session.post(
                url,
                json=json_body if json_body is not None else None,
                data=None if json_body is not None else "",
                params=sorgu,
                headers=self._basliklar(govdeli=json_body is not None, ek=ek_baslik),
                cookies=mobil_session.cerezler(),
                timeout=self.timeout,
                stream=stream,
                allow_redirects=False,   # HTTP 300'ü kendimiz ele alıyoruz
            )
        except requests.RequestException as e:
            raise MobilError(f"EKAP mobil {path} ağ hatası: {e}") from e

        # Oturum çerezini her yanıttan tazele (ASM her istekte yeniler).
        if resp.cookies:
            mobil_session.guncelle(dict(resp.cookies))
        return resp

    def _post(self, path, *, json_body=None, params=None, ek_baslik=None):
        """JSON döndüren uçlar için: captcha duvarını bir kez aşmayı dener."""
        resp = self._ham_istek(
            path, json_body=json_body, params=params, ek_baslik=ek_baslik
        )
        resp = self._captcha_asilirsa_tekrarla(
            resp, path, json_body=json_body, params=params, ek_baslik=ek_baslik
        )
        return self._govde(resp, path)

    def _captcha_asilirsa_tekrarla(self, resp, path, **kw):
        metin = "" if resp.status_code == 200 else (resp.text or "")
        if not captcha_mod.captcha_mi(resp.status_code, metin):
            return resp
        self.son_istek_captcha = True
        if captcha_mod.bekliyor_mu():
            raise MobilCaptchaError(
                "EKAP mobil CAPTCHA duvarı — geri çekilme süresi doluyor."
            )
        if not captcha_mod.coz(self):
            raise MobilCaptchaError(
                "EKAP mobil CAPTCHA çözülemedi; operatör cevabı bekleniyor."
            )
        return self._ham_istek(path, **kw)

    def _govde(self, resp, path):
        if resp.status_code != 200:
            raise MobilError(
                f"EKAP mobil {path} → HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            )
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ── Captcha uçları (throttle'a tabi DEĞİL) ──────────
    # ⚠️ Bunlar hız sınırının **çözümüdür**, tüketicisi değil: bütçeden düşmek
    # engel altındayken çıkışı da kilitlerdi.
    def captcha_getir(self) -> dict:
        resp = self._ham_istek(C.PATH_CAPTCHA_GETIR, throttle_uygula=False)
        if resp.status_code != 200:
            raise MobilCaptchaError(f"Captcha/Getir → HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise MobilCaptchaError("Captcha/Getir JSON döndürmedi") from e

    def captcha_sonuc(self, captcha_id: str, cevap: str) -> bool:
        resp = self._ham_istek(
            C.PATH_CAPTCHA_SONUC,
            json_body={"captchaId": captcha_id, "captchaAnswer": cevap},
            throttle_uygula=False,
        )
        if resp.status_code != 200:
            return False
        try:
            return bool((resp.json() or {}).get("success"))
        except ValueError:
            return False

    # ── Yüksek seviye uçlar ─────────────────────────────
    @staticmethod
    def liste_govdesi(**overrides) -> dict:
        govde = dict(C.LISTE_GOVDESI)
        govde.update({k: v for k, v in overrides.items() if v is not None})
        return govde

    def liste(self, govde: dict):
        """
        İhale araması. **Düz liste** döner (sarmalayıcı yok).

        ⚠️ En çok `LISTE_TAVAN` (250) kayıt; sayfalama parametresi YOK. Tam 250
        dönerse sonuç **kesilmiştir** → çağıran dilimlemeli (bkz. `tasks.kesif`).
        """
        return self._post(C.PATH_LISTE, json_body=govde)

    def ihale(self, ikn_yili, ikn_sayi):
        """Tek ihalenin detayı — `ilan_tarihi`nin ve idari şartnamenin kaynağı."""
        return self._post(
            C.PATH_IHALE, params={"iknYili": ikn_yili, "iknSayi": ikn_sayi}
        )

    def sonuc_ilanlari(self, ikn_yili, ikn_sayi, page_size=50, page_num=1):
        """Sonuç ilanları — **kısım/kazanan başına bir kayıt** (para zincirinin kaynağı)."""
        return self._post(C.PATH_SONUC_ILANLARI, params={
            "iknYili": ikn_yili, "iknSayi": ikn_sayi,
            "pageSize": page_size, "pageNum": page_num,
        })

    def dokuman_liste(self, ikn_yili, ikn_sayi):
        """
        İhale dokümanı listesi.

        ⚠️ Dönen `id` **tek kullanımlıktır ve her çağrıda değişir** → önbelleklenemez;
        liste ve indirme aynı istek zincirinde yapılmalıdır.
        """
        return self._post(
            C.PATH_DOKUMAN_LISTE,
            params={"iknYili": ikn_yili, "iknSayi": ikn_sayi},
            ek_baslik={"x-skip-error-dialog": "true"},
        )

    def dokuman_indir(self, ikn_yili, ikn_sayi, dosya_id):
        """Dokümanı indirir — `requests.Response` (stream) döner, JSON değil."""
        resp = self._ham_istek(
            C.PATH_DOKUMAN_INDIR,
            params={"iknYili": ikn_yili, "iknSayi": ikn_sayi, "dosyaId": dosya_id},
            ek_baslik={"x-skip-error-dialog": "true"},
            stream=True,
        )
        resp = self._captcha_asilirsa_tekrarla(
            resp, C.PATH_DOKUMAN_INDIR,
            params={"iknYili": ikn_yili, "iknSayi": ikn_sayi, "dosyaId": dosya_id},
            ek_baslik={"x-skip-error-dialog": "true"}, stream=True,
        )
        if resp.status_code != 200:
            raise MobilError(f"Doküman indirilemedi → HTTP {resp.status_code}")
        return resp

    def teknik_sartname(self, ikn_yili, ikn_sayi):
        return self._post(
            C.PATH_TEKNIK_SARTNAME,
            params={"iknYili": ikn_yili, "iknSayi": ikn_sayi},
            ek_baslik={"x-skip-error-dialog": "true"},
        )

    def idari_sartname(self, ikn_yili, ikn_sayi):
        return self._post(
            C.PATH_IDARI_SARTNAME,
            params={"iknYili": ikn_yili, "iknSayi": ikn_sayi},
            ek_baslik={"x-skip-error-dialog": "true"},
        )
