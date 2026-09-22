# backend/apps/core/management/commands/seed_sample_questions.py

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.questions.models import (
    Question,
    Category,
    Tag,
    ClinicalCase,
    clean_tag_name,
)
from apps.users.models import User

SAMPLE_QUESTIONS = [
    # ── Standalone questions ─────────────────────────────────────────
    {
        'question': 'ما هو العرض الأساسي الذي يميز الفصام عن اضطراب فصامي الشكل؟',
        'choices': ['وجود هلوسات سمعية', 'وجود ضلالات', 'مدة الأعراض', 'وجود أعراض سلبية'],
        'correct_answer': 3,
        'explanation': 'الفصام يتطلب وجود الأعراض لمدة 6 أشهر على الأقل، بينما اضطراب فصامي الشكل يستمر من شهر إلى 6 أشهر.',
        'tags': 'فصام,تشخيص تفريقي',
        'difficulty': 'medium',
        'category': 'الاضطرابات الذهانية',
    },
    {
        'question': 'أي من الأعراض التالية يعتبر من الأعراض الإيجابية للفصام؟',
        'choices': ['انعدام التلذذ', 'الهلاوس السمعية', 'الانسحاب الاجتماعي', 'تسطيح المشاعر'],
        'correct_answer': 2,
        'explanation': 'الهلاوس (خاصة السمعية) والضلالات هي أعراض إيجابية. الأعراض السلبية تشمل انعدام التلذذ والانسحاب.',
        'tags': 'فصام,أعراض إيجابية',
        'difficulty': 'easy',
        'category': 'الاضطرابات الذهانية',
    },
    {
        'question': 'ما هي المدة المطلوبة لتشخيص نوبة اكتئاب كبرى حسب DSM-5؟',
        'choices': ['أسبوع واحد', 'أسبوعين', 'شهر واحد', '6 أشهر'],
        'correct_answer': 2,
        'explanation': 'يجب أن تستمر الأعراض لمدة أسبوعين على الأقل وتشمل إما مزاج مكتئب أو فقدان الاهتمام.',
        'tags': 'اكتئاب,تشخيص',
        'difficulty': 'easy',
        'category': 'اضطرابات المزاج',
    },
    {
        'question': 'أي من الأدوية التالية هو الخط الأول لعلاج اضطراب ثنائي القطب الحاد؟',
        'choices': ['فلوكستين', 'ليثيوم', 'كلوزابين', 'ديازيبام'],
        'correct_answer': 2,
        'explanation': 'الليثيوم هو الدواء المعياري الذهبي لتثبيت المزاج في ثنائي القطب.',
        'tags': 'ثنائي القطب,علاج,ليثيوم',
        'difficulty': 'medium',
        'category': 'اضطرابات المزاج',
    },
    {
        'question': 'ما هو العرض المميز لاضطراب الهلع؟',
        'choices': ['الخوف من الأماكن المفتوحة', 'نوبات هلع متكررة غير متوقعة', 'الوساوس القهرية', 'القلق المستمر حول أمور متعددة'],
        'correct_answer': 2,
        'explanation': 'اضطراب الهلع يتميز بنوبات هلع متكررة غير متوقعة يتبعها قلق مستمر من تكرار النوبة.',
        'tags': 'هلع,قلق',
        'difficulty': 'easy',
        'category': 'اضطرابات القلق',
    },
    {
        'question': 'أي من الأدوية التالية يستخدم لعلاج الرهاب الاجتماعي (الأداء) بشكل مؤقت؟',
        'choices': ['سيرترالين', 'بروبرانولول', 'كلونازيبام', 'بوسبيرون'],
        'correct_answer': 2,
        'explanation': 'حاصرات بيتا (مثل بروبرانولول) تستخدم قبل التعرض للموقف المخيف لتقليل الأعراض الجسدية للقلق.',
        'tags': 'رهاب اجتماعي,علاج,بروبرانولول',
        'difficulty': 'medium',
        'category': 'اضطرابات القلق',
    },
    {
        'question': 'أي اضطراب شخصية يتميز بـ "التفكير السحري" والسلوك الغريب دون ذهان واضح؟',
        'choices': ['شخصية شبه فصامية', 'شخصية فصامية', 'شخصية بجنون العظمة', 'شخصية حدية'],
        'correct_answer': 1,
        'explanation': 'اضطراب الشخصية شبه الفصامي (Schizotypal) يتميز بمعتقدات غريبة وتفكير سحري.',
        'tags': 'شخصية,شبه فصامي',
        'difficulty': 'hard',
        'category': 'اضطرابات الشخصية',
    },
    {
        'question': 'ما هي المتلازمة التي تظهر بعد 48-96 ساعة من التوقف عن شرب الكحول وتتميز بالهياج والهلوسة وعدم الاستقرار التلقائي؟',
        'choices': ['هذيان ارتعاشي (DTs)', 'نوبة صرعية', 'هلوسة كحولية', 'اعتلال دماغي فيرنيكي'],
        'correct_answer': 1,
        'explanation': 'الهذيان الارتعاشي (Delirium Tremens) هو أخطر أشكال انسحاب الكحول، نسبة الوفاة 5%.',
        'tags': 'إدمان,كحول,انسحاب',
        'difficulty': 'medium',
        'category': 'الإدمان',
    },
    {
        'question': 'ما هو العرض المميز لداء الزهايمر في مراحله المبكرة؟',
        'choices': ['ضعف الوظائف التنفيذية', 'صعوبات بصرية مكانية', 'فقدان الذاكرة الحديثة', 'تغيرات في الشخصية'],
        'correct_answer': 3,
        'explanation': 'فقدان الذاكرة الحديثة (خاصة نسيان الأحداث القريبة) هو أول أعراض الزهايمر شيوعاً.',
        'tags': 'خرف,زهايمر,ذاكرة',
        'difficulty': 'easy',
        'category': 'الاضطرابات العصبية المعرفية',
    },
    {
        'question': 'ما هو اضطراب الطفولة الذي يتميز بصعوبة في الانتباه والاندفاعية وفرط الحركة؟',
        'choices': ['اضطراب التحدي المعارض', 'اضطراب السلوك', 'اضطراب نقص الانتباه مع فرط النشاط (ADHD)', 'اضطراب طيف التوحد'],
        'correct_answer': 3,
        'explanation': 'ADHD يتميز بأعراض عدم الانتباه، فرط النشاط، والاندفاعية في سياقين على الأقل.',
        'tags': 'أطفال,ADHD,انتباه',
        'difficulty': 'easy',
        'category': 'اضطرابات الأطفال والمراهقين',
    },

    # ── Case chain 1: major depressive episode ──────────────────────
    {
        'question': 'ما هو التشخيص الأكثر ترجيحاً لهذا المريض؟',
        'choices': [
            'اضطراب اكتئابي كبير',
            'اضطراب ثنائي القطب النوع الأول',
            'اضطراب فصامي عاطفي',
            'اضطراب تكيفي مع مزاج اكتئابي',
        ],
        'correct_answer': 1,
        'explanation': 'وجود 5 من الأعراض التسعة لمعايير DSM-5 لمدة تزيد عن أسبوعين — بما فيها المزاج المكتئب وفقدان الاهتمام — مع غياب نوبة هوس وأعراض ذهانية، يجعل الاضطراب الاكتئابي الكبير التشخيص الأرجح.',
        'tags': 'اكتئاب,DSM-5,تشخيص',
        'difficulty': 'easy',
        'category': 'اضطرابات المزاج',
        'case_key': 'case-depression-01',
        'case_stem': (
            'رجل يبلغ من العمر 42 عاماً، مهندس، يراجع العيادة النفسية برفقة زوجته. '
            'تشتكي الزوجة من أنه لم يعد يشاركها الأنشطة التي كان يستمتع بها سابقاً، '
            'وأنه ينام متأخراً ويستيقظ مبكراً دون سبب واضح. يقول المريض إنه يشعر '
            'بالحزن معظم اليوم، ويفكر في الموت عدة مرات أسبوعياً دون خطة محددة. '
            'وزنه انخفض 5 كغ خلال شهرين دون حمية. لا يوجد تاريخ سابق لأعراض '
            'ذهانية أو هلوسات، ولا تاريخ عائلي للاضطراب ثنائي القطب. '
            'الفحص البدني والمخبري طبيعي.'
        ),
    },
    {
        'question': 'ما هي المدة الدنيا المطلوبة لتشخيص نوبة اكتئاب كبرى حسب DSM-5؟',
        'choices': ['أسبوع واحد', 'أسبوعان', 'شهر واحد', 'ستة أشهر'],
        'correct_answer': 2,
        'explanation': 'يشترط DSM-5 استمرار الأعراض لمدة أسبوعين على الأقل مع وجود 5 أعراض أو أكثر من قائمة المعايير، بما فيها إما مزاج مكتئب أو فقدان الاهتمام أو المتعة.',
        'tags': 'اكتئاب,DSM-5',
        'difficulty': 'easy',
        'category': 'اضطرابات المزاج',
        'case_key': 'case-depression-01',
        'case_stem': (
            'رجل يبلغ من العمر 42 عاماً، مهندس، يراجع العيادة النفسية برفقة زوجته. '
            'تشتكي الزوجة من أنه لم يعد يشاركها الأنشطة التي كان يستمتع بها سابقاً، '
            'وأنه ينام متأخراً ويستيقظ مبكراً دون سبب واضح. يقول المريض إنه يشعر '
            'بالحزن معظم اليوم، ويفكر في الموت عدة مرات أسبوعياً دون خطة محددة. '
            'وزنه انخفض 5 كغ خلال شهرين دون حمية. لا يوجد تاريخ سابق لأعراض '
            'ذهانية أو هلوسات، ولا تاريخ عائلي للاضطراب ثنائي القطب. '
            'الفحص البدني والمخبري طبيعي.'
        ),
    },
    {
        'question': 'أي من الأدوية التالية هو الخط الأول لعلاج هذا المريض؟',
        'choices': [
            'مثبطات استرداد السيروتونين الانتقائية (SSRI)',
            'الليثيوم',
            'الكلوزابين',
            'اللورازيبام',
        ],
        'correct_answer': 1,
        'explanation': 'تُعتبر مثبطات استرداد السيروتونين الانتقائية (مثل الفلوكستين، السيرترالين، الإسيتالوبرام) الخط الأول لنوبة الاكتئاب الكبرى، نظراً لفعاليتها وأمانها النسبي ومعدل الآثار الجانبية المنخفض مقارنة بمضادات الاكتئاب ثلاثية الحلقات.',
        'tags': 'اكتئاب,علاج,SSRI',
        'difficulty': 'medium',
        'category': 'اضطرابات المزاج',
        'case_key': 'case-depression-01',
        'case_stem': (
            'رجل يبلغ من العمر 42 عاماً، مهندس، يراجع العيادة النفسية برفقة زوجته. '
            'تشتكي الزوجة من أنه لم يعد يشاركها الأنشطة التي كان يستمتع بها سابقاً، '
            'وأنه ينام متأخراً ويستيقظ مبكراً دون سبب واضح. يقول المريض إنه يشعر '
            'بالحزن معظم اليوم، ويفكر في الموت عدة مرات أسبوعياً دون خطة محددة. '
            'وزنه انخفض 5 كغ خلال شهرين دون حمية. لا يوجد تاريخ سابق لأعراض '
            'ذهانية أو هلوسات، ولا تاريخ عائلي للاضطراب ثنائي القطب. '
            'الفحص البدني والمخبري طبيعي.'
        ),
    },

    # ── Case chain 2: first-episode psychosis ───────────────────────
    {
        'question': 'ما هي الفئة الأكثر بروزاً من الأعراض التي يعاني منها المريض؟',
        'choices': [
            'الأعراض الإيجابية',
            'الأعراض السلبية',
            'الأعراض المعرفية',
            'الأعراض العاطفية',
        ],
        'correct_answer': 1,
        'explanation': 'الضلالات (اعتقاد بوجود أجهزة تنصت) والهلاوس السمعية (سماع أصوات تعلّق على سلوكه) هما العَرَضان الأكثر بروزاً لدى هذا المريض، وكلاهما يُصنّف ضمن الأعراض الإيجابية للفصام.',
        'tags': 'فصام,أعراض إيجابية,ذهان',
        'difficulty': 'easy',
        'category': 'الاضطرابات الذهانية',
        'case_key': 'case-psychosis-01',
        'case_stem': (
            'شاب عمره 22 عاماً، طالب هندسة، أُحضر إلى قسم الطوارئ من قبل عائلته. '
            'يصر المريض على أن جيرانه يضعون أجهزة تنصت في جدران شقته، وأنه يسمع '
            'أصواتاً تعلّق على سلوكه خلال الأشهر الستة الماضية. لاحظت العائلة '
            'انسحابه الاجتماعي وتدهور أدائه الدراسي بشكل كبير. لا يوجد تاريخ '
            'لتعاطي المخدرات (فحص البول سلبي)، ولا يوجد تاريخ طبي مهم. '
            'الفحص العصبي طبيعي.'
        ),
    },
    {
        'question': 'كم المدة المطلوبة لتشخيص الفصام حسب DSM-5؟',
        'choices': ['أسبوع واحد', 'شهر واحد', 'ستة أشهر', 'سنة كاملة'],
        'correct_answer': 3,
        'explanation': 'يشترط DSM-5 استمرار الاضطراب لمدة 6 أشهر على الأقل، تتضمن شهراً واحداً على الأقل من الأعراض النشطة (ضلالات، هلاوس، كلام مفكك، سلوك فوضوي، أعراض سلبية).',
        'tags': 'فصام,DSM-5,تشخيص',
        'difficulty': 'easy',
        'category': 'الاضطرابات الذهانية',
        'case_key': 'case-psychosis-01',
        'case_stem': (
            'شاب عمره 22 عاماً، طالب هندسة، أُحضر إلى قسم الطوارئ من قبل عائلته. '
            'يصر المريض على أن جيرانه يضعون أجهزة تنصت في جدران شقته، وأنه يسمع '
            'أصواتاً تعلّق على سلوكه خلال الأشهر الستة الماضية. لاحظت العائلة '
            'انسحابه الاجتماعي وتدهور أدائه الدراسي بشكل كبير. لا يوجد تاريخ '
            'لتعاطي المخدرات (فحص البول سلبي)، ولا يوجد تاريخ طبي مهم. '
            'الفحص العصبي طبيعي.'
        ),
    },
    {
        'question': 'ما هو الخط الأول لعلاج هذا المريض؟',
        'choices': [
            'مضادات الذهان',
            'مضادات الاكتئاب',
            'مثبتات المزاج',
            'البنزوديازيبينات',
        ],
        'correct_answer': 1,
        'explanation': 'تُعتبر مضادات الذهان (الجيل الأول أو الثاني) الخط الأول في علاج النوبة الذهانية الأولى، مع مراقبة استجابة المريض والآثار الجانبية (خاصة الأعراض خارج الهرمية).',
        'tags': 'فصام,علاج,مضادات الذهان',
        'difficulty': 'medium',
        'category': 'الاضطرابات الذهانية',
        'case_key': 'case-psychosis-01',
        'case_stem': (
            'شاب عمره 22 عاماً، طالب هندسة، أُحضر إلى قسم الطوارئ من قبل عائلته. '
            'يصر المريض على أن جيرانه يضعون أجهزة تنصت في جدران شقته، وأنه يسمع '
            'أصواتاً تعلّق على سلوكه خلال الأشهر الستة الماضية. لاحظت العائلة '
            'انسحابه الاجتماعي وتدهور أدائه الدراسي بشكل كبير. لا يوجد تاريخ '
            'لتعاطي المخدرات (فحص البول سلبياً)، ولا يوجد تاريخ طبي مهم. '
            'الفحص العصبي طبيعي.'
        ),
    },
]

