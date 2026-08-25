"""İhale Asistanı view'ları — profil, sohbet, öneriler."""
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    inline_serializer,
)
from drf_spectacular.types import OpenApiTypes
from rest_framework import permissions, serializers
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.premium import MSG_CHAT, require_premium
from core.response import api_response

from .models import (
    AssistantAction,
    ChatConversation,
    ChatMessage,
    CompanyProfile,
    TenderRecommendation,
)
from .serializers import (
    ChatConversationSerializer,
    ChatMessageSerializer,
    ChatSendSerializer,
    CompanyProfileSerializer,
    TenderRecommendationSerializer,
)

_TASK_RESPONSE = inline_serializer(
    name="AssistantTaskAccepted",
    fields={"task_id": serializers.CharField()},
)

_PROFILE_EXAMPLE = OpenApiExample(
    "Firma profili kaydet",
    request_only=True,
    value={
        "contractor": 40321,
        "company_name": "Örnek İnşaat Ltd. Şti.",
        "website": "https://ornekinsaat.com.tr",
        "il_id": 251,
    },
)

_PROFILE_MANUEL_EXAMPLE = OpenApiExample(
    "Firma profili kaydet (EKAP'ta bulunamayan firma)",
    request_only=True,
    value={
        "contractor": None,
        "company_name": "Yeni Kurulan İnşaat Ltd. Şti.",
        "website": "https://yenifirma.com.tr",
        "il_id": 251,
        "sector": "İnşaat / Yapım",
        "activity_areas": "Yol, altyapı ve bina inşaatı; asfalt serimi",
        "cities": [251, 284],
        "tender_types": [2],
        "budget_min": 1000000,
        "budget_max": 50000000,
    },
)


def _paginate(request, qs, serializer_class, default_page_size=30):
    """ekap.TenderListView ile aynı manuel sayfalama zarfı: {list, totalCount, page}."""
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(100, max(1, int(request.query_params.get("page_size", default_page_size))))
    except (TypeError, ValueError):
        page_size = default_page_size

    total = qs.count()
    start = (page - 1) * page_size
    data = serializer_class(qs[start : start + page_size], many=True).data
    return api_response(data={"list": data, "totalCount": total, "page": page})


# ── Firma Profili ──────────────────────────────────────
@extend_schema(tags=["assistant"])
class ProfileView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Firma profilini getir",
        description=(
            "Kullanıcının firma profilini döner. Profil yoksa **404** döner — "
            "mobil uygulama bunu 'onboarding gerekli' olarak yorumlar."
        ),
        responses={200: CompanyProfileSerializer},
    )
    def get(self, request):
        profile = CompanyProfile.objects.select_related("contractor").filter(user=request.user).first()
        if not profile:
            raise NotFound("Firma profili bulunamadı.")
        return Response(CompanyProfileSerializer(profile).data)

    @extend_schema(
        summary="Firma profilini kaydet (upsert)",
        description=(
            "Profili oluşturur veya günceller ve **profil haritası üretimini** arka "
            "planda başlatır. Yanıttaki `task_id` mevcut `GET /ai/tasks/{task_id}/` "
            "ucu ile sorgulanır; tamamlanınca `analysis` alanı profil haritasıdır.\n\n"
            "**`contractor` doluysa** (kullanıcı firmasını `GET /ekap/contractors/` "
            "aramasında buldu) geçmiş işler, çalışılan iller ve ihale türleri EKAP "
            "sözleşme geçmişinden türetilir — istemcinin `cities`/`tender_types`/"
            "`past_works` göndermesine gerek yoktur. Firma EKAP'ta bulunamadıysa "
            "`contractor: null` gönderilir ve bu alanlar kullanıcıdan istenir.\n\n"
            "`website` doluysa profil haritası üretilirken site **kısaca okunur**; "
            "erişilemezse sessizce atlanır, üretim başarısız olmaz."
        ),
        examples=[_PROFILE_EXAMPLE, _PROFILE_MANUEL_EXAMPLE],
        request=CompanyProfileSerializer,
        responses={
            202: inline_serializer(
                name="ProfileAccepted",
                fields={
                    "task_id": serializers.CharField(),
                    "profile": CompanyProfileSerializer(),
                },
            )
        },
    )
    def put(self, request):
        serializer = CompanyProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        profile, _created = CompanyProfile.objects.update_or_create(
            user=request.user, defaults=serializer.validated_data
        )

        from .tasks import generate_profile_map_task

        task = generate_profile_map_task.delay(request.user.id)
        return api_response(
            data={"task_id": task.id, "profile": CompanyProfileSerializer(profile).data},
            message="Profil kaydedildi, profil haritası üretiliyor.",
            status=202,
        )


