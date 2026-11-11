
"""
PE 1.1.5 -> MC3DS chunk converter.

Converts legacy terrain, block entities, entities and pending ticks from the
Bedrock LevelDB chunk records into the corresponding MC3DS CDB sections.
The target MC3DS world's level.dat and db/vdb are retained.
"""
from __future__ import annotations

import shutil
import struct
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import cdb, leveldb115


class PEConversionError(RuntimeError):
    """Raised when a PE world cannot be converted to MC3DS."""


PE_TAG_DATA2D = 0x2D
PE_TAG_SUBCHUNK_PREFIX = 0x2F
PE_TAG_BLOCK_ENTITY = 0x31
PE_TAG_ENTITY = 0x32
PE_TAG_TICK = 0x33

MC3DS_SUBCHUNK_Y_MIN = 0
MC3DS_SUBCHUNK_Y_MAX = cdb.MAX_SUBCHUNKS - 1

ZERO_SUBCHUNK = bytes(cdb.SUBCHUNK_PAYLOAD_SIZE)
ZERO_HEIGHTMAP = bytes(cdb.HEIGHTMAP_SIZE)
ZERO_BIOMES = bytes(cdb.BIOMES_SIZE)


def _status(progress: Any, message: str) -> None:
    if progress is not None and hasattr(progress, "status"):
        progress.status(message)


def _warning(progress: Any, message: str) -> None:
    if progress is not None and hasattr(progress, "warning"):
        progress.warning(message)


def _update(progress: Any, current: int, total: int) -> None:
    if progress is not None and hasattr(progress, "update"):
        progress.update(current, total)


def _decode_chunk_key(key: bytes):
    """
    Decode a PE 1.1.5 chunk record key.

    Overworld prefix:
        x(int32 LE) + z(int32 LE), 8 bytes.

    Nether/End prefix:
        x + z + dimension(int32 LE), 12 bytes.
    """
    if len(key) in (9, 10):
        prefix_size = 8
        dimension = 0

    elif len(key) in (13, 14):
        prefix_size = 12
        dimension = struct.unpack_from("<i", key, 8)[0]

    else:
        return None

    chunk_x, chunk_z = struct.unpack_from("<ii", key, 0)
    tag = key[prefix_size]

    if tag == PE_TAG_DATA2D and len(key) == prefix_size + 1:
        return chunk_x, chunk_z, dimension, tag, None

    if tag == PE_TAG_SUBCHUNK_PREFIX and len(key) == prefix_size + 2:
        subchunk_y = struct.unpack("<b", key[-1:])[0]
        return chunk_x, chunk_z, dimension, tag, subchunk_y

    if (
        tag in (
            PE_TAG_BLOCK_ENTITY,
            PE_TAG_ENTITY,
            PE_TAG_TICK,
        )
        and len(key) == prefix_size + 1
    ):
        return chunk_x, chunk_z, dimension, tag, None

    return None


# ---------------------------------------------------------------------------
# LevelDB SST decoding
# ---------------------------------------------------------------------------

def _read_varint(data: bytes, pos: int):
    value = 0
    shift = 0

    while True:
        if pos >= len(data):
            raise PEConversionError(
                "Unexpected end of SST varint"
            )

        byte = data[pos]
        pos += 1

        value |= (byte & 0x7F) << shift

        if byte < 0x80:
            return value, pos

        shift += 7

        if shift >= 64:
            raise PEConversionError(
                "Invalid SST varint"
            )


def _read_block_handle(data: bytes, pos: int = 0):
    offset, pos = _read_varint(data, pos)
    size, pos = _read_varint(data, pos)

    return (offset, size), pos


