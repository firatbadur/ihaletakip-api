"""Blog botunun gönderdiği yazının yapısal doğrulaması.

İçerik kuralları (yasak HTML, kapak, alt metin) modelde kalır; burası yalnızca
gövdenin biçimini ve sınırlarını denetler.
"""
from rest_framework import serializers

from .models import AZAMI_ETIKET, AZAMI_ETIKET_UZUNLUK


class BotYaziSerializer(serializers.Serializer):
    external_id = serializers.CharField(max_length=64)
    title = serializers.CharField(max_length=160)
    slug = serializers.SlugField(max_length=180)
    summary = serializers.CharField(max_length=300)
    content = serializers.CharField()
    cover_alt = serializers.CharField(max_length=200)
    category_slug = serializers.SlugField(max_length=80, required=False, allow_blank=True)
    tags = serializers.ListField(
        child=serializers.CharField(max_length=AZAMI_ETIKET_UZUNLUK),
        max_length=AZAMI_ETIKET, required=False,
    )
    author_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    seo_title = serializers.CharField(max_length=60, required=False, allow_blank=True)
    seo_description = serializers.CharField(max_length=155, required=False, allow_blank=True)
