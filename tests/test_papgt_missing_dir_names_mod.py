"""GitHub #225 ("All Color Tabs Unlocked", nexus 2281): disabling a PAPGT mod
can leave a dangling "Missing directory NNNN" entry in meta/0.papgt. The
post-apply verification dialog told the user "the mod name in brackets
indicates the likely cause" while the bracket only ever showed the bare
"PAPGT" category tag -- never a mod name.

_papgt_dir_owners maps each modded top-level directory to the mod(s) that
write into it, INCLUDING disabled mods (the dangling dir is almost always a
just-disabled mod's), so the dialog can name the responsible mod.
"""
from __future__ import annotations

import sqlite3


def _db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, enabled INTEGER)")
    conn.execute("CREATE TABLE mod_deltas (mod_id INTEGER, file_path TEXT)")
    return conn


def test_missing_dir_names_the_owning_mod_even_when_disabled():
    from cdumm.gui.fluent_window import _papgt_dir_owners
    conn = _db()
    # disabled mod -- the #225 scenario (user just disabled it)
    conn.execute("INSERT INTO mods VALUES (1, 'All Color Tabs Unlocked', 0)")
    conn.execute("INSERT INTO mod_deltas VALUES (1, '0207/0.paz')")
    conn.execute("INSERT INTO mod_deltas VALUES (1, '0207/0.pamt')")
    assert _papgt_dir_owners(conn).get("0207") == {"All Color Tabs Unlocked"}


def test_meta_and_nondigit_dirs_are_ignored():
    from cdumm.gui.fluent_window import _papgt_dir_owners
    conn = _db()
    conn.execute("INSERT INTO mods VALUES (1, 'M', 1)")
    conn.execute("INSERT INTO mod_deltas VALUES (1, 'meta/0.papgt')")
    conn.execute("INSERT INTO mod_deltas VALUES (1, 'bin64/plugin.asi')")
    assert _papgt_dir_owners(conn) == {}


def test_multiple_mods_sharing_a_dir_are_all_named():
    from cdumm.gui.fluent_window import _papgt_dir_owners
    conn = _db()
    conn.execute("INSERT INTO mods VALUES (1, 'A', 1)")
    conn.execute("INSERT INTO mods VALUES (2, 'B', 0)")
    conn.execute("INSERT INTO mod_deltas VALUES (1, '0099/0.paz')")
    conn.execute("INSERT INTO mod_deltas VALUES (2, '0099/0.pamt')")
    assert _papgt_dir_owners(conn).get("0099") == {"A", "B"}