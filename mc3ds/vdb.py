"""Reader and writer for MC3DS VDB files.

Index:
    Header: 6 little-endian uint32 values (0x18 bytes)
    Pointer array: pointer_count uint32 values
    Entries: 0x10C bytes each

Index entry:
    +0x000: key (0x20 bytes)
    +0x020: unknown data (0xE0 bytes)
    +0x100: slot (uint16)
    +0x102: page (uint16)
    +0x104: field0 (uint16)
    +0x106: field1 (uint16)
    +0x108: field2 (uint16)
    +0x10A: field3 (uint16)

Slot FileHeader:
    +0x000: something0 (uint16)
    +0x002: something1 (uint16)
    +0x004: subfileCount (uint32)
    +0x008: footerSize/dataOffset (uint32)
    +0x00C: subfileSize/pageSize (uint32)
    +0x010: unknown0 (uint32)

Record page:
    +0x000: parameters[8]
    +0x008: magic (uint32)
    +0x00C: nameSize (uint8)
    +0x00D: list[7]
    +0x014: unknown0 (uint32)
    +0x018: name[nameSize]
    +...  : unknown1 (uint16)
    +...  : unknown2 (uint16)
    +...  : payload[unknown1]
"""

from __future__ import annotations

import io
import os
import re
import struct

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INDEX_FILE = "newindex.vdb"

INDEX_HEADER_FORMAT = "<6I"
INDEX_HEADER_SIZE = 0x18
INDEX_ENTRY_SIZE = 0x10C

INDEX_ENTRY_KEY_OFFSET = 0x00
INDEX_ENTRY_KEY_SIZE = 0x20

INDEX_SLOT_OFFSET = 0x100
INDEX_PAGE_OFFSET = 0x102

INDEX_FIELD0_OFFSET = 0x104
INDEX_FIELD1_OFFSET = 0x106
INDEX_FIELD2_OFFSET = 0x108
INDEX_FIELD3_OFFSET = 0x10A

SLOT_HEADER_SIZE = 0x14

SLOT_PAGE_COUNT_OFFSET = 0x04
SLOT_DATA_OFFSET = 0x08
SLOT_PAGE_SIZE_OFFSET = 0x0C
SLOT_UNKNOWN_OFFSET = 0x10

RECORD_MAGIC = 0xABCDEF99

RECORD_PARAMETERS_OFFSET = 0x00
RECORD_MAGIC_OFFSET = 0x08
RECORD_NAME_SIZE_OFFSET = 0x0C
RECORD_LIST_OFFSET = 0x0D
RECORD_LIST_SIZE = 7
RECORD_UNKNOWN0_OFFSET = 0x14
RECORD_NAME_OFFSET = 0x18

DEFAULT_UNKNOWN0 = 4
DEFAULT_UNKNOWN2 = 0


class VDBError(Exception):
    """Raised when a VDB file is malformed or inconsistent."""


# ---------------------------------------------------------------------------
# Index metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IndexMetadata:
    field0: int
    field1: int
    field2: int = 0x0001
    field3: int = 0x8000


METADATA_NORMAL = IndexMetadata(
    field0=0x04FF,
    field1=0x0004,
)

METADATA_DIMENSION1 = IndexMetadata(
    field0=0x01FF,
    field1=0x0010,
    field2=0x0002,
)

METADATA_PLAYER = IndexMetadata(
    field0=0x01FF,
    field1=0x0008,
)

METADATA_MAP = IndexMetadata(
    field0=0x04FF,
    field1=0x0080,
)


def get_index_metadata(key: str) -> IndexMetadata:
    if key.startswith("map_"):
        return METADATA_MAP

    if key == "dimension1":
        return METADATA_DIMENSION1

    if key == "~local_player" or key.startswith("player_"):
        return METADATA_PLAYER

    return METADATA_NORMAL


def get_record_unknown0(key: str) -> int:
    """Return the observed record-header unknown0 for known keys."""

    known = {
        "AutonomousEntities": 8,
        "mVillages": 8,
        "BiomeData": 4,
        "Overworld": 4,
        "dimension0": 4,
        "Nether": 2,
        "TheEnd": 2,
        "portals": 4,
        "~local_player": 1,
        "dimension1": 2,
        "dimension2": 2,
    }

    if key in known:
        return known[key]

    if key.startswith("map_"):
        return 1

    if key.startswith("player_"):
        return 1

    return DEFAULT_UNKNOWN0