# ── Sohbet ─────────────────────────────────────────────
@extend_schema(
    tags=["assistant"],
    summary="Sohbet geçmişini listele",
    description=(
        "Asistan sohbet mesajlarını **en yeniden eskiye** döner (mobil inverted liste "
        "için). Yanıt: `data.list`, `data.totalCount`, `data.page`."
    ),
    parameters=[
        OpenApiParameter("page", int, description="Sayfa numarası (1'den başlar)."),
        OpenApiParameter("page_size", int, description="Sayfa boyutu (varsayılan 30, en çok 100)."),
    ],
    responses={
        200: inline_serializer(
            name="ChatMessagePage",
            fields={
                "list": ChatMessageSerializer(many=True),
                "totalCount": serializers.IntegerField(),
                "page": serializers.IntegerField(),
            },
        )
    },
)
class ChatMessageListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = ChatMessage.objects.filter(user=request.user)
        return _paginate(request, qs, ChatMessageSerializer)


@extend_schema(
    tags=["assistant"],
    summary="Asistana mesaj gönder",
    description=(
        "Mesajı kaydeder ve yanıtı arka planda üretir. `conversation` verilmezse "
        "yeni bir sohbet oturumu açılır; dönen `conversation_id` sonraki mesajlarda "
        "gönderilmelidir. Dönen `task_id`, mevcut `GET /ai/tasks/{task_id}/` ucu ile "
        "sorgulanır; tamamlanınca `analysis` alanı asistan mesajıdır: "
        "`{id, conversation, role, content, tender_cards, created_at}`.\n\n"
        "**Pro özellik:** Asistanla sohbet yalnızca Pro üyelere açıktır. Free üye "
        "profilini oluşturabilir ancak mesaj gönderince **403** alır "
        "(`errors.code = premium_required`) — mobil abonelik paketlerini sunar."
    ),
    request=ChatSendSerializer,
    responses={
        202: inline_serializer(
            name="ChatAccepted",
            fields={
                "task_id": serializers.CharField(),
                "conversation_id": serializers.IntegerField(),
            },
        )
    },
    examples=[
        OpenApiExample(
            "Mesaj gönder",
            request_only=True,
            value={"message": "Bana uygun ihale var mı?", "conversation": None},
        )
    ],
)
class ChatSendView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # Asistanla sohbet Pro'ya özeldir (profil oluşturma serbesttir).
        require_premium(request.user, MSG_CHAT)

        serializer = ChatSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not CompanyProfile.objects.filter(user=request.user).exists():
            return api_response(
                data=None,
                message="Önce firma profilinizi oluşturun.",
                success=False,
                status=400,
            )

        content = serializer.validated_data["message"]
        conv_id = serializer.validated_data.get("conversation")
        tender_ref = (serializer.validated_data.get("tender") or "").strip()

        if conv_id:
            conversation = ChatConversation.objects.filter(
                user=request.user, id=conv_id
            ).first()
            if not conversation:
                raise NotFound("Sohbet oturumu bulunamadı.")
        else:
            # Yeni konuşma; tender verildiyse ihale odaklı aç (başlık ihale adı olur)
            title = content[:120]
            tender_ikn = ""
            if tender_ref:
                from django.db.models import Q

                from ekap.models import Tender

                t = Tender.objects.filter(Q(ekap_id=tender_ref) | Q(ikn=tender_ref)).first()
                if t:
                    tender_ikn = t.ikn
                    title = f"İhale: {(t.ihale_adi or t.ikn)[:100]}"
            conversation = ChatConversation.objects.create(
                user=request.user,
                title=title,
                kind=ChatConversation.Kind.CHAT,
                tender_ikn=tender_ikn,
            )

        msg = ChatMessage.objects.create(
            user=request.user,
            conversation=conversation,
            role=ChatMessage.Role.USER,
            content=content,
        )

        from .tasks import assistant_chat_task

        task = assistant_chat_task.delay(request.user.id, msg.id)
        return api_response(
            data={"task_id": task.id, "conversation_id": conversation.id},
            message="Asistan yanıtlıyor.",
            status=202,
        )


