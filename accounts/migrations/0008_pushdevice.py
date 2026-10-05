"""
`User.fcm_token` → `PushDevice` tablosu (cihaz başına tekil token).

⚠️ Arıza (üretimde ölçüldü 2026-10-05): tek FCM token **dört** hesapta kayıtlıydı
(uid 3/4/5/309) → 08:00 özet görevi kullanıcı grain'inde çalıştığı için aynı telefona
**üç push** düştü (10/93/84 ihale). İkinci bir token iki gerçek müşteri hesabında
paylaşılıyordu (uid 299/313). Mükerrer bildirimden ağırı hesaplar arası sızıntıdır:
alarm push'unun BAŞLIĞI ihale adıdır.

⚠️⚠️ VERİ TAŞIMA BU DOSYADAN AYRILAMAZ. `web` entrypoint'i her başlangıçta `migrate`
koşuyor (CLAUDE.md "Ağır data-migration"); taşımayı ayrı bir yönetim komutuna bırakmak,
deploy ile komut arasında token'ların TAMAMEN kaybolduğu bir pencere açardı (bildirimler
sessizce susar). Migration atomic olduğu için yarım durum imkânsız.

⚠️⚠️ PAYLAŞILAN TOKEN'LAR HİÇ TAŞINMAZ — "kazanan hesap" seçilmez. `last_seen_at` ile
en yenisini tutmak denenebilirdi ama yanlış olurdu:
  • damga kullanıcı başına GÜNDE BİR KEZ yazılıyor (accounts/authentication.py, Redis
    guard) → aynı güne düşen iki hesap ayırt edilemez;
  • damga HERHANGİ BİR cihazdan gelen her istekle yazılıyor → "hesap aktif mi" sorusunu
    cevaplıyor, "bu cihazda hangi hesap açık" sorusunu HİÇ cevaplamıyor — bizim sorumuz
    ikincisi;
  • yanlış tahminin bedeli asimetrik: iki farklı kişinin hesabı paylaşıldığında yanlış
    seçim, alarmların yanlış telefona düşmeye DEVAM etmesi demek — düzeltme arızayı sürdürür.
Cihazda gerçekten oturum açmış hesap, uygulamanın bir sonraki açılışında token'ı geri
yazar (IhaleTakip/src/Route.js:220 + Login/index.js:66) → tahmin yerine kanıt. Maliyet
üst sınırı bir bildirim turu (≤24 saat).

⚠️ Tablo küçük (~313 satır) → milisaniyeler sürer. CONCURRENTLY / atomic=False / elle
`worker migrate` dansı GEREKMEZ (o desen 500 bin satırlık ekap_tender içindir) ve elle
migrate entrypoint'inkiyle yarışır (CLAUDE.md'de yaşanmış arıza).
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Count


def tokenlari_cihaza_tasi(apps, schema_editor):
    """Tekil token'ları `PushDevice`'a taşır; paylaşılanları atlar (docstring'e bkz.)."""
    User = apps.get_model("accounts", "User")          # tarihsel model — gerçek modeli import ETME
    PushDevice = apps.get_model("accounts", "PushDevice")

    paylasilan = set(
        User.objects.exclude(fcm_token="")
        .values("fcm_token")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .values_list("fcm_token", flat=True)
    )
    tasinacak = (
        User.objects.exclude(fcm_token="")
        .exclude(fcm_token__in=paylasilan)
        .values_list("id", "fcm_token")
    )
    PushDevice.objects.bulk_create(
        [PushDevice(user_id=uid, token=tok) for uid, tok in tasinacak],
        batch_size=500,
    )
    atlanan = User.objects.filter(fcm_token__in=paylasilan).count() if paylasilan else 0
    print(
        f"  PushDevice: {len(tasinacak)} cihaz taşındı, "
        f"{len(paylasilan)} paylaşılan token ({atlanan} satır) ATLANDI "
        f"— cihazlar bir sonraki uygulama açılışında yeniden kaydolacak"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_user_onboarding_geri"),
    ]

    operations = [
        # ⚠️ SIRA ÖNEMLİ: önce tablo, sonra veri, EN SON alan kaldırma.
        # `makemigrations` RemoveField'ı başa koyuyor; öyle bırakılsa taşınacak veri
        # okunmadan silinirdi (270 cihazın tamamı kaybolur, bildirimler susar).
        migrations.CreateModel(
            name="PushDevice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("token", models.CharField(max_length=500, unique=True, verbose_name="FCM token")),
                ("platform", models.CharField(blank=True, help_text="ios | android (opsiyonel)", max_length=10, verbose_name="platform")),
                ("last_registered_at", models.DateTimeField(auto_now=True, verbose_name="son kayıt")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="push_devices", to=settings.AUTH_USER_MODEL, verbose_name="kullanıcı")),
            ],
            options={
                "verbose_name": "Push Cihazı",
                "verbose_name_plural": "Push Cihazları",
                "ordering": ["-last_registered_at"],
            },
        ),
        # ⚠️ reverse noop: `fcm_token` kaldırıldıktan sonra geri yazılacak yer yok.
        # Geri alma tabloyu düşürür, veriyi geri getirmez.
        migrations.RunPython(tokenlari_cihaza_tasi, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="user",
            name="fcm_token",
        ),
    ]