def _snappy_decompress(data: bytes) -> bytes:
    """
    Decompress a Snappy block without requiring a LevelDB API.
    """
    expected, pos = _read_varint(data, 0)
    output = bytearray()

    while pos < len(data) and len(output) < expected:
        tag = data[pos]
        pos += 1

        kind = tag & 3

        if kind == 0:
            length = tag >> 2

            if length < 60:
                length += 1

            else:
                count = length - 59

                if pos + count > len(data):
                    raise PEConversionError(
                        "Invalid Snappy literal"
                    )

                length = int.from_bytes(
                    data[pos:pos + count],
                    "little",
                ) + 1

                pos += count

            if pos + length > len(data):
                raise PEConversionError(
                    "Truncated Snappy literal"
                )

            output.extend(data[pos:pos + length])
            pos += length

        elif kind == 1:
            length = 4 + ((tag >> 2) & 7)

            if pos >= len(data):
                raise PEConversionError(
                    "Truncated Snappy copy"
                )

            offset = (
                ((tag & 0xE0) << 3)
                | data[pos]
            )

            pos += 1

            if offset <= 0 or offset > len(output):
                raise PEConversionError(
                    "Invalid Snappy copy offset"
                )

            for _ in range(length):
                output.append(output[-offset])

        elif kind == 2:
            length = 1 + (tag >> 2)

            if pos + 2 > len(data):
                raise PEConversionError(
                    "Truncated Snappy copy"
                )

            offset = int.from_bytes(
                data[pos:pos + 2],
                "little",
            )

            pos += 2

            if offset <= 0 or offset > len(output):
                raise PEConversionError(
                    "Invalid Snappy copy offset"
                )

            for _ in range(length):
                output.append(output[-offset])

        else:
            length = 1 + (tag >> 2)

            if pos + 4 > len(data):
                raise PEConversionError(
                    "Truncated Snappy copy"
                )

            offset = int.from_bytes(
                data[pos:pos + 4],
                "little",
            )

            pos += 4

            if offset <= 0 or offset > len(output):
                raise PEConversionError(
                    "Invalid Snappy copy offset"
                )

            for _ in range(length):
                output.append(output[-offset])

    if len(output) != expected:
        raise PEConversionError(
            "Snappy output size mismatch"
        )

    return bytes(output)


def _decompress_sst_block(
    data: bytes,
    compression: int,
) -> bytes:
    """
    Decompress an SST block.

    Compression identifiers:
        0 = none
        1 = Snappy
        2 = Zlib
        4 = Raw Deflate
    """
    if compression == 0:
        return data

    if compression == 1:
        return _snappy_decompress(data)

    if compression == 2:
        return zlib.decompress(data)

    if compression == 4:
        return zlib.decompress(data, -15)

    raise PEConversionError(
        f"Unsupported SST compression: {compression}"
    )


def _read_sst_block(
    file,
    offset: int,
    size: int,
):
    """
    Read and decompress one SST block.

    Returns:
        (decompressed_data, compression_type)
    """
    file.seek(offset)

    block = file.read(size + 5)

    if len(block) != size + 5:
        raise PEConversionError(
            "Truncated SST block"
        )

    compression = block[size]

    raw = _decompress_sst_block(
        block[:size],
        compression,
    )

    return raw, compression


def _iter_sst_entries(raw: bytes):
    """
    Iterate prefix-compressed LevelDB block entries.

    Yields:
        key, value_offset, value_length, value
    """
    if len(raw) < 4:
        raise PEConversionError(
            "Invalid SST block"
        )

    restart_count = struct.unpack_from(
        "<I",
        raw,
        len(raw) - 4,
    )[0]

    entries_end = (
        len(raw)
        - 4
        - restart_count * 4
    )

    if restart_count < 1 or entries_end < 0:
        raise PEConversionError(
            "Invalid SST restart array"
        )

    pos = 0
    previous_key = b""

    while pos < entries_end:
        shared, pos = _read_varint(raw, pos)
        non_shared, pos = _read_varint(raw, pos)
        value_length, pos = _read_varint(raw, pos)

        if shared > len(previous_key):
            raise PEConversionError(
                "Invalid shared key length"
            )

        key_end = pos + non_shared
        value_end = key_end + value_length

        if value_end > entries_end:
            raise PEConversionError(
                "Truncated SST entry"
            )

        key = (
            previous_key[:shared]
            + raw[pos:key_end]
        )

        value_offset = key_end
        value = raw[value_offset:value_end]

        yield (
            key,
            value_offset,
            value_length,
            value,
        )

        previous_key = key
        pos = value_end


