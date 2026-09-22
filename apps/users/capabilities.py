# backend/apps/users/capabilities.py
"""
Central registry of capability strings and their default role assignments.

A capability is a dot-namespaced string like 'questions.verify' that
identifies a single grantable action. Capabilities are never stored on
the User model directly; the model stores per-user overrides that are
merged with the role defaults at resolution time.

The role defaults here are SEED values. The runtime source of truth is
the RoleCapabilities database row for each role. On first bootstrap,
`seed_capabilities` copies these constants into the DB. After that, the
panel can edit the DB freely; the constants remain as a recovery seed
and as the source-of-truth for which capabilities exist.

Adding a new capability
-----------------------
1. Add the string to CAPABILITIES.
2. Optionally add it to DEFAULT_ROLE_CAPABILITIES for each role that
   should hold it by default.
3. Run `python manage.py seed --only capabilities` to push the new
   default into existing RoleCapabilities rows.

No database migration is required — this is the entire point of the
JSON capability approach over Django's ContentType-bound Permission.

CONSOLIDATION HISTORY
---------------------
Two rounds of dead-capability cleanup have been applied.

Round 1 (earlier release) removed 'tests.use_srs' and
'tests.study_now'. Both were declared and granted by default, but no
view consulted them — StudyNowView and the SRS branch of
StartSessionView both gate on IsAuthenticated only.

Round 2 (this release) does two things:

  1. ENFORCES 'tests.start' and 'tests.view_own_history'. Both were
     declared and granted by default, but neither was consulted:
     StartSessionView and TestHistoryView were IsAuthenticated-only.
     They are now real gates (see apps/exams/views/session_views.py
     and apps/exams/views/history_views.py). The reason to enforce
     rather than remove them is that they name the two most
     meaningful "should this role be allowed to use tests at all"
     switches a deployment might want to toggle per-role, and there
     is no other place to express that intent.

  2. REMOVES 'analytics.view_own', 'groups.use', 'leaderboard.view',
     and 'planner.use'. These four were declared and granted by
     default but never consulted by any view. Unlike 'tests.start' /
     'tests.view_own_history', there is no meaningful semantic for
     them: personal analytics, groups, leaderboards, and the planner
     are all features whose access is already naturally scoped by
     IsAuthenticated, and the four strings existed only to make the
     panel look more granular than the code actually was. Toggling
     one off in the panel silently did nothing. They are gone so
     that a future deployment cannot be misled by their presence.
"""

# ═══════════════════════════════════════════════════════════════════════
# The canonical set. Every capability that exists anywhere in the
# system must appear here. Anything not in this set is dropped at
# resolution time even if it lingers in a DB row.
# ═══════════════════════════════════════════════════════════════════════
CAPABILITIES = frozenset({
    # ── Questions ─────────────────────────────────────────────────────
    'questions.create',
    'questions.edit_own',
    'questions.edit_any',
    'questions.delete_own',
    'questions.delete_any',
    'questions.duplicate',
    'questions.upload_image',
    'questions.edit_case_stem_own',
    'questions.edit_case_stem_any',
    'questions.verify',
    'questions.bulk_verify',
    'questions.bulk_tag',
    'questions.manage_tags',
    'categories.manage',

    # ── Tests / study ─────────────────────────────────────────────────
    #
    # 'tests.start'           — the gate on StartSessionView (plain
    #                           exam/study sessions). Master exams
    #                           have their own authorization via
    #                           IsMasterExamParticipant +
    #                           MasterExamService.user_can_access and
    #                           are NOT gated by this capability.
    # 'tests.view_own_history'— the gate on TestHistoryView when the
    #                           caller is viewing their own history
    #                           without 'tests.view_all_history'.
    # 'tests.view_all_history'— the cross-user read.
    # 'tests.use_blueprint'   — blueprint-based exam assembly.
    'tests.start',
    'tests.use_blueprint',
    'tests.view_own_history',
    'tests.view_all_history',

    # ── Master exams ──────────────────────────────────────────────────
    'master_exams.create',
    'master_exams.manage_own',
    'master_exams.manage_any',
    'master_exams.view_results_own',
    'master_exams.view_results_any',
    'master_exams.publish_to_bank',
    'master_exams.drafts_library',
    'master_exams.preview',

    # ── Social / planning ─────────────────────────────────────────────
    #
    # 'groups.admin' is the only capability in this namespace that is
    # actually consulted (by the admin group views). The former
    # 'groups.use' and 'leaderboard.view' strings existed here but no
    # view read them — group membership and leaderboard reads are
    # naturally scoped by IsAuthenticated and object-level membership.
    # The former 'planner.use' string is likewise gone — every
    # planner view is IsAuthenticated-only, and there is no
    # meaningful per-role distinction for "can this user open the
    # planner at all."
    'groups.admin',

    # ── Admin ─────────────────────────────────────────────────────────
    'admin.users',
    'admin.settings',
    'admin.database',
    'admin.flags',
    'admin.active_users',
    'admin.verification_stats',
    'admin.seed',
    'admin.permissions',
    'admin.blueprints',

    # ── Analytics ─────────────────────────────────────────────────────
    #
    # 'analytics.view_all' is the only analytics capability that is
    # consulted — it gates the admin sections of SummaryView.
    # 'analytics.view_own' was declared but never read; personal
    # analytics for the caller are gated by IsAuthenticated.
    'analytics.view_all',

    # ── System ────────────────────────────────────────────────────────
    #
    # 'system.bypass_expiry' exempts the holder from account expiry.
    # It is consulted by User.is_expired and User.renew_if_eligible.
    #
    # The admin role holds it automatically via the resolve_for_user
    # short-circuit, so the admin row does not need it explicitly.
    # It is NOT granted to 'moderator' or 'member' by default — the
    # whole point is that a deployment opts into granting it, either
    # by editing the role's capability set in the panel or by adding
    # a per-user override.
    'system.bypass_expiry',
})


