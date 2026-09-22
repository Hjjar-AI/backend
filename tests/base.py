# tests/base.py
"""
Shared test base classes.

CacheClearingTestCase exists because the capability system caches
role → capability sets in Django's cache (see
apps/users/services/permission_service.py). TestCase wraps DB writes
in a transaction that rolls back, but the cache is NOT transactional.
Without an explicit clear, one test's RoleCapabilities row would
leak into the next test's cache lookup.
"""
from django.core.cache import cache
from django.test import TestCase


class CacheClearingTestCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def tearDown(self):
        cache.clear()
        super().tearDown()