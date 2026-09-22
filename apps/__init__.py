# backend/apps/__init__.py
#
# This file makes `apps` a regular Python package rather than a
# PEP 420 namespace package. Without it, Django's migration loader
# does not find the migration modules of the apps nested underneath
# (`apps.users.migrations`, `apps.core.migrations`, ...), and
# `manage.py migrate` produces a plan that contains only Django's
# four built-in apps — leaving every custom table missing.
#
# Keep this file. Do not delete it even though it is empty.