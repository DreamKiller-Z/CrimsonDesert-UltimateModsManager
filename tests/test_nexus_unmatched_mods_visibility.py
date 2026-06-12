"""Faisal, 2026-06-11, second round: after the cached-state fix,
three mods still showed no up-to-date status. Causes and contracts
pinned here:

1. The mod PAGE returns 403/404 (hidden by moderation or removed,
   e.g. Can It Stack, mod 2180). Permanent, so the check emits a
   status with file_deleted_on_nexus=True (the UI's source-removed
   badge) instead of silently retrying forever.

2. The user renamed the mod locally and the row predates file-id
   tracking, so the display name matched nothing. The matcher now
   falls back to the ORIGINAL Nexus filename preserved in drop_name
   (suffix-stripped) before giving up.

3. The author renamed the file LINE on Nexus (Enhanced Internal
   Graphics -> InternalGraphicsMod, mod 651): genuinely unmatchable,
   stays grey by design; only an update/re-link fixes tracking.
"""
from __future__ import annotations

import time
import urllib.error
from types import SimpleNamespace

import pytest

import cdumm.engine.nexus_api as nx
from cdumm.engine.nexus_api import check_mod_updates, get_mod_files


def _mod(row_id, nexus_id, name="Foo", version="1.0", drop_name=None,
         real_file_id=None):
    return {
        "id": row_id, "name": name, "version": version,
        "nexus_mod_id": nexus_id, "nexus_real_file_id": real_file_id,
        "nexus_last_checked_at": 0, "drop_name": drop_name,
    }


def _http_error(code):
    return urllib.error.HTTPError(
        url="u", code=code, msg="m", hdrs=None, fp=None)


def test_page_403_returns_gone_sentinel(monkeypatch):
    def _raise(endpoint, key):
        raise _http_error(403)
    monkeypatch.setattr(nx, "_api_request", _raise)
    assert get_mod_files(2180, "key") == (None, None)


def test_page_500_stays_transient(monkeypatch):
    def _raise(endpoint, key):
        raise _http_error(500)
    monkeypatch.setattr(nx, "_api_request", _raise)
    assert get_mod_files(2180, "key") is None


def test_gone_page_emits_source_removed_status(monkeypatch):
    monkeypatch.setattr(nx, "get_recently_updated", lambda *a, **k: set())
    monkeypatch.setattr(nx, "get_mod_files", lambda mid, key: (None, None))

    updates, checked_ids, _, _ = check_mod_updates(
        [_mod(1, 2180, name="Can It Stack")], "key")
    assert len(updates) == 1
    u = updates[0]
    assert u.file_deleted_on_nexus is True
    assert u.has_update is False
    assert checked_ids == [1], (
        "permanent page-gone must stamp the timestamp so the mod is "
        "not re-fetched every cycle forever")


def _nexus_file(file_id, name, version, ts=1700000000):
    return SimpleNamespace(
        file_id=file_id, name=name, version=version,
        uploaded_timestamp=ts, file_name=name, category_id=1)


def test_renamed_local_mod_matches_via_drop_name(monkeypatch):
    """User renamed 'Enhanced Internal Graphics' to 'Graphics Mod';
    drop_name still holds the original Nexus filename and must match."""
    files = [
        _nexus_file(10, "Enhanced Internal Graphics V.3.0", "3.0"),
        _nexus_file(9, "Enhanced Internal Graphics V.2.8", "2.8"),
    ]
    monkeypatch.setattr(nx, "get_recently_updated", lambda *a, **k: {651})
    monkeypatch.setattr(nx, "get_mod_files", lambda mid, key: (files, []))

    mods = [_mod(1, 651, name="Graphics Mod", version="2.8",
                 drop_name="Enhanced Internal Graphics V2.8-651-2-8-1-1778002839")]
    updates, _, _, _ = check_mod_updates(mods, "key")
    assert len(updates) == 1, (
        "drop_name fallback did not rescue the renamed mod")
    assert updates[0].has_update is True
    assert updates[0].latest_version == "3.0"


def test_author_renamed_file_line_stays_unmatched(monkeypatch):
    """When NEITHER the display name nor the original filename matches
    (author renamed the whole file line on Nexus), the mod stays
    un-flagged: guessing on a multi-file page risks updating to the
    wrong file, and claiming up-to-date would be a lie.

    Real case, mod 651: local name 'Graphics Mod' / original filename
    'Enhanced Internal Graphics ...' vs the live file line
    'InternalGraphicsMod.v.3.1.4 (with extra shadows)'. The extra
    descriptor tokens drop token overlap below the 0.6 match
    threshold, so neither name resolves a file."""
    files = [
        _nexus_file(10, "InternalGraphicsMod.v.3.1.4 (with extra shadows)",
                    "v.3.1.5"),
        _nexus_file(9, "InternalGraphicsMod.V.3.1.4 (NO EXTRA SHADOWS )",
                    "3.1.5"),
    ]
    monkeypatch.setattr(nx, "get_recently_updated", lambda *a, **k: {651})
    monkeypatch.setattr(nx, "get_mod_files", lambda mid, key: (files, []))

    mods = [_mod(1, 651, name="Graphics Mod", version="2.8",
                 drop_name="Enhanced Internal Graphics V2.8-651-2-8-1-1778002839")]
    updates, _, _, _ = check_mod_updates(mods, "key")
    assert updates == [], (
        "an unmatchable renamed-file-line mod must stay un-flagged, "
        "not guess at a file on a multi-file page")
