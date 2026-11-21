from django.urls import path
from drf_spectacular.views import SpectacularAPIView

from . import views

urlpatterns = [
    path("overview/", views.OverviewView.as_view(), name="overview"),
    path("sites/<slug:slug>/", views.SiteDetailView.as_view(), name="site-detail"),
    path("box/", views.BoxView.as_view(), name="box"),
    path("refresh/<str:source>/", views.RefreshView.as_view(), name="refresh"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
]
