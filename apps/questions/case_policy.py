"""Authorization policy shared by case metadata write paths."""


def can_edit_case(user, case):
    if user.has_capability('questions.edit_case_stem_any'):
        return True
    return (
        user.has_capability('questions.edit_case_stem_own')
        and case.questions.filter(authored_by=user).exists()
    )
