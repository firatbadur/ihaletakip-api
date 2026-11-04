"""
İhale Asistanı kullanım ve başarısızlık raporu — salt okunur, LLM ÇAĞIRMAZ.

2026-11-04 sohbet analizi elle yapıldı (445 mesaj tek tek okundu). Bu komut o
analizin ölçütlerini tekrarlanabilir kılar: düzeltmelerin etkisi önce/sonra aynı
sayılarla görülsün.

    python manage.py asistan_rapor [--gun 14] [--haric 3,5] [--ornek 15]

Ölçütler:
- cevapsız: kullanıcı mesajından sonra asistan mesajı yok ya da `payload.hata` dolu.
- "bulamadım" kalıbı: cevap bilgi vermediğini söylüyor (aşağıdaki desen).
- tur tavanı: `usage.tur_sayisi > ASSISTANT_MAX_TURNS` (araç hakkı doldu).
- boş/başarısız araç: `usage.araclar` (2026-11-04'ten sonraki cevaplarda dolu).
"""
import re
import statistics as st
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

# Cevabın "bilgi veremedim" dediği ifadeler. Geniş tutuldu: amaç kesin sınıflama değil,
# okunacak örnekleri öne çıkarmak.
BILGI_YOK = re.compile(
    r"(bulamad|göremiyorum|goremiyorum|yapamıyorum|yapamam|elimde (bu|böyle|yok)|"
    r"mümkün değil|bilgim yok|veri(m)? yok|sunamıyorum|kapsamım|kapsamımın|"
    r"yanıt üretemedim|beklenenden uzun|doğrulayamad|bilemem|erişemedim)",
    re.I,
)

# Kaba niyet sınıfları — sıralı: ilk eşleşen kazanır.
NIYETLER = [
    ("kim_aldi", re.compile(r"(kim (al|yap|ver)|kime veril|alan firma|kazanan|yüklenici)", re.I)),
    ("fiyat", re.compile(r"(kaça|kırım|indirim|yaklaşık maliyet|ne kadar)", re.I)),
    ("dokuman", re.compile(r"(şartname|kalem|miktar|metraj|ödeme|süresi|yeterlilik|"
                           r"kısmi|fiyat farkı|kaç (adet|kutu|metre))", re.I)),
    ("dogrudan_temin", re.compile(r"doğrudan temin|dogrudan temin", re.I)),
    ("uygulama", re.compile(r"(giriş|şifre|bilgisayar|nereden|nerde|nerede|nasıl yap|"
                            r"kayıt ol|web ?site|paylaş|uygulama)", re.I)),
    ("takip", re.compile(r"(alarm|haber ver|kaydet|favori|ekle|bildir)", re.I)),
    ("tekrar", re.compile(r"(geçen (yıl|sene)|her yıl|tekrar)", re.I)),
    ("arama", re.compile(r"(ihale|uygun|var mı|bul|göster|getir)", re.I)),
]


def _niyet(metin):
    for ad, desen in NIYETLER:
        if desen.search(metin or ""):
            return ad
    return "diger"