# ---------------------------------------------------------------------------
# LevelDB WAL decoding
# ---------------------------------------------------------------------------

WAL_BLOCK_SIZE = 32768
WAL_HEADER_SIZE = 7

WAL_FULL = 1
WAL_FIRST = 2
WAL_MIDDLE = 3
WAL_LAST = 4

def _make_crc32c_table():
    table = []

    for i in range(256):
        value = i

        for _ in range(8):
            if value & 1:
                value = (
                    value >> 1
                ) ^ 0x82F63B78
            else:
                value >>= 1

        table.append(value)

    return tuple(table)


_CRC32C_TABLE = _make_crc32c_table()


def _crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF

    for byte in data:
        crc = (
            _CRC32C_TABLE[
                (crc ^ byte) & 0xFF
            ]
            ^ (crc >> 8)
        )

    return crc ^ 0xFFFFFFFF


def _mask_crc32c(crc: int) -> int:
    return (
        (
            (crc >> 15)
            | (crc << 17)
        )
        + 0xA282EAD8
    ) & 0xFFFFFFFF


def _iter_wal_records(path: Path):
    """
    Read physical LevelDB log records and reconstruct logical records.

    Supports FULL, FIRST, MIDDLE and LAST fragments.
    A truncated final fragment is ignored because it may be a torn write.
    """
    assembling = False
    fragments = bytearray()

    with path.open("rb") as file:
        block_number = 0

        while True:
            block = file.read(WAL_BLOCK_SIZE)

            if not block:
                break

            block_length = len(block)
            pos = 0

            while pos < block_length:
                remaining = block_length - pos

                # The final bytes of a block may be padding.
                if remaining < WAL_HEADER_SIZE:
                    break

                header = block[
                    pos:
                    pos + WAL_HEADER_SIZE
                ]

                checksum, length, record_type = (
                    struct.unpack(
                        "<IHB",
                        header,
                    )
                )

                if (
                    checksum == 0
                    and length == 0
                    and record_type == 0
                ):
                    break

                if record_type not in (
                    WAL_FULL,
                    WAL_FIRST,
                    WAL_MIDDLE,
                    WAL_LAST,
                ):
                    raise PEConversionError(
                        f"Invalid WAL record type "
                        f"{record_type} in "
                        f"{path.name}, block "
                        f"{block_number}."
                    )

                if length > remaining - WAL_HEADER_SIZE:
                    if block_length < WAL_BLOCK_SIZE:
                        break

                    raise PEConversionError(
                        f"Invalid WAL record length "
                        f"in {path.name}, block "
                        f"{block_number}."
                    )

                fragment_start = (
                    pos + WAL_HEADER_SIZE
                )

                fragment_end = (
                    fragment_start + length
                )

                fragment = block[
                    fragment_start:
                    fragment_end
                ]

                actual_checksum = _mask_crc32c(
                    _crc32c(
                        bytes((record_type,))
                        + fragment
                    )
                )

                if actual_checksum != checksum:
                    raise PEConversionError(
                        f"WAL CRC32C mismatch in "
                        f"{path.name}, block "
                        f"{block_number}."
                    )

                if record_type == WAL_FULL:
                    if assembling:
                        raise PEConversionError(
                            f"Unexpected FULL WAL record "
                            f"in {path.name}."
                        )

                    yield fragment

                elif record_type == WAL_FIRST:
                    if assembling:
                        raise PEConversionError(
                            f"Unexpected FIRST WAL record "
                            f"in {path.name}."
                        )

                    fragments = bytearray(fragment)
                    assembling = True

                elif record_type == WAL_MIDDLE:
                    if not assembling:
                        raise PEConversionError(
                            f"Unexpected MIDDLE WAL record "
                            f"in {path.name}."
                        )

                    fragments.extend(fragment)

                elif record_type == WAL_LAST:
                    if not assembling:
                        raise PEConversionError(
                            f"Unexpected LAST WAL record "
                            f"in {path.name}."
                        )

                    fragments.extend(fragment)

                    yield bytes(fragments)

                    fragments.clear()
                    assembling = False

                pos = fragment_end

            block_number += 1

    # An unfinished final FIRST/MIDDLE sequence is a torn tail.
    # It is intentionally ignored.


