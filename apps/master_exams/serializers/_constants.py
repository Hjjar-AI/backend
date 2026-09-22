# backend/apps/master_exams/serializers/_constants.py
"""
Serializer-level limits shared across the master-exam serializer
modules.

`MAX_CHOICES` is read from settings so a deployment can widen the
choice count without touching code. Keeping it in one module means
the write serializers and the attempt serializer agree on the same
number by construction.
"""

from django.conf import settings


MAX_CHOICES = getattr(settings, 'MAX_CHOICES', 8)