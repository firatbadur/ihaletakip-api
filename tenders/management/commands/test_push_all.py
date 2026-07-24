"""
Tüm bildirim sistemlerini elle test eder — her kategori için **gerçek mesaj +
gerçek deep-link payload'u** ile verilen FCM token'a (veya kullanıcının kayıtlı
token'ına) doğrudan push atar.

Pacing kapılarını (sessiz saat / günlük limit / min aralık / idempotency) **atlar**
(doğrudan `send_fcm`) → beşi de tek seferde gelir. Gerçek görevlerin (Celery/beat)
aksine veri (alarm/filtre/favori/kayıtlı ihale) veya Pro şartı **aramaz**; amaç
push'un cihaza ulaştığını ve mobil yönlendirmenin (deep-link) doğru çalıştığını
görmektir.

Kullanım:
    # Doğrudan bir token'a (kullanıcı gerekmez):
    python manage.py test_push_all --token <FCM_TOKEN>

    # Bir kullanıcının kayıtlı fcm_token'ına:
    python manage.py test_push_all --user 3

    # Yalnızca belirli tür(ler):
    python manage.py test_push_all --token <T> --only alarm,filter

    # Gerçek kayıtlara deep-link (mobil doğru ekrana açsın):
    python manage.py test_push_all --user 3 --filter-id 12 --authority-detsis 24308110 \
        --tender-ikn 2025/1234567 --okas 09134100,45233141 --conversation-id 45

    # Uygulama-içi Notification satırı da yaz (mobil bildirim listesi testi; --user şart):
    python manage.py test_push_all --user 3 --record

Türler (mobil deep-link önceliği: conversationId > filterId > authorityDetsis >
okasKodlar > tenderIkn/tenderId):
    info       — düz bilgi push'u (yönlendirme yok)
    digest     — İhale Asistanı öneri digest'i (type=chat → sohbet açılır)
    okas       — OKAS önerisi (type=tender + okasKodlar → OKAS araması açılır)
    alarm      — İhale alarmı özeti (type=alarm → tek ihale detayı)
    filter     — Kayıtlı filtre eşleşmesi (type=tender + filterId → filtre sonuçları)
    authority  — Favori idare eşleşmesi (type=tender + authorityDetsis → idare listesi)
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

ALL_KINDS = ["info", "digest", "okas", "alarm", "filter", "authority"]


class Command(BaseCommand):
    help = "Tüm bildirim türlerini gerçek mesaj + deep-link ile bir FCM token'a gönderir."

    def add_arguments(self, parser):
        parser.add_argument("--token", default="", help="Hedef FCM token (kullanıcı gerekmez).")
        parser.add_argument("--user", type=int, default=None,
                            help="Kullanıcı id'si — token verilmezse bu kullanıcının fcm_token'ı kullanılır.")
        parser.add_argument("--only", default="",
                            help=f"Yalnızca bu tür(ler); virgüllü. Seçenekler: {', '.join(ALL_KINDS)}.")
        parser.add_argument("--record", action="store_true",
                            help="Uygulama-içi Notification satırı da yaz (--user şart).")
        # Deep-link'i gerçek kayıtlara bağlamak için (verilmezse örnek değerler kullanılır).
        parser.add_argument("--tender-id", default="2025/1234567", help="alarm/info için ekap_id.")
        parser.add_argument("--tender-ikn", default="2025/1234567", help="alarm/digest için İKN.")
        parser.add_argument("--filter-id", type=int, default=1, help="filter için SavedFilter id.")
        parser.add_argument("--conversation-id", type=int, default=1, help="digest için ChatConversation id.")
        parser.add_argument("--authority-detsis", default="24308110", help="authority için detsis_no.")
        parser.add_argument("--okas", default="09134100,45233141", help="okas için OKAS kodları (CSV).")

    def handle(self, *args, **options):
        from tenders.models import Notification
        from tenders.services import push as push_mod
        from tenders.services import templates

        User = get_user_model()

        # ── Hedef token'ı çöz ──────────────────────────────
        user = None
        if options["user"] is not None:
            try:
                user = User.objects.get(pk=options["user"])
            except User.DoesNotExist:
                raise CommandError(f"Kullanıcı bulunamadı: id={options['user']}")

        token = (options["token"] or "").strip()
        if not token and user is not None:
            token = (user.fcm_token or "").strip()
        if not token:
            raise CommandError(
                "Hedef token yok. --token <FCM> ver ya da fcm_token'ı olan bir --user <id> seç."
            )

        if options["record"] and user is None:
            raise CommandError("--record için --user <id> gerekir (uygulama-içi satır kullanıcıya yazılır).")

        if not push_mod.is_enabled():
            self.stdout.write(self.style.ERROR(
                "FCM devre dışı (FCM_CREDENTIALS tanımsız veya kimlik dosyası yok) → push atılamaz."
            ))
            return

        only = [k.strip() for k in options["only"].split(",") if k.strip()]
        kinds = only or ALL_KINDS
        bad = [k for k in kinds if k not in ALL_KINDS]
        if bad:
            raise CommandError(f"Bilinmeyen tür: {', '.join(bad)}. Seçenekler: {', '.join(ALL_KINDS)}")

        T = Notification.Type
        tid = options["tender_id"]
        ikn = options["tender_ikn"]
        okas_csv = options["okas"]

        # ── Her tür için (title, body, data, in-app kwargs) ─
        # data anahtarları mobilin beklediği camelCase'tir (görevlerdeki payload ile birebir).
        builders = {}

        # 1) Bilgi — düz push, yönlendirme yok.
        builders["info"] = dict(
            title="Test Bildirimi",
            body="İhaleTakip push bildirimleri çalışıyor. 🎉",
            data={"type": T.INFO},
            rec=dict(type=T.INFO),
        )

        # 2) Asistan öneri digest'i (match_recommendations) — type=chat → sohbet açılır.
        d_title = "İhale Asistanı: 3 yeni öneri"
        d_body = "• Örnek İhale A\n• Örnek İhale B\n• Örnek İhale C"
        builders["digest"] = dict(
            title=d_title,
            body=d_body,
            data={
                "type": T.CHAT,
                "conversationId": options["conversation_id"],
                "tenderIkn": ikn,
                "tenderTitle": "Örnek İhale A",
            },
            rec=dict(type=T.CHAT, conversation_id=options["conversation_id"],
                     tender_ikn=ikn, tender_title="Örnek İhale A"),
        )

        # 3) OKAS önerisi (recommend_by_saved_okas) — type=tender + okasKodlar → OKAS araması.
        o_title, o_body = templates.okas_recommendation(count=4)
        builders["okas"] = dict(
            title=o_title,
            body=o_body,
            data={"type": T.TENDER, "okasKodlar": okas_csv},
            rec=dict(type=T.TENDER, okas_kodlar=okas_csv),
        )

        # 4) İhale alarmı (check_tender_alarms) — type=alarm, tek ihale → detay.
        a_title, a_body = templates.alarm_summary(reminder_count=1, document_count=1, completed_count=0)
        builders["alarm"] = dict(
            title=a_title,
            body=a_body,
            data={"type": T.ALARM, "tenderId": tid, "tenderIkn": ikn},
            rec=dict(type=T.ALARM, tender_id=tid, tender_ikn=ikn, tender_title="Örnek İhale"),
        )

        # 5) Kayıtlı filtre eşleşmesi (check_saved_filter_matches) — type=tender + filterId → filtre sonuçları.
        f_title, f_body = templates.saved_filter_match(filter_name="Otomasyon", count=5)
        builders["filter"] = dict(
            title=f_title,
            body=f_body,
            data={"type": T.TENDER, "filterId": options["filter_id"]},
            rec=dict(type=T.TENDER, filter_id=options["filter_id"]),
        )

        # 6) Favori idare eşleşmesi (check_favorite_authority_matches) — type=tender + authorityDetsis → idare listesi.
        au_title, au_body = templates.authority_match(
            authority_name="Ankara Büyükşehir Belediyesi", count=3,
        )
        builders["authority"] = dict(
            title=au_title,
            body=au_body,
            data={"type": T.TENDER, "authorityDetsis": options["authority_detsis"]},
            rec=dict(type=T.TENDER, authority_detsis=options["authority_detsis"],
                     tender_ikn=ikn, institution="Ankara Büyükşehir Belediyesi"),
        )

        self.stdout.write(
            f"Hedef token: …{token[-12:]}  |  türler: {', '.join(kinds)}"
            + (f"  |  kullanıcı: {user.pk}" if user else "")
        )

        sent = 0
        for kind in kinds:
            spec = builders[kind]
            # data değerleri iOS için string'e çevrilir; send_fcm bunu kendisi yapar.
            status = push_mod.send_fcm(token, spec["title"], spec["body"], spec["data"])

            if options["record"] and user is not None:
                from tenders.services import notify
                notify.record_notification(user, title=spec["title"], body=spec["body"], **spec["rec"])

            if status == push_mod.SENT:
                sent += 1
                self.stdout.write(self.style.SUCCESS(f"  ✓ {kind:<10} gönderildi — {spec['title']}"))
            elif status == push_mod.INVALID_TOKEN:
                self.stdout.write(self.style.ERROR(
                    f"  ✗ {kind:<10} GEÇERSİZ TOKEN — token ölü/hatalı biçimde. Doğru FCM token'ı ver."
                ))
                break  # token ölüyse kalanları denemeye gerek yok
            else:
                self.stdout.write(self.style.ERROR(f"  ✗ {kind:<10} gönderilemedi: {status}"))

        self.stdout.write(self.style.SUCCESS(f"\nToplam {sent}/{len(kinds)} push gönderildi."))
        if options["record"]:
            self.stdout.write("Uygulama-içi Notification satırları da yazıldı (mobil bildirim listesi).")
