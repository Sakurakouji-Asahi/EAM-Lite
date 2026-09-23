"""Use HTML date formats independently of the active display locale."""
from django import forms


def normalize_date_widgets(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.DateInput) and widget.input_type == "date":
            widget.format = "%Y-%m-%d"
