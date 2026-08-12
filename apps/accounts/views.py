from django.contrib.auth.views import LoginView, LogoutView


class ApplicationLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True


class ApplicationLogoutView(LogoutView):
    pass
