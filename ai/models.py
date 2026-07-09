"""AI analiz cache modeli — Firestore `analyses/{ikn}/results/{type}` karşılığı."""
from django.db import models


class AnalysisCache(models.Model):
    """
    İhale bazlı AI analiz sonucu önbelleği.
    Aynı (ikn, analysis_type) için Claude'a tekrar gitmeden döner.
    """

    ANALYSIS_TYPES = [
        ("tech_spec", "Teknik Şartname"),
        ("admin_spec", "İdari Şartname"),
        ("cost_analysis", "Maliyet Analizi"),
        ("generate_keywords", "Anahtar Kelime"),
    ]

    ikn = models.CharField("İKN", max_length=100, db_index=True)
    analysis_type = models.CharField(max_length=30, choices=ANALYSIS_TYPES)
    analysis = models.TextField()
    usage = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "AI Analiz (Cache)"
        verbose_name_plural = "AI Analizler (Cache)"
        constraints = [
            models.UniqueConstraint(
                fields=["ikn", "analysis_type"], name="uniq_ikn_analysis_type"
            )
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.ikn} / {self.analysis_type}"