def get_record_unknown2(key: str) -> int:
    """Return an observed unknown2 value.

    dimension1 has also been observed with value 1, so this is not
    treated as a map-only flag.
    """

    if key.startswith("map_"):
        return 1

    if key == "dimension1":
        return 1

    return DEFAULT_UNKNOWN2


def get_page_size_for_key(key: str) -> int:
    if key.startswith("map_"):
        return 0x20000

    if key == "~local_player" or key.startswith("player_"):
        return 0x2000

    if key == "dimension1":
        return 0x4000

    return 0x1000


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VDBEntry:
    key: str
    slot: int
    page: int

    field0: int
    field1: int
    field2: int
    field3: int

    @property
    def subfile(self) -> int:
        return self.page


@dataclass(frozen=True)
class VDBRecord:
    parameters: bytes
    name: str
    list_data: bytes
    unknown0: int
    unknown1: int
    unknown2: int
    payload: bytes


@dataclass
class TransferStats:
    copied: int = 0
    replaced: int = 0
    added: int = 0
    skipped_chunk_records: int = 0
    skipped_binary_keys: int = 0
    skipped_oversized_keys: int = 0
    skipped_oversized_values: int = 0


# ---------------------------------------------------------------------------
# VDB reader/writer
# ---------------------------------------------------------------------------

