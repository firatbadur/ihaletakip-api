import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0015_hosgeldin_blog_tercihi"),
    ]

    operations = [
        migrations.CreateModel(
            name="KimlikEsleme",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("eski_uid", models.PositiveIntegerField(db_index=True, verbose_name="token'daki kullanıcı ID")),
                ("iat_bas", models.DateTimeField(blank=True, null=True, verbose_name="iat ≥")),
                ("iat_bit", models.DateTimeField(blank=True, null=True, verbose_name="iat <")),
                ("aciklama", models.CharField(blank=True, max_length=200, verbose_name="açıklama")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("hedef", models.ForeignKey(blank=True, help_text="Boş → bu aralıktaki token reddedilir.", null=True, on_delete=django.db.models.deletion.CASCADE, related_name="+", to=settings.AUTH_USER_MODEL, verbose_name="hedef kullanıcı")),
            ],
            options={
                "verbose_name": "Kimlik Eşleme",
                "verbose_name_plural": "Kimlik Eşlemeleri",
                "ordering": ["eski_uid", "iat_bas"],
            },
        ),
    ]
