from django.core.paginator import Paginator


def paginate_query(request, rows, *, parameter='page', per_page=25):
    page = Paginator(rows, per_page).get_page(request.GET.get(parameter))
    query = request.GET.copy()
    query.pop(parameter, None)
    return page, query.urlencode()