# ── Sohbet Oturumları ──────────────────────────────────
@extend_schema(
    tags=["assistant"],
    summary="Sohbet oturumlarını listele",
    description=(
        "Kullanıcının geçmiş sohbetlerini **en son güncellenenden eskiye** döner. "
        "Yalnızca son **`days`** gün (varsayılan 30) içinde güncellenen sohbetler listelenir. "
        "`kind=digest` kayıtları günlük öneri özetleridir."
    ),
    parameters=[
        OpenApiParameter("days", int, description="Son kaç günlük sohbet (varsayılan 30)."),
        OpenApiParameter("page", int, description="Sayfa numarası (1'den başlar)."),
        OpenApiParameter("page_size", int, description="Sayfa boyutu (varsayılan 30, en çok 100)."),
    ],
    responses={
        200: inline_serializer(
            name="ConversationPage",
            fields={
                "list": ChatConversationSerializer(many=True),
                "totalCount": serializers.IntegerField(),
                "page": serializers.IntegerField(),
            },
        )
    },
)
class ConversationListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from datetime import timedelta

        from django.utils import timezone

        try:
            days = min(365, max(1, int(request.query_params.get("days", 30))))
        except (TypeError, ValueError):
            days = 30

        cutoff = timezone.now() - timedelta(days=days)
        qs = (
            ChatConversation.objects.filter(user=request.user, updated_at__gte=cutoff)
            .prefetch_related("messages")
        )
        return _paginate(request, qs, ChatConversationSerializer)


@extend_schema(tags=["assistant"])
class ConversationDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get_conversation(self, request, pk):
        conversation = ChatConversation.objects.filter(user=request.user, id=pk).first()
        if not conversation:
            raise NotFound("Sohbet oturumu bulunamadı.")
        return conversation

    @extend_schema(
        operation_id="api_v1_assistant_conversation_messages",
        summary="Sohbet oturumunun mesajlarını listele",
        description="Seçili oturumun mesajlarını **en yeniden eskiye** döner (inverted liste).",
        parameters=[
            OpenApiParameter("page", int, description="Sayfa numarası."),
            OpenApiParameter("page_size", int, description="Sayfa boyutu."),
        ],
        responses={
            200: inline_serializer(
                name="ConversationMessagePage",
                fields={
                    "list": ChatMessageSerializer(many=True),
                    "totalCount": serializers.IntegerField(),
                    "page": serializers.IntegerField(),
                },
            )
        },
    )
    def get(self, request, pk):
        conversation = self._get_conversation(request, pk)
        # En yeniden eskiye (inverted liste); (conversation, -created_at) indeksi kullanılır
        qs = ChatMessage.objects.filter(conversation=conversation).order_by("-created_at")
        return _paginate(request, qs, ChatMessageSerializer)

    @extend_schema(
        summary="Sohbet oturumunu sil",
        description="Oturumu ve içindeki tüm mesajları siler.",
        responses={
            200: inline_serializer(
                name="ConversationDeleted", fields={"deleted": serializers.IntegerField()}
            )
        },
    )
    def delete(self, request, pk):
        conversation = self._get_conversation(request, pk)
        conversation.delete()
        return Response({"deleted": 1})


# ── Öneriler ───────────────────────────────────────────
@extend_schema(
    tags=["assistant"],
    summary="İhale önerilerini listele",
    description=(
        "Günlük eşleştirme görevinin ürettiği kişisel ihale önerilerini döner. "
        "`days` parametresi kaç günlük önerinin listeleneceğini belirler."
    ),
    parameters=[
        OpenApiParameter("days", int, description="Kaç günlük öneri (varsayılan 7)."),
        OpenApiParameter("page", int, description="Sayfa numarası."),
        OpenApiParameter("page_size", int, description="Sayfa boyutu."),
    ],
    responses={
        200: inline_serializer(
            name="RecommendationPage",
            fields={
                "list": TenderRecommendationSerializer(many=True),
                "totalCount": serializers.IntegerField(),
                "page": serializers.IntegerField(),
            },
        )
    },
)
class RecommendationListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from datetime import timedelta

        from django.utils import timezone

        try:
            days = min(90, max(1, int(request.query_params.get("days", 7))))
        except (TypeError, ValueError):
            days = 7

        cutoff = timezone.localdate() - timedelta(days=days)
        qs = (
            TenderRecommendation.objects.filter(user=request.user, date__gte=cutoff)
            .select_related("tender")
            .order_by("-date", "-score")
        )
        return _paginate(request, qs, TenderRecommendationSerializer)


