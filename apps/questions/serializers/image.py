# backend/apps/questions/serializers/image.py
from rest_framework import serializers

from .constants import (
    MAX_IMAGE_SIZE,
    ALLOWED_IMAGE_EXTS,
    ALLOWED_IMAGE_MIMES,
)


class QuestionImageUploadSerializer(serializers.Serializer):
    image = serializers.FileField(required=True)

    def validate_image(self, value):
        name = getattr(value, 'name', '') or ''
        ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        if ext not in ALLOWED_IMAGE_EXTS:
            raise serializers.ValidationError(
                f'نوع الملف غير مدعوم. الأنواع المسموحة: {", ".join(sorted(ALLOWED_IMAGE_EXTS))}',
            )
        if value.size > MAX_IMAGE_SIZE:
            raise serializers.ValidationError(
                f'حجم الصورة يتجاوز الحد الأقصى ({MAX_IMAGE_SIZE // (1024 * 1024)} ميجابايت)',
            )

        try:
            import magic
        except ImportError:
            raise serializers.ValidationError(
                'تعذر التحقق من نوع الملف (الخادم غير مهيأ). تواصل مع المسؤول.',
            )
        try:
            head = value.read(1024)
            value.seek(0)
            detected = magic.from_buffer(head, mime=True)
        except Exception:
            try:
                value.seek(0)
            except Exception:
                pass
            raise serializers.ValidationError('تعذر التحقق من نوع الملف')

        if detected not in ALLOWED_IMAGE_MIMES:
            raise serializers.ValidationError(
                f'نوع المحتوى غير مطابق للصورة ({detected})',
            )
        return value