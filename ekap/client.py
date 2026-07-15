"""
EKAP v2 HTTP istemcisi.

Mobil `src/api/v1/{calls.js,api.js,urls.js}`'in sunucu tarafı karşılığı.
Sunucuda CORS/native köprü gerekmez; düz `requests` + imza header'ları yeterli.
Throttle + üstel backoff ile EKAP rate limitlerine takılmamak hedeflenir.
"""
import logging
import random
import time

from curl_cffi import requests as curl_requests
from django.conf import settings

from .constants import DEFAULT_SEARCH_BODY
from .signing import generate_signing_headers
from .throttle import wait_for_slot

logger = logging.getLogger("ihaletakip")

# Endpoint yolları (urls.js karşılığı)
PATH_SEARCH = "/b_ihalearama/api/Ihale/GetListByParameters"
PATH_DETAIL = "/b_ihalearama/api/IhaleDetay/GetByIhaleIdIhaleDetay"
PATH_ANNOUNCEMENTS = "/b_ihalearama/api/Ilan/GetList"
PATH_DOCUMENT_URL = "/b_ihalearama/api/EkapDokumanYonlendirme/GetDokumanUrl"
PATH_OKAS = "/b_ihalearama/api/IhtiyacKalemleri/GetAll"
PATH_DETSIS = "/b_idare/api/DetsisKurumBirim/DetsisAgaci"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


class EkapError(Exception):
    """EKAP isteği kalıcı olarak başarısız oldu."""