class Command(BaseCommand):
    help = "İhale Asistanı kullanım/başarısızlık raporu (salt okunur)."

    def add_arguments(self, parser):
        parser.add_argument("--gun", type=int, default=14)
        parser.add_argument("--haric", default="", help="Hariç tutulacak kullanıcı id'leri (virgüllü)")
        parser.add_argument("--ornek", type=int, default=15, help="Basılacak örnek sayısı")

    def handle(self, *args, **opts):
        from assistant.models import AssistantAction, ChatMessage

        bas = timezone.now() - timedelta(days=opts["gun"])
        haric = [int(x) for x in opts["haric"].split(",") if x.strip().isdigit()]
        msgs = (ChatMessage.objects.filter(created_at__gte=bas, conversation__isnull=False)
                .exclude(user_id__in=haric)
                .only("id", "user_id", "conversation_id", "role", "content", "payload",
                      "usage", "created_at")
                .order_by("conversation_id", "created_at"))

        konusmalar = {}
        for m in msgs:
            konusmalar.setdefault(m.conversation_id, []).append(m)

        sorular, cevapsiz, bilgisiz, sureler = [], [], [], []
        niyet_say, niyet_bilgisiz = Counter(), Counter()
        turlar, tavan, girdi = Counter(), 0, []
        arac_bos, arac_hata, arac_say = Counter(), Counter(), Counter()
        max_tur = settings.ASSISTANT_MAX_TURNS

        for liste in konusmalar.values():
            for i, m in enumerate(liste):
                if m.role != ChatMessage.Role.USER:
                    continue
                sorular.append(m)
                niyet = _niyet(m.content)
                niyet_say[niyet] += 1
                cevap = liste[i + 1] if i + 1 < len(liste) else None
                if cevap is None or cevap.role != ChatMessage.Role.ASSISTANT \
                        or (m.payload or {}).get("hata"):
                    cevapsiz.append((m, (m.payload or {}).get("hata") or "cevap_yok"))
                    continue
                sureler.append((cevap.created_at - m.created_at).total_seconds())
                if BILGI_YOK.search(cevap.content or ""):
                    bilgisiz.append((m, cevap))
                    niyet_bilgisiz[niyet] += 1
                u = cevap.usage or {}
                if u.get("tur_sayisi") is not None:
                    turlar[u["tur_sayisi"]] += 1
                    if u["tur_sayisi"] > max_tur:
                        tavan += 1
                if u:
                    girdi.append(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                                 + u.get("cache_creation_input_tokens", 0))
                for a in u.get("araclar") or []:
                    arac_say[a.get("arac")] += 1
                    if not a.get("ok"):
                        arac_hata[a.get("arac")] += 1
                    elif a.get("n") == 0:
                        arac_bos[a.get("arac")] += 1

        w = self.stdout.write
        kullanici = len({m.user_id for m in sorular})
        w(f"Son {opts['gun']} gün · {len(sorular)} kullanıcı mesajı · {kullanici} kullanıcı"
          + (f" · hariç: {haric}" if haric else ""))
        if not sorular:
            return
        w(f"cevapsız: {len(cevapsiz)} · 'bilgi yok' kalıplı cevap: {len(bilgisiz)} "
          f"(%{100 * len(bilgisiz) / len(sorular):.1f}) · tur tavanı: {tavan}")
        if sureler:
            sureler.sort()
            w(f"süre sn: medyan {st.median(sureler):.1f} · p90 {sureler[int(len(sureler) * .9)]:.1f}"
              f" · en çok {sureler[-1]:.0f}")
        if girdi:
            w(f"girdi token/cevap medyan: {st.median(girdi):,.0f}".replace(",", "."))
        w("tur dağılımı: " + ", ".join(f"{k}:{v}" for k, v in sorted(turlar.items())))

        w("\nNiyet            mesaj   bilgi-yok")
        for ad, n in niyet_say.most_common():
            w(f"  {ad:<15}{n:>6}   {niyet_bilgisiz[ad]:>6}")

        if arac_say:
            w("\nAraç             çağrı   boş   hata")
            for ad, n in arac_say.most_common():
                w(f"  {ad:<15}{n:>6}{arac_bos[ad]:>6}{arac_hata[ad]:>7}")

        puan = Counter(ChatMessage.objects.filter(created_at__gte=bas, geri_bildirim__isnull=False)
                       .exclude(user_id__in=haric).values_list("geri_bildirim", flat=True))
        if puan:
            w(f"\nGeri bildirim: 👍 {puan.get(1, 0)} · 👎 {puan.get(-1, 0)}")
            for m in (ChatMessage.objects.filter(created_at__gte=bas, geri_bildirim=-1)
                      .exclude(user_id__in=haric).order_by("-created_at")[:opts["ornek"]]):
                w(f"  👎 uid={m.user_id} {(m.geri_bildirim_notu or '-')[:80]} | C: {(m.content or '')[:100]}"
                  .replace("\n", " "))

        eylem = Counter(AssistantAction.objects.filter(created_at__gte=bas)
                        .exclude(user_id__in=haric).values_list("tur", "durum"))
        if eylem:
            w("\nOnay kartları: " + ", ".join(f"{t}/{d}={n}" for (t, d), n in sorted(eylem.items())))

        n = opts["ornek"]
        if cevapsiz:
            w(f"\n── Cevapsız (ilk {n}) ──")
            for m, kod in cevapsiz[:n]:
                w(f"  [{kod}] uid={m.user_id} {timezone.localtime(m.created_at):%m-%d %H:%M} "
                  f"{(m.content or '')[:120]}")
        if bilgisiz:
            w(f"\n── 'Bilgi yok' cevapları (son {n}) ──")
            for m, c in bilgisiz[-n:]:
                w(f"  uid={m.user_id} S: {(m.content or '')[:100]}")
                w(f"           C: {(c.content or '')[:160]}".replace("\n", " "))
