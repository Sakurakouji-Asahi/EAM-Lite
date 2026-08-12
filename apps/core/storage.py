"""Private attachment storage.

The attachment directory deliberately has no URL capability.  Downloads must
go through a permission-checked Django view (or a future authenticated
internal redirect), never ``storage.url()``.
"""

from django.core.files.storage import FileSystemStorage


class PrivateFileSystemStorage(FileSystemStorage):
    def url(self, name):
        raise ValueError("受保护附件没有公开 URL；请使用鉴权下载端点。")