@extend_schema(
    tags=["assistant"],
    summary="Öneriyi görüldü işaretle",
    description="Öneri kaydını görüldü yapar. Kayıt yoksa `data.updated` 0 döner.",
    request=None,
    responses={
        200: inline_serializer(
            name="RecommendationSeen", fields={"updated": serializers.IntegerField()}
        )
    },
)
class RecommendationSeenView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        updated = TenderRecommendation.objects.filter(user=request.user, id=pk).update(seen=True)
        return Response({"updated": updated})


# ── Asistan Eylemleri (onay kartı) ─────────────────────
def _eylem_karti(eylem):
    """Mobilin `payload.blocks` içinde beklediği kart sözlüğü."""
    return {
        "type": "action",
        "action_id": str(eylem.id),
        "tur": eylem.tur,
        "ozet": eylem.ozet,
        "durum": eylem.durum,
        "expires_at": eylem.expires_at.isoformat() if eylem.expires_at else None,
        "sonuc": eylem.sonuc,
    }


class _EylemBaseView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _eylem(self, request, action_id, kilitle=False):
        """
        ⚠️ Sahiplik URL'den değil SORGUDAN gelir: `user=request.user` filtresi olmadan
        başkasının action_id'si çalıştırılabilirdi. Bulunamayan kayıt 404 döner —
        "yetkiniz yok" demek eylemin varlığını sızdırır.
        """
        qs = AssistantAction.objects.filter(id=action_id, user=request.user)
        if kilitle:
            qs = qs.select_for_update()
        eylem = qs.first()
        if not eylem:
            raise NotFound("Eylem bulunamadı.")
        return eylem


