from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import permissions, response, status
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from apps.api_auth import mfa, refresh_cookie
from apps.api_auth.refresh_cookie import PERSIST_CLAIM, deliver_tokens
from apps.api_auth.serializers import (
    ErrorSerializer,
    LoginResponseSerializer,
    LoginSerializer,
    MeResponseSerializer,
    MfaCodeSerializer,
    MfaSetupResponseSerializer,
    MfaTokenSerializer,
    RefreshRequestSerializer,
    TokenRefreshResponseSerializer,
    TokenResponseSerializer,
    UserSerializer,
)
from apps.api_auth.throttles import (
    LoginRateThrottle,
    LoginUsernameRateThrottle,
    MfaRateThrottle,
    MfaUserRateThrottle,
)


def _token_payload(user: Any, *, persist: bool = True) -> dict[str, Any]:
    refresh = RefreshToken.for_user(user)
    if not persist:
        refresh[PERSIST_CLAIM] = False
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "user": UserSerializer(user).data,
    }


def _request_data(request: Request) -> Any:
    return request.data if hasattr(request.data, "get") else {}


class IsAuthenticatedOrCookieMode(permissions.BasePermission):
    """Logout: a Bearer token, or the cookie-mode opt-in (origin checked in the view).

    Lets the SPA sign out even when its in-memory access token is gone (expired,
    tab reloaded): blacklisting the caller's own refresh cookie needs nothing else.
    """

    def has_permission(self, request: Request, view: Any) -> bool:
        if refresh_cookie.wants_cookie(request):
            return True
        return bool(request.user and request.user.is_authenticated)


def _complete_login(
    request: Request, user: Any, *, remember_me: bool, **extra: Any
) -> response.Response:
    """Mint the JWT pair once the second factor is verified (the only path).

    Cookie mode defaults to a browser-session cookie: "remember me" (chosen at
    the password step, carried by the `mfa_token`) is an explicit choice. Body
    mode has no cookie, so the claim is moot.
    """
    persist = remember_me or not refresh_cookie.wants_cookie(request)
    return deliver_tokens(
        request,
        response.Response(
            {**extra, **_token_payload(user, persist=persist)},
            status=status.HTTP_200_OK,
        ),
    )


def _mfa_error(exc: mfa.MfaError) -> response.Response:
    return _error(exc.code, exc.detail)


class LoginView(APIView):
    """Password step. Never returns tokens: MFA is mandatory for every user.

    200 `{mfa_required, mfa_token}` when the user is enrolled, else
    `{mfa_setup_required, mfa_token}`. The token (5 min) is then presented to
    `mfa/verify/`, or to `mfa/setup/` + `mfa/setup/confirm/`.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes: list[type[Any]] = []
    # Login is the only barrier between the Internet and the fleet dashboard:
    # throttled per client IP (`NUM_PROXIES = 1`, so a
    # forged X-Forwarded-For prefix changes nothing) and per username.
    throttle_classes = [LoginRateThrottle, LoginUsernameRateThrottle]

    @extend_schema(
        operation_id="auth_login",
        request=LoginSerializer,
        responses={200: LoginResponseSerializer, 400: ErrorSerializer},
    )
    def post(self, request: Request) -> response.Response:
        if (rejected := refresh_cookie.cookie_origin_guard(request)) is not None:
            return rejected
        serializer = LoginSerializer(data=request.data, context={"request": request})
        if not serializer.is_valid():
            # Only failures count against the username (FLEET-7).
            LoginUsernameRateThrottle().record_failure(request, self)
            raise ValidationError(serializer.errors)
        user = serializer.validated_data["user"]
        token, enrolled = mfa.issue_mfa_token(
            user, remember_me=bool(serializer.validated_data.get("remember_me"))
        )
        key = "mfa_required" if enrolled else "mfa_setup_required"
        return response.Response(
            {key: True, "mfa_token": token}, status=status.HTTP_200_OK
        )


class _MfaCodeView(APIView):
    """Second-factor step: anonymous, Origin-checked in cookie mode, and
    throttled per client IP and per account (a 6-digit code is guessable)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes: list[type[Any]] = []
    throttle_classes = [MfaRateThrottle, MfaUserRateThrottle]


