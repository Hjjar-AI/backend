"""Common question query filters for lists and exports."""

from django.db.models import Q

from .models import Question

VALID_DIFFICULTIES = frozenset(value for value, _ in Question.DIFFICULTY_CHOICES)


def _values(raw, split_string=True):
    if raw is None or raw == '':
        return []
    if isinstance(raw, str):
        return raw.split(',') if split_string else [raw]
    try:
        return list(raw)
    except TypeError:
        return []


def _integer_ids(raw, *, positive_only=False):
    ids = []
    for value in _values(raw):
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if not positive_only or number > 0:
            ids.append(number)
    return ids


def filter_questions(queryset, filters=None, *, export=False, verified_only=False):
    if verified_only:
        queryset = queryset.filter(verified=True)
    if not filters:
        return queryset

    search = (filters.get('search') or '').strip()
    joins_tags = False
    if len(search) >= 2:
        search_query = (
            Q(question__icontains=search)
            | Q(explanation__icontains=search)
            | Q(source__icontains=search)
            | Q(source_document__icontains=search)
        )
        if not export:
            search_query |= Q(tags__name__icontains=search)
            joins_tags = True
        queryset = queryset.filter(search_query)

    if export:
        difficulties = []
        for piece in _values(filters.get('difficulty')):
            value = str(piece).strip().lower()
            if value in VALID_DIFFICULTIES and value not in difficulties:
                difficulties.append(value)
        if difficulties:
            queryset = queryset.filter(difficulty__in=difficulties)
    elif filters.get('difficulty'):
        queryset = queryset.filter(difficulty=filters['difficulty'])

    category_ids = _integer_ids(filters.get('category_ids'), positive_only=export)
    if category_ids:
        queryset = queryset.filter(category_id__in=category_ids)
    elif not export and not filters.get('category_ids') and filters.get('category'):
        try:
            category_id = int(filters['category'])
        except (TypeError, ValueError):
            category_id = None
        if category_id is not None:
            queryset = queryset.filter(category_id=category_id)

    tag = filters.get('tag')
    if export:
        tag = (tag or '').strip()
    if tag:
        queryset = queryset.filter(tags__name=tag)
        joins_tags = True

    tag_names = _values(filters.get('tags_filter'), split_string=export)
    if export:
        tag_names = [str(name).strip() for name in tag_names if str(name).strip()]
    if tag_names:
        queryset = queryset.filter(tags__name__in=tag_names)
        joins_tags = True

    if not export:
        if filters.get('verified') == 'yes':
            queryset = queryset.filter(verified=True)
        elif filters.get('verified') == 'no':
            queryset = queryset.filter(verified=False)
        if filters.get('bookmark_user_id'):
            queryset = queryset.filter(
                bookmarked_by__user_id=filters['bookmark_user_id'],
            )

    return queryset.distinct() if joins_tags else queryset
