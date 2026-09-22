# backend/apps/questions/views/tag_admin_views.py

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404

from ..models import Tag, QuestionTag, TAG_NAME_MAX_LENGTH
from ..serializers import TagRenameSerializer, TagMergeSerializer
from apps.core.permissions import HasCapability
from apps.core.utils import api_success, api_error
from apps.core.audit import log_privileged_action


def _validate_tag_name(raw, *, required_message):
    """
    Validate a user-supplied tag name.

    Returns (cleaned_name, None) on success, or (None, error_message)
    on failure.

    `required_message` is a parameter because the two callers
    address the field differently in their user-facing text:

      • `AdminTagRenameView.post` — "الاسم الجديد مطلوب" (the NEW
        name is required).
      • `AdminTagMergeView.post` — "الوسم الهدف مطلوب" (the TARGET
        tag is required).

    The two messages are not interchangeable: the rename view has a
    `old_name` from the URL and needs to distinguish "you did not
    provide the new name" from "the tag you are renaming does not
    exist"; the merge view has a list of source tags and needs to
    distinguish "you did not provide a target" from "you did not
    provide any source".

    The `length` failure message is shared — the same Arabic string
    was hand-copied into both views before this helper existed.

    Both serializer fields (`TagRenameSerializer.new_name`,
    `TagMergeSerializer.target_tag`) are bare `CharField()` WITHOUT
    a `max_length` argument, deliberately. If DRF short-circuited
    an over-long name here it would emit its own English message
    ("Ensure this field has no more than N characters") and this
    view would never see the request — the caller would get a
    different error for the same rule depending on which endpoint
    they hit. See the docstring on `serializers/tag.py` for the
    full rationale.
    """
    cleaned = '' if raw is None else str(raw).strip()
    if not cleaned:
        return None, required_message
    if len(cleaned) > TAG_NAME_MAX_LENGTH:
        return None, (
            f'اسم الوسم يجب ألا يتجاوز {TAG_NAME_MAX_LENGTH} حرفاً'
        )
    return cleaned, None


