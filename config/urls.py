from django.urls import path

from apps.accounts.forms import ChineseAuthenticationForm
from apps.accounts.views import ApplicationLoginView, ApplicationLogoutView
from apps.core.views import home


urlpatterns = [
    path("", home, name="home"),
    path(
        "login/",
        ApplicationLoginView.as_view(authentication_form=ChineseAuthenticationForm),
        name="login",
    ),
    path("logout/", ApplicationLogoutView.as_view(), name="logout"),
]
