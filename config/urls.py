from django.urls import include, path

from apps.accounts.forms import ChineseAuthenticationForm
from apps.accounts.views import ApplicationLoginView, ApplicationLogoutView
from apps.core.views import home
from apps.masterdata.views import setup_overview, setup_step


urlpatterns = [
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
]
