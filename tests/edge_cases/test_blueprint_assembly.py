# tests/edge_cases/test_blueprint_assembly.py
from django.db import IntegrityError, transaction

from apps.exams.models import Blueprint, BlueprintWeight
from apps.exams.services import BlueprintService
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question, make_category


class BlueprintSelectionTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.cat_a = make_category('cat-a')
        self.cat_b = make_category('cat-b')

        for _ in range(10):
            make_question(owner=self.user, category=self.cat_a)
            make_question(owner=self.user, category=self.cat_b)

        self._bp_counter = 0

    def _bp(self, weights):
        self._bp_counter += 1
        bp = Blueprint.objects.create(
            name=f'BP{self._bp_counter}', created_by='test',
        )
        for cid, w in weights.items():
            BlueprintWeight.objects.create(
                blueprint=bp, category_id=cid, weight=w,
            )
        return bp

    def test_no_weights_returns_any_questions(self):
        bp = Blueprint.objects.create(name='Empty', created_by='test')
        ids = BlueprintService.select_question_ids(bp, 5)
        self.assertEqual(len(ids), 5)

    def test_weighted_selection_returns_requested_count(self):
        bp = self._bp({self.cat_a.id: 3.0, self.cat_b.id: 1.0})
        ids = BlueprintService.select_question_ids(bp, 8)
        self.assertEqual(len(ids), 8)

    def test_never_returns_duplicates(self):
        bp = self._bp({self.cat_a.id: 1.0})
        ids = BlueprintService.select_question_ids(bp, 20)
        self.assertEqual(len(ids), len(set(ids)))

    def test_shortfall_backfilled_from_other_categories(self):
        # Only one 10-question category is weighted, but 15 are
        # requested. The shortfall must be filled from the other
        # category rather than returned short.
        bp = self._bp({self.cat_a.id: 1.0})
        ids = BlueprintService.select_question_ids(bp, 15)
        self.assertEqual(len(ids), 15)

    def test_deleted_category_is_skipped(self):
        bp = self._bp({self.cat_a.id: 1.0, self.cat_b.id: 1.0})
        self.cat_a.delete()
        # BlueprintWeight for cat_a cascades. Selection must not crash.
        ids = BlueprintService.select_question_ids(bp, 5)
        self.assertEqual(len(ids), 5)


class BlueprintWeightConstraintTests(CacheClearingTestCase):
    """
    The BlueprintWeight CheckConstraint refuses weight <= 0. The
    serializer drops zeros before they reach this layer, but the
    constraint is what actually enforces it — a migration, a shell
    session, or an admin edit cannot slip a zero in.

    This test replaces the earlier (wrong) attempt to create a
    zero-weight row and assert the selection fell back to "any
    question". The zero can never be stored, so the fallback path
    is unreachable from that direction.
    """
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.cat = make_category('cat-x')
        self.bp = Blueprint.objects.create(name='Constraint', created_by='test')

    def test_zero_weight_is_refused_by_constraint(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BlueprintWeight.objects.create(
                    blueprint=self.bp, category=self.cat, weight=0.0,
                )

    def test_negative_weight_is_refused_by_constraint(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BlueprintWeight.objects.create(
                    blueprint=self.bp, category=self.cat, weight=-1.0,
                )

    def test_small_positive_weight_is_accepted(self):
        # The constraint is `weight > 0`, so any positive value
        # passes — there is no minimum magnitude.
        BlueprintWeight.objects.create(
            blueprint=self.bp, category=self.cat, weight=0.0001,
        )
        self.assertEqual(self.bp.weight_entries.count(), 1)