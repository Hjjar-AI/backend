# backend/apps/users/serializers.py

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers

from apps.users.capabilities import CAPABILITIES

from .models import User, ActiveSession, LoginAttempt

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """
    Read serializer for a User.

    Two fields are informational only and never writable through any
    endpoint that consumes this serializer:

      • `uuid`     — the portable cross-system identifier used by the
                     state envelope. Local integer `id` remains the
                     primary key; `uuid` is a parallel identifier for
                     cross-installation references.
      • `is_stub`  — True for import-created author records that
                     represent an external contributor with no local
                     account. Stubs are filtered out of every user
                     list, cannot log in, and accumulate a
                     trust_score of 0.

    `author_rank` and `author_rank_label_ar` are derived properties
    on the model; they follow `questions_count` / `trust_score`,
    which are recomputed from `authored_by`.
    """
    author_rank = serializers.CharField(read_only=True)
    author_rank_label_ar = serializers.CharField(read_only=True)

    # Ship the resolved capability set as a flat, sorted list. The
    # frontend never needs to know about role defaults or per-user
    # overrides; it just checks `capabilities.includes('x.y')`.
    capabilities = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'uuid', 'username', 'full_name', 'email', 'role',
            'is_active', 'is_stub',
            'created_at', 'last_login', 'questions_count',
            'trust_score', 'must_change_password', 'expires_at', 'auto_renew_days',
            'current_streak', 'longest_streak', 'last_study_date',
            'author_rank', 'author_rank_label_ar',
            'capabilities',
        ]
        read_only_fields = [
            'id', 'uuid', 'is_stub',
            'created_at', 'last_login', 'questions_count', 'trust_score',
            'current_streak', 'longest_streak', 'last_study_date',
            'author_rank', 'author_rank_label_ar',
            'capabilities',
        ]

    def get_capabilities(self, obj):
        return sorted(obj.resolved_capabilities())


class UserCreateSerializer(serializers.ModelSerializer):
    """
    Write serializer for creating a regular user. Stubs are NOT
    creatable through this serializer — they are created exclusively
    by ImportService when an envelope names an author with no local
    account. Every account created here is a real, loginable user.

    ADMIN PASSWORD
    --------------
    `admin_password` is declared here so the field is part of the
    serializer's documented contract and so a malformed payload that
    happens to include it is rejected at the same layer as every
    other field.

    It is NOT read from `validated_data` by the view. The view
    (`AdminUserListView.post`) calls `_require_admin_password(request)`
    BEFORE running serializer validation, so a wrong password
    short-circuits with a 403 before a badly-shaped payload can
    trigger a 400. The declared field exists so that a reader of this
    serializer sees the full wire contract — including the credential
    the endpoint actually requires — without having to open the view.

    A future refactor that moves the password check after validation
    should also switch the view to read `serializer.validated_data
    ['admin_password']` in the same change; the field is declared and
    ready for it.
    """
    password = serializers.CharField(write_only=True, required=True)
    expiry_days = serializers.IntegerField(
        write_only=True, required=False, default=60, min_value=0,
    )
    auto_renew_days = serializers.IntegerField(
        write_only=True, required=False, default=0, min_value=0,
    )
    is_active = serializers.BooleanField(required=False, default=True)

    capabilities = serializers.DictField(
        child=serializers.BooleanField(),
        required=False,
        default=dict,
    )

    admin_password = serializers.CharField(
        write_only=True,
        required=True,
        max_length=128,
        help_text=(
            'The acting admin\'s own password, re-checked server-side '
            'to re-authenticate the caller. See '
            'AdminUserListView.post and _require_admin_password.'
        ),
    )

    class Meta:
        model = User
        fields = [
            'username', 'password', 'full_name', 'role', 'is_active',
            'expiry_days', 'auto_renew_days',
            'capabilities',
            'admin_password',
        ]

    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError('اسم المستخدم موجود مسبقاً')
        return value

    def validate_capabilities(self, value):
        if not value:
            return {}
        unknown = set(value) - CAPABILITIES
        if unknown:
            raise serializers.ValidationError(
                f'Unknown capabilities: {sorted(unknown)}'
            )
        return {k: bool(v) for k, v in value.items()}

    def validate(self, attrs):
        password = attrs.get('password')
        if password:
            temp_user = User(
                username=attrs.get('username', ''),
                full_name=attrs.get('full_name') or '',
            )
            try:
                validate_password(password, user=temp_user)
            except DjangoValidationError as e:
                raise serializers.ValidationError({'password': list(e.messages)})
        return attrs

    def create(self, validated_data):
        expiry_days = validated_data.pop('expiry_days', 0)
        auto_renew_days = validated_data.pop('auto_renew_days', 0)
        capabilities = validated_data.pop('capabilities', {}) or {}
        password = validated_data.pop('password')
        # `admin_password` has already been checked by the view; drop
        # it so it never reaches `User(**validated_data)`.
        validated_data.pop('admin_password', None)

        user = User(**validated_data)
        user.capabilities = capabilities
        # Explicitly clear the stub flag — a user created through this
        # serializer is always a real account, never a stub.
        user.is_stub = False

        if validated_data.get('role', 'member') != 'admin' and expiry_days > 0:
            user.expires_at = timezone.now() + timedelta(days=expiry_days)
        user.auto_renew_days = auto_renew_days
        user.set_password(password)
        user.save()
        return user


