from django.urls import path

from apps.api_auth import views

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="auth-login"),
    path("mfa/setup/", views.MfaSetupView.as_view(), name="auth-mfa-setup"),
    path(
        "mfa/setup/confirm/",
        views.MfaSetupConfirmView.as_view(),
        name="auth-mfa-setup-confirm",
    ),
    path("mfa/verify/", views.MfaVerifyView.as_view(), name="auth-mfa-verify"),
    path("refresh/", views.TokenRefreshView.as_view(), name="auth-refresh"),
    path("logout/", views.LogoutView.as_view(), name="auth-logout"),
    path("me/", views.MeView.as_view(), name="auth-me"),
]
