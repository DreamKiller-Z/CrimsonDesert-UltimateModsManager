"""CDUMM-native iteminfo parser, clean-room replacement for the
crimson_rs Rust extension's parse_iteminfo_from_bytes /
serialize_iteminfo functions.

The vendored crimson_rs.pyd parses the pre-1.0.4.1 game iteminfo
layout. After Pearl Abyss shipped a post-1.0.4.1 patch (visible in
Faisal's 2026-04-29 game update), each iteminfo record grew by 10
bytes and the .pyd parser misaligns with "CArray count 15386081
exceeds remaining bytes" on the first record.

We need our own parser that walks the current layout. Trust anchor:
parse + serialize on the live extracted iteminfo.pabgb must produce
byte-identical output. Without that, applying any list-of-dict
intent will corrupt the file.
"""
from __future__ import annotations

from pathlib import Path

import pytest


_LIVE_BODY = Path(
    "C:/Users/faisa/AppData/Local/Temp/iteminfo_postpatch.pabgb"
)


def _have_live_fixture() -> bool:
    return _LIVE_BODY.exists()


@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="iteminfo_postpatch.pabgb fixture not present",
)
def test_native_parser_first_record_size_matches_pabgh_index():
    """Parse the first record from the live iteminfo. Its on-disk
    size must equal what the .pabgh index says (offset of record 1
    minus offset of record 0). Catches schema-misalignment where
    the parser walks fewer or more bytes than the actual record.
    """
    from cdumm.engine.iteminfo_native_parser import parse_first_record_size

    body = _LIVE_BODY.read_bytes()
    header = _LIVE_BODY.with_suffix(".pabgh").read_bytes()

    from cdumm.semantic.parser import parse_pabgh_index
    _, offsets = parse_pabgh_index(header, "iteminfo")
    sorted_offs = sorted(offsets.items(), key=lambda kv: kv[1])
    expected_first_size = sorted_offs[1][1] - sorted_offs[0][1]

    actual = parse_first_record_size(body)
    assert actual == expected_first_size, (
        f"first record size: parser walked {actual} bytes, "
        f"pabgh index says {expected_first_size} bytes")


@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="iteminfo_postpatch.pabgb fixture not present",
)
def test_native_parser_walks_every_record_to_correct_boundary():
    """For every entry in the .pabgh index, our parser's walked
    size must equal (next_offset - this_offset). One drift on any
    record means we'd serialize-corrupt the file."""
    from cdumm.engine.iteminfo_native_parser import parse_record_at

    body = _LIVE_BODY.read_bytes()
    header = _LIVE_BODY.with_suffix(".pabgh").read_bytes()

    from cdumm.semantic.parser import parse_pabgh_index
    _, offsets = parse_pabgh_index(header, "iteminfo")
    sorted_offs = sorted(offsets.items(), key=lambda kv: kv[1])

    drifts: list[tuple[int, int, int]] = []
    for i, (key, off) in enumerate(sorted_offs):
        end = (sorted_offs[i + 1][1]
               if i + 1 < len(sorted_offs) else len(body))
        expected = end - off
        try:
            actual = parse_record_at(body, off, rec_end=end) - off
        except Exception as e:
            drifts.append((key, expected, -1))
            if len(drifts) <= 3:
                print(f"key={key} off=0x{off:X}: parse failed: {e}")
            continue
        if actual != expected:
            drifts.append((key, expected, actual))

    assert not drifts, (
        f"{len(drifts)}/{len(sorted_offs)} records misaligned. "
        f"first 3: {drifts[:3]}")


@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="iteminfo_postpatch.pabgb fixture not present",
)
def test_native_parser_round_trips_byte_identical():
    """The trust anchor: parse + serialize on live iteminfo bytes
    must produce identical output. Anything less means writing a
    Format 3 list intent through this parser will corrupt the
    iteminfo binary."""
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes, serialize_iteminfo,
    )

    body = _LIVE_BODY.read_bytes()
    items = parse_iteminfo_from_bytes(body)
    re_encoded = serialize_iteminfo(items)
    if re_encoded != body:
        n = min(len(re_encoded), len(body))
        i = 0
        while i < n and re_encoded[i] == body[i]:
            i += 1
        pytest.fail(
            f"round-trip diverged at byte {i} (0x{i:X}). "
            f"orig size={len(body)} new size={len(re_encoded)}.")
