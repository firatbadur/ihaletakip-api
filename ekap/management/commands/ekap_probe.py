"""
İmzalama canlı testi — tek imzalı arama isteği atar.

EKAP 200 + list dönerse Python↔CryptoJS AES imzalama eşdeğerliği kanıtlanmış olur.
Ayrıca imza header'larını lokal olarak decrypt edip tutarlılığı gösterir.
Throttle nedeniyle tek istek, rate-limit'e dokunmaz.
"""
from django.core.management.base import BaseCommand

from ekap import keyfetch
from ekap.client import EkapV2Client
from ekap.signing import decrypt_cbc_b64, generate_signing_headers
from ekap.sync import extract_list


class Command(BaseCommand):
    help = "EKAP'a tek imzalı istek atıp imzalamanın doğruluğunu test eder."

    def add_arguments(self, parser):
        parser.add_argument("--take", type=int, default=3)

    def handle(self, *args, **options):
        # 0) Çalışan imza şeması (anahtar + başlık adları rotasyona tabi)
        scheme = keyfetch.resolve()
        names = scheme["headers"]
        self.stdout.write(
            f"🧩 Şema: anahtar {len(scheme['key'])} karakter ({scheme['key'][:4]}…), "
            f"başlıklar {sorted(names.values())}"
        )

        # 1) İmza header'larını lokal doğrula (decrypt → eşleşme)
        h = generate_signing_headers(path="/b_ihalearama/api/Ihale/GetListByParameters")
        guid_dec = decrypt_cbc_b64(h[names["r8id"]], h[names["iv"]])
        ts_dec = decrypt_cbc_b64(h[names["ts"]], h[names["iv"]])
        ok = guid_dec == h[names["guid"]]
        self.stdout.write(f"🔐 İmza self-test: guid eşleşme={ok}, ts={ts_dec}")
        if not ok:
            self.stderr.write(self.style.ERROR("İmza self-test BAŞARISIZ."))
            return

        # 2) Canlı EKAP isteği
        self.stdout.write("📡 EKAP'a imzalı arama isteği gönderiliyor...")
        client = EkapV2Client()
        body = client.build_search_body(
            orderBy="ilanTarihi", siralamaTipi="desc",
            paginationSkip=0, paginationTake=options["take"],
        )
        try:
            resp = client.search(body)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"❌ EKAP isteği başarısız: {e}"))
            return

        items, total = extract_list(resp)
        if items:
            self.stdout.write(self.style.SUCCESS(
                f"✅ EKAP yanıt verdi: {len(items)} kayıt (totalCount={total}). İmzalama DOĞRU."
            ))
            first = items[0]
            self.stdout.write(f"   Örnek: {first.get('ikn')} — {str(first.get('ihaleAdi'))[:60]}")
        else:
            self.stdout.write(self.style.WARNING(
                f"⚠️ İstek geçti ama liste boş döndü. Ham yanıt: {str(resp)[:300]}"
            ))
