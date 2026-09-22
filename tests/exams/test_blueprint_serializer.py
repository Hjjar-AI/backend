# tests/exams/test_blueprint_serializer.py
"""
BlueprintSerializer — the `weights` dict round-trip through the
BlueprintWeight through-table.

Wire shape: `weights` is { "category_id": weight }. Zero weights
are dropped (an absent key is the canonical "category not in this
blueprint"). Non-numeric and unknown-category-id entries are
dropped silently. An update with no `weights` key leaves the
through-table alone.
"""
from apps.exams.models import Blueprint, BlueprintWeight
from apps.exams.serializers import BlueprintSerializer
from tests.base import CacheClearingTestCase
from tests.factories import make_category


class BlueprintSerializerCreateTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.cat_a = make_category('cat-a')
        self.cat_b = make_category('cat-b')

    def _serializer(self, data):
        return BlueprintSerializer(data=data)

    def test_create_without_weights(self):
        s = self._serializer({'name': 'Bare', 'weights': {}})
        self.assertTrue(s.is_valid(), s.errors)
        bp = s.save(created_by='test')
        self.assertEqual(bp.weight_entries.count(), 0)

    def test_create_with_weights(self):
        s = self._serializer({
            'name': 'Weighted',
            'weights': {
                str(self.cat_a.id): 2.0,
                str(self.cat_b.id): 3.5,
            },
        })
        self.assertTrue(s.is_valid(), s.errors)
        bp = s.save(created_by='test')
        weights = {
            e.category_id: e.weight
            for e in bp.weight_entries.all()
        }
        self.assertEqual(weights, {self.cat_a.id: 2.0, self.cat_b.id: 3.5})

    def test_zero_weight_dropped(self):
        s = self._serializer({
            'name': 'Zero-dropped',
            'weights': {str(self.cat_a.id): 0.0},
        })
        self.assertTrue(s.is_valid(), s.errors)
        bp = s.save(created_by='test')
        self.assertEqual(bp.weight_entries.count(), 0)

    def test_unknown_category_id_dropped(self):
        s = self._serializer({
            'name': 'Unknown-cat',
            'weights': {
                str(self.cat_a.id): 1.0,
                '999999': 5.0,
            },
        })
        self.assertTrue(s.is_valid(), s.errors)
        bp = s.save(created_by='test')
        weights = {
            e.category_id for e in bp.weight_entries.all()
        }
        self.assertEqual(weights, {self.cat_a.id})

    def test_negative_weight_rejected(self):
        s = self._serializer({
            'name': 'Negative',
            'weights': {str(self.cat_a.id): -1.0},
        })
        self.assertFalse(s.is_valid())
        self.assertIn('weights', s.errors)

    def test_non_numeric_weight_rejected(self):
        s = self._serializer({
            'name': 'Non-numeric',
            'weights': {str(self.cat_a.id): 'abc'},
        })
        self.assertFalse(s.is_valid())
        self.assertIn('weights', s.errors)

    def test_representation_emits_weights_dict(self):
        s = self._serializer({
            'name': 'Emitted',
            'weights': {str(self.cat_a.id): 2.0},
        })
        self.assertTrue(s.is_valid(), s.errors)
        bp = s.save(created_by='test')
        bp = Blueprint.objects.prefetch_related('weight_entries').get(pk=bp.pk)
        data = BlueprintSerializer(bp).data
        self.assertEqual(data['weights'], {str(self.cat_a.id): 2.0})


class BlueprintSerializerUpdateTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.cat_a = make_category('cat-a')
        self.cat_b = make_category('cat-b')
        self.bp = Blueprint.objects.create(name='Original', created_by='t')
        BlueprintWeight.objects.create(
            blueprint=self.bp, category=self.cat_a, weight=1.0,
        )

    def test_omitting_weights_leaves_table_alone(self):
        s = BlueprintSerializer(
            self.bp, data={'name': 'Renamed'}, partial=True,
        )
        self.assertTrue(s.is_valid(), s.errors)
        s.save()
        self.bp.refresh_from_db()
        self.assertEqual(self.bp.name, 'Renamed')
        self.assertEqual(self.bp.weight_entries.count(), 1)

    def test_empty_weights_clears_table(self):
        s = BlueprintSerializer(
            self.bp, data={'weights': {}}, partial=True,
        )
        self.assertTrue(s.is_valid(), s.errors)
        s.save()
        self.assertEqual(self.bp.weight_entries.count(), 0)

    def test_new_weights_replaces_old(self):
        s = BlueprintSerializer(
            self.bp,
            data={'weights': {str(self.cat_b.id): 4.0}},
            partial=True,
        )
        self.assertTrue(s.is_valid(), s.errors)
        s.save()
        weights = {
            e.category_id: e.weight
            for e in self.bp.weight_entries.all()
        }
        self.assertEqual(weights, {self.cat_b.id: 4.0})