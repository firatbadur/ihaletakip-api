"""
EKAP **Mobil API** istemcisi — projenin BİRİNCİL veri kaynağı.

EKAP'a resmî başvuru yapıldı ve `ekapmobil.kik.gov.tr` kullanımı onaylandı. Bu uçta
Turnstile insan doğrulaması, AES imzası ve kimlik doğrulaması **yoktur**; buna karşılık
IP tabanlı bir **hız sınırı** ve aşıldığında `HTTP 300 CAPTCHA_REQUIRED` vardır.

v2 (`ekap/client.py`) katmanı **yedek** olarak yerinde kalır: mobilde bulunmayan
alanları (`idare_id`, DETSIS ağacı, OKAS kataloğu, düzeltme/iptal ilanları) yalnızca o
verebiliyor.

Tasarım kararları için bkz. `docs/ekap-mobil-api.md` ve `CLAUDE.md`.
"""