def _parse_write_batch(data: bytes):
    """
    Decode one LevelDB WriteBatch.

    Yields:
        (sequence, value_type, key, value)

    value_type:
        1 = put
        0 = delete
    """
    if len(data) < 12:
        raise PEConversionError(
            "Invalid WAL WriteBatch header"
        )

    sequence = struct.unpack_from(
        "<Q",
        data,
        0,
    )[0]

    count = struct.unpack_from(
        "<I",
        data,
        8,
    )[0]

    pos = 12

    for index in range(count):
        if pos >= len(data):
            raise PEConversionError(
                "Truncated WAL WriteBatch"
            )

        value_type = data[pos]
        pos += 1

        if value_type not in (0, 1):
            raise PEConversionError(
                f"Unsupported WAL WriteBatch "
                f"record type: {value_type}"
            )

        key_length, pos = _read_varint(
            data,
            pos,
        )

        key_end = pos + key_length

        if key_end > len(data):
            raise PEConversionError(
                "Truncated WAL WriteBatch key"
            )

        key = data[pos:key_end]
        pos = key_end

        value = None

        if value_type == 1:
            value_length, pos = _read_varint(
                data,
                pos,
            )

            value_end = pos + value_length

            if value_end > len(data):
                raise PEConversionError(
                    "Truncated WAL WriteBatch value"
                )

            value = data[pos:value_end]
            pos = value_end

        yield (
            sequence + index,
            value_type,
            key,
            value,
        )

    if pos != len(data):
        raise PEConversionError(
            "Unexpected trailing bytes in WAL WriteBatch"
        )

# ---------------------------------------------------------------------------
# Direct SST reader
# ---------------------------------------------------------------------------

