# tests/users/test_admin_lock_ordering.py
"""
`_acquire_admin_lock_set` — deadlock-free lock ordering, plus the
post-lock state re-check that closes the concurrent-demotion race.

TWO DIFFERENT GUARANTEES, TWO DIFFERENT CLASSES
-----------------------------------------------
`AdminLockOrderingTests` covers the deadlock-free *ordering* of the
lock acquisition. A regression there would surface as an
InnoDB deadlock under concurrent admin operations — rare, hard to
reproduce, and invisible to a single-threaded test unless the test
inspects the acquisition sequence. The three tests in that class
spy on `select_for_update().filter()` calls and assert the
sequence is monotonically increasing.

`PostLockRecheckRaceTests` covers a *different* bug in the same
helper: the last-admin guard used to trust a pre-lock snapshot of
the other admins' roles. Two concurrent demotions of two different
admins could each see the other as "still an admin" in their
snapshot, each pass the guard, and leave the system with zero
admins.

HOW THE RACE IS MADE DETERMINISTIC
----------------------------------
A real race needs two concurrent transactions. This file
reproduces it single-threaded by intercepting the lock acquisition
and mutating the returned row's `role` in memory before it reaches
the helper. From the helper's point of view, that is
indistinguishable from a concurrent transaction that demoted the
row after the probe ran but before the lock was granted — the only
state the helper inspects is `row.role` and `row.is_active`, both
of which the mutation changes.

The mutation is in-memory only; nothing is written to the DB. That
means the test runs on any backend (including SQLite, which does
not honor `select_for_update`) and does not require
`TransactionTestCase`.
"""
from unittest.mock import patch

from apps.users.models import User
from apps.users.views.admin_user_views import _acquire_admin_lock_set
from tests.base import CacheClearingTestCase
from tests.factories import make_admin, make_user

class AdminLockOrderingTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.admins = [
            make_admin(f'admin_{i}', 'admin-pw-1234') for i in range(5)
        ]

    def test_locks_acquired_in_ascending_id_order(self):
        target = self.admins[0]
        recorded_ids = []

        original_sfu = User.objects.select_for_update

        class SpyWrapper:
            def __init__(self, qs):
                self.qs = qs

            def filter(self, **kw):
                recorded_ids.append(kw.get('id'))
                return self.qs.filter(**kw)

        def spy_sfu(*args, **kwargs):
            return SpyWrapper(original_sfu(*args, **kwargs))

        with patch.object(
            User.objects, 'select_for_update', spy_sfu,
        ):
            _acquire_admin_lock_set(target.id)

        self.assertEqual(
            recorded_ids, sorted(recorded_ids),
            f'locks acquired out of order: {recorded_ids}',
        )
        self.assertEqual(
            set(recorded_ids), {a.id for a in self.admins},
        )

    def test_non_admin_target_locks_only_its_own_row(self):
        """
        When the target is not an active admin, the guard has no
        reason to touch any other row. The lock set is exactly one
        element.
        """
        target = make_user('regular_user')
        recorded_ids = []

        original_sfu = User.objects.select_for_update

        class SpyWrapper:
            def __init__(self, qs):
                self.qs = qs

            def filter(self, **kw):
                recorded_ids.append(kw.get('id'))
                return self.qs.filter(**kw)

        def spy_sfu(*args, **kwargs):
            return SpyWrapper(original_sfu(*args, **kwargs))

        with patch.object(
            User.objects, 'select_for_update', spy_sfu,
        ):
            _acquire_admin_lock_set(target.id)

        self.assertEqual(recorded_ids, [target.id])

    def test_missing_target_locks_nothing(self):
        recorded_ids = []

        original_sfu = User.objects.select_for_update

        class SpyWrapper:
            def __init__(self, qs):
                self.qs = qs

            def filter(self, **kw):
                recorded_ids.append(kw.get('id'))
                return self.qs.filter(**kw)

        def spy_sfu(*args, **kwargs):
            return SpyWrapper(original_sfu(*args, **kwargs))

        with patch.object(
            User.objects, 'select_for_update', spy_sfu,
        ):
            user, other_ids = _acquire_admin_lock_set(999999)

        self.assertIsNone(user)
        self.assertEqual(other_ids, [])
        self.assertEqual(recorded_ids, [])

# ═════════════════════════════════════════════════════════════════════
# Post-lock state re-check — the concurrent-demotion race
# ═════════════════════════════════════════════════════════════════════

class _MutatingSFUWrapper:
    """
    Wraps a `select_for_update()` queryset so that `.filter(id=uid)
    .first()` returns a row whose `role` has been overridden, for the
    uids listed in `mutate_map`.

    `mutate_map` is { uid: callable(row) -> row }. The callable is
    applied to the row *after* it is fetched from the DB and *before*
    it is returned to the caller — the in-memory equivalent of "a
    concurrent transaction committed a demotion between our probe
    and our lock acquisition".
    """
    def __init__(self, real_queryset, mutate_map):
        self._real = real_queryset
        self._mutate_map = mutate_map

    def filter(self, **kw):
        uid = kw.get('id')
        return _MutatingFilteredQS(
            self._real.filter(**kw), uid, self._mutate_map,
        )