@extend_schema(
    tags=["assistant"],
    summary="Asistan önerisini uygula",
    description=(
        "Asistanın önerdiği eylemi (ihale kaydet / alarm kur / filtre kaydet) **kullanıcı "
        "adına** uygular. Yazma işlemi burada olur; model hiçbir zaman doğrudan yazmaz.\n\n"
        "· Eylem zaten işlenmişse **200** + mevcut sonuç döner (idempotent: çift dokunuş "
        "ikinci kaydı üretmez).\n"
        "· Süresi dolmuşsa **410** döner; kart pasifleştirilmeli.\n"
        "· Alarm ve alarmlı filtre **Pro**'dur: Free üye **403** `premium_required` alır ve "
        "eylem `bekliyor` durumunda KALIR (Pro alıp tekrar basabilir)."
    ),
    request=None,
    responses={200: OpenApiTypes.OBJECT},
)
class AssistantActionExecuteView(_EylemBaseView):
    def post(self, request, action_id):
        from django.db import transaction
        from django.utils import timezone

        from tenders.services import actions as yazma

        with transaction.atomic():
            eylem = self._eylem(request, action_id, kilitle=True)

            # Zaten işlenmiş → idempotent yanıt (ağ tekrarı / çift dokunuş)
            if eylem.durum != AssistantAction.Durum.BEKLIYOR:
                return api_response(
                    data={"durum": eylem.durum, "sonuc": eylem.sonuc},
                    message="Bu öneri daha önce işlendi.",
                )

            if eylem.suresi_doldu:
                eylem.durum = AssistantAction.Durum.SURESI_DOLDU
                eylem.save(update_fields=["durum"])
                return api_response(
                    data={"durum": eylem.durum},
                    message="Bu öneri güncelliğini yitirdi. Asistana tekrar sorabilirsiniz.",
                    success=False,
                    status=410,
                )

            p = dict(eylem.params or {})
            # ⚠️ require_premium buradan fırlarsa (403) transaction geri alınır ve eylem
            # `bekliyor` KALIR — kullanıcı Pro alıp aynı karta tekrar basabilsin diye.
            T = AssistantAction.Tur
            try:
                if eylem.tur == T.IHALE_KAYDET:
                    klasor = p.pop("klasor", None)
                    # ⚠️ Klasör YOKSA oluşturulur. Eskiden yok sayılıyordu: kullanıcı
                    # "Yol İşleri klasörüne kaydet" deyip onaylıyor, kayıt sessizce
                    # Genel'e düşüyordu — asistan yalan söylemiş oluyordu.
                    grup = yazma.klasor_bul_veya_olustur(request.user, klasor) if klasor else None
                    kayit, _yeni = yazma.ihale_kaydet(request.user, group=grup, **p)
                    sonuc = {"kaydedildi": True, "ikn": kayit.tender_ikn,
                             "klasor": grup.name if grup else "Genel"}
                elif eylem.tur == T.TOPLU_IHALE_KAYDET:
                    klasor = p.get("klasor")
                    grup = yazma.klasor_bul_veya_olustur(request.user, klasor) if klasor else None
                    iknler = []
                    for satir in p.get("kayitlar") or []:
                        kayit, _yeni = yazma.ihale_kaydet(request.user, group=grup, **satir)
                        iknler.append(kayit.tender_ikn)
                    sonuc = {"kaydedildi": True, "adet": len(iknler), "iknler": iknler,
                             "klasor": grup.name if grup else "Genel"}
                elif eylem.tur == T.IHALE_TASI:
                    kayit = yazma.ihale_tasi(request.user, **p)
                    sonuc = {"tasindi": True, "ikn": kayit.tender_ikn,
                             "klasor": kayit.group.name if kayit.group_id else "Genel"}
                elif eylem.tur == T.ALARM_KUR:
                    alarm, _yeni = yazma.alarm_kur(request.user, **p)
                    sonuc = {"alarm_kuruldu": True, "tender_id": alarm.tender_id}
                elif eylem.tur == T.FILTRE_KAYDET:
                    filtre = yazma.filtre_kaydet(request.user, **p)
                    sonuc = {"filtre_kaydedildi": True, "id": filtre.pk, "ad": filtre.name}
                elif eylem.tur == T.FIRMA_TAKIP:
                    fav, _yeni = yazma.firma_takip_et(request.user, **p)
                    sonuc = {"takip_ediliyor": True, "contractor_id": fav.contractor_id}
                elif eylem.tur == T.IDARE_FAVORI:
                    fav, _yeni = yazma.idare_favori_ekle(request.user, **p)
                    sonuc = {"favoriye_eklendi": True, "detsis_no": fav.detsis_no}
                elif eylem.tur == T.KAYIT_SIL:
                    silinen = yazma.kaydi_sil(request.user, **p)
                    sonuc = {"silindi": True, "adet": silinen, "tur": p.get("tur")}
                else:
                    return api_response(
                        data=None, message="Bilinmeyen eylem türü.", success=False, status=400
                    )
            except ValueError as e:
                # Servis katmanının iş kuralı reddi (klasör limiti, kayıt yok…).
                # ⚠️ `hata` durumuna DÜŞÜRÜLÜR: bu istekle çözülmeyecek bir sorundur,
                # `bekliyor` bırakmak kullanıcıya sonuçsuz bir kart bırakırdı.
                # (Pro 403'ü farklıdır — orada exception transaction'ı geri alır.)
                eylem.durum = AssistantAction.Durum.HATA
                eylem.sonuc = {"hata": str(e)}
                eylem.save(update_fields=["durum", "sonuc"])
                return api_response(data={"durum": eylem.durum}, message=str(e),
                                    success=False, status=400)

            eylem.durum = AssistantAction.Durum.ONAYLANDI
            eylem.sonuc = sonuc
            eylem.executed_at = timezone.now()
            eylem.save(update_fields=["durum", "sonuc", "executed_at"])

        return api_response(data={"durum": eylem.durum, "sonuc": sonuc}, message="Tamamdır.")


@extend_schema(
    tags=["assistant"],
    summary="Asistan önerisini reddet",
    description="Öneriyi `reddedildi` yapar. Kart pasifleşir; yeniden onaylanamaz.",
    request=None,
    responses={200: OpenApiTypes.OBJECT},
)
class AssistantActionDismissView(_EylemBaseView):
    def post(self, request, action_id):
        eylem = self._eylem(request, action_id)
        if eylem.durum == AssistantAction.Durum.BEKLIYOR:
            eylem.durum = AssistantAction.Durum.REDDEDILDI
            eylem.save(update_fields=["durum"])
        return api_response(data={"durum": eylem.durum})