class PELevelDBSSTReader:
    """
    Read PE LevelDB SST files directly.

    The LevelDB C++ API is not used.

    Values are not retained in memory. Only record metadata and block
    locations are retained. Values are decompressed on demand.
    """

    MAGIC = bytes.fromhex(
        "57fb808b247547db"
    )

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

        self.records = {}
        self.positions = set()

        self.cache = OrderedDict()
        self.cache_limit = 4

        files = sorted(
            self.db_path.glob("*.ldb"),
            key=lambda path: int(path.stem),
        )

        if not files:
            raise PEConversionError(
                "No SST files found"
            )

        for path in files:
            self._scan_table(path)

        # Apply WAL records after scanning SST files.
        # Sequence numbers determine which version is newest.
        logs = sorted(
            (
                path
                for path in self.db_path.glob("*.log")
                if path.stat().st_size > 0
            ),
            key=lambda path: int(path.stem),
        )

        for path in logs:
            self._scan_log(path)

        # Discover chunk positions from the latest live records.
        for key, record in self.records.items():
            sequence, value_type = record[:2]

            if value_type != 1:
                continue

            decoded = _decode_chunk_key(key)

            if decoded is None:
                continue

            x, z, dimension, tag, subchunk_y = decoded

            if dimension in (0, 1, 2):
                self.positions.add(
                    (x, z, dimension)
                )

    def _scan_table(self, path: Path):
        """
        Scan the index block and all data blocks in one SST table.
        """
        with path.open("rb") as file:
            file.seek(0, 2)
            file_size = file.tell()

            if file_size < 48:
                raise PEConversionError(
                    f"Invalid SST file: {path.name}"
                )

            file.seek(-48, 2)
            footer = file.read(48)

            if footer[-8:] != self.MAGIC:
                raise PEConversionError(
                    f"Invalid SST magic: {path.name}"
                )

            # Footer contains:
            # metaindex block handle
            # index block handle
            # padding
            # magic number
            _, pos = _read_block_handle(
                footer,
                0,
            )

            index_handle, _ = _read_block_handle(
                footer,
                pos,
            )

            index_offset, index_size = index_handle

            index_raw, _ = _read_sst_block(
                file,
                index_offset,
                index_size,
            )

            # Each index entry contains a data block handle.
            for (
                _,
                _,
                _,
                handle_data,
            ) in _iter_sst_entries(index_raw):

                data_handle, _ = _read_block_handle(
                    handle_data
                )

                block_offset, block_size = data_handle

                data_raw, compression = _read_sst_block(
                    file,
                    block_offset,
                    block_size,
                )

                for (
                    internal_key,
                    value_offset,
                    value_length,
                    _,
                ) in _iter_sst_entries(data_raw):

                    if len(internal_key) < 8:
                        continue

                    # LevelDB internal key:
                    # user_key + 8-byte sequence/type.
                    user_key = internal_key[:-8]

                    if _decode_chunk_key(user_key) is None:
                        continue

                    sequence_type = struct.unpack(
                        "<Q",
                        internal_key[-8:],
                    )[0]

                    sequence = sequence_type >> 8
                    value_type = sequence_type & 0xFF

                    if value_type not in (0, 1):
                        continue

                    old = self.records.get(user_key)

                    # Keep the newest version of each key.
                    if (
                        old is None
                        or sequence > old[0]
                    ):
                        self.records[user_key] = (
                            sequence,
                            value_type,
                            str(path),
                            block_offset,
                            block_size,
                            compression,
                            value_offset,
                            value_length,
                        )

    def _scan_log(self, path: Path):
        """
        Scan a LevelDB WAL file and merge its WriteBatch records.

        WAL records are represented separately from SST block records.
        """
        for batch in _iter_wal_records(path):
            for (
                sequence,
                value_type,
                key,
                value,
            ) in _parse_write_batch(batch):

                if _decode_chunk_key(key) is None:
                    continue

                old = self.records.get(key)

                # Keep the newest version, including deletion records.
                if (
                    old is None
                    or sequence > old[0]
                ):
                    self.records[key] = (
                        sequence,
                        value_type,
                        "wal",
                        value,
                    )


    def get(self, key: bytes):
        """
        Retrieve one live user key from SST or WAL.
        """
        record = self.records.get(key)

        if record is None:
            return None

        sequence, value_type = record[:2]

        # A deletion record overrides older values.
        if value_type == 0:
            return None

        # WAL values are already decoded from WriteBatch.
        if record[2] == "wal":
            return record[3]

        (
            sequence,
            value_type,
            path,
            offset,
            size,
            compression,
            value_offset,
            value_length,
        ) = record

        cache_key = (
            path,
            offset,
            size,
            compression,
        )

        raw = self.cache.get(cache_key)

        if raw is None:
            with open(path, "rb") as file:
                raw, _ = _read_sst_block(
                    file,
                    offset,
                    size,
                )

            self.cache[cache_key] = raw

            while len(self.cache) > self.cache_limit:
                self.cache.popitem(
                    last=False
                )

        else:
            self.cache.move_to_end(cache_key)

        return raw[
            value_offset:
            value_offset + value_length
        ]


# ---------------------------------------------------------------------------
# PE key construction
# ---------------------------------------------------------------------------

def _make_pe_key(
    chunk_x: int,
    chunk_z: int,
    dimension: int,
    tag: int,
    subchunk_y: int | None = None,
) -> bytes:
    key = struct.pack(
        "<ii",
        chunk_x,
        chunk_z,
    )

    if dimension != 0:
        key += struct.pack(
            "<i",
            dimension,
        )

    key += bytes((tag,))

    if subchunk_y is not None:
        key += struct.pack(
            "<b",
            subchunk_y,
        )

    return key


# ---------------------------------------------------------------------------
# PE chunk reading
# ---------------------------------------------------------------------------

