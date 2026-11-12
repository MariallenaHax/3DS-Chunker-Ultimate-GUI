
# Licensed under the MIT License

from __future__ import annotations

import shutil
import struct
import zlib
from pathlib import Path

import leveldb

from . import cdb
from . import leveldb115
from .progress import Progress


class PEConversionError(Exception):
    pass


def iter_cdb_chunks(world_path: Path):
    """Read all available sections from MC3DS CDB chunks."""

    cdb_directory = Path(world_path) / "db" / "cdb"
    indexes = cdb.load_indexes(cdb_directory)
    merged = cdb.merged_index_map(indexes)

    slot_streams = {}
    slot_headers = {}

    try:
        for packed, location in merged.items():
            slot, subfile, _, _ = location

            if slot not in slot_streams:
                path = cdb.slot_path(cdb_directory, slot)
                slot_streams[slot] = open(path, "rb")
                slot_headers[slot] = cdb.read_slot_header(
                    slot_streams[slot]
                )

            stream = slot_streams[slot]

            (
                _,
                _,
                _,
                data_offset,
                subfile_size,
                _,
            ) = slot_headers[slot]

            stream.seek(data_offset + subfile * subfile_size)
            blob = stream.read(subfile_size)

            if len(blob) != subfile_size:
                raise PEConversionError(
                    f"Truncated CDB subfile: {slot}/{subfile}"
                )

            magic, stored_position = struct.unpack_from("<II", blob, 0)
            isNUMC = 0
            if magic == 0:
                continue

            if magic not in (cdb.MAGIC_CDB, cdb.ALT_MAGIC_CDB):
                if magic == cdb.NUMC_MAGIC_CDB:
                    isNUMC = 1
                else:
                    raise PEConversionError(
                        f"Invalid CDB magic: {magic:#x}"
                    )

            if stored_position != packed:
                raise PEConversionError(
                    f"Chunk position mismatch: {packed:#x}"
                )

            chunk_x, chunk_z, dimension = cdb.unpack_position(packed)

            sections = {}

            descriptor_base = 0x14 if isNUMC else 0x10

            for descriptor_number in range(cdb.SECTION_COUNT):
                descriptor_offset = (
                    descriptor_base
                    + descriptor_number
                    * struct.calcsize(cdb.SECTION_DESCRIPTOR_FORMAT)
                )

                (
                    section_index,
                    section_offset,
                    compressed_size,
                    raw_size,
                ) = struct.unpack_from(
                    cdb.SECTION_DESCRIPTOR_FORMAT,
                    blob,
                    descriptor_offset,
                )

                # Empty section.
                if section_index == -1 and compressed_size == 0:
                    continue

                if not 0 <= section_index < cdb.SECTION_COUNT:
                    raise PEConversionError(
                        f"Invalid section index {section_index} "
                        f"at ({chunk_x}, {chunk_z})"
                    )

                if compressed_size <= 0 or raw_size < 0:
                    raise PEConversionError(
                        f"Invalid section sizes at ({chunk_x}, {chunk_z}), "
                        f"section {section_index}: "
                        f"offset={section_offset:#x}, "
                        f"compressed_size={compressed_size:#x}, "
                        f"raw_size={raw_size:#x}, "
                        f"descriptor_offset={descriptor_offset:#x}"
                    )

                if section_index in sections:
                    raise PEConversionError(
                        f"Duplicate section {section_index} "
                        f"at ({chunk_x}, {chunk_z})"
                    )

                section_end = section_offset + compressed_size
                header_size = (
                    cdb.NUMC_CHUNK_HEADER_SIZE
                    if isNUMC
                    else cdb.CHUNK_HEADER_SIZE
                )

                if section_offset < header_size:
                    raise PEConversionError(
                        f"Invalid section offset at ({chunk_x}, {chunk_z})"
                    )

                if section_end > len(blob):
                    raise PEConversionError(
                        f"Section {section_index} exceeds subfile "
                        f"at ({chunk_x}, {chunk_z})"
                    )

                compressed = blob[section_offset:section_end]

                try:
                    raw = zlib.decompress(compressed)
                except zlib.error as exc:
                    raise PEConversionError(
                        f"Cannot decompress section {section_index} "
                        f"at ({chunk_x}, {chunk_z}): {exc}"
                    ) from exc

                if len(raw) != raw_size:
                    raise PEConversionError(
                        f"Section size mismatch at ({chunk_x}, {chunk_z}), "
                        f"section {section_index}: "
                        f"expected {raw_size}, got {len(raw)}"
                    )

                sections[section_index] = raw

            yield chunk_x, chunk_z, dimension, sections

    finally:
        for stream in slot_streams.values():
            stream.close()