class MfaSetupView(APIView):
    """Enrolment, step 1: the pending secret as an `otpauth://` URI and a QR.

    Calling it again returns the same pending secret. It checks no code, so
    the default anonymous throttle is enough.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes: list[type[Any]] = []
    throttle_classes = [AnonRateThrottle]

    @extend_schema(
        operation_id="auth_mfa_setup",
        request=MfaTokenSerializer,
        responses={200: MfaSetupResponseSerializer, 400: ErrorSerializer},
    )
    def post(self, request: Request) -> response.Response:
        if (rejected := refresh_cookie.cookie_origin_guard(request)) is not None:
            return rejected
        serializer = MfaTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user, secret = mfa.pending_setup(serializer.validated_data["mfa_token"])
        except mfa.MfaError as exc:
            return _mfa_error(exc)
        uri = mfa.provisioning_uri(secret, user.get_username())
        return response.Response(
            {
                "otpauth_uri": uri,
                "secret": secret,
                "qr_svg": mfa.qr_svg_data_uri(uri),
            },
            status=status.HTTP_200_OK,
        )


class MfaSetupConfirmView(_MfaCodeView):
    """Enrolment, step 2: a first code enables MFA and completes the login.

    The 10 recovery codes are in this response and nowhere else, ever.
    """

    @extend_schema(
        operation_id="auth_mfa_setup_confirm",
        request=MfaCodeSerializer,
        responses={200: TokenResponseSerializer, 400: ErrorSerializer},
    )
    def post(self, request: Request) -> response.Response:
        if (rejected := refresh_cookie.cookie_origin_guard(request)) is not None:
            return rejected
        serializer = MfaCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user, remember_me, codes = mfa.confirm_setup(
                serializer.validated_data["mfa_token"],
                serializer.validated_data["code"],
            )
        except mfa.MfaError as exc:
            return _mfa_error(exc)
        return _complete_login(
            request, user, remember_me=remember_me, recovery_codes=codes
        )


class MfaVerifyView(_MfaCodeView):
    """A TOTP code (or a one-time recovery code) completes the login."""

    @extend_schema(
        operation_id="auth_mfa_verify",
        request=MfaCodeSerializer,
        responses={200: TokenResponseSerializer, 400: ErrorSerializer},
    )
    def post(self, request: Request) -> response.Response:
        if (rejected := refresh_cookie.cookie_origin_guard(request)) is not None:
            return rejected
        serializer = MfaCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user, remember_me = mfa.verify(
                serializer.validated_data["mfa_token"],
                serializer.validated_data["code"],
            )
        except mfa.MfaError as exc:
            return _mfa_error(exc)
        return _complete_login(request, user, remember_me=remember_me)


class TokenRefreshView(APIView):
    """Rotate the refresh token (the presented one is blacklisted).

    Body mode: `{refresh}` in, `{access, refresh}` out. Cookie mode
    (`X-Auth-Mode: cookie`): the HttpOnly cookie is used when the body has no
    `refresh` (401 `refresh_cookie_missing` if there is none); the rotated token
    replaces the cookie and is omitted from the body.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes: list[type[Any]] = []

    def get_authenticate_header(self, request: Request) -> str:
        # Without an authenticator DRF turns a rejected token into a 403; the
        # SPA ends the session on 401/403 alike, but 401 is the honest answer.
        return 'Bearer realm="api"'

    @extend_schema(
        operation_id="auth_refresh",
        request=RefreshRequestSerializer,
        responses={200: TokenRefreshResponseSerializer, 401: ErrorSerializer},
    )
    def post(self, request: Request) -> response.Response:
        if (rejected := refresh_cookie.cookie_origin_guard(request)) is not None:
            return rejected
        token = str(_request_data(request).get("refresh") or "")
        if refresh_cookie.wants_cookie(request):
            token = token or refresh_cookie.read_refresh_cookie(request)
            if not token:
                return response.Response(
                    {
                        "code": "refresh_cookie_missing",
                        "detail": "No refresh cookie: sign in again.",
                    },
                    status=status.HTTP_401_UNAUTHORIZED,
                )
        serializer = TokenRefreshSerializer(data={"refresh": token})
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            raise InvalidToken(exc.args[0]) from exc
        return deliver_tokens(
            request,
            response.Response(
                dict(serializer.validated_data), status=status.HTTP_200_OK
            ),
        )


class MeView(APIView):
    @extend_schema(
        operation_id="auth_me",
        request=None,
        responses={200: MeResponseSerializer},
    )
    def get(self, request: Request) -> response.Response:
        return response.Response({"user": UserSerializer(request.user).data})


class LogoutView(APIView):
    """Blacklist the current refresh token.

    Cookie mode (no Bearer needed): blacklists the body token or else the
    cookie, always clears the cookie, answers 204 (idempotent). Body mode
    (Bearer required): `{refresh}`.
    """

    permission_classes = [IsAuthenticatedOrCookieMode]

    @extend_schema(
        operation_id="auth_logout",
        request=RefreshRequestSerializer,
        responses={204: None, 400: ErrorSerializer},
    )
    def post(self, request: Request) -> response.Response:
        if (rejected := refresh_cookie.cookie_origin_guard(request)) is not None:
            return rejected
        cookie_mode = refresh_cookie.wants_cookie(request)
        token = str(_request_data(request).get("refresh") or "")
        if cookie_mode:
            token = token or refresh_cookie.read_refresh_cookie(request)

        if not token and not cookie_mode:
            return response.Response(
                {
                    "code": "refresh_token_required",
                    "detail": "Refresh token is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if token:
            try:
                RefreshToken(token).blacklist()  # type: ignore[arg-type]
            except TokenError:
                # Idempotent: whatever the token's state, the caller ends up
                # signed out.
                pass

        result = response.Response(status=status.HTTP_204_NO_CONTENT)
        if cookie_mode:
            refresh_cookie.clear_refresh_cookie(result)
        return result


def _error(code: str, detail: str) -> response.Response:
    return response.Response(
        {"code": code, "detail": detail}, status=status.HTTP_400_BAD_REQUEST
    )
