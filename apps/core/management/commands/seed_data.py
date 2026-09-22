from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from apps.questions.models import Category
from apps.core.models import Setting
import os
import secrets
import sys

User = get_user_model()


class Command(BaseCommand):
    help = 'Seed database with default admin, categories, settings'

    def handle(self, *args, **options):
        self.seed_admin()
        self.seed_categories()
        self.seed_settings()
        self.stdout.write(self.style.SUCCESS('Data seeded successfully'))

    def seed_admin(self):
        if User.objects.filter(username='admin').exists():
            self.stdout.write('Admin already exists')
            return
        admin_pass = os.getenv('ADMIN_PASSWORD', secrets.token_urlsafe(10))
        User.objects.create_superuser(
            username='admin',
            password=admin_pass,
            full_name='مدير النظام',
            role='admin',
            is_staff=True,
            is_superuser=True,
            must_change_password=True,
        )

        if 'ADMIN_PASSWORD' not in os.environ:
            sys.stderr.write('Username: admin\n')
            sys.stderr.write(f'Admin created with password: {admin_pass}\n')
            sys.stderr.write('(This is a one-time credential. Log in and change it immediately.)\n')
        else:
            self.stdout.write('Admin created using ADMIN_PASSWORD from environment')

    def seed_categories(self):
        categories = [
            # ═══ Symptoms & signs — foundational vocabulary ════════
            ("الأعراض والعلامات",
             "التوهمات، الإهلاسات، اضطرابات شكل التفكير، كاتاتونيا، العظمة، تبدد الشخصية",
             "#ffc107", "bi-journal-text"),

            # ═══ Clinical process ══════════════════════════════════
            ("التشخيص والصياغة",
             "المقابلة والفحص النفسي، أخذ التاريخ، فحص الحالة العقلية، التشخيص التفريقي، الصياغة (4Ps)",
             "#11998e", "bi-clipboard2-pulse"),

            # ═══ Core disorder groups ══════════════════════════════
            ("الاضطرابات الذهانية",
             "فصام، ذهاني وجيز، اضطراب توهمي",
             "#dc3545", "bi-moon-stars"),

            ("اضطرابات المزاج",
             "اكتئاب، ثنائي القطب، اضطراب دورية المزاج",
             "#f093fb", "bi-emoji-frown"),

            ("اضطرابات القلق",
             "قلق معمم، هلع، رهاب، قلق اجتماعي",
             "#ffc107", "bi-chat-dots"),

            # OCD was removed from the anxiety chapter by both ICD-11
            # and DSM-5 because its core psychopathology is
            # compulsivity, not anxiety. Includes OCD, body dysmorphic
            # disorder, hoarding disorder, trichotillomania, and
            # excoriation disorder.
            ("الوسواس القهري والاضطرابات المرتبطة",
             "وسواس قهري، اضطراب تشوه الجسد، الاكتناز، نتف الشعر، خدش الجلد",
             "#ffc107", "bi-arrow-repeat"),

            ("الاضطرابات المرتبطة بالصدمة والضغوط",
             "PTSD، الكرب الحاد للضغط، اضطراب التكيف، الفقد، الصدمة المعقدة",
             "#dc3545", "bi-shield-exclamation"),

            # Covers both the ICD-11 dimensional model (severity +
            # five trait domains) and the DSM-5 categorical model
            # (clusters A, B, C).
            ("اضطرابات الشخصية",
             "اضطراب الشخصية العام، الأنماط A (غرابة/انعزال)، B (دراماتيكية/اندفاعية)، C (قلق/خوف)",
             "#764ba2", "bi-person-bounding-box"),

            # Mirrors ICD-11 "Disorders due to substance use or
            # addictive behaviours" and DSM-5 "Substance-Related and
            # Addictive Disorders". Only gambling and gaming belong
            # here — kleptomania, pyromania, and compulsive sexual
            # behaviour disorder are impulse-control disorders, not
            # addictions.
            ("الإدمان والسلوكيات الإدمانية",
             "كحول، مخدرات، قمار، ألعاب",
             "#fd7e14", "bi-cup-straw"),

            # ICD-11 impulse control disorders: pyromania, kleptomania,
            # intermittent explosive disorder, compulsive sexual
            # behaviour disorder, and other specified/unspecified.
            ("اضطرابات التحكم بالاندفاع",
             "انفجار متقطع، هوس السرقة، الحرائق المرضية، السلوك الجنسي القهري",
             "#d63384", "bi-lightning-charge"),

            ("اضطرابات الأكل",
             "فقدان الشهية، نهام، نهم",
             "#fd7e14", "bi-basket"),

            ("اضطرابات النوم",
             "أرق، نوم قهري، انقطاع النفس",
             "#6610f2", "bi-alarm"),

            ("الاضطرابات التفارقية",
             "فقدان الذاكرة، تبدد الشخصية",
             "#e83e8c", "bi-person-video2"),

            ("الاضطرابات الجسدية والمفتعلة",
             "عرض جسدي، تحويل، مفتعل",
             "#28a745", "bi-bandaid"),

            # ═══ Neurological & cognitive ══════════════════════════
            ("الاضطرابات العصبية والمعرفية",
             "خرف، زهايمر، هذيان، صرع، اضطرابات حركية، إصابات الدماغ، السكتة الدماغية، التهابات الجهاز العصبي المركزي",
             "#20c997", "bi-brain"),

            # ═══ Life-stage & population psychiatry ════════════════
            ("طب الشيخوخة النفسي",
             "كبار السن، حزن، خرف",
             "#6c757d", "bi-hospital"),

            ("اضطرابات الأطفال والمراهقين",
             "توحد، ADHD، تأخر نمائي، اضطراب التحدي المعارض، الإعاقة الذهنية",
             "#17a2b8", "bi-people"),

            ("الطب النفسي التناسلي والصحة الجنسية",
             "الدورة الشهرية، الضيق السابق للحيض، الحمل والنفاس، الوظيفة الجنسية، الهوية الجنسية",
             "#d63384", "bi-gender-ambiguous"),

            # ═══ Settings of care ══════════════════════════════════
            ("الطب النفسي الاستشاري",
             "التقييم النفسي في المستشفى العام، الهذيان، السعة القانونية، الأعراض الطبية غير المفسرة، تقييم زراعة الأعضاء، إيذاء الذات في قسم الطوارئ",
             "#0dcaf0", "bi-hospital-fill"),

            ("الطوارئ النفسية",
             "هياج، انتحار، عدوانية",
             "#dc3545", "bi-exclamation-triangle"),

            # ═══ Treatment ═════════════════════════════════════════
            ("العلاجات النفسية",
             "CBT، تحليلي، عائلي، جماعي",
             "#20c997", "bi-journal-bookmark-fill"),

            ("العلاجات الجسدية الأخرى",
             "ECT، التحفيز المغناطيسي عبر الجمجمة، التحفيز العميق للدماغ، تحفيز العصب المبهم، العلاج بالضوء، جراحة الأمراض النفسية",
             "#20c997", "bi-lightning-charge-fill"),

            ("علم النفس الدوائي",
             "مضادات الذهان، مضادات الاكتئاب، مثبتات المزاج، الآثار الجانبية، NMS، متلازمة السيروتونين، خلل الحركة المتأخر، فرط البرولاكتين، إطالة QT",
             "#0d6efd", "bi-capsule"),

            # ═══ Law, culture, foundations ═════════════════════════
            ("الطب النفسي الشرعي والقانوني",
             "أهلية المحاكمة، الدفاع بالجنون، الموافقة، السعة القانونية، السرية، تشريعات الصحة النفسية، اللياقة للقيادة",
             "#6c757d", "bi-gavel"),

            ("الطب النفسي الثقافي",
             "مفاهيم الضيق الثقافية، الفروق العابرة للثقافات، الصياغة الثقافية في DSM-5، dhat، koro، hwa-byung",
             "#fd7e14", "bi-globe2"),

            ("تاريخ وفلسفة الطب النفسي",
             "تطور التصنيف، مناهضة الطب النفسي، الوصمة، نماذج المرض، الطب النفسي التطوري، قوة الإيحاء",
             "#795548", "bi-hourglass-split"),

            ("الوبائيات والإحصاء الحيوي",
             "معدلات الانتشار، منهجية GRADE، الطب المبني على البراهين، مشكلات التصنيف",
             "#6c757d", "bi-bar-chart"),

            ("الصحة النفسية للأطباء",
             "اكتئاب الأطباء، الانتحار حسب التخصص، عوائق طلب المساعدة، الواجب المهني تجاه الزملاء المتضررين",
             "#198754", "bi-person-badge"),
        ]

        created_count = 0
        for name, desc, color, icon in categories:
            _, was_created = Category.objects.get_or_create(
                name=name,
                defaults={'description': desc, 'color': color, 'icon': icon}
            )
            if was_created:
                created_count += 1

        if created_count:
            self.stdout.write(
                f'[categories] created {created_count} categor'
                f'{"y" if created_count == 1 else "ies"}'
            )

    def seed_settings(self):
        from apps.core.runtime_settings import DEFAULT_RUNTIME_SETTINGS
        defaults = DEFAULT_RUNTIME_SETTINGS
        for key, value in defaults.items():
            Setting.objects.get_or_create(key=key, defaults={'value': value})