class EkapV2Client:
    """EKAP v2 imzalı POST istemcisi (throttle + retry)."""

    def __init__(self, base_url=None, timeout=None, max_retries=None):
        self.base_url = (base_url or settings.EKAP_BASE_URL).rstrip("/")
        self.timeout = timeout or settings.EKAP_TIMEOUT
        self.max_retries = max_retries if max_retries is not None else settings.EKAP_MAX_RETRIES
        self.impersonate = getattr(settings, "EKAP_IMPERSONATE", "chrome")
        # curl_cffi: EKAP WAF'ının TLS parmak izi engelini aşmak için tarayıcı taklidi
        self.session = curl_requests.Session(impersonate=self.impersonate)

    # ── Düşük seviye ────────────────────────────────────
    def _post(self, path: str, payload: dict):
        url = f"{self.base_url}{path}"
        last_exc = None

        for attempt in range(self.max_retries + 1):
            wait_for_slot()  # throttle
            headers = {
                "Accept": "application/json",
                # EKAP enum açıklamalarını (ihaleTipAciklama/ihaleUsulAciklama/
                # ihaleDurumAciklama) Türkçe döndürsün. curl_cffi'nin chrome taklidi
                # varsayılan olarak `Accept-Language: en-US` gönderdiği için EKAP
                # açıklamaları İngilizce dönüyordu; bu başlık o varsayılanı ezer.
                "Accept-Language": "tr-TR,tr;q=0.9",
                "Content-Type": "application/json",
                "api-version": "v1",
                **generate_signing_headers(),
            }
            try:
                resp = self.session.post(
                    url, json=payload, headers=headers, timeout=self.timeout
                )
            except Exception as e:  # curl_cffi ağ/curl hatası → retry
                last_exc = e
                self._backoff(attempt, reason=str(e))
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    return resp.text

            # 429 / 5xx → backoff + retry
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                last_exc = EkapError(f"HTTP {resp.status_code}")
                self._backoff(attempt, retry_after=retry_after, reason=f"HTTP {resp.status_code}")
                continue

            # 4xx (kalıcı) → hemen hata
            raise EkapError(
                f"EKAP {path} → HTTP {resp.status_code}: {resp.text[:200]}"
            )

        raise EkapError(f"EKAP {path} {self.max_retries} denemede başarısız: {last_exc}")

    def _backoff(self, attempt, retry_after=None, reason=""):
        if retry_after:
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = 2 ** attempt
        else:
            delay = min(2 ** attempt, 30) + random.uniform(0, 1)  # jitter
        logger.warning("EKAP backoff %.1fs (deneme %s, %s)", delay, attempt + 1, reason)
        time.sleep(delay)

    # ── Yüksek seviye (api.js karşılıkları) ─────────────
    @staticmethod
    def build_search_body(**overrides) -> dict:
        body = dict(DEFAULT_SEARCH_BODY)
        body.update({k: v for k, v in overrides.items() if v is not None})
        return body

    def search(self, body: dict):
        """POST GetListByParameters — {list, totalCount} döner."""
        return self._post(PATH_SEARCH, body)

    def get_detail(self, ihale_id):
        """POST IhaleDetay — {item: {...}} veya {...} döner."""
        return self._post(PATH_DETAIL, {"ihaleId": str(ihale_id)})

    def get_announcements(self, ihale_id):
        """POST Ilan/GetList — ilan listesi. Not: id genelde detayın ilanList'inde
        zaten mevcut; bu ayrı uç seyrek gerekir. id sayısal değilse string geçilir."""
        try:
            payload_id = int(ihale_id)
        except (TypeError, ValueError):
            payload_id = str(ihale_id)
        return self._post(PATH_ANNOUNCEMENTS, {"ihaleId": payload_id})

    def get_document_url(self, ihale_id, islem_id="1"):
        """POST GetDokumanUrl — {url} döner (dinamik, kısa ömürlü)."""
        return self._post(PATH_DOCUMENT_URL, {"ihaleId": str(ihale_id), "islemId": islem_id})

    @staticmethod
    def _load_options(filter_expr, take):
        """DevExtreme loadOptions gövdesi (api.js OKAS/DETSIS ile birebir)."""
        return {
            "loadOptions": {
                "filter": {
                    "sort": [], "group": [],
                    "filter": filter_expr,
                    "totalSummary": [], "groupSummary": [],
                    "select": [], "preSelect": [], "primaryKey": [],
                },
                "take": take,
            }
        }

    def okas_get_all(self, take=500):
        """Tüm OKAS kalemleri (loadOptions, boş filtre)."""
        return self._post(PATH_OKAS, self._load_options([], take))

    def okas_search(self, query, take=50):
        filter_expr = [["kalemAdi", "contains", query], "or", ["kalemAdiEng", "contains", query]]
        return self._post(PATH_OKAS, self._load_options(filter_expr, take))

    def detsis_agaci(self, take=500):
        """DETSIS kurum ağacı — boş filtre (düz arama korpusu). Ağaç için aşağıdakileri kullan."""
        return self._post(PATH_DETSIS, self._load_options([], take))

    # ── DETSIS ağacı (parentIdareKimlikKodu tabanlı lazy-tree) ──
    # Ağaç anahtarı = `detsisNo`; çocuk, ebeveynine `parentIdareKimlikKodu`=ebeveyn
    # detsisNo ile bağlanır. Kök düğümlerin parent'ı `0`. Tüm ağaç 2 istekle çekilir.
    def detsis_roots(self, take=100):
        """Kök düğümler (bakanlıklar + üst kategoriler) — parent = 0."""
        return self._post(PATH_DETSIS, self._load_options([["parentIdareKimlikKodu", "=", 0]], take))

    def detsis_children(self, parent_detsis, take=5000):
        """Bir düğümün doğrudan çocukları — parent = o düğümün detsisNo'su."""
        flt = [["parentIdareKimlikKodu", "=", int(parent_detsis)]]
        return self._post(PATH_DETSIS, self._load_options(flt, take))

    def detsis_all_descendants(self, take=300000):
        """Kök hariç tüm alt düğümler (parent > 0) — tek istekte tüm ağaç (~70k düğüm)."""
        flt = [["parentIdareKimlikKodu", ">", 0]]
        return self._post(PATH_DETSIS, self._load_options(flt, take))

    def detsis_search(self, query, take=50):
        """DETSIS ağacında ad ile arama — eşleşen düğümleri gerçek parent'larıyla döner."""
        return self._post(PATH_DETSIS, self._load_options([["ad", "contains", query]], take))
