from django import forms
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import transaction

MAX_UPLOAD_FILES = 20
MAX_UPLOAD_TOTAL_BYTES = 50 * 1024 * 1024

class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    widget = MultiFileInput

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        if not files:
            return super().clean(None, initial) or []
        if len(files) > MAX_UPLOAD_FILES or sum(getattr(file, 'size', 0) for file in files) > MAX_UPLOAD_TOTAL_BYTES:
            raise ValidationError('每次最多 20 个文件，总大小不得超过 50 MB。')
        return [super(MultiFileField,self).clean(file, initial) for file in files]


def upload_many(service, *, uploaded_file, **kwargs):
    files = uploaded_file if isinstance(uploaded_file, (list, tuple)) else [uploaded_file]
    links = []
    try:
        with transaction.atomic():
            for file in files:
                try:
                    links.append(service(uploaded_file=file, **kwargs))
                except ValidationError as exc:
                    raise ValidationError([f'{file.name}：{message}' for message in exc.messages]) from exc
    except Exception:
        # Database rollback cannot remove storage objects written by earlier files.
        for link in links:
            default_storage.delete(link.attachment.storage_key)
        raise
    return links
