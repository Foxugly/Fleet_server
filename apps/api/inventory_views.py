"""Inventory read/write API. Every write needs a fresh TOTP code (step-up)."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status as http
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.api_auth.mfa import verify_totp
from apps.inventory.models import App, InventoryChange
from apps.inventory.validation import clean_app

from .serializers import (
    AppChangeSerializer,
    AppInSerializer,
    AppOutSerializer,
    TotpOnlySerializer,
)

ACTION = InventoryChange.Action


def _out(app: App) -> dict[str, Any]:
    return {
        **app.as_dict(),
        "archived": app.archived,
        "archived_at": app.archived_at,
        "created_at": app.created_at,
        "updated_at": app.updated_at,
    }


def _get_app(slug: str, *, lock: bool = False) -> App:
    qs = App.objects.select_for_update() if lock else App.objects
    app = qs.filter(slug=slug).first()
    if app is None:
        raise Http404
    return app


def _totp_refused() -> Response:
    return Response({"code": "totp_invalid"}, status=http.HTTP_403_FORBIDDEN)


def _step_up_ok(request: Request) -> bool:
    data = request.data if hasattr(request.data, "get") else {}
    code = data.get("totp_code")
    return isinstance(code, str) and verify_totp(request.user, code)


def _audit(request: Request, slug: str, action: str, before: Any, after: Any) -> None:
    InventoryChange.objects.create(
        user=request.user, app_slug=slug, action=action, before=before, after=after
    )


class _WriteThrottled(APIView):
    """Reads keep the default throttles; writes also use the MFA scope (5/min)."""

    throttle_scope = "mfa"

    def get_throttles(self) -> list[Any]:
        throttles = super().get_throttles()
        if self.request.method not in ("GET", "HEAD", "OPTIONS"):
            throttles.append(ScopedRateThrottle())
        return throttles


class AppsView(_WriteThrottled):
    @extend_schema(operation_id="apps_list", responses=AppOutSerializer(many=True))
    def get(self, request: Request) -> Response:
        return Response([_out(a) for a in App.objects.all()])

    @extend_schema(
        operation_id="apps_create",
        request=AppInSerializer,
        responses={201: AppOutSerializer},
    )
    def post(self, request: Request) -> Response:
        if not _step_up_ok(request):
            return _totp_refused()
        fields = clean_app(_without_code(request.data))
        try:
            with transaction.atomic():
                app = App.objects.create(**fields)
                _audit(request, app.slug, ACTION.CREATE, None, app.as_dict())
        except IntegrityError:
            raise ValidationError({"slug": ["This slug already exists."]}) from None
        return Response(_out(app), status=http.HTTP_201_CREATED)


class AppDetailView(_WriteThrottled):
    @extend_schema(operation_id="apps_retrieve", responses=AppOutSerializer)
    def get(self, request: Request, slug: str) -> Response:
        return Response(_out(_get_app(slug)))

    @extend_schema(
        operation_id="apps_update",
        request=AppInSerializer,
        responses={200: AppOutSerializer},
    )
    def put(self, request: Request, slug: str) -> Response:
        _get_app(slug)  # 404 first: no code is spent on an unknown app
        if not _step_up_ok(request):
            return _totp_refused()
        data = _without_code(request.data)
        if data.get("slug", slug) != slug:
            raise ValidationError({"slug": ["The slug cannot be changed."]})
        fields = clean_app({**data, "slug": slug})
        with transaction.atomic():
            app = _get_app(slug, lock=True)
            before = app.as_dict()
            for key, value in fields.items():
                setattr(app, key, value)
            app.save()
            _audit(request, slug, ACTION.UPDATE, before, app.as_dict())
        return Response(_out(app))


class _ArchiveToggle(_WriteThrottled):
    archive = True

    def post(self, request: Request, slug: str) -> Response:
        _get_app(slug)
        if not _step_up_ok(request):
            return _totp_refused()
        with transaction.atomic():
            app = _get_app(slug, lock=True)
            if app.archived == self.archive:
                state = "already archived" if self.archive else "not archived"
                raise ValidationError({"detail": [f"This app is {state}."]})
            app.archived_at = timezone.now() if self.archive else None
            app.save()
            fields = app.as_dict()
            action = ACTION.ARCHIVE if self.archive else ACTION.RESTORE
            _audit(request, slug, action, fields, fields)
        return Response(_out(app))


class AppArchiveView(_ArchiveToggle):
    archive = True

    @extend_schema(
        operation_id="apps_archive",
        request=TotpOnlySerializer,
        responses={200: AppOutSerializer},
    )
    def post(self, request: Request, slug: str) -> Response:
        return super().post(request, slug)


class AppRestoreView(_ArchiveToggle):
    archive = False

    @extend_schema(
        operation_id="apps_restore",
        request=TotpOnlySerializer,
        responses={200: AppOutSerializer},
    )
    def post(self, request: Request, slug: str) -> Response:
        return super().post(request, slug)


class AppChangesView(APIView):
    @extend_schema(
        operation_id="apps_changes", responses=AppChangeSerializer(many=True)
    )
    def get(self, request: Request, slug: str) -> Response:
        _get_app(slug)
        rows = InventoryChange.objects.filter(app_slug=slug).select_related("user")
        return Response(
            [
                {
                    "at": c.at,
                    "user": c.user.get_username() if c.user else None,
                    "action": c.action,
                    "before": c.before,
                    "after": c.after,
                }
                for c in rows
            ]
        )


def _without_code(data: Any) -> dict[str, Any]:
    if not hasattr(data, "items"):
        raise ValidationError({"non_field_errors": ["Expected an object."]})
    return {k: v for k, v in data.items() if k != "totp_code"}
