"""Second-factor storage: one TOTP device per user and its recovery codes."""

from __future__ import annotations

from django.conf import settings
from django.db import models


class MfaDevice(models.Model):
    """The user's TOTP second factor (mandatory for the web login).

    One row per user, created at the first password step. `nonce` binds the
    short-lived `mfa_token` to this row: it is rotated whenever a login
    completes (and after repeated wrong codes), which makes the token
    single-use. Secrets are stored encrypted
    (`apps.api_auth.mfa`), never in clear. Deleted with the user (CASCADE).
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mfa_device",
    )
    #: Encrypted base32 secret, set once enrolment is confirmed.
    secret = models.TextField(blank=True, default="")
    #: Encrypted base32 secret shown by `mfa/setup/`, awaiting a first code.
    pending_secret = models.TextField(blank=True, default="")
    enabled = models.BooleanField(default=False)
    #: Last accepted TOTP time-step: a code is never accepted twice.
    last_used_step = models.BigIntegerField(null=True, blank=True)
    nonce = models.CharField(max_length=64)
    #: Wrong codes since the last success (FLEET-3): every
    #: `FLEET_MFA_MAX_FAILURES_PER_TOKEN` of them spend the `mfa_token`, and
    #: `FLEET_MFA_LOCKOUT_THRESHOLD` lock the second factor.
    failed_attempts = models.PositiveIntegerField(default=0)
    #: Set when the factor locks; no code is accepted before. `reset_mfa`
    #: lifts it at once (the row is deleted).
    locked_until = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mfa_devices"

    def __str__(self) -> str:
        state = "enabled" if self.enabled else "not enrolled"
        return f"MFA for {self.user_id} ({state})"


class MfaRecoveryCode(models.Model):
    """One-time recovery code. Only a salted SHA-256 is stored."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mfa_recovery_codes",
    )
    salt = models.CharField(max_length=32)
    code_hash = models.CharField(max_length=64)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mfa_recovery_codes"

    def __str__(self) -> str:
        state = "used" if self.used_at else "unused"
        return f"recovery code of {self.user_id} ({state})"
