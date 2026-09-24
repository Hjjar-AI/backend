from apps.learning.knowledge_map import build_knowledge_map
from apps.learning.models import UserQuestionAttempt
from apps.questions.models import KnowledgeObject
from tests.base import CacheClearingTestCase
from tests.factories import make_category, make_question, make_user


class KnowledgeMapTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('map_user')
        self.category = make_category('Map category')
        self.object = KnowledgeObject.objects.create(
            title='Concept one',
            learning_objective='Explain concept one',
            category=self.category,
            created_by=self.user,
        )
        self.question = make_question(
            owner=self.user,
            knowledge_object=self.object,
        )

    def test_unattempted_object_is_visible_as_unstarted(self):
        result = build_knowledge_map(self.user)

        self.assertEqual(result['summary']['total_objects'], 1)
        self.assertEqual(result['summary']['unstarted_objects'], 1)
        self.assertEqual(result['items'][0]['status'], 'unstarted')
        self.assertEqual(result['items'][0]['question_ids'], [self.question.id])

    def test_correct_confident_retained_attempt_is_mastered(self):
        UserQuestionAttempt.objects.create(
            user=self.user,
            question=self.question,
            last_correct=True,
            last_confidence=True,
            last_confidence_score=3,
            attempts=2,
            ever_correct=True,
            repetitions=3,
        )

        result = build_knowledge_map(self.user)
        item = result['items'][0]

        self.assertEqual(item['mastery_score'], 100)
        self.assertEqual(item['status'], 'mastered')
        self.assertEqual(item['average_confidence'], 3)
        self.assertEqual(result['summary']['mastered_objects'], 1)

    def test_draft_questions_do_not_create_map_entries(self):
        self.question.is_draft = True
        self.question.draft_owner = self.user
        self.question.save(update_fields=['is_draft', 'draft_owner'])

        result = build_knowledge_map(self.user)

        self.assertEqual(result['summary']['total_objects'], 0)
        self.assertEqual(result['items'], [])
