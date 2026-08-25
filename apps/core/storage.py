"""Private attachment storage.

The attachment directory deliberately has no URL capability.  Downloads must
go through a permission-checked Django view (or a future authenticated
internal redirect), never ``storage.url()``.
"""

from django.contrib.staticfiles.storage import ManifestStaticFilesStorage
from django.core.files.storage import FileSystemStorage


class PrivateFileSystemStorage(FileSystemStorage):
    def url(self, name):
        raise ValueError("受保护附件没有公开 URL；请使用鉴权下载端点。")


class EAMManifestStaticFilesStorage(ManifestStaticFilesStorage):
    """Hash packaged assets without resolving optional vendor source maps.

    EAM-Lite's runtime CSS and JavaScript are self-contained. Bootstrap's
    minified files retain upstream debug source-map comments, while the maps
    are intentionally not shipped because browsers do not need them. Empty
    patterns keep strict manifest hashing but skip rewriting those comments.
    """

    patterns = ()
