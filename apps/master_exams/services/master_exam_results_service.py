# backend/apps/master_exams/services/master_exam_results_service.py

import csv
import io

from django.db.models import Avg
from django.utils import timezone

from apps.questions.models import Question
from apps.questions.services.exporting.formula_sanitizer import (
    sanitize_formula_cell,
)
from apps.feedback.models import QuestionFlag

from ..models import MasterExamAttempt


class MasterExamResultsService:

    # ── Live dashboard ───────────────────────────────────────────────
    @staticmethod
    def live_summary(exam):
        attempts = MasterExamAttempt.objects.filter(master_exam=exam)

        from .master_exam_service import MasterExamService
        audience_ids = set(
            MasterExamService.resolve_audience(exam).values_list('id', flat=True)
        )
        total_assigned = len(audience_ids)

        started = attempts.count()
        finished = attempts.filter(is_complete=True).count()
        in_progress = started - finished

        finished_qs = attempts.filter(is_complete=True)
        avg_accuracy = finished_qs.aggregate(avg=Avg('accuracy'))['avg'] or 0.0
        avg_weighted = finished_qs.aggregate(avg=Avg('weighted_score'))['avg'] or 0.0
        not_started = max(0, total_assigned - started)

        return {
            'total_assigned': total_assigned,
            'started': started,
            'finished': finished,
            'in_progress': in_progress,
            'not_started': not_started,
            'avg_accuracy': round(avg_accuracy, 2),
            'avg_weighted_score': round(avg_weighted, 2),
        }

    @staticmethod
    def per_user_rows(exam):
        rows = []
        for a in (
            MasterExamAttempt.objects
            .filter(master_exam=exam)
            .select_related('user')
            .order_by('-accuracy', 'user__username')
        ):
            rows.append({
                'user_id': a.user_id,
                'username': a.user.username,
                'full_name': a.user.full_name or a.user.username,
                'is_complete': a.is_complete,
                'is_makeup': a.is_makeup,
                'forced_finish': a.forced_finish,
                'correct_count': a.correct_count,
                'total_questions': a.total_questions,
                'accuracy': round(a.accuracy, 2),
                'weighted_score': round(a.weighted_score, 2),
                'started_at': a.started_at.isoformat(),
                'finished_at': a.finished_at.isoformat() if a.finished_at else None,
            })
        return rows

    @staticmethod
    def _ordered_question_ids(exam):
        return list(
            exam.exam_questions
            .order_by('order')
            .values_list('question_id', flat=True)
        )

    @staticmethod
    def dashboard_snapshot(exam):
        """Read attempts and question metadata once for all dashboard breakdowns."""
        completed = list(
            MasterExamAttempt.objects
            .filter(master_exam=exam, is_complete=True)
            .only('answers', 'results')
        )
        qids = MasterExamResultsService._ordered_question_ids(exam)
        questions = {
            q.id: q
            for q in Question.objects.filter(id__in=qids).select_related('category')
        }
        stats = {
            qid: {'answered': 0, 'correct': 0, 'distribution': {}, 'saved_key': None}
            for qid in qids
        }
        for attempt in completed:
            saved = {
                row.get('question_id'): row
                for row in (attempt.results or {}).get('questions', [])
                if isinstance(row, dict)
            }
            for qid in qids:
                raw = (attempt.answers or {}).get(str(qid))
                if not isinstance(raw, dict) or raw.get('answer') is None:
                    continue
                bucket = stats[qid]
                answer = raw['answer']
                bucket['answered'] += 1
                key = str(answer)
                bucket['distribution'][key] = bucket['distribution'].get(key, 0) + 1
                result = saved.get(qid)
                if result is not None:
                    bucket['correct'] += bool(result.get('is_correct'))
                    if bucket['saved_key'] is None:
                        bucket['saved_key'] = result.get('correct_answer')
                elif qid in questions:
                    # Completed attempts from before result snapshots existed.
                    bucket['correct'] += answer == questions[qid].correct_answer

        per_question = []
        by_category = {}
        by_difficulty = {
            key: {'total': 0, 'answered': 0, 'correct': 0}
            for key in ('easy', 'medium', 'hard')
        }
        for qid in qids:
            q = questions.get(qid)
            if q is None:
                per_question.append({
                    'question_id': qid, 'question_text': '(محذوف)',
                    'answered_count': 0, 'correct_count': 0,
                    'correct_rate': 0.0, 'distribution': {},
                })
                continue
            values = stats[qid]
            answered = values['answered']
            correct = values['correct']
            per_question.append({
                'question_id': qid,
                'question_text': q.question[:120],
                'correct_answer': values['saved_key'] if values['saved_key'] is not None else q.correct_answer,
                'answered_count': answered,
                'correct_count': correct,
                'correct_rate': round(correct / answered * 100, 1) if answered else 0.0,
                'distribution': values['distribution'],
            })

            name = q.category.name if q.category else 'بدون تصنيف'
            category = by_category.setdefault(name, {
                'category_name': name,
                'category_color': q.category.color if q.category else '#999',
                'total': 0, 'answered': 0, 'correct': 0,
            })
            category['total'] += 1
            category['answered'] += answered
            category['correct'] += correct

            difficulty = by_difficulty.get(q.difficulty, by_difficulty['medium'])
            difficulty['total'] += 1
            difficulty['answered'] += answered
            difficulty['correct'] += correct

        def with_rate(row):
            return {
                **row,
                'correct_rate': (
                    round(row['correct'] / row['answered'] * 100, 1)
                    if row['answered'] else 0.0
                ),
            }

        return {
            'per_question': per_question,
            'per_category': [with_rate(row) for row in by_category.values()],
            'per_difficulty': [
                with_rate({'difficulty': key, **row})
                for key, row in by_difficulty.items()
            ],
        }

    @staticmethod
    def per_question_stats(exam, snapshot=None):
        return (snapshot or MasterExamResultsService.dashboard_snapshot(exam))['per_question']

    @staticmethod
    def per_category_stats(exam, snapshot=None):
        return (snapshot or MasterExamResultsService.dashboard_snapshot(exam))['per_category']

    @staticmethod
    def per_difficulty_stats(exam, snapshot=None):
        return (snapshot or MasterExamResultsService.dashboard_snapshot(exam))['per_difficulty']

    @staticmethod
    def score_histogram(exam):
        buckets = [0, 0, 0, 0, 0]
        for a in MasterExamAttempt.objects.filter(master_exam=exam, is_complete=True):
            acc = max(0.0, min(100.0, a.accuracy or 0.0))
            idx = min(4, int(acc // 20))
            buckets[idx] += 1
        return [
            {'range': '0-20', 'count': buckets[0]},
            {'range': '20-40', 'count': buckets[1]},
            {'range': '40-60', 'count': buckets[2]},
            {'range': '60-80', 'count': buckets[3]},
            {'range': '80-100', 'count': buckets[4]},
        ]

    @staticmethod
    def flags_raised_during_exam(exam):
        attempts = MasterExamAttempt.objects.filter(master_exam=exam)
        attempt_ids = list(attempts.values_list('id', flat=True))
        if not attempt_ids:
            return []
        flags = (
            QuestionFlag.objects
            .filter(master_exam_attempt_id__in=attempt_ids, resolved=False)
            .select_related('user', 'question')
            .order_by('-created_at')
        )
        return [
            {
                'flag_id': f.id,
                'question_id': f.question_id,
                'question_text': f.question.question[:120] if f.question else '(محذوف)',
                'flagger_username': f.user.username,
                'reason': f.reason or '',
                'created_at': f.created_at.isoformat(),
            }
            for f in flags
        ]

    # ── CSV exports ──────────────────────────────────────────────────
    #
    # Both writers apply `sanitize_formula_cell` to every user-supplied
    # string column. Without it, a `User.full_name` beginning with
    # `=`, `+`, `-`, `@`, `\t`, or `\r` would be interpreted by Excel,
    # LibreOffice, or Google Sheets as a formula when the CSV is
    # opened — a spreadsheet formula-injection vector. The flat export
    # path (`apps/questions/services/exporting/flat_export.py`) already
    # applies the same defense; these two writers were the outliers.
    #
    # `username` is regex-restricted at the model layer
    # (alphanumeric, 3–50 chars) so it cannot begin with a trigger
    # character today. It is sanitized anyway: the defense should not
    # depend on an invariant defined in a different module, and the
    # cost of a no-op call on an already-safe string is a single
    # `str.startswith` check.

    @staticmethod
    def csv_summary(exam):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            'full_name', 'username', 'score', 'correct_count', 'total',
            'accuracy', 'weighted_score', 'is_makeup', 'forced_finish',
            'started_at', 'finished_at',
        ])
        for a in (
            MasterExamAttempt.objects
            .filter(master_exam=exam)
            .select_related('user')
            .order_by('-accuracy', 'user__username')
        ):
            full_name = a.user.full_name or a.user.username
            writer.writerow([
                sanitize_formula_cell(full_name),
                sanitize_formula_cell(a.user.username),
                f'{a.correct_count}/{a.total_questions}' if a.is_complete else '(لم يكمل)',
                a.correct_count,
                a.total_questions,
                f'{a.accuracy:.1f}%' if a.is_complete else '',
                f'{a.weighted_score:.1f}%' if a.is_complete else '',
                'نعم' if a.is_makeup else 'لا',
                'نعم' if a.forced_finish else 'لا',
                a.started_at.isoformat() if a.started_at else '',
                a.finished_at.isoformat() if a.finished_at else '',
            ])
        return buffer.getvalue()

    @staticmethod
    def csv_matrix(exam):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        qids = MasterExamResultsService._ordered_question_ids(exam)
        questions = {
            q.id: q
            for q in Question.objects.filter(id__in=qids)
        }
        header = ['full_name', 'username'] + [
            f'Q{idx + 1}' for idx in range(len(qids))
        ]
        writer.writerow(header)
        for a in (
            MasterExamAttempt.objects
            .filter(master_exam=exam, is_complete=True)
            .select_related('user')
            .order_by('-accuracy', 'user__username')
        ):
            saved = {
                item.get('question_id'): item
                for item in (a.results or {}).get('questions', [])
                if isinstance(item, dict)
            }
            full_name = a.user.full_name or a.user.username
            row = [
                sanitize_formula_cell(full_name),
                sanitize_formula_cell(a.user.username),
            ]
            for qid in qids:
                q = questions.get(qid)
                raw = (a.answers or {}).get(str(qid))
                if q is None or raw is None:
                    row.append('—')
                    continue
                historical = saved.get(qid)
                if historical is not None:
                    is_correct = bool(historical.get('is_correct'))
                else:
                    is_correct = raw.get('answer') == q.correct_answer
                if is_correct:
                    row.append('✓')
                else:
                    row.append('✗')
            writer.writerow(row)
        return buffer.getvalue()
