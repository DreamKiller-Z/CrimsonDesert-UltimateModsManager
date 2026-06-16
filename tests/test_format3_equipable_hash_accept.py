"""#191 (AbyssGearUnlock, pinapana): equipable_hash iteminfo intents
must pass the import-time validator so they reach the iteminfo writer.

The writer (iteminfo_writer.build_iteminfo_intent_change) already
resolves equipable_hash across separator/case variants and round-trips
it byte-exact, but the schema walker cannot reach the field (a
preceding variable-length field has no descriptor), so validate_intents
classified every intent as unsupported and the whole mod was dropped at
import with a 0 byte changes result. format3_handler now early-accepts
the normalized field name. These tests pin that, including the DMM
camelCase spelling the real mod variants use.
"""
from __future__ import annotations

from cdumm.engine.format3_handler import Format3Intent, validate_intents


def _intent(field: str) -> Format3Intent:
    return Format3Intent(
        entry="Item_Stat_AbyssGear_DDD_LV1", key=1002785,
        field=field, op="set", new=0, old=None)


def test_equipable_hash_snake_is_supported() -> None:
    v = validate_intents("iteminfo.pabgb", [_intent("equipable_hash")])
    assert len(v.supported) == 1
    assert len(v.skipped) == 0


def test_equipable_hash_dmm_camelcase_is_supported() -> None:
    # The DMM export spells it _equipAbleHash; the writer resolves it to
    # equipable_hash, so the validator must accept it too.
    v = validate_intents("iteminfo.pabgb", [_intent("_equipAbleHash")])
    assert len(v.supported) == 1
    assert len(v.skipped) == 0


def test_equipable_hash_separatorless_is_supported() -> None:
    v = validate_intents("iteminfo.pabgb", [_intent("equipablehash")])
    assert len(v.supported) == 1
    assert len(v.skipped) == 0


def test_unrelated_unreachable_field_still_skips() -> None:
    # Guard: the early-accept is scoped to equipable_hash, it must not
    # blanket-accept other unreachable fields.
    v = validate_intents("iteminfo.pabgb", [_intent("some_unknown_field")])
    assert len(v.supported) == 0
    assert len(v.skipped) == 1
