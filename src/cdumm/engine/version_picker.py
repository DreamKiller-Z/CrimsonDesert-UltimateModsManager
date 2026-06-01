"""Post-import mods.version precedence resolver.

GitHub #187 (Balzhur, 2026-06-01): a mod whose modinfo.json bumped
to v1.2.1 but whose Nexus archive filename still carried the older
``1-1`` version slot was being reported as v1.1 in CDUMM. The
import worker correctly wrote 1.2.1 from the manifest, but the
post-import block in fluent_window then ran ``_get_drop_version``
on the Nexus filename and overwrote the row with 1.1.

The resolver returns the version that should be written to
``mods.version`` after import, or ``None`` to mean "keep the row
as-is" (because the manifest already won).

Decision tree:
  1. If the row's current version came from a real manifest
     (non-empty AND not the import-handler default "1.0") AND
     this is NOT a click-to-update import → preserve. Return None.
  2. Else compute ``drop_ver`` from the user-supplied path's
     filename, then from the candidate fallback path's filename,
     then from the cached Nexus latest-version for this mod_id.
  3. If ``drop_ver`` ends up different from the existing manifest
     value (or the manifest was empty/default), return it. Else
     return None.

Click-to-update detection: NXM downloads land as
``nxm_<mod>_<file>.bin`` temp files with no modinfo.json. In that
case the import worker leaves the row's version at the prior or
default value, and the cached Nexus ``latest_version`` is the
correct new value to bump to.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional


def pick_post_import_version(
    manifest_ver: str,
    orig_path: Optional[Path],
    fallback_path: Path,
    nexus_cached_version: Optional[str],
    get_drop_version: Callable[[Path], str],
) -> Optional[str]:
    """Return the version to write to ``mods.version``, or None.

    Args:
        manifest_ver: Existing value from the ``mods.version`` column
            (already populated by the import worker from the mod's
            manifest, or the import-handler default ``"1.0"``).
        orig_path: The user-originated file path, ``None`` when the
            import did not preserve it. Used both for click-to-update
            detection and as the primary filename to parse.
        fallback_path: Secondary path the filename parser tries when
            ``orig_path`` yielded nothing (usually the extracted
            archive path).
        nexus_cached_version: The cached Nexus ``latest_version`` for
            the mod's ``nexus_mod_id``, or ``None`` / empty string when
            no cache entry exists. Only consulted when the filename
            parsers come up empty.
        get_drop_version: Callable that pulls a version string out of
            a filename (typically the parse-Nexus-filename helper on
            the main window). Must return ``""`` when no version is
            detectable.

    Returns:
        The version string to UPDATE into the row, or ``None`` when
        the existing value should stay.
    """
    is_click_to_update = (
        orig_path is not None
        and orig_path.name.startswith("nxm_")
        and orig_path.name.endswith(".bin")
    )
    manifest_present = bool(manifest_ver) and manifest_ver != "1.0"
    if manifest_present and not is_click_to_update:
        return None

    drop_ver = get_drop_version(orig_path) if orig_path else ""
    if not drop_ver:
        drop_ver = get_drop_version(fallback_path)
    if not drop_ver and nexus_cached_version:
        drop_ver = nexus_cached_version.strip()
    if not drop_ver:
        return None
    if drop_ver == manifest_ver:
        return None
    return drop_ver
