from django.urls import include, path

from apps.accounts.forms import ChineseAuthenticationForm
from apps.accounts.views import ApplicationLoginView, ApplicationLogoutView
from apps.core.views import error_400, error_403, error_404, error_500, healthz, home
from apps.masterdata.views import setup_overview, setup_step


urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("", home, name="home"),
    path(
        "login/",
        ApplicationLoginView.as_view(authentication_form=ChineseAuthenticationForm),
        name="login",
    ),
    path("logout/", ApplicationLogoutView.as_view(), name="logout"),
    path("setup/", setup_overview, name="setup"),
    path("setup/<int:step>/", setup_step, name="setup-step"),
    path("master-data/", include("apps.masterdata.urls")),
    path("assets/", include("apps.assets.urls")),
    path("finance/", include("apps.finance.urls")),
    path("imports/", include("apps.imports.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("maintenance/", include("apps.maintenance.urls")),
    path("offboarding/", include("apps.offboarding.urls")),
    path("audit/", include("apps.audit.urls")),
    path("reports/", include("apps.reports.urls")),
    path("operations/", include("apps.operations.urls")),
]

handler400 = error_400
handler403 = error_403
handler404 = error_404
handler500 = error_500
