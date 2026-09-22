# backend/apps/exams/views/blueprint_views.py
"""
Blueprint CRUD.

Blueprint assembly is a per-category question-distribution template
used by StartSessionView when the caller passes a blueprint_id.
Management is gated by 'admin.blueprints'.
"""

from django.shortcuts import get_object_or_404
from rest_framework.views import APIView

from ..models import Blueprint
from ..serializers import BlueprintSerializer
from apps.core.permissions import HasCapability
from apps.core.utils import api_success, api_error
from apps.core.audit import log_privileged_action


def _reload_with_weights(blueprint_pk):
    """
    Re-fetch a Blueprint with `weight_entries` prefetched.

    Both the post-save response in `BlueprintListView.post` and the
    post-save response in `BlueprintDetailView.put` serialized a
    freshly-saved instance whose `weights` property would otherwise
    fire a fresh query per through-row. The prefetch was already
    the right pattern (it appears in the `get` and `get detail`
    paths too); only the two write responses were hand-rolling it.
    """
    return (
        Blueprint.objects
        .prefetch_related('weight_entries')
        .get(pk=blueprint_pk)
    )


class BlueprintListView(APIView):
    """
    Blueprint CRUD, list and create.
    Requires 'admin.blueprints'.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.blueprints'

    def get(self, request):
        # Prefetch the weight through-rows so serializing `weights`
        # does not fire one query per blueprint.
        blueprints = (
            Blueprint.objects
            .prefetch_related('weight_entries')
            .order_by('name')
        )
        serializer = BlueprintSerializer(blueprints, many=True)
        return api_success(data={
            'items': serializer.data,
            'total': blueprints.count(),
        })

    def post(self, request):
        serializer = BlueprintSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        blueprint = serializer.save(created_by=request.user.username)
        blueprint = _reload_with_weights(blueprint.pk)
        log_privileged_action(request, 'blueprint.create', target=blueprint)
        return api_success(
            data=BlueprintSerializer(blueprint).data,
            message='تم إنشاء النموذج',
            code=201,
        )


class BlueprintDetailView(APIView):
    """
    Blueprint CRUD, detail. Requires 'admin.blueprints'.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.blueprints'

    def get(self, request, blueprint_id):
        blueprint = get_object_or_404(
            Blueprint.objects.prefetch_related('weight_entries'),
            id=blueprint_id,
        )
        return api_success(data=BlueprintSerializer(blueprint).data)

    def put(self, request, blueprint_id):
        blueprint = get_object_or_404(Blueprint, id=blueprint_id)
        serializer = BlueprintSerializer(
            blueprint, data=request.data, partial=True,
        )
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)
        blueprint = serializer.save()
        blueprint = _reload_with_weights(blueprint.pk)
        log_privileged_action(request, 'blueprint.update', target=blueprint)
        return api_success(
            data=BlueprintSerializer(blueprint).data,
            message='تم تحديث النموذج',
        )

    def delete(self, request, blueprint_id):
        blueprint = get_object_or_404(Blueprint, id=blueprint_id)
        blueprint_name = blueprint.name
        blueprint.delete()
        log_privileged_action(request, 'blueprint.delete', target_repr=blueprint_name,
                              details={'blueprint_id': blueprint_id})
        return api_success(message='تم حذف النموذج')