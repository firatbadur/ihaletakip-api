"""Özel User manager — email tabanlı oluşturma yardımcıları."""
from django.contrib.auth.models import UserManager as DjangoUserManager


def split_full_name(full_name):
    """
    "Ahmet Mehmet Yılmaz" → ("Ahmet Mehmet", "Yılmaz"). Tek kelimede soyad boş.
    Türkçede birden çok ön ad yaygın, soyad genelde tek kelime → son boşluktan bölünür.
    """
    parts = (full_name or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


class UserManager(DjangoUserManager):
    """Sosyal giriş için email tabanlı kullanıcı oluşturmayı kolaylaştırır."""

    def get_or_create_social(
        self,
        *,
        email,
        provider,
        provider_uid,
        display_name="",
        photo_url="",
        first_name="",
        last_name="",
    ):
        """
        Google/Apple girişinde kullanıcıyı bul veya oluştur.
        Öncelik: provider_uid → email eşleşmesi.

        `first_name`/`last_name` verilmezse `display_name` son boşluktan bölünür
        (Apple yalnızca tam ad gönderir). Mevcut kullanıcıda yalnızca BOŞ alanlar
        doldurulur — kullanıcının sihirbazda düzelttiği ad ezilmez.
        """
        if not (first_name or last_name) and display_name:
            first_name, last_name = split_full_name(display_name)
        first_name = (first_name or "")[:150]
        last_name = (last_name or "")[:150]

        user = None
        if provider_uid:
            user = self.filter(provider=provider, provider_uid=provider_uid).first()
        if user is None and email:
            user = self.filter(email__iexact=email).first()

        created = False
        if user is None:
            username = self._unique_username(email or f"{provider}_{provider_uid}")
            user = self.create(
                username=username,
                email=email or "",
                display_name=display_name,
                first_name=first_name,
                last_name=last_name,
                photo_url=photo_url,
                provider=provider,
                provider_uid=provider_uid,
            )
            user.set_unusable_password()
            user.save(update_fields=["password"])
            created = True
        else:
            # Var olan kullanıcıda eksik alanları güncelle
            updates = {}
            if provider_uid and not user.provider_uid:
                updates["provider_uid"] = provider_uid
                updates["provider"] = provider
            if display_name and not user.display_name:
                updates["display_name"] = display_name
            if photo_url and not user.photo_url:
                updates["photo_url"] = photo_url
            # Ad/soyad birlikte ele alınır: yarısı dolu bir kaydı başka kaynaktan
            # tamamlamak karışık bir ad üretebilir.
            if (first_name or last_name) and not (user.first_name or user.last_name):
                updates["first_name"] = first_name
                updates["last_name"] = last_name
            if updates:
                for k, v in updates.items():
                    setattr(user, k, v)
                user.save(update_fields=list(updates.keys()))

        return user, created

    def _unique_username(self, base):
        """Email/uid'den benzersiz bir username üret."""
        base = (base or "user").split("@")[0]
        base = "".join(c for c in base if c.isalnum() or c in "._-")[:140] or "user"
        username = base
        i = 1
        while self.filter(username=username).exists():
            username = f"{base}{i}"
            i += 1
        return username
