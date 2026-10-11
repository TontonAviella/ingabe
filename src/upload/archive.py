"""Unpacking uploaded ZIP archives (zipped shapefiles, KMZ) without letting one archive fill the disk.

`ZipFile.extractall` trusts the archive: a few kilobytes can expand to gigabytes (a "zip bomb"), and nothing
limits how many entries it writes. `extract_zip` caps both, counting the bytes actually written (an entry's
declared size can lie), skips symbolic links and macOS metadata, and keeps every path inside the target folder.
"""

from __future__ import annotations

import os
import stat
import zipfile
from typing import Callable, Optional

MAX_ENTRIES = 2_000
MAX_TOTAL_BYTES = 2 * 1024 ** 3  # 2 GiB unpacked
_CHUNK = 1024 * 1024


class ArchiveTooLarge(ValueError):
    """The archive holds more entries or more unpacked bytes than allowed."""


def _counted(member: zipfile.ZipInfo) -> bool:
    """A file entry that unpacking considers: not a folder, not macOS metadata."""
    return not member.is_dir() and "__MACOSX" not in member.filename.split("/")


def member_names(zip_path: str) -> list[str]:
    """The names of the files in the archive that `extract_zip` considers, read from its table of contents only.
    Raises zipfile.BadZipFile for a file that is not a ZIP."""
    with zipfile.ZipFile(zip_path) as archive:
        return [m.filename for m in archive.infolist() if _counted(m)]


def extract_zip(zip_path: str, target_dir: str, *, max_entries: int = MAX_ENTRIES,
                max_total_bytes: int = MAX_TOTAL_BYTES, only: Optional[Callable[[str], bool]] = None) -> list[str]:
    """Unpack `zip_path` into `target_dir` and return the paths written. With `only`, unpack just the entries
    whose name it accepts (the caps then count only those). Raises ArchiveTooLarge past either cap (what was
    written so far stays in `target_dir` for the caller to remove), and zipfile.BadZipFile for a file that is not
    a ZIP."""
    root = os.path.realpath(target_dir)
    written: list[str] = []
    total = 0
    with zipfile.ZipFile(zip_path) as archive:
        members = [m for m in archive.infolist() if _counted(m) and (only is None or only(m.filename))]
        if len(members) > max_entries:
            raise ArchiveTooLarge(f"The ZIP holds {len(members)} files; at most {max_entries} are allowed")
        for member in members:
            if stat.S_ISLNK(member.external_attr >> 16):
                continue
            destination = os.path.realpath(os.path.join(root, member.filename))
            if not destination.startswith(root + os.sep):
                continue  # an absolute or "../" path would land outside the folder
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with archive.open(member) as source, open(destination, "wb") as out:
                while chunk := source.read(_CHUNK):
                    total += len(chunk)
                    if total > max_total_bytes:
                        raise ArchiveTooLarge(f"The ZIP unpacks to more than {max_total_bytes // 1024 ** 2} MB")
                    out.write(chunk)
            written.append(destination)
    return written
