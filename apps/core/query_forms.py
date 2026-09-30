"""Date-range validation shared by operational list pages."""

from django import forms


class DateRangeQueryForm(forms.Form):
    date_from = forms.DateField(label="开始日期", required=False, input_formats=["%Y-%m-%d"],
                                widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    date_to = forms.DateField(label="结束日期", required=False, input_formats=["%Y-%m-%d"],
                              widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and end < start:
            raise forms.ValidationError("结束日期不得早于开始日期。")
        return cleaned


def date_query_errors(form):
    return [f"{form.fields[name].label}：{message}" if name in form.fields else str(message)
            for name, errors in form.errors.items() for message in errors]
