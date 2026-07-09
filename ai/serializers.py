"""ai serializer'ları — analiz isteği doğrulama."""
from rest_framework import serializers

FILE_TYPES = {"tech_spec", "admin_spec"}
META_TYPES = {"cost_analysis", "generate_keywords"}


class AnalyzeRequestSerializer(serializers.Serializer):
    analysis_type = serializers.ChoiceField(
        choices=["tech_spec", "admin_spec", "cost_analysis", "generate_keywords"]
    )
    file_base64 = serializers.CharField(required=False, allow_blank=True)
    file_name = serializers.CharField(required=False, allow_blank=True)
    tender_meta = serializers.JSONField(required=False)
    similar_tenders = serializers.JSONField(required=False)
    ikn = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs):
        atype = attrs["analysis_type"]
        if atype in FILE_TYPES:
            if not attrs.get("file_base64") or not attrs.get("file_name"):
                raise serializers.ValidationError(
                    "Bu analiz türü için dosya (file_base64, file_name) gerekli."
                )
        if atype in META_TYPES and not attrs.get("tender_meta"):
            raise serializers.ValidationError(
                "Bu analiz türü için tender_meta gerekli."
            )
        return attrs


class TTSRequestSerializer(serializers.Serializer):
    text = serializers.CharField()
