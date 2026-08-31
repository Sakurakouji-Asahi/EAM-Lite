from django.contrib.auth.views import LoginView, LogoutView
from django.db import transaction


class ApplicationLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def form_valid(self, form):
        """Do not leave an authenticated session when mandatory audit fails."""

        try:
            with transaction.atomic():
                return super().form_valid(form)
        except Exception:
            # django.contrib.auth.login mutates the session before emitting
            # user_logged_in.  If our append-only audit receiver fails, an
            # ordinary 500 response would otherwise still persist those
            # authentication keys in SessionMiddleware.process_response.
            self.request.session.flush()
            raise


class ApplicationLogoutView(LogoutView):
    pass
