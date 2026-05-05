"""buffinfo.pabgb byte walker for CDUMM Format 3 apply path.

Decodes the BuffInfo wrapper struct (13 fields per entry) and exposes
per-field byte offsets so ``_intents_to_v2_changes`` can emit byte
patches at the right location for intents like::

    {"entry": "BuffLevel_Socket_ContributionExp",
     "key": 1000114,
     "field": "min_level",
     "op": "set", "new": 1}

The variable-length ``buff_data_list`` region is currently treated as
an opaque byte slice , each item is a tagged variant from a 120-
member family that this module doesn't yet decode. The wrapper-level
fields (key, name, is_blocked, count, min/max level, sequencer name,
template/component, status info, flags) ARE decoded and round-trip
byte-perfectly.

On-disk layout (verified against all 280 entries of CD v1.05 vanilla
buffinfo.pabgb)::

    [ 0:  4]  key                              u32 LE
    [ 4:  8]  name length                      u32 LE
    [ 8:N0]   name                             utf-8
    [N0    ]  is_blocked                       u8
    [N0+1 :N0+5]  buff_data_list count         u32 LE
    [N0+5 :N1]    buff_data_list items         opaque (variant tags)
    [N1   :N1+4]  min_level                    u32 LE
    [N1+4 :N1+8]  max_level                    u32 LE
    [N1+8 :N1+12] sequencer_file_name length   u32 LE
    [N1+12:N2]    sequencer_file_name          utf-8
    [N2   ]   buff_level_calculate_type        u8
    [N2+1 :N2+5]  ui_template_name             u32 LE
    [N2+5 :N2+9]  ui_component_name            u32 LE
    [N2+9 :N2+13] elemental_status_info        u32 LE
    [N2+13]   is_use_skill_info_pattern_descr  u8
    [N2+14]   use_counting_by_global_timer     u8
    [end of entry]

The opaque ``[N0+5 : N1]`` region is located by walking BACKWARD from
the entry's known end size: the trailing 15 bytes are fixed-width
fields, before that comes the sequencer_file_name CString, before
that two u32 levels. Solving the fixed-point relation
``cstring_len_at_pos == cstring_len`` finds N1 deterministically.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass


# Trailing fixed-width region after sequencer_file_name's UTF-8 bytes.
# Comprises buff_level_calculate_type (1) + ui_template_name (4) +
# ui_component_name (4) + elemental_status_info (4) +
# is_use_skill_info_pattern_description (1) + use_counting_by_global_timer (1).
_TRAILING_FIXED_BYTES = 15

# Bytes between buff_data_list items end and the sequencer_file_name
# length prefix: min_level (4) + max_level (4). The length prefix
# itself sits AT this offset and is what the back-walk searches for,
# so it's deliberately excluded here.
_POST_ITEMS_PRE_CSTRING_BYTES = 8


@dataclass(frozen=True)
class BuffinfoEntryHeader:
    """Decoded prefix of a single buffinfo.pabgb entry.

    Verified field-by-field across all 280 entries of CD v1.05
    vanilla buffinfo.pabgb. ``body_start`` is the byte offset where
    the variable-length buff_data_list items begin.
    """
    entry_key: int
    name: str
    is_blocked: int
    is_blocked_offset: int
    buff_data_count: int
    buff_data_count_offset: int
    body_start: int
    prefix_end: int


@dataclass(frozen=True)
class BuffinfoEntry:
    """Full decoded BuffInfo wrapper for one buffinfo.pabgb entry.

    All ``_offset`` fields are byte positions within the entry that
    CDUMM's intent expander can target for byte patches. Fields named
    after the engine schema; values are raw on-disk integers.

    ``buff_data_list_bytes`` is the un-decoded variable-length region
    holding ``buff_data_count`` BuffData items. Future passes will
    decode item internals and expose per-item offsets; until then
    the bytes are preserved verbatim so the entry round-trips.
    """
    # Header (already exposed via parse_entry_prefix)
    entry_key: int
    name: str

    # Wrapper fields with offsets
    is_blocked: int
    is_blocked_offset: int

    buff_data_count: int
    buff_data_count_offset: int

    buff_data_list_bytes: bytes
    buff_data_list_offset: int  # start of items region

    min_level: int
    min_level_offset: int

    max_level: int
    max_level_offset: int

    sequencer_file_name: str
    sequencer_file_name_offset: int  # byte offset of the length u32

    buff_level_calculate_type: int
    buff_level_calculate_type_offset: int

    ui_template_name: int
    ui_template_name_offset: int

    ui_component_name: int
    ui_component_name_offset: int

    elemental_status_info: int
    elemental_status_info_offset: int

    is_use_skill_info_pattern_description: int
    is_use_skill_info_pattern_description_offset: int

    use_counting_by_global_timer: int
    use_counting_by_global_timer_offset: int


def parse_entry_prefix(entry_bytes: bytes) -> BuffinfoEntryHeader:
    """Decode just the header (key + name + is_blocked + count).

    Cheaper than parse_entry when only the header fields are needed
    (e.g. to look up an entry by key without decoding the full
    wrapper). Raises ``ValueError`` on truncation or implausible
    counts.
    """
    if len(entry_bytes) < 8:
        raise ValueError(
            f"buffinfo entry too short for prefix: {len(entry_bytes)}B"
        )
    entry_key = struct.unpack_from("<I", entry_bytes, 0)[0]
    slen = struct.unpack_from("<I", entry_bytes, 4)[0]
    if slen > 1_000_000 or 8 + slen > len(entry_bytes):
        raise ValueError(
            f"buffinfo entry has implausible name length {slen} "
            f"(entry size {len(entry_bytes)}B)"
        )
    name = entry_bytes[8:8 + slen].decode("utf-8", errors="replace")
    prefix_end = 8 + slen

    if prefix_end + 5 > len(entry_bytes):
        raise ValueError(
            f"buffinfo entry truncated at body header: need 5 bytes "
            f"after name, got {len(entry_bytes) - prefix_end}"
        )
    is_blocked = entry_bytes[prefix_end]
    buff_data_count = struct.unpack_from(
        "<I", entry_bytes, prefix_end + 1)[0]
    if buff_data_count > 10_000:
        raise ValueError(
            f"buffinfo entry has implausible buff_data_list count "
            f"{buff_data_count} for entry {name!r}"
        )

    return BuffinfoEntryHeader(
        entry_key=entry_key,
        name=name,
        is_blocked=is_blocked,
        is_blocked_offset=prefix_end,
        buff_data_count=buff_data_count,
        buff_data_count_offset=prefix_end + 1,
        body_start=prefix_end + 5,
        prefix_end=prefix_end,
    )


def _find_sequencer_length(
    entry_bytes: bytes, body_start: int,
) -> int:
    """Locate the sequencer_file_name length by walking backward from
    the entry's known end.

    The trailing 15 bytes are fixed-width fields, so the sequencer's
    UTF-8 bytes end at ``len - 15``. The length u32 sits 4 bytes
    before the UTF-8 starts, so for a candidate length ``L``:

        cstring_len_pos = len - 15 - L - 4

    A consistent layout requires ``u32_at(cstring_len_pos) == L``.
    Iterate small L upward until the relation holds (sequencer names
    are short , typically 0-100 bytes in observed vanilla data).
    """
    n = len(entry_bytes)
    floor_pos = body_start + _POST_ITEMS_PRE_CSTRING_BYTES
    # Maximum candidate L is bounded by the entry size minus the
    # mandatory pre-cstring (12) + trailing (15) overhead.
    max_l = n - floor_pos - _TRAILING_FIXED_BYTES
    if max_l < 0:
        raise ValueError(
            f"buffinfo entry too small to contain wrapper trailer "
            f"(size {n}, body_start {body_start})"
        )
    for candidate_len in range(max_l + 1):
        cstring_len_pos = n - _TRAILING_FIXED_BYTES - candidate_len - 4
        if cstring_len_pos < floor_pos:
            break
        actual = struct.unpack_from("<I", entry_bytes, cstring_len_pos)[0]
        if actual == candidate_len:
            return candidate_len
    raise ValueError(
        "buffinfo entry: could not locate a self-consistent "
        "sequencer_file_name length via backward walk"
    )


def parse_entry(entry_bytes: bytes) -> BuffinfoEntry:
    """Decode the full BuffInfo wrapper for one entry.

    Items inside ``buff_data_list`` are NOT decoded yet; their bytes
    are preserved as ``buff_data_list_bytes`` so the entry round-trips
    via ``serialize_entry``. Wrapper fields all expose ``_offset``
    annotations callers can use to emit byte patches.
    """
    head = parse_entry_prefix(entry_bytes)

    # Locate sequencer_file_name by back-walking from the entry tail.
    seq_len = _find_sequencer_length(entry_bytes, head.body_start)
    n = len(entry_bytes)
    seq_len_pos = n - _TRAILING_FIXED_BYTES - seq_len - 4
    seq_data_pos = seq_len_pos + 4
    sequencer_name = entry_bytes[
        seq_data_pos:seq_data_pos + seq_len].decode(
            "utf-8", errors="replace")

    # min_level + max_level immediately precede the cstring length.
    max_level_pos = seq_len_pos - 4
    min_level_pos = max_level_pos - 4
    min_level = struct.unpack_from("<I", entry_bytes, min_level_pos)[0]
    max_level = struct.unpack_from("<I", entry_bytes, max_level_pos)[0]

    # buff_data_list items occupy [body_start..min_level_pos].
    items_bytes = bytes(entry_bytes[head.body_start:min_level_pos])
    items_offset = head.body_start

    # Trailing fixed-width region. Order from spec is buff_level_-
    # calculate_type, ui_template_name, ui_component_name, elemental_-
    # status_info, is_use_skill_info_pattern_description, use_-
    # counting_by_global_timer.
    blct_pos = seq_data_pos + seq_len
    uit_pos = blct_pos + 1
    uic_pos = uit_pos + 4
    esi_pos = uic_pos + 4
    iuspd_pos = esi_pos + 4
    ucbgt_pos = iuspd_pos + 1

    blct = entry_bytes[blct_pos]
    uit = struct.unpack_from("<I", entry_bytes, uit_pos)[0]
    uic = struct.unpack_from("<I", entry_bytes, uic_pos)[0]
    esi = struct.unpack_from("<I", entry_bytes, esi_pos)[0]
    iuspd = entry_bytes[iuspd_pos]
    ucbgt = entry_bytes[ucbgt_pos]

    return BuffinfoEntry(
        entry_key=head.entry_key,
        name=head.name,
        is_blocked=head.is_blocked,
        is_blocked_offset=head.is_blocked_offset,
        buff_data_count=head.buff_data_count,
        buff_data_count_offset=head.buff_data_count_offset,
        buff_data_list_bytes=items_bytes,
        buff_data_list_offset=items_offset,
        min_level=min_level,
        min_level_offset=min_level_pos,
        max_level=max_level,
        max_level_offset=max_level_pos,
        sequencer_file_name=sequencer_name,
        sequencer_file_name_offset=seq_len_pos,
        buff_level_calculate_type=blct,
        buff_level_calculate_type_offset=blct_pos,
        ui_template_name=uit,
        ui_template_name_offset=uit_pos,
        ui_component_name=uic,
        ui_component_name_offset=uic_pos,
        elemental_status_info=esi,
        elemental_status_info_offset=esi_pos,
        is_use_skill_info_pattern_description=iuspd,
        is_use_skill_info_pattern_description_offset=iuspd_pos,
        use_counting_by_global_timer=ucbgt,
        use_counting_by_global_timer_offset=ucbgt_pos,
    )


def serialize_entry(entry: BuffinfoEntry) -> bytes:
    """Re-emit an entry's bytes from a decoded BuffinfoEntry.

    Used for round-trip verification and (eventually) write-back of
    intent-applied edits. Re-uses ``buff_data_list_bytes`` verbatim
    until the items decoder lands.
    """
    name_bytes = entry.name.encode("utf-8")
    seq_bytes = entry.sequencer_file_name.encode("utf-8")
    out = bytearray()
    out += struct.pack("<I", entry.entry_key)
    out += struct.pack("<I", len(name_bytes))
    out += name_bytes
    out += bytes([entry.is_blocked])
    out += struct.pack("<I", entry.buff_data_count)
    out += entry.buff_data_list_bytes
    out += struct.pack("<I", entry.min_level)
    out += struct.pack("<I", entry.max_level)
    out += struct.pack("<I", len(seq_bytes))
    out += seq_bytes
    out += bytes([entry.buff_level_calculate_type])
    out += struct.pack("<I", entry.ui_template_name)
    out += struct.pack("<I", entry.ui_component_name)
    out += struct.pack("<I", entry.elemental_status_info)
    out += bytes([entry.is_use_skill_info_pattern_description])
    out += bytes([entry.use_counting_by_global_timer])
    return bytes(out)


# Mapping of intent-path field names to a (offset_attr, width, dtype)
# triple. Width is the byte width on disk; dtype is a tag used by the
# intent expander to format the patched bytes correctly.
_WRAPPER_FIELDS: dict[str, tuple[str, int, str]] = {
    "is_blocked": ("is_blocked_offset", 1, "u8"),
    "buff_data_count": ("buff_data_count_offset", 4, "u32"),
    "min_level": ("min_level_offset", 4, "u32"),
    "max_level": ("max_level_offset", 4, "u32"),
    "buff_level_calculate_type":
        ("buff_level_calculate_type_offset", 1, "u8"),
    "ui_template_name": ("ui_template_name_offset", 4, "u32"),
    "ui_component_name": ("ui_component_name_offset", 4, "u32"),
    "elemental_status_info":
        ("elemental_status_info_offset", 4, "u32"),
    "is_use_skill_info_pattern_description":
        ("is_use_skill_info_pattern_description_offset", 1, "u8"),
    "use_counting_by_global_timer":
        ("use_counting_by_global_timer_offset", 1, "u8"),
}


def locate_buff_field(
    entry_bytes: bytes, field_path: str,
) -> tuple[int, int, str] | None:
    """Resolve a field path to ``(byte_offset, width, dtype)`` within
    an entry, or ``None`` if the path can't be resolved yet.

    Top-level wrapper fields (``min_level``, ``max_level``,
    ``ui_template_name``, ``elemental_status_info``, etc.) resolve
    via the decoded BuffinfoEntry. Nested paths into the
    ``buff_data_list`` items (``buff_data_list[i].data.base.X``)
    require the items decoder which isn't implemented yet , those
    return ``None``.
    """
    if "[" in field_path or "." in field_path:
        return None
    spec = _WRAPPER_FIELDS.get(field_path)
    if spec is None:
        return None
    offset_attr, width, dtype = spec
    entry = parse_entry(entry_bytes)
    return getattr(entry, offset_attr), width, dtype