class VDB:
    """Read, replace, add, and delete records in an MC3DS VDB directory."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.index_path = self.directory / INDEX_FILE

        if not self.index_path.is_file():
            raise VDBError(
                f"VDB index not found: {self.index_path}"
            )

        self._index_blob = self.index_path.read_bytes()

        self._entries: dict[str, VDBEntry] = {}
        self._entry_offsets: dict[str, int] = {}

        self._slot_blobs: dict[int, bytes] = {}
        self._slot_page_sizes: dict[int, int] = {}
        self._slot_data_offsets: dict[int, int] = {}
        self._slot_page_counts: dict[int, int] = {}

        self.header_constant0 = 0
        self.entry_count = 0
        self.header_unknown0 = 0
        self.entry_size = INDEX_ENTRY_SIZE
        self.pointer_count = 0
        self.header_constant1 = 0

        self.pointers: list[int] = []
        self.entries_offset = 0

        self._parse_index()
    # ------------------------------------------------------------------
    # Index parsing
    # ------------------------------------------------------------------

    def _parse_index(self) -> None:
        blob = self._index_blob

        if len(blob) < INDEX_HEADER_SIZE:
            raise VDBError("newindex.vdb is too small")

        (
            self.header_constant0,
            self.entry_count,
            self.header_unknown0,
            self.entry_size,
            self.pointer_count,
            self.header_constant1,
        ) = struct.unpack_from(INDEX_HEADER_FORMAT, blob, 0)

        if self.entry_size != INDEX_ENTRY_SIZE:
            raise VDBError(
                f"Unsupported index entry size: 0x{self.entry_size:X}"
            )

        pointer_bytes = self.pointer_count * 4
        self.entries_offset = INDEX_HEADER_SIZE + pointer_bytes

        if self.entries_offset > len(blob):
            raise VDBError("Index pointer array is truncated")

        if self.pointer_count:
            self.pointers = list(
                struct.unpack_from(
                    f"<{self.pointer_count}I",
                    blob,
                    INDEX_HEADER_SIZE,
                )
            )
        else:
            self.pointers = []

        entries_end = (
            self.entries_offset
            + self.entry_count * self.entry_size
        )

        if entries_end > len(blob):
            raise VDBError(
                "Index entry array is truncated: "
                f"expected 0x{entries_end:X}, got 0x{len(blob):X}"
            )

        self._entries.clear()
        self._entry_offsets.clear()

        for i in range(self.entry_count):
            offset = self.entries_offset + i * self.entry_size
            raw = blob[offset:offset + self.entry_size]

            key_field = raw[
                INDEX_ENTRY_KEY_OFFSET:
                INDEX_ENTRY_KEY_OFFSET + INDEX_ENTRY_KEY_SIZE
            ]

            nul = key_field.find(b"\0")

            if nul < 0:
                raise VDBError(
                    f"Index entry {i} has no NUL-terminated key"
                )

            try:
                key = key_field[:nul].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise VDBError(
                    f"Invalid UTF-8 key in index entry {i}"
                ) from exc

            if not key:
                continue

            if key in self._entries:
                raise VDBError(f"Duplicate VDB key: {key!r}")

            slot, page = struct.unpack_from(
                "<HH",
                raw,
                INDEX_SLOT_OFFSET,
            )

            field0, field1, field2, field3 = struct.unpack_from(
                "<HHHH",
                raw,
                INDEX_FIELD0_OFFSET,
            )

            entry = VDBEntry(
                key=key,
                slot=slot,
                page=page,
                field0=field0,
                field1=field1,
                field2=field2,
                field3=field3,
            )

            # Validate the referenced record using the C structure.
            self._parse_record(slot, page, key)

            self._entries[key] = entry
            self._entry_offsets[key] = offset

    # ------------------------------------------------------------------
    # Slot loading
    # ------------------------------------------------------------------

    def _load_slot(self, slot: int) -> bytes:
        if slot not in self._slot_blobs:
            path = self.directory / f"slt{slot}.vdb"

            if not path.is_file():
                raise VDBError(f"Slot file not found: {path}")

            blob = path.read_bytes()

            if len(blob) < SLOT_HEADER_SIZE:
                raise VDBError(f"Slot file is too small: {path}")

            page_count = struct.unpack_from(
                "<I",
                blob,
                SLOT_PAGE_COUNT_OFFSET,
            )[0]

            data_offset = struct.unpack_from(
                "<I",
                blob,
                SLOT_DATA_OFFSET,
            )[0]

            page_size = struct.unpack_from(
                "<I",
                blob,
                SLOT_PAGE_SIZE_OFFSET,
            )[0]

            if data_offset < SLOT_HEADER_SIZE:
                raise VDBError(
                    f"Invalid slot data offset in {path.name}: "
                    f"0x{data_offset:X}"
                )

            if page_size <= 0:
                raise VDBError(
                    f"Invalid page size in {path.name}: {page_size}"
                )

            expected_size = data_offset + page_count * page_size

            if expected_size > len(blob):
                raise VDBError(
                    f"Slot file is truncated: {path.name}; "
                    f"expected 0x{expected_size:X}, "
                    f"got 0x{len(blob):X}"
                )

            self._slot_blobs[slot] = blob
            self._slot_page_sizes[slot] = page_size
            self._slot_data_offsets[slot] = data_offset
            self._slot_page_counts[slot] = page_count

        return self._slot_blobs[slot]

    def _get_page_count(self, slot: int) -> int:
        self._load_slot(slot)
        return self._slot_page_counts[slot]

    # ------------------------------------------------------------------
    # Record parsing
    # ------------------------------------------------------------------

    def _parse_record(
        self,
        slot: int,
        page: int,
        expected_key: str | None = None,
    ) -> VDBRecord:
        blob = self._load_slot(slot)

        page_size = self._slot_page_sizes[slot]
        data_offset = self._slot_data_offsets[slot]
        page_count = self._slot_page_counts[slot]

        if not 0 <= page < page_count:
            raise VDBError(
                f"Page {page} is outside slt{slot}.vdb"
            )

        # data_offset points to the magic, not the beginning
        # of the complete record header.
        page_start = data_offset + page * page_size - 8
        page_end = page_start + page_size

        if page_start < 0 or page_end > len(blob):
            raise VDBError(
                f"Page {page} exceeds slt{slot}.vdb"
            )

        magic = struct.unpack_from(
            "<I",
            blob,
            page_start + RECORD_MAGIC_OFFSET,
        )[0]

        if magic != RECORD_MAGIC:
            raise VDBError(
                f"Bad VDB magic for {expected_key!r}: "
                f"0x{magic:08X} "
                f"(slot={slot}, page={page}, "
                f"offset=0x{page_start + RECORD_MAGIC_OFFSET:X})"
            )

        name_size = blob[
            page_start + RECORD_NAME_SIZE_OFFSET
        ]

        list_start = page_start + RECORD_LIST_OFFSET
        list_end = list_start + RECORD_LIST_SIZE
        list_data = blob[list_start:list_end]

        unknown0 = struct.unpack_from(
            "<I",
            blob,
            page_start + RECORD_UNKNOWN0_OFFSET,
        )[0]

        name_start = page_start + RECORD_NAME_OFFSET
        name_end = name_start + name_size

        if name_end + 4 > page_end:
            raise VDBError(
                f"Record header exceeds page for {expected_key!r}"
            )

        name_bytes = blob[name_start:name_end]

        try:
            name = name_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VDBError(
                f"Invalid record name encoding for {expected_key!r}"
            ) from exc
            
        unknown1, unknown2 = struct.unpack_from("<HH", blob, name_end)

        payload_start = name_end + 4

        if unknown2 == 1:
            payload_size = unknown1 + 65536
        else:
            payload_size = unknown1

        payload_end = payload_start + payload_size

        if payload_end > page_end:
            raise VDBError(
                f"Record payload exceeds page for {name!r}"
            )

        if expected_key is not None and name != expected_key:
            raise VDBError(
                "Index/record key mismatch: "
                f"index={expected_key!r}, record={name!r}"
            )

        parameters = blob[page_start:page_start + 8]
        payload = blob[payload_start:payload_end]

        return VDBRecord(
            parameters=parameters,
            name=name,
            list_data=list_data,
            unknown0=unknown0,
            unknown1=unknown1,
            unknown2=unknown2,
            payload=payload,
        )

    def _record_location(
        self,
        slot: int,
        page: int,
    ) -> tuple[int, int, int]:

        blob = self._load_slot(slot)

        page_size = self._slot_page_sizes[slot]
        data_offset = self._slot_data_offsets[slot]

        page_start = data_offset + page * page_size - 8
        page_end = page_start + page_size

        name_size = blob[
            page_start + RECORD_NAME_SIZE_OFFSET
        ]

        payload_start = (
            page_start
            + RECORD_NAME_OFFSET
            + name_size
            + 4
        )

        return page_start, payload_start, page_end

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def keys(self) -> list[str]:
        return list(self._entries)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in self._entries

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def get_entry(self, key: str) -> VDBEntry:
        try:
            return self._entries[key]
        except KeyError:
            raise KeyError(key) from None

    def get_record(self, key: str) -> VDBRecord:
        entry = self.get_entry(key)
        return self._parse_record(
            entry.slot,
            entry.page,
            key,
        )

    def get(self, key: str) -> bytes:
        return self.get_record(key).payload

    # ------------------------------------------------------------------
    # Index serialization
    # ------------------------------------------------------------------

    def _get_index_trailer(self) -> bytes:
        entries_end = (
            self.entries_offset
            + self.entry_count * self.entry_size
        )

        return self._index_blob[entries_end:]

    def _get_raw_entries(self) -> list[bytes]:
        result = []

        for i in range(self.entry_count):
            offset = self.entries_offset + i * self.entry_size

            result.append(
                self._index_blob[
                    offset:offset + self.entry_size
                ]
            )

        return result

    def _build_index(
        self,
        entry_blobs: list[bytes],
    ) -> bytes:
        header = bytearray(
            self._index_blob[:INDEX_HEADER_SIZE]
        )

        struct.pack_into(
            "<I",
            header,
            0x04,
            len(entry_blobs),
        )

        # Pointer values are preserved exactly as read.
        pointer_blob = (
            struct.pack(
                f"<{len(self.pointers)}I",
                *self.pointers,
            )
            if self.pointers
            else b""
        )

        entries_blob = b"".join(entry_blobs)
        trailer = self._get_index_trailer()

        return (
            bytes(header)
            + pointer_blob
            + entries_blob
            + trailer
        )

    def _commit_index(self, new_index: bytes) -> None:
        self._atomic_replace(self.index_path, new_index)

        self._index_blob = new_index

        # Slot cache is retained; index parsing validates all entries.
        self._parse_index()

    # ------------------------------------------------------------------
    # Existing record replacement
    # ------------------------------------------------------------------

    def put(
        self,
        key: str,
        payload: bytes | bytearray | memoryview,
        *,
        record_type: int | None = None,
    ) -> None:
        """Replace an existing payload.

        record_type is accepted for compatibility with older callers,
        but the C VDBHeader structure contains no record_type field.
        It is therefore intentionally ignored.
        """

        if key not in self._entries:
            self.add(key, payload)
            return

        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes-like")

        payload = bytes(payload)

        entry = self.get_entry(key)
        record = self.get_record(key)

        blob = self._load_slot(entry.slot)
        page_size = self._slot_page_sizes[entry.slot]

        page_start = (
            self._slot_data_offsets[entry.slot]
            + entry.page * page_size - 8
        )
        page_end = page_start + page_size

        _, payload_start, _ = self._record_location(
            entry.slot,
            entry.page,
        )

        if payload_start + len(payload) > page_end:
            raise VDBError(
                f"Payload for {key!r} does not fit in its page"
            )

        updated_slot = bytearray(blob)

        # unknown1 is the uint16 payload size.
        name_end = (
            page_start
            + RECORD_NAME_OFFSET
            + len(key.encode("utf-8"))
        )
        unknown2 = get_record_unknown2(key)
        struct.pack_into(
            "<H",
            updated_slot,
            name_end,
            len(payload) - 65536 if unknown2 == 1 else len(payload)
        )

        # Clear the old payload and remaining page bytes.
        updated_slot[payload_start:page_end] = bytes(
            page_end - payload_start
        )

        updated_slot[
            payload_start:payload_start + len(payload)
        ] = payload

        slot_path = self.directory / f"slt{entry.slot}.vdb"

        self._atomic_replace(
            slot_path,
            bytes(updated_slot),
        )

        self._slot_blobs[entry.slot] = bytes(updated_slot)

    # ------------------------------------------------------------------
    # New record allocation
    # ------------------------------------------------------------------

    def _list_slot_paths(self) -> list[tuple[int, Path]]:
        result = []

        for path in self.directory.glob("slt*.vdb"):
            suffix = path.stem[3:]

            if suffix.isdigit():
                result.append((int(suffix), path))

        result.sort()
        return result

    def _find_empty_page(
        self,
        page_size: int,
    ) -> tuple[int, int] | None:
        """Find an entirely zeroed page of the requested size."""

        for slot, _ in self._list_slot_paths():
            blob = self._load_slot(slot)

            if self._slot_page_sizes[slot] != page_size:
                continue

            data_offset = self._slot_data_offsets[slot]
            page_count = self._slot_page_counts[slot]

            for page in range(page_count):
                start = data_offset + page * page_size - 8
                end = start + page_size

                if blob[start:end] == bytes(page_size):
                    return slot, page

        return None

    def _make_index_entry(
        self,
        key: str,
        slot: int,
        page: int,
    ) -> bytes:
        key_bytes = key.encode("utf-8")

        if len(key_bytes) > INDEX_ENTRY_KEY_SIZE - 1:
            raise VDBError(
                f"Key is too long for index: {key!r}"
            )

        # Copy the unknown region from an existing entry when possible.
        template = None

        metadata = get_index_metadata(key)

        for existing in self._get_raw_entries():
            field0, field1 = struct.unpack_from(
                "<HH",
                existing,
                INDEX_FIELD0_OFFSET,
            )

            if (field0, field1) == (
                metadata.field0,
                metadata.field1,
            ):
                template = existing
                break

        if template is None and self.entry_count:
            template = self._get_raw_entries()[0]

        entry = bytearray(
            template if template is not None
            else bytes(INDEX_ENTRY_SIZE)
        )

        entry[
            INDEX_ENTRY_KEY_OFFSET:
            INDEX_ENTRY_KEY_OFFSET + INDEX_ENTRY_KEY_SIZE
        ] = bytes(INDEX_ENTRY_KEY_SIZE)

        entry[
            INDEX_ENTRY_KEY_OFFSET:
            INDEX_ENTRY_KEY_OFFSET + len(key_bytes)
        ] = key_bytes

        struct.pack_into(
            "<HH",
            entry,
            INDEX_SLOT_OFFSET,
            slot,
            page,
        )

        struct.pack_into(
            "<HHHH",
            entry,
            INDEX_FIELD0_OFFSET,
            metadata.field0,
            metadata.field1,
            metadata.field2,
            metadata.field3,
        )

        return bytes(entry)


    def apply_mode(self, mode: str) -> list[str]:
        """
        Remove VDB records according to the selected conversion mode.

        keep_all:
            Keep every record.
        keep_biome:
            Keep only BiomeData records.
        clear_all:
            Remove every record.

        ~local_player is handled separately after this method.
        """
        if mode not in ("keep_all", "keep_biome", "clear_all"):
            raise ValueError(f"Invalid VDB mode: {mode!r}")

        if mode == "keep_all":
            return []

        if mode == "keep_biome":
            keep = {"BiomeData"}
        else:
            keep = set()

        removed = []

        for key in self.keys():
            if key not in keep:
                self.delete(key)
                removed.append(key)

        return removed

    def add(
        self,
        key: str,
        payload: bytes | bytearray | memoryview,
        *,
        record_type: int | None = None,
    ) -> VDBEntry:
        """Add a record using an existing empty page.

        New slot creation is deliberately disabled. The meaning of the
        index pointer array and the eight record parameter bytes must be
        established before creating new slot files safely.
        """

        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")

        if "\x00" in key:
            raise ValueError("VDB key cannot contain NUL")

        if key in self:
            raise VDBError(f"VDB key already exists: {key!r}")

        key_bytes = key.encode("utf-8")

        if len(key_bytes) > 0xFF:
            raise VDBError("Record name exceeds uint8 nameSize")

        if len(key_bytes) > INDEX_ENTRY_KEY_SIZE - 1:
            raise VDBError("Key is too long for the index")

        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes-like")

        payload = bytes(payload)

        page_size = get_page_size_for_key(key)

        page_header_size = (
            RECORD_NAME_OFFSET
            + len(key_bytes)
            + 4
        )

        if page_header_size + len(payload) > page_size:
            raise VDBError("Record does not fit in selected page size")

        empty = self._find_empty_page(page_size)

        if empty is None:
            template_path = None

            for slot, path in sorted(self._list_slot_paths()):
                blob = path.read_bytes()

                if len(blob) < SLOT_HEADER_SIZE:
                    continue

                slot_page_size = struct.unpack_from(
                    "<I", blob, SLOT_PAGE_SIZE_OFFSET
                )[0]

                if slot_page_size == page_size:
                    template_path = path
                    break

            if template_path is None:
                raise VDBError(
                    f"No template slot found for page size 0x{page_size:X}"
                )

            new_slot = max(
                (slot for slot, _ in self._list_slot_paths()),
                default=-1
            ) + 1

            new_path = self.directory / f"slt{new_slot}.vdb"
            template_data = template_path.read_bytes()

            new_path.write_bytes(template_data)

            self._slot_blobs.pop(new_slot, None)
            self._slot_page_sizes.pop(new_slot, None)
            self._slot_data_offsets.pop(new_slot, None)
            self._slot_page_counts.pop(new_slot, None)

            copied_data = new_path.read_bytes()
            
            copied_page_size = struct.unpack_from(
                "<I", new_path.read_bytes(), SLOT_PAGE_SIZE_OFFSET
            )[0]

            if copied_page_size != page_size:
                raise VDBError(
                    f"Cloned slot has invalid page size: "
                    f"0x{copied_page_size:X}, expected 0x{page_size:X}"
                )

            empty = (new_slot, 0)

        slot, page = empty

        blob = self._load_slot(slot)
        data_offset = self._slot_data_offsets[slot]

        page_start = data_offset + page * page_size - 8

        page_data = bytearray(page_size)

        if page == 0:
            page_data[:8] = blob[page_start:page_start + 8]

        # The eight parameter bytes are retained as zero for a new page.
        # Their nonzero values in existing records are not yet understood.
        page_data[0:8] = bytes(8)

        struct.pack_into(
            "<I",
            page_data,
            RECORD_MAGIC_OFFSET,
            RECORD_MAGIC,
        )

        page_data[RECORD_NAME_SIZE_OFFSET] = len(key_bytes)

        # The seven-byte list is unknown. Initialize to zero.
        page_data[
            RECORD_LIST_OFFSET:
            RECORD_LIST_OFFSET + RECORD_LIST_SIZE
        ] = bytes(RECORD_LIST_SIZE)

        unknown0 = get_record_unknown0(key)

        struct.pack_into(
            "<I",
            page_data,
            RECORD_UNKNOWN0_OFFSET,
            unknown0,
        )

        name_start = RECORD_NAME_OFFSET
        name_end = name_start + len(key_bytes)

        page_data[name_start:name_end] = key_bytes
        unknown2 = get_record_unknown2(key)
        if unknown2 == 1:
            unknown1 = len(payload) - 65536
        else:
            unknown1 = len(payload)
        struct.pack_into(
            "<HH",
            page_data,
            name_end,
            unknown1,
            unknown2,
        )

        payload_start = name_end + 4

        page_data[
            payload_start:payload_start + len(payload)
        ] = payload

        updated_slot = bytearray(blob)

        write_start = page_start + (8 if page == 0 else 0)
        write_data = page_data[8:] if page == 0 else page_data

        updated_slot[
            write_start:write_start + len(write_data)
        ] = write_data

        new_entry = self._make_index_entry(
            key,
            slot,
            page,
        )

        raw_entries = self._get_raw_entries()
        raw_entries.append(new_entry)

        new_index = self._build_index(raw_entries)

        slot_path = self.directory / f"slt{slot}.vdb"

        old_slot = blob

        self._atomic_replace(
            slot_path,
            bytes(updated_slot),
        )

        try:
            self._atomic_replace(
                self.index_path,
                new_index,
            )
        except OSError:
            self._atomic_replace(slot_path, old_slot)
            raise

        for old_key, old_entry in self._entries.items():
            if old_key == key:
                continue

            if old_entry.slot < 0 or old_entry.page < 0:
                raise VDBError(
                    f"Invalid existing entry: {old_key!r}"
                )
        self._slot_blobs[slot] = bytes(updated_slot)
        self._index_blob = new_index
        self._parse_index()

        return self._entries[key]

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(self, key: str) -> None:
        entry = self.get_entry(key)

        blob = self._load_slot(entry.slot)
        page_size = self._slot_page_sizes[entry.slot]
        data_offset = self._slot_data_offsets[entry.slot]

        page_start = data_offset + entry.page * page_size - 8
        page_end = page_start + page_size

        updated_slot = bytearray(blob)

        clear_start = page_start + (8 if entry.page == 0 else 0)

        updated_slot[clear_start:page_end] = bytes(
            page_end - clear_start
        )

        raw_entries = self._get_raw_entries()
        new_entries = []

        found = False

        for raw in raw_entries:
            key_field = raw[
                INDEX_ENTRY_KEY_OFFSET:
                INDEX_ENTRY_KEY_OFFSET + INDEX_ENTRY_KEY_SIZE
            ]

            nul = key_field.find(b"\0")

            candidate = (
                key_field[:nul].decode("utf-8")
                if nul >= 0
                else ""
            )

            if candidate == key:
                found = True
                continue

            new_entries.append(raw)

        if not found:
            raise VDBError(
                f"Could not find index entry for {key!r}"
            )

        new_index = self._build_index(new_entries)

        self._atomic_replace(
            self.index_path,
            new_index,
        )

        self._index_blob = new_index
        self._parse_index()

        slot_path = self.directory / f"slt{entry.slot}.vdb"

        self._atomic_replace(
            slot_path,
            bytes(updated_slot),
        )

        self._slot_blobs[entry.slot] = bytes(updated_slot)


    # ------------------------------------------------------------------
    # NBT helpers
    # ------------------------------------------------------------------

    def put_nbt(
        self,
        key: str,
        value,
        *,
        record_type: int | None = None,
    ) -> None:
        try:
            import nbtlib
        except ImportError as exc:
            raise VDBError("put_nbt requires nbtlib") from exc

        nbt_file = (
            value
            if isinstance(value, nbtlib.File)
            else nbtlib.File(value)
        )

        buffer = io.BytesIO()

        try:
            nbt_file.write(
                buffer,
                byteorder="little",
            )
        except Exception as exc:
            raise VDBError(
                f"Could not serialize NBT for {key!r}: {exc}"
            ) from exc

        if key in self:
            self.put(key, buffer.getvalue())
        else:
            self.add(key, buffer.getvalue())

    def get_nbt(self, key: str):
        try:
            import nbtlib
        except ImportError as exc:
            raise VDBError("get_nbt requires nbtlib") from exc

        try:
            return nbtlib.File.parse(
                io.BytesIO(self.get(key)),
                byteorder="little",
            )
        except Exception as exc:
            raise VDBError(
                f"Could not parse {key!r} as little-endian NBT: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Atomic file replacement
    # ------------------------------------------------------------------

    @staticmethod
    def _atomic_replace(path: Path, data: bytes) -> None:
        temp_path = path.with_name(path.name + ".tmp")

        try:
            with temp_path.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())

            os.replace(temp_path, path)

        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# PE 1.1.5 LevelDB transfer
# ---------------------------------------------------------------------------

PE_CHUNK_TAGS = {
    0x2D,
    0x2F,
    0x31,
    0x32,
    0x33,
    0x36,
    0x76,
}


def is_pe_chunk_record_key(key: bytes) -> bool:
    key = bytes(key)

    if not 9 <= len(key) <= 14:
        return False

    if len(key) in (9, 10):
        prefix_size = 8
    elif len(key) in (13, 14):
        prefix_size = 12
    else:
        return False

    tag = key[prefix_size]

    if tag not in PE_CHUNK_TAGS:
        return False

    if tag == 0x2F:
        return len(key) == prefix_size + 2

    return len(key) == prefix_size + 1


class VDBTransferError(RuntimeError):
    """Raised when a LevelDB record cannot be transferred safely."""


def import_leveldb_records(
    records: Iterable[tuple[bytes, bytes]],
    vdb: VDB,
    *,
    record_type_resolver: Callable[[str], int] | None = None,
    default_record_type: int = 2,
    warning: Callable[[str], None] | None = None,
) -> TransferStats:
    """Import non-chunk LevelDB records into an MC3DS VDB.

    record_type_resolver/default_record_type remain accepted for
    compatibility. The C VDB record structure has no record_type field,
    so these values are not written.
    """

    stats = TransferStats()

    pending_player: tuple[str, bytes] | None = None
    player_record_count = 0
    explicit_local_player_found = False

    def store_record(key: str, value: bytes) -> None:
        key_bytes = key.encode("utf-8")

        if not key or "\x00" in key:
            stats.skipped_binary_keys += 1

            if warning:
                warning(f"Skipped unsupported VDB key {key!r}")

            return

        if len(key_bytes) > INDEX_ENTRY_KEY_SIZE - 1:
            stats.skipped_oversized_keys += 1

            if warning:
                warning(
                    f"Skipped VDB key too long for index: {key!r}"
                )

            return

        try:
            if key in vdb:
                vdb.put(key, value)
                stats.replaced += 1
            else:
                vdb.add(key, value)
                stats.added += 1

            stats.copied += 1

        except (VDBError, OSError, ValueError, TypeError) as exc:
            raise VDBTransferError(
                f"Could not transfer LevelDB record "
                f"{key!r} ({len(value)} bytes): {exc}"
            ) from exc

    for raw_key, raw_value in records:
        key_bytes = bytes(raw_key)
        value = bytes(raw_value)

        if is_pe_chunk_record_key(key_bytes):
            stats.skipped_chunk_records += 1
            continue

        try:
            key = key_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            stats.skipped_binary_keys += 1
            continue

        if re.fullmatch(
            r"player_[0-9a-fA-F]{8}-"
            r"[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{12}",
            key,
        ):
            player_record_count += 1

            if pending_player is None:
                pending_player = (key, value)

            continue

        if key == "~local_player":
            explicit_local_player_found = True

        store_record(key, value)

    if not explicit_local_player_found and pending_player is not None:
        if player_record_count == 1:
            _, player_value = pending_player
            store_record("~local_player", player_value)

        elif warning:
            warning(
                "Skipped PE player records: found multiple "
                "player_<UUID> entries and no explicit "
                "~local_player record."
            )

    return stats


def export_vdb_records(
    vdb: VDB,
    leveldb_db,
    *,
    skip_keys: Iterable[str] = (),
) -> TransferStats:
    """Export VDB records into an open PE LevelDB database."""

    import nbtlib

    skipped = set(skip_keys)
    stats = TransferStats()

    for key in vdb.keys():
        if key in skipped:
            continue

        payload = vdb.get(key)

        # Set map scale to 4 before exporting to PE.
        if key.startswith("map_"):
            try:
                map_nbt = nbtlib.File.parse(
                    io.BytesIO(payload),
                    byteorder="little",
                )

                if "scale" in map_nbt:
                    map_nbt["scale"] = nbtlib.Byte(4)

                else:
                    raise VDBError(
                        f"Map record {key!r} has no scale field"
                    )

                buffer = io.BytesIO()
                map_nbt.write(
                    buffer,
                    byteorder="little",
                )
                payload = buffer.getvalue()

            except Exception as exc:
                raise VDBTransferError(
                    f"Could not update map scale for {key!r}: {exc}"
                ) from exc

        leveldb_db.put(
            key.encode("utf-8"),
            payload,
        )

        stats.copied += 1
        stats.replaced += 1

    return stats