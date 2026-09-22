"""Consistent admin password verification for privileged actions."""


def admin_password_matches(user, raw_password):
    return bool(raw_password) and user.check_password(raw_password)