class UserUpdateSerializer(serializers.ModelSerializer):
    """
    Write serializer for updating a user.

    `is_stub` is deliberately NOT in Meta.fields. Promoting a stub to
    a real account is a distinct administrative action (change the
    flag, set a password) — not something this serializer performs as
    a side effect of a role change.
    """
    new_password = serializers.CharField(
        write_only=True, required=False, allow_blank=True, max_length=128,
    )
    admin_password = serializers.CharField(
        write_only=True, required=True, max_length=128,
    )

    expiry_days = serializers.IntegerField(
        write_only=True, required=False, min_value=0,
    )
    auto_renew_days = serializers.IntegerField(
        write_only=True, required=False, min_value=0,
    )

    capabilities = serializers.DictField(
        child=serializers.BooleanField(allow_null=True),
        required=False,
    )

    class Meta:
        model = User
        fields = [
            'full_name', 'role', 'is_active', 'new_password', 'admin_password',
            'expiry_days', 'auto_renew_days',
            'capabilities',
        ]

    def validate_new_password(self, value):
        if not value:
            return value
        try:
            validate_password(value, user=self.instance)
        except DjangoValidationError as e:
            raise serializers.ValidationError(list(e.messages))
        return value

    def validate_capabilities(self, value):
        if value is None:
            return value
        unknown = set(value) - CAPABILITIES
        if unknown:
            raise serializers.ValidationError(
                f'Unknown capabilities: {sorted(unknown)}'
            )
        cleaned = {}
        for k, v in value.items():
            if v is None:
                continue
            cleaned[k] = bool(v)
        return cleaned

    def update(self, instance, validated_data):
        validated_data.pop('admin_password', None)

        new_password = validated_data.pop('new_password', None)
        expiry_days = validated_data.pop('expiry_days', None)
        auto_renew_days = validated_data.pop('auto_renew_days', None)
        capabilities = validated_data.pop('capabilities', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if capabilities is not None:
            instance.capabilities = capabilities

        if expiry_days is not None:
            if expiry_days > 0:
                instance.expires_at = timezone.now() + timedelta(days=expiry_days)
            else:
                instance.expires_at = None
        if auto_renew_days is not None:
            instance.auto_renew_days = auto_renew_days
        if new_password:
            instance.set_password(new_password)
            instance.must_change_password = True

        instance.save()

        if hasattr(instance, '_resolved_caps_cache'):
            delattr(instance, '_resolved_caps_cache')

        return instance


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=80)
    password = serializers.CharField(max_length=128, write_only=True)


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(max_length=128)
    new_password = serializers.CharField(
        max_length=128, validators=[validate_password],
    )


class AdminResetPasswordSerializer(serializers.Serializer):
    admin_password = serializers.CharField(max_length=128)
    new_password = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=128,
    )


class RoleCapabilitiesSerializer(serializers.Serializer):
    """Wire format for one role → capability-set entry."""
    role = serializers.CharField(max_length=20)
    capabilities = serializers.ListField(
        child=serializers.CharField(max_length=100),
        allow_empty=True,
    )

    def validate_role(self, value):
        from apps.users.capabilities import DEFAULT_ROLE_CAPABILITIES
        if value not in DEFAULT_ROLE_CAPABILITIES:
            raise serializers.ValidationError(f'Unknown role: {value}')
        return value

    def validate_capabilities(self, value):
        unknown = set(value) - CAPABILITIES
        if unknown:
            raise serializers.ValidationError(
                f'Unknown capabilities: {sorted(unknown)}'
            )
        return list(dict.fromkeys(value))