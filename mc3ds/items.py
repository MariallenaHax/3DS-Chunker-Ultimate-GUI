"""
Java item stack -> MC3DS ItemInstance NBT.

Game format (ItemInstance::load @ 0x1d1b30 / save @ 0x6b27b4):
  short  id       — numeric block/item id (NOT a string)
  short  Damage   — aux / durability
  byte   Count
  byte   Slot     — when inside a container list
  compound tag    — optional enchantments etc.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent / "data"

# Minimal PE/Bedrock 1.1-era item ids (blocks share 0-255). Extend via items.json.
_FALLBACK_ITEMS = {
    "minecraft:air": 0,
    "minecraft:stone": 1,
    "minecraft:grass_block": 2,
    "minecraft:dirt": 3,
    "minecraft:cobblestone": 4,
    "minecraft:oak_planks": 5,
    "minecraft:oak_sapling": 6,
    "minecraft:bedrock": 7,
    "minecraft:sand": 12,
    "minecraft:gravel": 13,
    "minecraft:oak_log": 17,
    "minecraft:oak_leaves": 18,
    "minecraft:glass": 20,
    "minecraft:lapis_block": 22,
    "minecraft:dispenser": 23,
    "minecraft:sandstone": 24,
    "minecraft:note_block": 25,
    "minecraft:powered_rail": 27,
    "minecraft:detector_rail": 28,
    "minecraft:sticky_piston": 29,
    "minecraft:cobweb": 30,
    "minecraft:piston": 33,
    "minecraft:white_wool": 35,
    "minecraft:dandelion": 37,
    "minecraft:poppy": 38,
    "minecraft:brown_mushroom": 39,
    "minecraft:red_mushroom": 40,
    "minecraft:gold_block": 41,
    "minecraft:iron_block": 42,
    "minecraft:tnt": 46,
    "minecraft:bookshelf": 47,
    "minecraft:obsidian": 49,
    "minecraft:torch": 50,
    "minecraft:chest": 54,
    "minecraft:diamond_block": 57,
    "minecraft:crafting_table": 58,
    "minecraft:furnace": 61,
    "minecraft:ladder": 65,
    "minecraft:rail": 66,
    "minecraft:lever": 69,
    "minecraft:redstone_torch": 76,
    "minecraft:snow": 80,
    "minecraft:ice": 79,
    "minecraft:cactus": 81,
    "minecraft:clay": 82,
    "minecraft:sugar_cane": 83,
    "minecraft:jukebox": 84,
    "minecraft:oak_fence": 85,
    "minecraft:pumpkin": 86,
    "minecraft:netherrack": 87,
    "minecraft:soul_sand": 88,
    "minecraft:glowstone": 89,
    "minecraft:jack_o_lantern": 91,
    "minecraft:barrier": 95,
    "minecraft:oak_trapdoor": 96,
    "minecraft:stone_bricks": 98,
    "minecraft:iron_bars": 101,
    "minecraft:glass_pane": 102,
    "minecraft:melon": 103,
    "minecraft:vine": 106,
    "minecraft:oak_fence_gate": 107,
    "minecraft:brick_stairs": 108,
    "minecraft:stone_brick_stairs": 109,
    "minecraft:mycelium": 110,
    "minecraft:lily_pad": 111,
    "minecraft:nether_bricks": 112,
    "minecraft:nether_brick_fence": 113,
    "minecraft:nether_brick_stairs": 114,
    "minecraft:enchanting_table": 116,
    "minecraft:end_portal_frame": 120,
    "minecraft:end_stone": 121,
    "minecraft:dragon_egg": 122,
    "minecraft:redstone_lamp": 123,
    "minecraft:dropper": 125,
    "minecraft:activator_rail": 126,
    "minecraft:oak_slab": 158,  # may vary — prefer blocks.json
    "minecraft:emerald_block": 133,
    "minecraft:beacon": 138,
    "minecraft:cobblestone_wall": 139,
    "minecraft:flower_pot": 140,
    "minecraft:anvil": 145,
    "minecraft:trapped_chest": 146,
    "minecraft:daylight_detector": 151,
    "minecraft:redstone_block": 152,
    "minecraft:hopper": 154,
    "minecraft:quartz_block": 155,
    "minecraft:stained_glass": 160,
    "minecraft:repeating_command_block": 188,
    "minecraft:chain_command_block": 189,
    "minecraft:grass_path": 198,
    "minecraft:end_rod": 208,
    "minecraft:concrete": 236,
    "minecraft:concrete_powder": 237,
    "minecraft:structure_block": 252,
    "minecraft:iron_shovel": 256,
    "minecraft:iron_pickaxe": 257,
    "minecraft:iron_axe": 258,
    "minecraft:flint_and_steel": 259,
    "minecraft:apple": 260,
    "minecraft:bow": 261,
    "minecraft:arrow": 262,
    "minecraft:coal": 263,
    "minecraft:charcoal": (263, 1),
    "minecraft:diamond": 264,
    "minecraft:iron_ingot": 265,
    "minecraft:gold_ingot": 266,
    "minecraft:iron_sword": 267,
    "minecraft:wooden_sword": 268,
    "minecraft:wooden_shovel": 269,
    "minecraft:wooden_pickaxe": 270,
    "minecraft:wooden_axe": 271,
    "minecraft:stone_sword": 272,
    "minecraft:stone_shovel": 273,
    "minecraft:stone_pickaxe": 274,
    "minecraft:stone_axe": 275,
    "minecraft:diamond_sword": 276,
    "minecraft:diamond_shovel": 277,
    "minecraft:diamond_pickaxe": 278,
    "minecraft:diamond_axe": 279,
    "minecraft:stick": 280,
    "minecraft:bowl": 281,
    "minecraft:mushroom_stew": 282,
    "minecraft:golden_sword": 283,
    "minecraft:golden_axe": 286,
    "minecraft:string": 287,
    "minecraft:feather": 288,
    "minecraft:gunpowder": 289,
    "minecraft:iron_hoe": 292,
    "minecraft:wheat_seeds": 295,
    "minecraft:wheat": 296,
    "minecraft:bread": 297,
    "minecraft:leather_helmet": 298,
    "minecraft:leather_chestplate": 299,
    "minecraft:leather_leggings": 300,
    "minecraft:leather_boots": 301,
    "minecraft:chainmail_helmet": 302,
    "minecraft:chainmail_chestplate": 303,
    "minecraft:chainmail_leggings": 304,
    "minecraft:chainmail_boots": 305,
    "minecraft:iron_helmet": 306,
    "minecraft:iron_chestplate": 307,
    "minecraft:iron_leggings": 308,
    "minecraft:iron_boots": 309,
    "minecraft:diamond_helmet": 310,
    "minecraft:diamond_chestplate": 311,
    "minecraft:diamond_leggings": 312,
    "minecraft:diamond_boots": 313,
    "minecraft:golden_helmet": 314,
    "minecraft:golden_chestplate": 315,
    "minecraft:golden_leggings": 316,
    "minecraft:golden_boots": 317,
    "minecraft:flint": 318,
    "minecraft:porkchop": 319,
    "minecraft:cooked_porkchop": 320,
    "minecraft:painting": 321,
    "minecraft:golden_apple": 322,
    "minecraft:oak_sign": 323,
    "minecraft:oak_door": 324,
    "minecraft:bucket": 325,
    "minecraft:water_bucket": 326,
    "minecraft:lava_bucket": 327,
    "minecraft:minecart": 328,
    "minecraft:saddle": 329,
    "minecraft:iron_door": 330,
    "minecraft:redstone": 331,
    "minecraft:snowball": 332,
    "minecraft:oak_boat": 333,
    "minecraft:leather": 334,
    "minecraft:milk_bucket": 335,
    "minecraft:brick": 336,
    "minecraft:clay_ball": 337,
    "minecraft:sugar_cane": 338,
    "minecraft:paper": 339,
    "minecraft:book": 340,
    "minecraft:slime_ball": 341,
    "minecraft:chest_minecart": 342,
    "minecraft:egg": 344,
    "minecraft:compass": 345,
    "minecraft:fishing_rod": 346,
    "minecraft:clock": 347,
    "minecraft:glowstone_dust": 348,
    "minecraft:cod": 349,
    "minecraft:cooked_cod": 350,
    "minecraft:ink_sac": 351,
    "minecraft:bone": 352,
    "minecraft:sugar": 353,
    "minecraft:cake": 354,
    "minecraft:bed": 355,
    "minecraft:repeater": 356,
    "minecraft:cookie": 357,
    "minecraft:map": 358,
    "minecraft:shears": 359,
    "minecraft:melon_slice": 360,
    "minecraft:pumpkin_seeds": 361,
    "minecraft:melon_seeds": 362,
    "minecraft:beef": 363,
    "minecraft:cooked_beef": 364,
    "minecraft:chicken": 365,
    "minecraft:cooked_chicken": 366,
    "minecraft:rotten_flesh": 367,
    "minecraft:ender_pearl": 368,
    "minecraft:blaze_rod": 369,
    "minecraft:ghast_tear": 370,
    "minecraft:gold_nugget": 371,
    "minecraft:nether_wart": 372,
    "minecraft:potion": 373,
    "minecraft:glass_bottle": 374,
    "minecraft:spider_eye": 375,
    "minecraft:fermented_spider_eye": 376,
    "minecraft:blaze_powder": 377,
    "minecraft:magma_cream": 378,
    "minecraft:brewing_stand": 379,
    "minecraft:cauldron": 380,
    "minecraft:ender_eye": 381,
    "minecraft:glistering_melon_slice": 382,
    "minecraft:experience_bottle": 384,
    "minecraft:fire_charge": 385,
    "minecraft:writable_book": 386,
    "minecraft:written_book": 387,
    "minecraft:emerald": 388,
    "minecraft:item_frame": 389,
    "minecraft:flower_pot": 390,
    "minecraft:carrot": 391,
    "minecraft:potato": 392,
    "minecraft:baked_potato": 393,
    "minecraft:poisonous_potato": 394,
    "minecraft:golden_carrot": 396,
    "minecraft:skeleton_skull": 397,
    "minecraft:carrot_on_a_stick": 398,
    "minecraft:nether_star": 399,
    "minecraft:pumpkin_pie": 400,
    "minecraft:firework_rocket": 401,
    "minecraft:firework_star": 402,
    "minecraft:enchanted_book": 403,
    "minecraft:comparator": 404,
    "minecraft:nether_brick": 405,
    "minecraft:quartz": 406,
    "minecraft:tnt_minecart": 407,
    "minecraft:hopper_minecart": 408,
    "minecraft:prismarine_shard": 409,
    "minecraft:hopper": 410,
    "minecraft:rabbit": 411,
    "minecraft:cooked_rabbit": 412,
    "minecraft:rabbit_stew": 413,
    "minecraft:rabbit_foot": 414,
    "minecraft:rabbit_hide": 415,
    "minecraft:leather_horse_armor": 416,
    "minecraft:iron_horse_armor": 417,
    "minecraft:golden_horse_armor": 418,
    "minecraft:diamond_horse_armor": 419,
    "minecraft:lead": 420,
    "minecraft:name_tag": 421,
    "minecraft:prismarine_crystals": 422,
    "minecraft:mutton": 423,
    "minecraft:cooked_mutton": 424,
    "minecraft:armor_stand": 425,
    "minecraft:end_crystal": 426,
    "minecraft:spruce_door": 427,
    "minecraft:birch_door": 428,
    "minecraft:jungle_door": 429,
    "minecraft:acacia_door": 430,
    "minecraft:dark_oak_door": 431,
    "minecraft:chorus_fruit": 432,
    "minecraft:popped_chorus_fruit": 433,
    "minecraft:beetroot": 434,
    "minecraft:beetroot_seeds": 435,
    "minecraft:beetroot_soup": 436,
    "minecraft:dragon_breath": 437,
    "minecraft:splash_potion": 438,
    "minecraft:spectral_arrow": 439,
    "minecraft:tipped_arrow": 440,
    "minecraft:lingering_potion": 441,
    "minecraft:shield": 442,
    "minecraft:elytra": 443,
    "minecraft:totem_of_undying": 450,
    "minecraft:shulker_shell": 445,  # may differ by build
    "minecraft:iron_nugget": 452,
    "minecraft:enchanted_golden_apple": 466,
}

POTION_DAMAGE = {
    "minecraft:water": 0,
    "minecraft:mundane": 1,
    "minecraft:long_mundane": 2,
    "minecraft:thick": 3,
    "minecraft:awkward": 4,
    "minecraft:night_vision": 5,
    "minecraft:long_night_vision": 6,
    "minecraft:invisibility": 7,
    "minecraft:long_invisibility": 8,
    "minecraft:leaping": 9,
    "minecraft:long_leaping": 10,
    "minecraft:strong_leaping": 11,
    "minecraft:fire_resistance": 12,
    "minecraft:long_fire_resistance": 13,
    "minecraft:swiftness": 14,
    "minecraft:long_swiftness": 15,
    "minecraft:strong_swiftness": 16,
    "minecraft:slowness": 17,
    "minecraft:long_slowness": 18,
    "minecraft:water_breathing": 19,
    "minecraft:long_water_breathing": 20,
    "minecraft:healing": 21,
    "minecraft:strong_healing": 22,
    "minecraft:harming": 23,
    "minecraft:strong_harming": 24,
    "minecraft:poison": 25,
    "minecraft:long_poison": 26,
    "minecraft:strong_poison": 27,
    "minecraft:regeneration": 28,
    "minecraft:long_regeneration": 29,
    "minecraft:strong_regeneration": 30,
    "minecraft:strength": 31,
    "minecraft:long_strength": 32,
    "minecraft:strong_strength": 33,
    "minecraft:weakness": 34,
    "minecraft:long_weakness": 35,
    "water": 0,
    "mundane": 1,
    "long_mundane": 2,
    "thick": 3,
    "awkward": 4,
    "night_vision": 5,
    "long_night_vision": 6,
    "invisibility": 7,
    "long_invisibility": 8,
    "leaping": 9,
    "long_leaping": 10,
    "strong_leaping": 11,
    "fire_resistance": 12,
    "long_fire_resistance": 13,
    "swiftness": 14,
    "long_swiftness": 15,
    "strong_swiftness": 16,
    "slowness": 17,
    "long_slowness": 18,
    "water_breathing": 19,
    "long_water_breathing": 20,
    "healing": 21,
    "strong_healing": 22,
    "harming": 23,
    "strong_harming": 24,
    "poison": 25,
    "long_poison": 26,
    "strong_poison": 27,
    "regeneration": 28,
    "long_regeneration": 29,
    "strong_regeneration": 30,
    "strength": 31,
    "long_strength": 32,
    "strong_strength": 33,
    "weakness": 34,
    "long_weakness": 35
}

def _nbt_value(value):
    return value.value if hasattr(value, "value") else value


def get_potion_type(stack: dict) -> str | None:
    tag = stack.get("tag") or {}
    potion = tag.get("Potion")

    if potion is not None:
        return str(_nbt_value(potion))

    components = stack.get("components") or {}
    contents = components.get("minecraft:potion_contents")

    if contents is not None:
        potion = contents.get("potion")
        if potion is not None:
            return str(_nbt_value(potion))

class ItemMapper:
    def __init__(self,blocks_json: Path | None = None,items_json: Path | None = None,) -> None:
        self._name_to_id: dict[str, tuple[int, int]] = {}

        for name, val in _FALLBACK_ITEMS.items():
            if isinstance(val, tuple):
                self._name_to_id[name] = (int(val[0]), int(val[1]))
            else:
                self._name_to_id[name] = (int(val), 0)

        path = items_json or (DATA / "items.json")

        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            items = raw.get("items", raw)

            for key, name in items.items():
                try:
                    item_id, meta = map(int, key.split(":"))
                except (ValueError, AttributeError):
                    continue

                self._name_to_id[str(name)] = (item_id, meta)

        path = blocks_json or (DATA / "blocks.json")

        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            blocks = raw.get("blocks", {})

            for key, value in blocks.items():
                try:
                    bid, meta = map(int, key.split(":"))
                except (ValueError, AttributeError):
                    continue

                name = str(value).split("[", 1)[0]

                self._name_to_id.setdefault(name, (bid, meta))

        extras = DATA / "items_extra.json"

        if extras.is_file():
            for name, val in json.loads(extras.read_text(encoding="utf-8")).items():
                if isinstance(val, list):
                    self._name_to_id[name] = (int(val[0]),int(val[1]))
                else:
                    self._name_to_id[name] = (int(val), 0)

        self.unmapped: dict[str, int] = {}

    def lookup(self, name: str, java_damage: int = 0) -> tuple[int, int]:
        name = name.strip()
        if not name.startswith("minecraft:"):
            name = "minecraft:" + name
        # direct
        if name in self._name_to_id:
            bid, meta = self._name_to_id[name]
            return bid, java_damage if java_damage else meta
        # strip blockstate
        bare = name.split("[", 1)[0]
        if bare in self._name_to_id:
            bid, meta = self._name_to_id[bare]
            return bid, java_damage if java_damage else meta
        self.unmapped[name] = self.unmapped.get(name, 0) + 1
        return 0, 0

    def convert_stack(self, stack: dict, slot: int | None = None) -> dict | None:
        """Java item compound -> MC3DS item compound. Returns None for empty."""
        if not stack:
            return None
        # Java 1.20+: id string; older: id short + Damage + Count
        name = stack.get("id") or stack.get("Name")
        count = int(stack.get("Count", stack.get("count", 1)))
        if count <= 0:
            return None
        damage = int(stack.get("Damage", stack.get("damage", 0)))
        if isinstance(name, int):
            item_id, meta = int(name), damage
        else:
            name = str(name)
            item_id, meta = self.lookup(name, damage)

            if name in ("minecraft:potion","minecraft:splash_potion","minecraft:lingering_potion","minecraft:tipped_arrow",):
                potion_type = get_potion_type(stack)

                if potion_type is not None:
                    meta = POTION_DAMAGE.get(potion_type, 0)
                    if name == "minecraft:tipped_arrow":
                        meta += 1
        
        if item_id == 0 and str(name) not in ("minecraft:air", "air", "0"):
            # keep a placeholder stone so the slot isn't silently empty? prefer skip
            return None
        if item_id == 0:
            return None
        out: dict[str, Any] = {
            "id": int(item_id),
            "Damage": int(meta),
            "Count": int(count) & 0xFF,
        }
        if slot is not None:
            out["Slot"] = int(slot) & 0xFF
        # preserve nested tag / components lightly
        tag = stack.get("tag") or stack.get("components")
        if isinstance(tag, dict) and tag:
            out["tag"] = self._convert_tag(tag)
        return out

    def _convert_tag(self, tag: dict) -> dict:
        # Pass through primitives; convert display/ench names when obvious
        out = {}
        for k, v in tag.items():
            if k == "ench" and isinstance(v, list):
                out["ench"] = v  # same shape in PE
            elif k == "display" and isinstance(v, dict):
                out["display"] = v
            elif isinstance(v, (int, float, str, list, dict, bool)):
                out[k] = v
        return out


_SIGN_JSON = re.compile(r'"text"\s*:\s*"((?:\\.|[^"\\])*)"')


def sign_line_to_plain(text: Any) -> str:
    """Java sign TextN may be JSON text component; Bedrock wants plain string."""
    if text is None:
        return ""
    s = str(text)
    if not s:
        return ""
    if s.startswith("{") or s.startswith("["):
        parts = _SIGN_JSON.findall(s)
        if parts:
            return "".join(
                p.encode("utf-8").decode("unicode_escape") if "\\" in p else p for p in parts
            )
        # empty json component
        if '"text":""' in s or s in ("{}", "[]"):
            return ""
    # Java sometimes stores a raw JSON string: "Hello"
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        inner = s[1:-1]
        try:
            return inner.encode("utf-8").decode("unicode_escape") if "\\" in inner else inner
        except Exception:
            return inner
    return s
