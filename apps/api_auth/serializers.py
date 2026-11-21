from __future__ import annotations

from typing import Any

from django.contrib.auth import authenticate, get_user_model
from rest_framework import serializers


class LoginSerializer(serializers.Serializer[dict[str, Any]]):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=128, trim_whitespace=False)
    #: Cookie mode only: a persistent cookie instead of a browser-session one.
    remember_me = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        request = self.context.get("request")
        user = authenticate(
            request=request,
            username=attrs["username"],
            password=attrs["password"],
        )
        if user is None or not user.is_active:
            raise serializers.ValidationError("Invalid credentials")
        attrs["user"] = user
        return attrs


class UserSerializer(serializers.ModelSerializer[Any]):
    mfa_enabled = serializers.SerializerMethodField()

    class Meta:
        model = get_user_model()
        fields = ["id", "username", "is_staff", "is_superuser", "mfa_enabled"]

    def get_mfa_enabled(self, user: Any) -> bool:
        from apps.api_auth.mfa import is_enabled

        return is_enabled(user)


class MfaTokenSerializer(serializers.Serializer[dict[str, Any]]):
    mfa_token = serializers.CharField(max_length=512, trim_whitespace=True)


class MfaCodeSerializer(MfaTokenSerializer):
    #: A 6-digit TOTP code or a recovery code (`XXXXX-XXXXX`).
    code = serializers.CharField(max_length=32, trim_whitespace=True)
