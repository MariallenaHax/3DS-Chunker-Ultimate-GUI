from __future__ import annotations

import struct
from typing import Iterable, Optional


TAG_DATA2D = 0x2D
TAG_SUBCHUNK_PREFIX = 0x2F
TAG_BLOCK_ENTITY = 0x31
TAG_ENTITY = 0x32
TAG_TICK = 0x33
TAG_CHUNK_STATE = 0x36
TAG_CHUNK_VERSION = 0x76

SUBCHUNK_VERSION_OLD = 0

BLOCKS_SIZE = 4096
NIBBLES_SIZE = 2048

SUBCHUNK_SIZE = 1 + BLOCKS_SIZE + NIBBLES_SIZE

CHUNK_STATE_VALUE = b"\x02\x00\x00\x00"
CHUNK_VERSION_VALUE = b"\x04"



class BedrockWriteError(ValueError):
    pass


def _i32(value: int) -> bytes:
    if not -(2**31) <= int(value) < 2**31:
        raise BedrockWriteError(f"chunk coordinate out of int32 range: {value}")
    return struct.pack("<i", int(value))

def chunk_record_key(
    chunk_x: int,
    chunk_z: int,
    tag: int,
    dimension: int = 0,
) -> bytes:
    if tag not in (
        TAG_BLOCK_ENTITY,
        TAG_ENTITY,
        TAG_TICK,
    ):
        raise BedrockWriteError(
            f"Unsupported chunk record tag: {tag:#x}"
        )

    return chunk_prefix(
        chunk_x,
        chunk_z,
        dimension,
    ) + bytes((tag,))

def chunk_prefix(chunk_x: int, chunk_z: int, dimension: int = 0) -> bytes:
    """
    Bedrock chunk key prefix.
    Overworld: x(int32 LE) + z(int32 LE)
    Nether/End: x + z + dimension(int32 LE), where 1=Nether, 2=End.
    """
    prefix = _i32(chunk_x) + _i32(chunk_z)
    if dimension:
        if dimension not in (1, 2):
            raise BedrockWriteError(
                "dimension must be 0 (Overworld), 1 (Nether), or 2 (End)"
            )
        prefix += _i32(dimension)
    return prefix


def subchunk_key(
    chunk_x: int, chunk_z: int, subchunk_y: int, dimension: int = 0
) -> bytes:
    if not -128 <= int(subchunk_y) <= 127:
        raise BedrockWriteError(f"subchunk Y out of int8 range: {subchunk_y}")
    return chunk_prefix(chunk_x, chunk_z, dimension) + bytes(
        (TAG_SUBCHUNK_PREFIX, subchunk_y & 0xFF)
    )


def data2d_key(chunk_x: int, chunk_z: int, dimension: int = 0) -> bytes:
    return chunk_prefix(chunk_x, chunk_z, dimension) + bytes((TAG_DATA2D,))


def pack_nibbles(values: bytes) -> bytes:
    """Pack 4096 metadata/light values (0..15), low nibble first."""
    if len(values) != BLOCKS_SIZE:
        raise BedrockWriteError(f"expected 4096 nibble values, got {len(values)}")
    out = bytearray(NIBBLES_SIZE)
    for i in range(0, BLOCKS_SIZE, 2):
        a, b = values[i], values[i + 1]
        if a > 15 or b > 15:
            raise BedrockWriteError("nibble values must be in range 0..15")
        out[i // 2] = a | (b << 4)
    return bytes(out)


def chunk_state_key(
    chunk_x: int,
    chunk_z: int,
    dimension: int = 0,
) -> bytes:
    return chunk_prefix(
        chunk_x,
        chunk_z,
        dimension,
    ) + bytes([TAG_CHUNK_STATE])


def chunk_version_key(
    chunk_x: int,
    chunk_z: int,
    dimension: int = 0,
) -> bytes:
    return chunk_prefix(
        chunk_x,
        chunk_z,
        dimension,
    ) + bytes([TAG_CHUNK_VERSION])


def build_subchunk(
    block_ids: bytes,
    block_data: bytes,
) -> bytes:
    """Build a PE 1.1.5 legacy SubChunk."""

    if len(block_ids) != BLOCKS_SIZE:
        raise ValueError(
            f"Invalid block IDs size: {len(block_ids)}"
        )

    if len(block_data) != NIBBLES_SIZE:
        raise ValueError(
            f"Invalid block data size: {len(block_data)}"
        )

    return (
        bytes([SUBCHUNK_VERSION_OLD])
        + block_ids
        + block_data
    )



def build_data2d(heightmap: Optional[bytes], biomes: Optional[bytes]) -> bytes:
    """
    Build old Data2D value: 256 little-endian uint16 height values + 256
    one-byte biome IDs. Heightmap is zeroed if not supplied.
    """
    heightmap = bytes(512) if heightmap is None else bytes(heightmap)
    biomes = bytes(256) if biomes is None else bytes(biomes)
    if len(heightmap) != 512:
        raise BedrockWriteError("heightmap must be 512 bytes (256 little-endian uint16)")
    if len(biomes) != 256:
        raise BedrockWriteError("biomes must be 256 bytes")
    return heightmap + biomes


def put_chunk(
    db,
    chunk_x: int,
    chunk_z: int,
    subchunks: Iterable[bytes],
    biomes: Optional[bytes] = None,
    heightmap: Optional[bytes] = None,
    dimension: int = 0,
) -> int:
    """
    Write one chunk's subchunks and Data2D record into an open DB.

    `db` must expose put(bytes_key, bytes_value). This method only updates the
    records it owns; unrelated world/player/entity records remain untouched.
    Returns the number of subchunks written.
    """
    prefix = chunk_prefix(chunk_x, chunk_z, dimension)
    count = 0
    for subchunk_y, payload in enumerate(subchunks):
        if subchunk_y > 127:
            raise BedrockWriteError("too many subchunks for signed int8 Y")
        value = bytes(payload)
        if len(value) != SUBCHUNK_SIZE or value[0] != SUBCHUNK_VERSION_OLD:
            raise BedrockWriteError(
                f"subchunk {subchunk_y} must be a version-0 payload "
                f"of {SUBCHUNK_SIZE} bytes"
            )
        
        key = prefix + bytes((TAG_SUBCHUNK_PREFIX, subchunk_y & 0xFF))
        db.put(key, value)
        count += 1

    data2d = build_data2d(heightmap, biomes)
    key = prefix + bytes((TAG_DATA2D,))
    db.put(key, data2d)
    return count

def put_chunk_from_mc3ds_payload(
    db,
    chunk_x: int,
    chunk_z: int,
    payloads: Iterable[bytes],
    biomes: Optional[bytes] = None,
    dimension: int = 0,
) -> int:
    """
    Convert MC3DS terrain payloads (4096 IDs + 2048 metadata + 2048 sky +
    2048 block light, without the MC3DS version byte) into MCPE 1.1.5
    SubChunkPrefix values. The block-array ordering is preserved.
    """
    converted = []
    for payload in payloads:
        payload = bytes(payload)
        if len(payload) != BLOCKS_SIZE + 3 * NIBBLES_SIZE:
            raise BedrockWriteError(
                "MC3DS subchunk payload must be 10240 bytes "
                "(4096 IDs + 3 x 2048 nibble arrays)"
            )
        converted.append(bytes((SUBCHUNK_VERSION_OLD,)) + payload)
    return put_chunk(
        db, chunk_x, chunk_z, converted, biomes=biomes, dimension=dimension
    )