def _read_pe_chunk(
    reader,
    chunk_x: int,
    chunk_z: int,
    dimension: int,
    progress=None,
):
    chunk = {
        "subchunks": {},
        "heightmap": ZERO_HEIGHTMAP,
        "biomes": ZERO_BIOMES,
        "has_data2d": False,
        "block_entities": [],
        "entities": [],
        "ticks": [],
    }

    found = False

    def get_record(
        tag,
        subchunk_y=None,
    ):
        return reader.get(
            _make_pe_key(
                chunk_x,
                chunk_z,
                dimension,
                tag,
                subchunk_y,
            )
        )

    # Data2D
    value = get_record(
        PE_TAG_DATA2D
    )

    if value is not None:
        found = True

        expected = (
            cdb.HEIGHTMAP_SIZE
            + cdb.BIOMES_SIZE
        )

        if len(value) == expected:
            chunk["heightmap"] = (
                value[:cdb.HEIGHTMAP_SIZE]
            )

            chunk["biomes"] = (
                value[cdb.HEIGHTMAP_SIZE:]
            )

            chunk["has_data2d"] = True

        else:
            _warning(
                progress,
                f"Invalid Data2D at ({chunk_x}, {chunk_z}).",
            )

    # SubChunks
    for y in range(
        MC3DS_SUBCHUNK_Y_MIN,
        MC3DS_SUBCHUNK_Y_MAX + 1,
    ):
        value = get_record(
            PE_TAG_SUBCHUNK_PREFIX,
            y,
        )

        if value is None:
            continue

        found = True

        
        if len(value) != leveldb115.SUBCHUNK_SIZE:
            _warning(
                progress,
                f"Invalid SubChunk at ({chunk_x}, {chunk_z}, Y={y}): "
                f"expected {leveldb115.SUBCHUNK_SIZE}, got {len(value)}."
            )
            continue

        if value[0] != leveldb115.SUBCHUNK_VERSION_OLD:
            _warning(
                progress,
                f"Unsupported SubChunk version at "
                f"({chunk_x}, {chunk_z}, Y={y})."
            )
            continue

        legacy_payload = value[1:]

        if len(legacy_payload) == 6144:
            sky_light = bytes([0x0]) * 2048
            block_light = bytes(2048)

            mc3ds_payload = (
                legacy_payload
                + sky_light
                + block_light
            )

        elif len(legacy_payload) == cdb.SUBCHUNK_PAYLOAD_SIZE:
            mc3ds_payload = legacy_payload

        else:
            _warning(
                progress,
                f"Unsupported SubChunk payload size at "
                f"({chunk_x}, {chunk_z}, Y={y}): "
                f"{len(legacy_payload)} bytes."
            )
            continue

        chunk["subchunks"][y] = mc3ds_payload


    # Block Entities, Entities and Ticks
    for tag, name in (
        (
            PE_TAG_BLOCK_ENTITY,
            "block_entities",
        ),
        (
            PE_TAG_ENTITY,
            "entities",
        ),
        (
            PE_TAG_TICK,
            "ticks",
        ),
    ):
        value = get_record(tag)

        if value is None:
            continue

        found = True

        try:
            chunk[name].extend(
                cdb.read_nbt_stream(value)
            )

        except Exception as exc:
            _warning(
                progress,
                f"Could not read NBT record "
                f"0x{tag:02X} at "
                f"({chunk_x}, {chunk_z}): {exc}",
            )

    return chunk if found else None


# ---------------------------------------------------------------------------
# Terrain conversion
# ---------------------------------------------------------------------------

def _build_mc3ds_terrain(
    chunk: dict,
) -> bytes:
    """
    Build the uncompressed MC3DS terrain section for one PE chunk.
    """
    subchunks = chunk["subchunks"]

    count = (
        max(subchunks) + 1
        if subchunks
        else 0
    )

    raw = bytearray((count,))

    for subchunk_y in range(count):
        payload = subchunks.get(
            subchunk_y,
            ZERO_SUBCHUNK,
        )

        if (
            len(payload)
            != cdb.SUBCHUNK_PAYLOAD_SIZE
        ):
            raise PEConversionError(
                f"Internal SubChunk size error "
                f"at Y={subchunk_y}: {len(payload)}"
            )

        raw.append(
            cdb.SUBCHUNK_VERSION
        )

        raw.extend(payload)

    heightmap = bytes(
        chunk["heightmap"]
    )

    biomes = bytes(
        chunk["biomes"]
    )

    if len(heightmap) != cdb.HEIGHTMAP_SIZE:
        raise PEConversionError(
            "Internal heightmap size error"
        )

    if len(biomes) != cdb.BIOMES_SIZE:
        raise PEConversionError(
            "Internal biome array size error"
        )

    raw.extend(heightmap)
    raw.extend(biomes)

    return bytes(raw)


