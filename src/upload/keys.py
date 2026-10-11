"""Object-storage keys for uploaded files: one place builds them and one place checks them.

An upload lands at uploads/<user>/<project>/<layer><ext>. The browser sends that key back on every later step
(multipart parts, status, complete, upload-complete), so each step must check that the key is the caller's own,
for the project and layer the request is about; otherwise anyone who learns another object's key could read it
into their own map or write into it.
"""

from __future__ import annotations

import re
from typing import Optional

_SEGMENT = r"[A-Za-z0-9_-]+"
_EXTENSION = r"(?:\.[a-z0-9_-]{1,16})+"  # ".tif", ".laz", ".gz", ".tif.gz"


class NotYourUpload(ValueError):
    """The key is not an upload of this user, project or layer."""


def upload_key(user_id: str, project_id: str, layer_id: str, extension: str) -> str:
    """Where a new upload with this file extension (".tif", ".laz", ".fgb") for this user, project and layer is
    stored."""
    return f"uploads/{user_id}/{project_id}/{layer_id}{extension}"


def check_upload_key(s3_key: str, user_id: str, *, project_id: Optional[str] = None,
                     layer_id: Optional[str] = None) -> None:
    """Raise NotYourUpload unless `s3_key` is an upload key of `user_id` (and of `project_id` and `layer_id` when
    given), shaped exactly as `upload_key` builds it: no other prefix, no extra or empty segments, no "..".
    """
    pattern = (rf"uploads/{re.escape(user_id)}/"
               rf"{re.escape(project_id) if project_id else _SEGMENT}/"
               rf"{re.escape(layer_id) if layer_id else _SEGMENT}{_EXTENSION}")
    if not re.fullmatch(pattern, s3_key):
        raise NotYourUpload("This upload does not belong to you, or to this map or layer")
