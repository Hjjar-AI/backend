from apps.feedback.models import QuestionFlag
from apps.questions.models import Question
from apps.questions.services.content_quality import (
    build_data_quality_report,
    flag_data_quality_report,
    quality_key,
)
from tests.base import CacheClearingTestCase
from tests.factories import make_category, make_question, make_user


class ContentQualityReportTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user('quality_admin')
        self.category = make_category('quality')

    def test_report_finds_duplicate_questions_choices_and_missing_metadata(self):
        first = make_question(
            owner=self.admin,
            category=self.category,
            question='Repeated question?',
            source_document='source.pdf',
        )
        second = make_question(
            owner=self.admin,
            category=None,
            question='Repeated question? [DUP-ABCD]',
            explanation='',
            source_document=None,
        )
        # Bypass interactive model validation to represent an imported legacy
        # row whose duplicate choices still need to be reported.
        Question.objects.filter(pk=second.pk).update(
            choices=['Same', 'Same', 'Other'],
        )

        report = build_data_quality_report()
        by_id = {item['id']: item for item in report['items']}

        self.assertEqual(quality_key(first.question), quality_key(second.question))
        self.assertIn('duplicate_question', {
            issue['code'] for issue in by_id[first.id]['issues']
        })
        second_codes = {issue['code'] for issue in by_id[second.id]['issues']}
        self.assertTrue({
            'duplicate_question', 'duplicate_choices', 'missing_explanation',
            'missing_category', 'missing_source_document',
        }.issubset(second_codes))

    def test_flagging_report_creates_one_normal_open_flag_per_question(self):
        question = make_question(
            owner=self.admin,
            category=None,
            explanation='',
            source_document=None,
        )
        report = build_data_quality_report()

        created = flag_data_quality_report(report, self.admin)
        created_again = flag_data_quality_report(build_data_quality_report(), self.admin)

        self.assertEqual(created, 1)
        self.assertEqual(created_again, 0)
        self.assertTrue(QuestionFlag.objects.filter(
            question=question,
            user=self.admin,
            resolved=False,
            reason__startswith='[DATA_QUALITY]',
        ).exists())