# ---------------------------------------------------------------------------
# Tick conversion
# ---------------------------------------------------------------------------

def _tick_int(
    tick: dict,
    *names: str,
):
    for name in names:
        value = tick.get(name)

        if value is not None:
            try:
                return int(value)

            except (TypeError, ValueError):
                pass

    return None


def _convert_ticks(
    source_ticks: list[dict],
    progress,
    chunk_x: int,
    chunk_z: int,
    dimension: int,
) -> list[dict]:
    """
    Map Bedrock pending-tick compounds to the MC3DS tickList schema.
    """
    converted = []

    for source in source_ticks:
        x = _tick_int(
            source,
            "x",
            "X",
        )

        y = _tick_int(
            source,
            "y",
            "Y",
        )

        z = _tick_int(
            source,
            "z",
            "Z",
        )

        block_id = _tick_int(
            source,
            "blockId",
            "blockID",
            "tileID",
            "id",
        )

        delay = _tick_int(
            source,
            "tickDelay",
            "time",
            "delay",
            "t",
        )

        if (
            x is None
            or y is None
            or z is None
            or block_id is None
        ):
            _warning(
                progress,
                f"Pending tick at chunk "
                f"({chunk_x}, {chunk_z}), "
                f"dimension {dimension} is missing "
                f"coordinates or block ID; skipped.",
            )

            continue

        if not (
            0 <= y < cdb.WORLD_HEIGHT
        ):
            continue

        converted.append({
            "x": x,
            "y": y,
            "z": z,
            "tileID": block_id,
            "time": (
                0 if delay is None else delay
            ),
        })

    return converted


# ---------------------------------------------------------------------------
# CDB writing
# ---------------------------------------------------------------------------

