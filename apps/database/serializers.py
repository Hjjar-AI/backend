from rest_framework import serializers

class RestoreBackupSerializer(serializers.Serializer):
    backup_name = serializers.CharField()
    admin_password = serializers.CharField()


class ClearDatabaseSerializer(serializers.Serializer):
    admin_password = serializers.CharField()


class PdfFrontMatterFieldSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=60, trim_whitespace=True)
    value = serializers.CharField(max_length=500, trim_whitespace=True)


class PdfFrontMatterSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(default=False)
    heading = serializers.CharField(
        max_length=150, required=False, allow_blank=True, default='',
    )
    body = serializers.CharField(
        max_length=3000, required=False, allow_blank=True, default='',
    )
    fields = PdfFrontMatterFieldSerializer(many=True, required=False, default=list)

    def validate_fields(self, value):
        if len(value) > 10:
            raise serializers.ValidationError('A maximum of 10 custom fields is allowed.')
        return value


class PdfExportRequestSerializer(serializers.Serializer):
    title = serializers.CharField(
        max_length=150, required=False, allow_blank=True, default='',
    )
    theme = serializers.CharField(
        max_length=32, required=False, allow_blank=True, default='',
    )
    locale = serializers.ChoiceField(
        choices=('ar', 'en'), required=False,
    )
    filters = serializers.DictField(required=False, default=dict)
    front_matter = PdfFrontMatterSerializer(required=False)

    def validate_filters(self, value):
        allowed = {'search', 'difficulty', 'category_ids', 'tag', 'tags_filter'}
        unknown = set(value) - allowed
        if unknown:
            raise serializers.ValidationError(
                f"Unknown filter fields: {', '.join(sorted(unknown))}"
            )

        cleaned = {}
        for key, raw in value.items():
            if isinstance(raw, list):
                raw = ','.join(str(item) for item in raw)
            if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                raise serializers.ValidationError(
                    f"Filter '{key}' must be text, a number, or a list."
                )
            text = str(raw).strip()
            if text:
                cleaned[key] = text[:500]
        return cleaned
