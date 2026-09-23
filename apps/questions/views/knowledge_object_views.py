from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.utils import api_error, api_success

from ..models import KnowledgeObject
from ..serializers import KnowledgeObjectSerializer


def _knowledge_queryset():
    return (
        KnowledgeObject.objects
        .select_related('category', 'created_by')
        .prefetch_related('tags')
        .annotate(question_count=Count('questions', distinct=True))
    )


def _may_manage(user, instance=None):
    if user.has_capability('questions.edit_any'):
        return True
    if instance is None:
        return user.has_capability('questions.create')
    return (
        user.has_capability('questions.edit_own')
        and instance.created_by_id == user.id
    )


class KnowledgeObjectListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _knowledge_queryset()
        search = (request.query_params.get('search') or '').strip()
        if search:
            qs = qs.filter(
                Q(title__icontains=search)
                | Q(learning_objective__icontains=search)
                | Q(canonical_answer__icontains=search)
            )
        status = request.query_params.get('status')
        if status in dict(KnowledgeObject.STATUS_CHOICES):
            qs = qs.filter(status=status)
        category = request.query_params.get('category')
        if category:
            qs = qs.filter(category_id=category)
        return api_success(data={
            'items': KnowledgeObjectSerializer(qs[:500], many=True).data,
            'count': qs.count(),
        })

    def post(self, request):
        if not _may_manage(request.user):
            return api_error('غير مصرح لك بإنشاء أهداف معرفية', 403)
        serializer = KnowledgeObjectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save(created_by=request.user)
        instance.question_count = 0
        return api_success(
            data=KnowledgeObjectSerializer(instance).data,
            message='تم إنشاء الهدف المعرفي',
            code=201,
        )


class KnowledgeObjectDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, pk):
        return get_object_or_404(_knowledge_queryset(), pk=pk)

    def get(self, request, pk):
        return api_success(data=KnowledgeObjectSerializer(self.get_object(pk)).data)

    def put(self, request, pk):
        instance = self.get_object(pk)
        if not _may_manage(request.user, instance):
            return api_error('غير مصرح لك بتعديل هذا الهدف المعرفي', 403)
        serializer = KnowledgeObjectSerializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        instance.question_count = instance.questions.count()
        return api_success(data=KnowledgeObjectSerializer(instance).data)

    def delete(self, request, pk):
        instance = self.get_object(pk)
        if not _may_manage(request.user, instance):
            return api_error('غير مصرح لك بحذف هذا الهدف المعرفي', 403)
        instance.delete()
        return api_success(message='تم حذف الهدف المعرفي')