def _write_chunk(
    cdb_directory: Path,
    indexes,
    merged: dict,
    allocator,
    packed: int,
    terrain_raw: bytes,
    block_entities: list[dict] | None = None,
    entities: list[dict] | None = None,
    ticks: list[dict] | None = None,
) -> None:

    terrain_compressed = zlib.compress(
        terrain_raw
    )

    raw_be = cdb.write_nbt_stream(
        block_entities or []
    )

    be_compressed = (
        zlib.compress(raw_be)
        if raw_be
        else None
    )

    raw_entities = cdb.write_nbt_stream(
        entities or []
    )

    entities_compressed = (
        zlib.compress(raw_entities)
        if raw_entities
        else None
    )

    raw_ticks = cdb.write_tick_section(
        ticks or []
    )

    ticks_compressed = (
        zlib.compress(raw_ticks)
        if raw_ticks
        else None
    )

    if packed in merged:
        slot, subfile = merged[packed][:2]

    else:
        slot, subfile, _created = (
            allocator.allocate()
        )

    def make_blob(
        page_magic: int,
    ) -> bytes:
        return cdb.build_chunk_blob(
            packed,
            terrain_compressed,
            len(terrain_raw),
            be_compressed,
            len(raw_be),
            page_magic,
            entities_compressed,
            len(raw_entities),
            ticks_compressed,
            len(raw_ticks),
        )

    cdb.write_chunk_to_slot(
        cdb_directory,
        slot,
        subfile,
        make_blob,
    )

    for index in indexes:
        replaced = index.update_entry(
            packed,
            slot,
            subfile,
            1,
            0,
        )

        allocator.record(
            slot,
            subfile,
            replaced,
        )

    merged[packed] = (
        slot,
        subfile,
        1,
        0,
    )


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert(
    pe_world: Path,
    target_world: Path,
    output_world: Path,
    progress=None,
) -> int:
    """
    Insert PE 1.1.5 terrain into a copy of an existing MC3DS world.

    The target MC3DS world's level.dat and db/vdb are preserved.
    Its CDB indexes and slot files are updated with terrain read from PE.

    Returns:
        Number of chunks written.
    """
    pe_world = Path(pe_world)
    target_world = Path(target_world)
    output_world = Path(output_world)

    pe_db_path = pe_world / "db"
    target_cdb = (
        target_world
        / "db"
        / "cdb"
    )

    if (
        not (pe_world / "level.dat").is_file()
        or not pe_db_path.is_dir()
    ):
        raise PEConversionError(
            f"{pe_world} is not a PE world "
            f"(level.dat and db are required)"
        )

    if (
        not (target_world / "level.dat").is_file()
        or not target_cdb.is_dir()
    ):
        raise PEConversionError(
            f"{target_world} is not an MC3DS world "
            f"(level.dat and db/cdb are required)"
        )

    if output_world.exists():
        raise FileExistsError(
            f"Output world already exists: {output_world}"
        )

    if output_world.resolve() in (
        pe_world.resolve(),
        target_world.resolve(),
    ):
        raise PEConversionError(
            "The output folder must differ "
            "from both source worlds."
        )

    slot_files = cdb.list_slot_files(
        target_cdb
    )

    if not slot_files:
        raise PEConversionError(
            f"No slt*.cdb files found in {target_cdb}"
        )

    output_world.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    _status(
        progress,
        f"Copying MC3DS target world "
        f"{target_world.name}...",
    )

    shutil.copytree(
        target_world,
        output_world,
    )

    try:
        _status(
            progress,
            "Reading PE SST files...",
        )

        reader = PELevelDBSSTReader(
            pe_db_path
        )

        positions = reader.positions

        if not positions:
            raise PEConversionError(
                "No supported PE chunk records were found."
            )

        cdb_directory = (
            output_world
            / "db"
            / "cdb"
        )

        indexes = cdb.load_indexes(
            cdb_directory
        )

        for index in indexes:
            index.chunk_capacity = (
                indexes[0].chunk_capacity
            )

            index.constant1 = (
                indexes[0].constant1
            )

        output_slots = cdb.list_slot_files(
            cdb_directory
        )

        if not output_slots:
            raise PEConversionError(
                "Copied MC3DS world has no CDB slot files."
            )

        template = output_slots[0].read_bytes()

        merged = cdb.merged_index_map(
            indexes
        )

        allocator = cdb.SlotAllocator(
            indexes,
            cdb_directory,
            template,
        )

        total = len(positions)
        written = 0
        processed = 0

        for (
            chunk_x,
            chunk_z,
            dimension,
        ) in sorted(positions):

            processed += 1

            chunk = _read_pe_chunk(
                reader,
                chunk_x,
                chunk_z,
                dimension,
                progress,
            )

            if chunk is None:
                _update(
                    progress,
                    processed,
                    total,
                )

                continue

            packed = cdb.pack_position(
                chunk_x,
                chunk_z,
                dimension,
            )

            terrain_raw = (
                _build_mc3ds_terrain(chunk)
            )

            converted_ticks = _convert_ticks(
                chunk["ticks"],
                progress,
                chunk_x,
                chunk_z,
                dimension,
            )

            _write_chunk(
                cdb_directory,
                indexes,
                merged,
                allocator,
                packed,
                terrain_raw,
                chunk["block_entities"],
                chunk["entities"],
                converted_ticks,
            )

            written += 1

            _update(
                progress,
                processed,
                total,
            )

            _status(
                progress,
                f"Converted chunk "
                f"{processed}/{total}: "
                f"({chunk_x}, {chunk_z}), "
                f"dimension {dimension}",
            )

        _status(
            progress,
            "Writing MC3DS CDB indexes...",
        )

        for index in indexes:
            index.write()

        _status(
            progress,
            f"Completed PE → MC3DS: "
            f"{written} chunks.",
        )

        return written

    except BaseException:
        shutil.rmtree(
            output_world,
            ignore_errors=True,
        )

        raise
