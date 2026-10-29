"""Core serializer'ları."""
from rest_framework import serializers

from .models import SupportMessage, SupportTicket
from .support import MESAJ_UZUNLUK_SINIRI, onizleme

# İstemcinin `meta` ile gönderebileceği anahtarlar (serbest JSON admin'i kirletmesin).
_META_ANAHTARLARI = ("app_version", "build", "platform", "os_version", "device_model")


class SupportMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportMessage
        fields = ["id", "sender", "body", "created_at"]
        read_only_fields = fields


class SupportTicketSerializer(serializers.ModelSerializer):
    """Liste satırı + oluşturma.

    ⚠️ Eski istemci sözleşmesi korunur: `POST {email, phone, message}` çalışmaya devam
    eder (`category` ve `meta` isteğe bağlı). `reply` artık son destek yanıtıdır.
    """

    category = serializers.ChoiceField(
        choices=SupportTicket.CATEGORY_CHOICES, required=False, default="diger"
    )
    category_label = serializers.CharField(source="get_category_display", read_only=True)
    message = serializers.CharField(max_length=MESAJ_UZUNLUK_SINIRI)
    meta = serializers.DictField(required=False, write_only=True)
    unread = serializers.BooleanField(source="user_unread", read_only=True)
    last_message = serializers.SerializerMethodField()
    reply = serializers.SerializerMethodField()

    class Meta:
        model = SupportTicket
        fields = [
            "id", "category", "category_label", "email", "phone", "message", "meta",
            "status", "unread", "last_message", "last_message_at", "reply", "created_at",
        ]
        read_only_fields = ["id", "status", "last_message_at", "created_at"]

    def _son(self, obj, sender=None):
        # Listede `prefetch_related("messages")` var → ek sorgu yok.
        mesajlar = list(obj.messages.all())
        if sender:
            mesajlar = [m for m in mesajlar if m.sender == sender]
        return mesajlar[-1] if mesajlar else None

    def get_last_message(self, obj):
        m = self._son(obj)
        if not m:
            return None
        return {"sender": m.sender, "preview": onizleme(m.body), "created_at": m.created_at}

    def get_reply(self, obj):
        m = self._son(obj, SupportMessage.SENDER_STAFF)
        return m.body if m else obj.reply

    def validate_message(self, value):
        value = value.strip()
        if len(value) < 10:
            raise serializers.ValidationError("Mesajınız en az 10 karakter olmalıdır.")
        return value

    def validate_meta(self, value):
        return {k: str(value[k])[:100] for k in _META_ANAHTARLARI if value.get(k) not in (None, "")}


class SupportTicketDetailSerializer(SupportTicketSerializer):
    messages = SupportMessageSerializer(many=True, read_only=True)

    class Meta(SupportTicketSerializer.Meta):
        fields = SupportTicketSerializer.Meta.fields + ["messages"]


class SupportReplySerializer(serializers.Serializer):
    body = serializers.CharField(max_length=MESAJ_UZUNLUK_SINIRI, trim_whitespace=True)