class TagListView(APIView):
    """
    User-facing tag list with counts. Read-only; any authenticated
    user.

    The former `AdminTagListView` endpoint has been deleted — it
    returned a byte-identical payload behind a stricter capability
    gate. The admin tag panel now calls this endpoint.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tags = Tag.objects.annotate(
            count=Count('questiontag'),
            verified_count=Count(
                'questiontag__question',
                filter=Q(questiontag__question__verified=True),
            ),
        )
        data = [
            {
                'name': tag.name,
                'count': tag.count,
                'verified_count': tag.verified_count,
            }
            for tag in tags
        ]
        return api_success(data={'items': data, 'total': len(data)})


class AdminTagTreeView(APIView):
    """
    Hierarchical tag tree built from the self-referential `parent`
    FK. Requires 'questions.manage_tags'.
    """
    permission_classes = [HasCapability]
    required_capability = 'questions.manage_tags'

    def get(self, request):
        tags = Tag.objects.all()
        tag_map = {
            tag.id: {'id': tag.id, 'name': tag.name, 'children': []}
            for tag in tags
        }
        roots = []
        for tag in tags:
            if tag.parent_id is None:
                roots.append(tag_map[tag.id])
            else:
                parent = tag_map.get(tag.parent_id)
                if parent:
                    parent['children'].append(tag_map[tag.id])
        return api_success(data={'tree': roots})


class AdminTagRenameView(APIView):
    """
    Rename a tag. The rename is a single UPDATE — existing QuestionTag
    rows reference the tag by id, so they follow automatically.
    Requires 'questions.manage_tags'.
    """
    permission_classes = [HasCapability]
    required_capability = 'questions.manage_tags'

    def post(self, request, old_name):
        body = TagRenameSerializer(data=request.data)
        if not body.is_valid():
            return api_error('بيانات غير صالحة', 400, details=body.errors)

        new_name, err = _validate_tag_name(
            body.validated_data['new_name'],
            required_message='الاسم الجديد مطلوب',
        )
        if err is not None:
            return api_error(err, 400)

        tag = get_object_or_404(Tag, name=old_name)
        if Tag.objects.filter(name=new_name).exists():
            return api_error('الوسم الجديد موجود مسبقاً', 400)

        tag.name = new_name
        tag.save()
        log_privileged_action(request, 'tag.rename', target=tag,
                              details={'old_name': old_name})
        return api_success(message='تمت إعادة التسمية')


class AdminTagDeleteView(APIView):
    """
    Delete a tag. QuestionTag rows referencing it are removed via
    cascade; the questions themselves are untouched.
    Requires 'questions.manage_tags'.
    """
    permission_classes = [HasCapability]
    required_capability = 'questions.manage_tags'

    def delete(self, request, name):
        tag = get_object_or_404(Tag, name=name)
        tag_id = tag.id
        tag.delete()
        log_privileged_action(request, 'tag.delete', target_repr=name,
                              details={'tag_id': tag_id})
        return api_success(message='تم حذف الوسم')


class AdminTagMergeView(APIView):
    """
    Move every QuestionTag row from each source tag to the target tag,
    then delete the source tags. Requires 'questions.manage_tags'.

    The target tag is created if it does not exist. A source entry
    equal to the target is skipped.

    NON-QUESTION RELATIONSHIPS (fix — issue: merge drops them)
    ---------------------------------------------------------
    Before this revision, the merge moved `QuestionTag` rows and then
    deleted each source tag. That cascade silently dropped every
    OTHER relationship the tag participated in:

      • `StudyPlanner.target_tags` — a planner whose tag list
        contained a merged source tag silently lost that
        subscription.
      • `Tag.parent` — every child of a merged source tag had its
        `parent_id` set to NULL.

    The merge now handles both relations BEFORE deleting the source.
    """
    permission_classes = [HasCapability]
    required_capability = 'questions.manage_tags'

    def post(self, request):
        body = TagMergeSerializer(data=request.data)
        if not body.is_valid():
            return api_error('بيانات غير صالحة', 400, details=body.errors)

        source_tags = body.validated_data['source_tags']

        # Source-empty check runs first — the original order. When a
        # caller sends neither sources nor a target, the error they
        # see is the source one, matching the pre-refactor behaviour.
        if not source_tags:
            return api_error('يجب اختيار وسم واحد على الأقل للدمج', 400)

        target_name, err = _validate_tag_name(
            body.validated_data['target_tag'],
            required_message='الوسم الهدف مطلوب',
        )
        if err is not None:
            return api_error(err, 400)

        target, _ = Tag.objects.get_or_create(name=target_name)

        # Lazy import — `planning.models` has no dependency on
        # `questions.models`, but keeping the import inside the method
        # avoids adding a `questions → planning` edge at module load.
        from apps.planning.models import StudyPlanner

        moved = 0
        reparented = 0
        plans_migrated = 0
        sources_processed = []

        for src_name in source_tags:
            src_name = src_name.strip()
            if not src_name or src_name == target_name:
                continue

            try:
                src = Tag.objects.get(name=src_name)
            except Tag.DoesNotExist:
                continue

            # Idempotency guard: if a previous merge already consumed
            # this source name, skip it.
            if src.id == target.id:
                continue

            with transaction.atomic():
                # 1. Reparent children.
                children_updated = Tag.objects.filter(parent=src).update(parent=target)
                reparented += children_updated

                # 2. Migrate planner subscriptions.
                affected_planners = (
                    StudyPlanner.objects
                    .filter(target_tags=src)
                    .distinct()
                )
                for planner in affected_planners:
                    planner.target_tags.add(target)
                    planner.target_tags.remove(src)
                    plans_migrated += 1

                # 3. Move QuestionTag rows, preserving the
                #    (question, tag) unique constraint.
                for qt in src.questiontag_set.all():
                    if not QuestionTag.objects.filter(
                        question_id=qt.question_id, tag=target,
                    ).exists():
                        QuestionTag.objects.create(
                            question_id=qt.question_id, tag=target,
                        )
                    moved += 1

                # 4. Delete the source.
                src.delete()

            sources_processed.append(src_name)

        log_privileged_action(
            request,
            'tag.merge',
            target=target,
            details={
                'sources': sources_processed,
                'question_links_moved': moved,
                'children_reparented': reparented,
                'planners_migrated': plans_migrated,
            },
        )
        return api_success(
            data={
                'moved': moved,
                'reparented': reparented,
                'planners_migrated': plans_migrated,
                'sources': sources_processed,
            },
            message=f'تم دمج {len(sources_processed)} وسم',
        )