# ═══════════════════════════════════════════════════════════════════════
# Human-readable groupings for the panel UI. Order within a group is
# the display order; order of the groups is the display order of the
# sections. Every capability in CAPABILITIES must appear exactly once
# across all groups (enforced by the assertion below).
# ═══════════════════════════════════════════════════════════════════════
CAPABILITY_GROUPS = (
    ('Questions', (
        'questions.create',
        'questions.edit_own',
        'questions.edit_any',
        'questions.delete_own',
        'questions.delete_any',
        'questions.duplicate',
        'questions.upload_image',
        'questions.edit_case_stem_own',
        'questions.edit_case_stem_any',
    )),
    ('Verification & taxonomy', (
        'questions.verify',
        'questions.bulk_verify',
        'questions.bulk_tag',
        'questions.manage_tags',
        'categories.manage',
    )),
    ('Tests & study', (
        'tests.start',
        'tests.use_blueprint',
        'tests.view_own_history',
        'tests.view_all_history',
    )),
    ('Master exams', (
        'master_exams.create',
        'master_exams.manage_own',
        'master_exams.manage_any',
        'master_exams.view_results_own',
        'master_exams.view_results_any',
        'master_exams.publish_to_bank',
        'master_exams.drafts_library',
        'master_exams.preview',
    )),
    ('Social & planning', (
        'groups.admin',
    )),
    ('Administration', (
        'admin.users',
        'admin.settings',
        'admin.database',
        'admin.flags',
        'admin.active_users',
        'admin.verification_stats',
        'admin.seed',
        'admin.permissions',
        'admin.blueprints',
    )),
    ('Analytics', (
        'analytics.view_all',
    )),
    ('System', (
        'system.bypass_expiry',
    )),
)


# The two constants must agree exactly. A load-time assertion is the
# cheapest way to catch a typo in either one — this fires at import,
# not at first request.
_grouped = frozenset(c for _, group in CAPABILITY_GROUPS for c in group)
assert _grouped == CAPABILITIES, (
    f'CAPABILITY_GROUPS and CAPABILITIES disagree. '
    f'Only in groups: {sorted(_grouped - CAPABILITIES)}. '
    f'Only in CAPABILITIES: {sorted(CAPABILITIES - _grouped)}.'
)


# ═══════════════════════════════════════════════════════════════════════
# Default role assignments.
#
# These are the seed values written into RoleCapabilities the first
# time `seed_capabilities` runs. After seeding, the database is the
# source of truth. The constants are consulted only when a role has
# no DB row yet (e.g. a fresh deployment) or when running with
# `--reset`.
#
# Three roles in this deployment:
#   • member     — the default role. Can author their own questions.
#   • moderator  — member plus content moderation and community mgmt.
#   • admin      — every capability. Short-circuited in resolve_for_user.
# ═══════════════════════════════════════════════════════════════════════

_MEMBER = frozenset({
    # Questions — own only
    'questions.create',
    'questions.edit_own',
    'questions.delete_own',
    'questions.duplicate',
    'questions.upload_image',
    'questions.edit_case_stem_own',
    # Tests
    'tests.start',
    'tests.view_own_history',
})

_MODERATOR = _MEMBER | frozenset({
    # Question moderation — edits/deletes anything, not just own
    'questions.edit_any',
    'questions.delete_any',
    'questions.edit_case_stem_any',
    'questions.verify',
    'questions.bulk_verify',
    'questions.bulk_tag',
    'questions.manage_tags',
    'categories.manage',
    # Test extras unlocked by moderator status
    'tests.use_blueprint',
    'tests.view_all_history',
    # Master exam authoring
    'master_exams.create',
    'master_exams.manage_own',
    'master_exams.view_results_own',
    'master_exams.drafts_library',
    'master_exams.preview',
    'master_exams.publish_to_bank',
    # Community management
    'groups.admin',
    # Admin surface — only the flags queue and blueprint editor
    'admin.flags',
    'admin.blueprints',
    # Full analytics dashboard
    'analytics.view_all',
})


DEFAULT_ROLE_CAPABILITIES = {
    'member':    _MEMBER,
    'moderator': _MODERATOR,
    # Admin is always the full set and cannot be edited from the panel.
    'admin':     CAPABILITIES,
}


# Every role in User.ROLE_CHOICES must have an entry here. Catches the
# class of bug where someone adds a role to the model but forgets to
# declare its defaults.
_ROLES_WITH_DEFAULTS = frozenset(DEFAULT_ROLE_CAPABILITIES.keys())
_REQUIRED_ROLES = frozenset({'member', 'moderator', 'admin'})
assert _ROLES_WITH_DEFAULTS >= _REQUIRED_ROLES, (
    f'DEFAULT_ROLE_CAPABILITIES is missing defaults for: '
    f'{sorted(_REQUIRED_ROLES - _ROLES_WITH_DEFAULTS)}'
)