"""
Java entities -> MC3DS chunk entity NBT (SECTION_ENTITIES).

Dump-backed (Entity::save @ 0x5f5cac / saveWithoutId @ 0x5ea2c8):
  id            int   EntityType (NOT a string identifier)
  definitions   list  of strings (component toggles; we emit [\"+\", \"minecraft:...\"] style)
  UniqueID      long
  Pos           list of 3 doubles
  Rotation      list of 2 floats
  Motion        list of 3 doubles (optional)
  CustomName, CustomNameVisible, FallDistance, Fire, OnGround, Invulnerable, ...

Item frames are BlockEntityType 0x10 — handled in block_entities, not here.
ArmorStand is absent from this 1.9.19 build.
"""
from __future__ import annotations

import json
import itertools
from pathlib import Path
from typing import Any

from .items import ItemMapper, sign_line_to_plain

DATA = Path(__file__).resolve().parent / "data"

_DIM_IDS = {
    "0": 0,
    "overworld": 0,
    "minecraft:overworld": 0,
    "-1": 1,
    "nether": 1,
    "the_nether": 1,
    "minecraft:the_nether": 1,
    "1": 2,
    "end": 2,
    "the_end": 2,
    "minecraft:the_end": 2,
}


def dimension_id(value, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower()
    if s in _DIM_IDS:
        return _DIM_IDS[s]
    if s.endswith(":overworld"):
        return 0
    if s.endswith(":the_nether") or s.endswith(":nether"):
        return 1
    if s.endswith(":the_end") or s.endswith(":end"):
        return 2
    try:
        return int(s)
    except ValueError:
        return default

# PE entity type packing: (family << 8) | legacy_id — from dump + EntityTypeToString.
# Full ids verified by extract / Map Bedrock entity subagent where noted.
_BUILTIN: dict[str, dict[str, Any]] = {
    # Passive / players
    "minecraft:player": {"type_id": 0x013F, "save_name": "Player"},
    # Passive-ish
    "minecraft:villager": {"type_id": 0x030F, "save_name": "Villager"},
    "minecraft:snow_golem": {"type_id": 0x0315, "save_name": "snow_golem"},
    "minecraft:iron_golem": {"type_id": 0x0314, "save_name": "IronGolem"},
    # Monsters — full EntityType ids from dump (variant getters pack extra high bits).
    # Creeper/Slime/… verified via EntityTypeToString; Zombie/Skeleton/Husk/Stray via getters.
    "minecraft:zombie": {"type_id": 0x30B20, "save_name": "Zombie"},
    "minecraft:creeper": {"type_id": 0x0B21, "save_name": "Creeper"},
    "minecraft:skeleton": {"type_id": 0x110B22, "save_name": "Skeleton"},
    "minecraft:spider": {"type_id": 0x0B23, "save_name": "Spider"},
    "minecraft:zombie_pigman": {"type_id": 0x0B24, "save_name": "PigZombie"},
    "minecraft:zombified_piglin": {"type_id": 0x0B24, "save_name": "PigZombie"},
    "minecraft:slime": {"type_id": 0x0B25, "save_name": "Slime"},
    "minecraft:enderman": {"type_id": 0x0B26, "save_name": "Enderman"},
    "minecraft:silverfish": {"type_id": 0x0B27, "save_name": "Silverfish"},
    "minecraft:cave_spider": {"type_id": 0x0B28, "save_name": "CaveSpider"},
    "minecraft:ghast": {"type_id": 0x0B29, "save_name": "Ghast"},
    "minecraft:magma_cube": {"type_id": 0x0B2A, "save_name": "magma_cube"},
    "minecraft:blaze": {"type_id": 0x0B2B, "save_name": "Blaze"},
    "minecraft:zombie_villager": {"type_id": 0x0B2C, "save_name": "ZombieVillager"},
    "minecraft:witch": {"type_id": 0x0B2D, "save_name": "Witch"},
    "minecraft:stray": {"type_id": 0x110B2E, "save_name": "Stray"},
    "minecraft:husk": {"type_id": 0x30B2F, "save_name": "Husk"},
    "minecraft:wither_skeleton": {"type_id": 0x110B30, "save_name": "WitherSkeleton"},
    "minecraft:guardian": {"type_id": 0x0B31, "save_name": "Guardian"},
    "minecraft:elder_guardian": {"type_id": 0x0B32, "save_name": "ElderGuardian"},
    "minecraft:wither": {"type_id": 0x0B34, "save_name": "Wither"},
    "minecraft:ender_dragon": {"type_id": 0x0B35, "save_name": "ender_dragon"},
    "minecraft:shulker": {"type_id": 0x0B36, "save_name": "Shulker"},
    "minecraft:endermite": {"type_id": 0x0B37, "save_name": "Endermite"},
    "minecraft:vindicator": {"type_id": 0x0B39, "save_name": "Vindicator"},
    "minecraft:evoker": {"type_id": 0x0B68, "save_name": "evocation_illager"},
    "minecraft:evocation_illager": {"type_id": 0x0B68, "save_name": "evocation_illager"},
    "minecraft:vex": {"type_id": 0x0B69, "save_name": "Vex"},
    # Animals (family 0x13)
    "minecraft:chicken": {"type_id": 0x130A, "save_name": "Chicken"},
    "minecraft:cow": {"type_id": 0x130B, "save_name": "Cow"},
    "minecraft:pig": {"type_id": 0x130C, "save_name": "Pig"},
    "minecraft:sheep": {"type_id": 0x130D, "save_name": "Sheep"},
    "minecraft:wolf": {"type_id": 0x130E, "save_name": "Wolf"},
    "minecraft:mooshroom": {"type_id": 0x1310, "save_name": "mooshroom"},
    "minecraft:squid": {"type_id": 0x2311, "save_name": "Squid"},
    "minecraft:rabbit": {"type_id": 0x1312, "save_name": "Rabbit"},
    "minecraft:bat": {"type_id": 0x1313, "save_name": "Bat"},
    "minecraft:ocelot": {"type_id": 0x1316, "save_name": "Ocelot"},
    "minecraft:cat": {"type_id": 0x1316, "save_name": "Ocelot"},
    "minecraft:horse": {"type_id": 0x1317, "save_name": "Horse"},
    "minecraft:donkey": {"type_id": 0x1318, "save_name": "Donkey"},
    "minecraft:mule": {"type_id": 0x1319, "save_name": "Mule"},
    "minecraft:skeleton_horse": {"type_id": 0x131A, "save_name": "SkeletonHorse"},
    "minecraft:zombie_horse": {"type_id": 0x131B, "save_name": "ZombieHorse"},
    "minecraft:polar_bear": {"type_id": 0x131C, "save_name": "polar_bear"},
    "minecraft:llama": {"type_id": 0x131D, "save_name": "Llama"},
    # Objects — dump-verified where noted (Item 0x40, Painting 0x400053, MinecartChest 0x80062)
    "minecraft:item": {"type_id": 0x0040, "save_name": "Item"},
    "minecraft:tnt": {"type_id": 0x0041, "save_name": "PrimedTnt"},
    "minecraft:falling_block": {"type_id": 0x0042, "save_name": "FallingSand"},
    "minecraft:xp_orb": {"type_id": 0x0045, "save_name": "XPOrb"},
    "minecraft:experience_orb": {"type_id": 0x0045, "save_name": "XPOrb"},
    "minecraft:painting": {"type_id": 0x400053, "save_name": "Painting"},
    "minecraft:arrow": {"type_id": 0x0050, "save_name": "Arrow"},
    "minecraft:snowball": {"type_id": 0x0051, "save_name": "Snowball"},
    "minecraft:egg": {"type_id": 0x0052, "save_name": "ThrownEgg"},
    "minecraft:ender_pearl": {"type_id": 0x0057, "save_name": "ThrownEnderpearl"},
    "minecraft:eye_of_ender": {"type_id": 0x0046, "save_name": "EyeOfEnderSignal"},
    "minecraft:boat": {"type_id": 0x005A, "save_name": "Boat"},
    "minecraft:oak_boat": {"type_id": 0x005A, "save_name": "Boat"},
    "minecraft:minecart": {"type_id": 0x80054, "save_name": "Minecart"},
    "minecraft:chest_minecart": {"type_id": 0x80062, "save_name": "MinecartChest"},
    "minecraft:hopper_minecart": {"type_id": 0x80060, "save_name": "MinecartHopper"},
    "minecraft:tnt_minecart": {"type_id": 0x80061, "save_name": "MinecartTNT"},
    "minecraft:furnace_minecart": {"type_id": 0x80055, "save_name": "MinecartFurnace"},
}


class EntityMapper:
    def __init__(self) -> None:
        self._by_ident: dict[str, dict[str, Any]] = dict(_BUILTIN)
        path = DATA / "entity_types.json"
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for ident, info in raw.get("by_identifier", {}).items():
                self._by_ident[ident] = info
        self._uid = itertools.count(1)
        self.unmapped: dict[str, int] = {}

    def lookup(self, java_id: str) -> dict[str, Any] | None:
        name = str(java_id or "").strip()
        if not name:
            return None
        if not name.startswith("minecraft:"):
            # Java 1.8- style: "Creeper" / "Pig"
            bare = name
            name = "minecraft:" + bare.lower().replace(" ", "_")
            # also try PascalCase save name match
            for info in self._by_ident.values():
                if info.get("save_name") == bare:
                    return info
        info = self._by_ident.get(name)
        if info:
            return info
        # strip path variants
        bare = name.split(":", 1)[-1]
        for key, info in self._by_ident.items():
            if key.endswith(":" + bare) or info.get("save_name", "").lower() == bare:
                return info
        self.unmapped[name] = self.unmapped.get(name, 0) + 1
        return None

    def next_unique_id(self) -> int:
        return next(self._uid)


def _f64_list(values, n: int, default: float = 0.0) -> list[float]:
    out = []
    for i in range(n):
        try:
            out.append(float(values[i]))
        except Exception:
            out.append(default)
    return out


def _f32_list(values, n: int, default: float = 0.0) -> list[float]:
    return _f64_list(values, n, default)


def convert_entity(
    entity: dict,
    mapper: EntityMapper,
    item_mapper: ItemMapper | None = None,
    y_shift: int = 0,
) -> dict | None:
    """
    Full entity compound for CDB section 2 / .mc3w entity records.
    Returns None if the type is unknown or unsupported on 3DS.
    """
    raw_id = entity.get("id") or entity.get("Id") or entity.get("identifier") or ""
    # Skip players in chunk entity lists — they live in level.dat Player
    if str(raw_id).lower() in ("player", "minecraft:player"):
        return None

    info = mapper.lookup(str(raw_id))
    if not info:
        return None

    type_id = int(info["type_id"])
    save_name = str(info.get("save_name") or raw_id)
    ident = "minecraft:" + save_name.lower().replace(" ", "_")
    # Prefer canonical minecraft: from lookup key if available
    for k, v in mapper._by_ident.items():
        if v is info:
            ident = k
            break

    pos = entity.get("Pos") or [0.0, 0.0, 0.0]
    pos = _f64_list(pos, 3)
    pos[1] += float(y_shift)

    rot = entity.get("Rotation") or [0.0, 0.0]
    rot = _f32_list(rot, 2)

    motion = entity.get("Motion") or [0.0, 0.0, 0.0]
    motion = _f64_list(motion, 3)

    uuid_most = entity.get("UUIDMost")
    uuid_least = entity.get("UUIDLeast")

    if uuid_most is not None and uuid_least is not None:
        unique = ((int(uuid_most) & 0xFFFFFFFFFFFFFFFF) << 64) | (int(uuid_least) & 0xFFFFFFFFFFFFFFFF)
        unique &= 0xFFFFFFFFFFFFFFFF
    elif entity.get("UniqueID") is not None:
        unique = int(entity["UniqueID"])
    else:
        unique = mapper.next_unique_id()
        
    unique &= 0xFFFFFFFFFFFFFFFF
    if unique >= 0x8000000000000000:
        unique -= 0x10000000000000000

    result: dict[str, Any] = {
        "id": int(type_id),
        "definitions": [] if (type_id & 0xFF) == 0x53 else ["+" + ident],
        "UniqueID": int(unique),
        "Pos": pos,
        "Rotation": rot,
        "Motion": motion,
        "FallDistance": float(entity.get("FallDistance", 0.0)),
        "Fire": int(entity.get("Fire", 0)),
        "OnGround": int(bool(entity.get("OnGround", False))),
        "Invulnerable": int(bool(entity.get("Invulnerable", False))),
        "PortalCooldown": int(entity.get("PortalCooldown", 0)),
        "IsGlobal": 0,
        "IsAutonomous": 0,
        "LastDimensionId": dimension_id(entity.get("Dimension", entity.get("DimensionId", 0))),
    }

    custom = entity.get("CustomName")
    if custom is not None:
        result["CustomName"] = sign_line_to_plain(custom)
        result["CustomNameVisible"] = int(bool(entity.get("CustomNameVisible", True)))

    # --- Mob / animal extras ---
    source_attributes = entity.get("Attributes") or []
    healths = float(0)
    attribute_names = {
        "generic.maxHealth": "minecraft:health",
        "minecraft:generic.max_health": "minecraft:health",

        "generic.movementSpeed": "minecraft:movement",
        "generic.movement_speed": "minecraft:movement",
        "minecraft:generic.movement_speed": "minecraft:movement",

        "generic.knockbackResistance": "minecraft:knockback_resistance",
        "generic.knockback_resistance": "minecraft:knockback_resistance",
        "minecraft:generic.knockback_resistance": "minecraft:knockback_resistance",

        "generic.attackDamage": "minecraft:attack_damage",
        "generic.attack_damage": "minecraft:attack_damage",
        "minecraft:generic.attack_damage": "minecraft:attack_damage",

        "generic.armor": "minecraft:armor",
        "minecraft:generic.armor": "minecraft:armor",

        "generic.luck": "minecraft:luck",
        "minecraft:generic.luck": "minecraft:luck",
    }

    converted_attributes = {}

    for attr in source_attributes:
        if not isinstance(attr, dict):
            continue

        source_name = attr.get("Name")
        target_name = attribute_names.get(source_name)

        if target_name is None:
            continue

        base = attr.get("Base")
        if base is None:
            base = attr.get("Current")

        if base is None:
            continue
        if source_name == "generic.maxHealth" or source_name == "minecraft:generic.max_health":
            healths = float(attr.get("Max", base))
        converted_attributes[target_name] = {
            "Base": float(base),
            "Current": float(base),
            "Max": float(attr.get("Max", base)),
            "Name": target_name,
        }

    health = entity.get("Health")

    if health is not None:
        health = float(health)
        converted_attributes["minecraft:health"] = {
            "Name": "minecraft:health",
            "Base": healths,
            "Current": health,
            "Max": healths,
        }

    if converted_attributes:
        result["Attributes"] = list(converted_attributes.values())

    if "HurtTime" in entity:
        result["HurtTime"] = int(entity["HurtTime"])
    if "DeathTime" in entity:
        result["DeathTime"] = int(entity["DeathTime"])
    if "PersistenceRequired" in entity:
        result["Persistent"] = int(bool(entity["PersistenceRequired"]))
    else:
        result["Persistent"] = 1

    # Sheep color / sheared
    if (type_id & 0xFF) == 0x0D and (type_id >> 8) == 0x13:
        if "Color" in entity:
            result["Color"] = int(entity["Color"]) & 0xFF
        if "Sheared" in entity:
            result["Sheared"] = int(bool(entity["Sheared"]))

    # Creeper (match on low byte — high bits vary by variant packing)
    if (type_id & 0xFF) == 0x21:
        if "powered" in entity:
            result["IsPowered"] = int(bool(entity["powered"]))
        if "ExplosionRadius" in entity:
            result["ExplosionRadius"] = int(entity["ExplosionRadius"])
        if "Fuse" in entity:
            result["Fuse"] = int(entity["Fuse"])

    # Slime / magma size
    if (type_id & 0xFF) in (0x25, 0x2A) and "Size" in entity:
        result["Size"] = int(entity["Size"])

    # Wolf
    if (type_id & 0xFF) == 0x0E and (type_id >> 8) == 0x13:
        if "Angry" in entity:
            result["Angry"] = int(bool(entity["Angry"]))
        if "CollarColor" in entity:
            result["CollarColor"] = int(entity["CollarColor"])
        if "OwnerUUID" in entity or "Owner" in entity:
            result["OwnerNew"] = str(entity.get("OwnerUUID") or entity.get("Owner") or "")

    # Item entity
    if (type_id & 0xFF) == 0x40 and item_mapper is not None:
        item = entity.get("Item")
        if isinstance(item, dict):
            converted = item_mapper.convert_stack(item)
            if converted:
                result["Item"] = converted
        if "Age" in entity:
            result["Age"] = int(entity["Age"])
        if "PickupDelay" in entity:
            result["PickupDelay"] = int(entity["PickupDelay"])

    # Painting
    if (type_id & 0xFF) == 0x53:
        painting_names = {
            "kebab": "Kebab",
            "aztec": "Aztec",
            "alban": "Alban",
            "aztec2": "Aztec2",
            "bomb": "Bomb",
            "plant": "Plant",
            "wasteland": "Wasteland",
            "pool": "Pool",
            "courbet": "Courbet",
            "sea": "Sea",
            "sunset": "Sunset",
            "creebet": "Creebet",
            "wanderer": "Wanderer",
            "graham": "Graham",
            "match": "Match",
            "bust": "Bust",
            "stage": "Stage",
            "void": "Void",
            "skull_and_roses": "SkullAndRoses",
            "wither": "Wither",
        }

        motive = entity.get("Motive", entity.get("Variant", ""))
        motive = str(motive).replace("minecraft:", "")

        result["Motive"] = painting_names.get(
            motive, motive.capitalize()
        )
        pos[2] += float(1)
        for key in ("TileX", "TileY", "TileZ"):
            if key in entity:
                if key in ("TileZ"):
                    result[key] = int(entity[key] + 1)
                else:
                    result[key] = int(entity[key])

        facing = entity.get(
            "Facing",
            entity.get("Direction", entity.get("facing"))
        )

        if facing is not None:
            result["Direction"] = int(facing) & 0xFF

        result["Dir"] = int(entity.get("Dir", 0)) & 0xFF

    # Minecart with chest / hopper — Items
    if (type_id & 0xFF) in (0x62, 0x60) and item_mapper is not None:
        items = entity.get("Items") or []
        out_items = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            slot = entry.get("Slot")
            converted = item_mapper.convert_stack(entry, int(slot) if slot is not None else None)
            if converted:
                out_items.append(converted)
        if out_items:
            result["Items"] = out_items

    # Armor on mobs
    if item_mapper is not None and "ArmorItems" in entity:
        armor = []
        for entry in entity["ArmorItems"] or []:
            if isinstance(entry, dict):
                converted = item_mapper.convert_stack(entry)
                armor.append(converted or {"id": 0, "Damage": 0, "Count": 0})
            else:
                armor.append({"id": 0, "Damage": 0, "Count": 0})
        if any(a.get("id") for a in armor):
            result["Armor"] = armor

    if item_mapper is not None and "HandItems" in entity:
        hands = entity["HandItems"] or []
        if hands and isinstance(hands[0], dict):
            main = item_mapper.convert_stack(hands[0])
            if main:
                result["Mainhand"] = main

    return result
