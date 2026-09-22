# backend/apps/questions/serializers/category.py
from rest_framework import serializers

from ..models import Category


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = [
            'id', 'uuid', 'name', 'description', 'color', 'icon',
            'created_at', 'created_by',
        ]
        read_only_fields = ['id', 'uuid', 'created_at', 'created_by']