def write_chunk_to_pe(
    db,
    chunk_x: int,
    chunk_z: int,
    dimension: int,
    sections: dict[int, bytes],
):
    """Write one MC3DS chunk into PE 1.1.5."""
    if dimension not in (0, 1, 2):
        raise PEConversionError(f"Unsupported dimension: {dimension}")
    written = 0
    terrain = sections.get(cdb.SECTION_TERRAIN)
    if terrain is not None:
        subchunks, heightmap, biomes = (cdb.parse_terrain_section(terrain))
        for subchunk_y, payload in enumerate(subchunks):
            if len(payload) != 10240:
                raise PEConversionError(f"Invalid MC3DS subchunk size: "f"{len(payload)} at Y={subchunk_y}")
            value = leveldb115.build_subchunk(payload[:4096],payload[4096:6144])
            key = leveldb115.subchunk_key(chunk_x,chunk_z,subchunk_y,dimension)
            db.put(key, value)
            written += 1
        db.put(leveldb115.data2d_key(chunk_x,chunk_z,dimension),leveldb115.build_data2d(heightmap, biomes))
        db.put(leveldb115.chunk_state_key(chunk_x,chunk_z,dimension),leveldb115.CHUNK_STATE_VALUE)
        db.put(leveldb115.chunk_version_key(chunk_x,chunk_z,dimension), leveldb115.CHUNK_VERSION_VALUE)
    block_entities = sections.get(cdb.SECTION_BLOCK_ENTITIES,b"")
    if block_entities:
        db.put(leveldb115.chunk_record_key(chunk_x,chunk_z,leveldb115.TAG_BLOCK_ENTITY,dimension),block_entities)
    entities = sections.get(cdb.SECTION_ENTITIES,b"")
    if entities:
        db.put(leveldb115.chunk_record_key(chunk_x,chunk_z,leveldb115.TAG_ENTITY,dimension),entities)
    ticks = sections.get(cdb.SECTION_TICKS,b"")
    if ticks:
        db.put(leveldb115.chunk_record_key(chunk_x,chunk_z,leveldb115.TAG_TICK,dimension),ticks)
    return written




def convert_3ds_to_pe(
    source_world: Path,
    output_world: Path,
    progress: Progress | None = None,
):
    source_world = Path(source_world)
    output_world = Path(output_world)
    if not (source_world / "db" / "cdb").is_dir():
        raise PEConversionError("MC3DS CDB directory not found")
    if output_world.exists():
        raise FileExistsError(f"Output world already exists: {output_world}")
    output_world.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_world / "level.dat",output_world / "level.dat")
    db_path = output_world / "db"
    db = leveldb.LevelDB(str(db_path),create_if_missing=True,compression_type=leveldb.CompressionType.NoCompression)
    chunk_count = 0
    subchunk_count = 0
    total_chunks = 0
    try:
        for _ in iter_cdb_chunks(source_world):
            total_chunks += 1
        for (chunk_x,chunk_z,dimension,sections) in iter_cdb_chunks(source_world):
            written = write_chunk_to_pe(db,chunk_x,chunk_z,dimension,sections)
            chunk_count += 1
            subchunk_count += written

            if progress is not None:
                progress.status(f"Converted chunk {chunk_x:d}, {chunk_z:d}...")
                progress.update(chunk_count, total_chunks)
    finally:
        db.close()
    if progress is not None:
        progress.status("Conversion completed.")
        progress.status(f"Chunks: {chunk_count}")
        progress.status(f"Subchunks: {subchunk_count}")
    return chunk_count
