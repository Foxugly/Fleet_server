from django.contrib import admin
from django.urls import include, path

from config.health import health

urlpatterns = [
    path("health/", health, name="health"),
    path("api/v1/auth/", include("apps.api_auth.urls")),
    path("api/v1/", include("apps.api.urls")),
    path("admin/", admin.site.urls),
]
