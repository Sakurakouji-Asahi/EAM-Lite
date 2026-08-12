import uuid

from django import forms


class ImportUploadForm(forms.Form):
    IMPORT_CHOICES = (
        ("department", "部门"),
        ("employee", "人员"),
    )

    import_type = forms.ChoiceField(label="导入类型", choices=IMPORT_CHOICES)
    file = forms.FileField(
        label="Excel 文件",
        help_text="仅接受本页标准模板生成的 .xlsx 文件。",
        widget=forms.ClearableFileInput(
            attrs={"accept": ".xlsx", "class": "form-control"}
        ),
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput(), required=False)

    def __init__(self, *args, import_type=None, **kwargs):
        super().__init__(*args, **kwargs)
        if import_type:
            self.fields["import_type"].initial = import_type
            self.fields["import_type"].widget = forms.HiddenInput()
        self.fields["idempotency_key"].initial = str(uuid.uuid4())

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if not uploaded.name.lower().endswith(".xlsx"):
            raise forms.ValidationError("只允许上传无宏的 .xlsx 标准模板。")
        return uploaded

    def clean_idempotency_key(self):
        value = (self.cleaned_data.get("idempotency_key") or "").strip()
        try:
            return str(uuid.UUID(value)) if value else str(uuid.uuid4())
        except ValueError as exc:
            raise forms.ValidationError("请求幂等标识无效，请刷新页面重试。") from exc