# Derived from the model so the two cannot drift. The previous
# literal `{'easy', 'medium', 'hard'}` was duplicated in
# `apps/questions/services/importing/validators.py`; both now derive
# from `Question.DIFFICULTY_CHOICES`.
VALID_DIFFICULTIES = frozenset(c for c, _ in Question.DIFFICULTY_CHOICES)


def _resolve_seed_owner():
    """
    Return the User that seeded questions should be owned by.

    Preference order:
      1. Any admin — the standard case, since seeding runs from an
         admin-driven command.
      2. Any active non-stub member — used on a system where the
         admin account has not been created yet but a moderator has.

    Returns None if there is no usable owner; the caller then skips
    seeding rather than crashing on the CheckConstraint.
    """
    admin = User.objects.filter(role='admin', is_active=True).order_by('id').first()
    if admin is not None:
        return admin
    return User.objects.filter(
        is_active=True, is_stub=False,
    ).order_by('id').first()


class Command(BaseCommand):
    help = 'Seed sample psychiatry questions (including clinical case chains)'

    def handle(self, *args, **options):
        max_choices = getattr(settings, 'MAX_CHOICES', 8)
        count = 0

        # Resolve the acting owner ONCE. The CheckConstraint on
        # Question requires every non-draft row to have an owner; the
        # seed data has no natural owner (it is not authored by any
        # human), so we attribute ownership to the running system's
        # primary admin. `authored_by` stays NULL for seed content —
        # that is the honest state (see the docstring on
        # Question.authored_by).
        seed_owner = _resolve_seed_owner()
        if seed_owner is None:
            self.stderr.write(
                'Cannot seed sample questions: no active admin or '
                'non-stub member exists. Create an admin first '
                '(python manage.py createsuperuser) and re-run.'
            )
            return

        # Cache cases by key so a case with 3 questions only triggers
        # one get_or_create for the case itself.
        case_cache = {}

        for item in SAMPLE_QUESTIONS:
            q_text = item['question'].strip()

            # ── Resolve/create the case before the question, so a
            #    duplicate question still gets its case linkage
            #    repaired if it drifted. The case is attributed to
            #    the seed owner, matching the question's owner.
            case = None
            if item.get('case_key'):
                case = case_cache.get(item['case_key'])
                if case is None:
                    case, created = ClinicalCase.objects.get_or_create(
                        key=item['case_key'],
                        defaults={
                            'stem': item.get('case_stem') or None,
                            'authored_by': seed_owner,
                        },
                    )
                    if created is False and item.get('case_stem') and not case.stem:
                        case.stem = item['case_stem']
                        case.save(update_fields=['stem', 'updated_at'])
                    case_cache[item['case_key']] = case

            existing = Question.objects.filter(question=q_text).first()
            if existing is not None:
                # Repair the case linkage and the missing owner if
                # either drifted. Do not touch content of an existing
                # question.
                dirty = []
                if case is not None and existing.case_id != case.id:
                    existing.case = case
                    dirty.append('case')
                if existing.owned_by_id is None and not existing.is_draft:
                    existing.owned_by = seed_owner
                    dirty.append('owned_by')
                if dirty:
                    existing.save(update_fields=dirty)
                continue

            category = None
            if item.get('category'):
                category = Category.objects.filter(name=item['category']).first()

            choices = [c.strip() for c in item['choices'] if c and c.strip()]

            if len(choices) > max_choices:
                self.stderr.write(
                    f'Skipped ({len(choices)} choices exceeds '
                    f'MAX_CHOICES={max_choices}): {q_text[:60]}'
                )
                continue
            if len(choices) < 2:
                self.stderr.write(
                    f'Skipped (fewer than 2 valid choices): {q_text[:60]}'
                )
                continue

            try:
                correct = int(item['correct_answer'])
            except (TypeError, ValueError):
                self.stderr.write(
                    f'Skipped (correct_answer is not numeric): {q_text[:60]}'
                )
                continue
            if correct < 1 or correct > len(choices):
                self.stderr.write(
                    f'Skipped (correct_answer out of range): {q_text[:60]}'
                )
                continue

            difficulty = str(item.get('difficulty', 'medium')).lower().strip()
            if difficulty not in VALID_DIFFICULTIES:
                difficulty = 'medium'

            tags = [t.strip() for t in item['tags'].split(',') if t.strip()]

            # Determine case_order from the current maximum within
            # the case, so seed data ends up in the order it appears
            # in SAMPLE_QUESTIONS.
            case_order = None
            if case is not None:
                from django.db.models import Max
                current_max = (
                    case.questions.aggregate(m=Max('case_order'))['m'] or 0
                )
                case_order = current_max + 1

            question = Question.objects.create(
                question=q_text,
                choices=choices,
                correct_answer=correct,
                explanation=item['explanation'],
                source='1st Aid',
                difficulty=difficulty,
                category=category,
                # Seed content has no human author. `authored_by`
                # stays NULL; `owned_by` is the running admin so the
                # CheckConstraint is satisfied and someone can edit
                # the row. See the docstring on Question.authored_by
                # for why this is the honest state.
                authored_by=None,
                owned_by=seed_owner,
                verified=True,
                verified_by='system',
                verified_at=timezone.now(),
                verification_notes='نموذج تجريبي',
                case=case,
                case_order=case_order,
            )

            for tag_name in tags:
                tag, _ = Tag.objects.get_or_create(name=clean_tag_name(tag_name))
                question.tags.add(tag)

            count += 1

        self.stdout.write(self.style.SUCCESS(f'Added {count} sample questions.'))
        if count == 0:
            self.stdout.write('No new questions added (may already exist).')