class _MutatingFilteredQS:
    def __init__(self, qs, uid, mutate_map):
        self._qs = qs
        self._uid = uid
        self._mutate_map = mutate_map

    def first(self):
        row = self._qs.first()
        if row is None:
            return None
        mutator = self._mutate_map.get(self._uid)
        if mutator is not None:
            row = mutator(row)
        return row

class PostLockRecheckRaceTests(CacheClearingTestCase):
    """
    The two-admins-demote-each-other race.

    Setup: exactly two active admins, A and B. Target is B.

    Interleaving reproduced:
      1. The helper's probe (unlocked) reads B and computes
         `other_ids = [A.id]` from the DB, where A is still an active
         admin.
      2. Before the helper locks A's row, a concurrent transaction
         (T1) acquires the lock, demotes A, and commits.
      3. The helper's lock acquisition now returns A's row with
         `role='member'`.

    The helper must NOT report A as a remaining active admin. If it
    does, two such operations running concurrently would each pass
    the last-admin guard and leave the system with zero active
    admins.
    """
    def setUp(self):
        super().setUp()
        self.a = make_admin('admin_a', 'admin-pw-1234')
        self.b = make_admin('admin_b', 'admin-pw-1234')

    def _spy_with_demotion(self, demoted_uid):
        """
        Return a `select_for_update` replacement that mutates the
        locked row for `demoted_uid` to role='member' before handing
        it back, and passes every other row through unchanged.
        """
        original_sfu = User.objects.select_for_update

        def _demote(row):
            row.role = 'member'
            return row

        mutate_map = {demoted_uid: _demote}

        def spy(*args, **kwargs):
            return _MutatingSFUWrapper(
                original_sfu(*args, **kwargs), mutate_map,
            )

        return spy

    def test_concurrently_demoted_admin_is_not_counted(self):

        with patch.object(
            User.objects, 'select_for_update',
            self._spy_with_demotion(self.a.id),
        ):
            user, other_active_admin_ids = _acquire_admin_lock_set(self.b.id)

        self.assertIsNotNone(user)
        self.assertEqual(user.id, self.b.id)

        self.assertNotIn(
            self.a.id, other_active_admin_ids,
            (
                'Regression: a concurrently-demoted admin was counted '
                'as a remaining active admin. The last-admin guard '
                'will read this as "another admin still exists" and '
                'permit the demotion, leaving zero active admins. '
                'The post-lock state re-check in '
                '_acquire_admin_lock_set exists to close exactly this '
                'race; see the docstring there.'
            ),
        )
        self.assertEqual(
            other_active_admin_ids, [],
            'The demoted admin must not be reported as a colleague.',
        )

    def test_still_active_admin_is_counted(self):
        """
        Complement to the regression test above.

        If the locked row still says `role='admin'` and
        `is_active=True`, the helper must count it. This guards
        against over-correction — e.g. a future refactor that drops
        the "colleague" branch entirely and always returns `[]`,
        which would make the last-admin guard refuse every demotion.
        """
        with patch.object(
            User.objects, 'select_for_update',
            self._spy_with_demotion(999_999),   # nobody is mutated
        ):
            user, other_active_admin_ids = _acquire_admin_lock_set(self.b.id)

        self.assertIsNotNone(user)
        self.assertIn(
            self.a.id, other_active_admin_ids,
            (
                'A genuinely still-active admin must be reported as '
                'a colleague. Returning [] here would make the '
                'last-admin guard refuse every demotion, including '
                'those that are safe.'
            ),
        )

    def test_deactivated_other_admin_is_not_counted(self):
        """
        Same mechanism as the demotion test, but the concurrent
        transaction deactivated the row instead of changing its
        role. Both fields participate in the guard, and the
        post-lock re-check must reject either form of loss.
        """
        original_sfu = User.objects.select_for_update

        def _deactivate(row):
            row.is_active = False
            return row

        mutate_map = {self.a.id: _deactivate}

        def spy(*args, **kwargs):
            return _MutatingSFUWrapper(
                original_sfu(*args, **kwargs), mutate_map,
            )

        with patch.object(User.objects, 'select_for_update', spy):
            user, other_active_admin_ids = _acquire_admin_lock_set(self.b.id)

        self.assertIsNotNone(user)
        self.assertEqual(other_active_admin_ids, [])

    def test_target_is_never_counted_as_its_own_colleague(self):
        """
        The target row is in `locked` (it must be, or the caller
        would 404). It must never appear in the "other admins" list
        — otherwise a single-admin deployment would always see
        itself as a colleague and the last-admin guard would never
        fire.

        This is the degenerate case for a one-admin install, and it
        is the shape a future refactor is most likely to break by
        dropping the explicit `uid != probe.id` exclusion.
        """
        # Demote `self.a` in the DB so B is the only active admin.
        self.a.role = 'member'
        self.a.save(update_fields=['role'])
        self.a.refresh_from_db()

        with patch.object(
            User.objects, 'select_for_update',
            self._spy_with_demotion(999_999),   # nobody is mutated
        ):
            user, other_active_admin_ids = _acquire_admin_lock_set(self.b.id)

        self.assertIsNotNone(user)
        self.assertEqual(
            other_active_admin_ids, [],
            (
                'The target admin must not appear in the colleague '
                'list. If it does, the last-admin guard never fires '
                'and the sole admin can be demoted out of existence.'
            ),
        )