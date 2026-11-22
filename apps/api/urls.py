from django.urls import path
from drf_spectacular.views import SpectacularAPIView

from . import inventory_views as inv
from . import views

urlpatterns = [
    path("overview/", views.OverviewView.as_view(), name="overview"),
    path("updates/", views.UpdatesView.as_view(), name="updates"),
    path("sites/<slug:slug>/", views.SiteDetailView.as_view(), name="site-detail"),
    path("apps/", inv.AppsView.as_view(), name="apps"),
    path("apps/<slug:slug>/", inv.AppDetailView.as_view(), name="app-detail"),
    path("apps/<slug:slug>/changes/", inv.AppChangesView.as_view(), name="app-changes"),
    path("apps/<slug:slug>/archive/", inv.AppArchiveView.as_view(), name="app-archive"),
    path("apps/<slug:slug>/restore/", inv.AppRestoreView.as_view(), name="app-restore"),
    path("box/", views.BoxView.as_view(), name="box"),
    path("refresh/<str:source>/", views.RefreshView.as_view(), name="refresh"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
]
