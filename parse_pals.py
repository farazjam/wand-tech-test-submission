import json
import re
import sys
from collections import defaultdict
from spawn_clusters import build_spawn_locations

# --- Constants ---
# Rows starting with these prefixes are never emitted (raid/gym-mode reskins)
OUTPUT_SKIP    = ("RAID_", "GYM_")
# Rows starting with these prefixes cannot be the canonical paldex entry of a tribe
CANONICAL_SKIP = ("BOSS_", "RAID_", "GYM_", "Quest_")

MAX_DROP_SLOTS       = 10
DEFAULT_SPAWN_RADIUS = 15000
CLI_LIMIT_MAX        = 100  # values above this are treated as "all tribes"

# --- Pre-compiled regex for clean_string ---
_RE_CHAR_TAG   = re.compile(r'<characterName id=\|([^|]+)\|/>')
_RE_ITEM_TAG   = re.compile(r'<itemName id=\|([^|]+)\|/>')
_RE_OTHER_TAG  = re.compile(r'<\w+ id=\|([^|]+)\|/>')
_RE_WHITESPACE = re.compile(r'(\\[rnt]|\s)+')


def strip_prefix(value):
    """Strip Unreal enum prefix (e.g. 'EPalSizeType::') from a value string."""
    if "::" in value:
        return value.split("::")[-1]
    return value


def get_text(table, key):
    """Return the SourceString from a localisation table entry, or None."""
    return (table.get(key) or {}).get("TextData", {}).get("SourceString")


def clean_string(value, name_rows=None, item_name_rows=None):
    """Normalise whitespace and resolve in-string XML-style entity tags."""
    if not value:
        return value
    if name_rows:
        def resolve_name(m):
            return (name_rows.get(f"PAL_NAME_{m.group(1)}") or {}).get("TextData", {}).get("SourceString") or m.group(1)
        value = _RE_CHAR_TAG.sub(resolve_name, value)
    if item_name_rows:
        def resolve_item(m):
            return (item_name_rows.get(f"ITEM_NAME_{m.group(1)}") or {}).get("TextData", {}).get("SourceString") or m.group(1)
        value = _RE_ITEM_TAG.sub(resolve_item, value)
    value = _RE_OTHER_TAG.sub(lambda m: m.group(1), value)
    return _RE_WHITESPACE.sub(' ', value).strip()


def _find_tribe_canonical(members):
    """
    Return the (row_name, row) of the canonical paldex entry within a tribe, or None.

    The canonical entry is IsPal=True with a positive ZukanIndex and no excluded
    prefix.  When multiple qualify (e.g. same tribe holds suffix="" and suffix="B"
    entries), the one with the empty suffix is preferred.
    """
    candidates = [
        (rn, row) for rn, row in members
        if (row.get("IsPal") is True
            and row.get("ZukanIndex", -1) > 0
            and not any(rn.startswith(p) for p in CANONICAL_SKIP))
    ]
    if not candidates:
        return None
    # Empty suffix first (primary entry), then alphabetically by row name
    candidates.sort(key=lambda item: (item[1].get("ZukanIndexSuffix") or "", item[0]))
    return candidates[0]


def load_sources():
    """
    Load all source JSON tables and build the tribe-based pal index.

    Design:
      - Every row in DT_PalMonsterParameter_Common is grouped by its Tribe field
        (EPalTribeID::X  →  "X").  This naturally clusters canonical pals with
        their quest variants, boss variants, and raid/gym reskins — no pattern
        matching on row names is needed.
      - Tribes are sorted by their canonical pal's (ZukanIndex, ZukanSuffix),
        so the paldex order is preserved and A/B pairs stay together.
      - Within a tribe members are ordered: canonical → suffix paldex variants
        → quest variants → boss variants (raid/gym retained but skipped on output).

    Returns (sorted_tribes, tribe_members, sources):
      - sorted_tribes:  tribe names in paldex order
      - tribe_members:  {tribe_name: [(row_name, row), ...]} in emit order
      - sources:        all lookup tables used by build_entry
    """
    def rows(filename):
        with open(filename, encoding="utf-8") as f:
            return json.load(f)[0]["Rows"]

    def rows_optional(filename):
        try:
            return rows(filename)
        except FileNotFoundError:
            return {}

    pal_rows = rows("DT_PalMonsterParameter_Common.json")

    # --- Group every row by tribe ---
    tribe_members = defaultdict(list)
    for row_name, row in pal_rows.items():
        tribe = strip_prefix(row.get("Tribe") or "")
        if tribe:
            tribe_members[tribe].append((row_name, row))

    # --- Identify the canonical entry for each tribe ---
    # tribe_canonical[tribe_name] = (ZukanIndex, ZukanSuffix, canonical_row_name)
    tribe_canonical = {}
    for tribe, members in tribe_members.items():
        result = _find_tribe_canonical(members)
        if result:
            row_name, row = result
            tribe_canonical[tribe] = (row["ZukanIndex"], row.get("ZukanIndexSuffix") or "", row_name)

    # Keep only tribes that have a canonical paldex entry; sort by (index, suffix)
    sorted_tribes = sorted(
        (t for t in tribe_members if t in tribe_canonical),
        key=lambda t: tribe_canonical[t][:2],
    )

    # --- Sort members within each tribe ---
    def _member_sort_key(item):
        row_name, row = item
        if any(row_name.startswith(p) for p in OUTPUT_SKIP):
            return (9, "", row_name)           # RAID_/GYM_ — kept but skipped on output
        if row_name.startswith("BOSS_"):
            return (3, "", row_name)           # boss variants last
        if row_name.startswith("Quest_"):
            return (2, "", row_name)           # quest variants before bosses
        if row.get("IsPal") is True and row.get("ZukanIndex", -1) > 0:
            suf = row.get("ZukanIndexSuffix") or ""
            return (0 if not suf else 1, suf, row_name)   # canonical (suf="") then paldex suffixes
        return (2, "", row_name)               # other misc entries

    for tribe in tribe_members:
        tribe_members[tribe].sort(key=_member_sort_key)

    # --- Other lookup tables ---
    waza_table_index = {}
    for entry in rows("DT_WazaDataTable_Common.json").values():
        raw_id = entry.get("WazaType") or ""
        if raw_id:
            waza_table_index[strip_prefix(raw_id)] = entry

    icon_rows = {k.lower(): v for k, v in rows("DT_partnerSkillIconDataTable.json").items()}

    waza_by_pal = defaultdict(list)
    for entry in rows("DT_WazaMasterLevel_Common.json").values():
        pal_id = entry.get("PalId")
        if pal_id:
            waza_by_pal[pal_id].append(entry)
    for lst in waza_by_pal.values():
        lst.sort(key=lambda e: e.get("Level", 0))

    sources = {
        "name_rows":          rows("DT_PalNameText_Common.json"),
        "desc_rows":          rows("DT_PalLongDescriptionText.json"),
        "waza_by_pal":        waza_by_pal,
        "skill_name_rows":    rows("DT_SkillNameText_Common.json"),
        "skill_desc_rows":    rows("DT_SkillDescText_Common.json"),
        "distribution_rows":  rows("DT_PaldexDistributionData.json"),
        "drop_rows":          rows("DT_PalDropItem_Common.json"),
        "item_name_rows":     rows("DT_ItemNameText_Common.json"),
        "partner_desc_rows":  rows("DT_PalFirstActivatedInfoText.json"),
        "ui_text_rows":       rows("DT_UI_Common_Text_Common.json"),
        "name_prefix_rows":   rows_optional("DT_NamePrefixText_Common.json"),
        "icon_rows":          icon_rows,
        "waza_table_index":   waza_table_index,
        "tribe_canonical":    tribe_canonical,
    }
    return sorted_tribes, tribe_members, sources


def build_entry(row_name, row, base_name, paldex_index, sources):
    """
    Build the output dict for a single pal row.

    base_name is the canonical row name of the tribe (e.g. "BluePlatypus" for the
    Blueplatypus tribe).  All shared text lookups (PAL_NAME_, PAL_LONG_DESC_,
    PARTNERSKILL_, etc.) are keyed by this value.  Using the canonical row name
    rather than the raw tribe ID string avoids casing mismatches (EPalTribeID can
    differ in capitalisation from the actual row names used as lookup keys).
    """
    name_rows         = sources["name_rows"]
    desc_rows         = sources["desc_rows"]
    waza_by_pal       = sources["waza_by_pal"]
    skill_name_rows   = sources["skill_name_rows"]
    skill_desc_rows   = sources["skill_desc_rows"]
    waza_table_index  = sources["waza_table_index"]
    distribution_rows = sources["distribution_rows"]
    drop_rows         = sources["drop_rows"]
    item_name_rows    = sources["item_name_rows"]
    partner_desc_rows = sources["partner_desc_rows"]
    icon_rows         = sources["icon_rows"]
    ui_text_rows      = sources["ui_text_rows"]
    name_prefix_rows  = sources["name_prefix_rows"]

    # base_name is the canonical row name of the tribe — passed in by the caller.
    palPublicName = get_text(name_rows, f"PAL_NAME_{base_name}") or None

    prefix_id = row.get("NamePrefixID")
    if prefix_id and prefix_id != "None":
        title = clean_string(get_text(name_prefix_rows, prefix_id), name_rows) or None
    else:
        title = None

    description = clean_string(get_text(desc_rows, f"PAL_LONG_DESC_{base_name}"), name_rows) or None

    rarity = row.get("Rarity")
    suffix = row.get("ZukanIndexSuffix") or None
    size   = strip_prefix(row.get("Size") or "")
    genus  = strip_prefix(row.get("GenusCategory") or "")

    stats = {
        "hp":           row.get("Hp"),
        "meleeAttack":  row.get("MeleeAttack"),
        "rangedAttack": row.get("ShotAttack"),
        "defense":      row.get("Defense"),
        "support":      row.get("Support"),
        "craftSpeed":   row.get("CraftSpeed"),
        "stamina":      row.get("Stamina"),
    }

    speed = {
        "walk":       row.get("WalkSpeed"),
        "run":        row.get("RunSpeed"),
        "rideSprint": row.get("RideSprintSpeed"),
        "swim":       row.get("SwimSpeed"),
    }

    work = {
        "kindling":      row.get("WorkSuitability_EmitFlame"),
        "watering":      row.get("WorkSuitability_Watering"),
        "planting":      row.get("WorkSuitability_Seeding"),
        "electricity":   row.get("WorkSuitability_GenerateElectricity"),
        "handiwork":     row.get("WorkSuitability_Handcraft"),
        "gathering":     row.get("WorkSuitability_Collection"),
        "lumbering":     row.get("WorkSuitability_Deforest"),
        "mining":        row.get("WorkSuitability_Mining"),
        "oilExtraction": row.get("WorkSuitability_OilExtraction"),
        "medicine":      row.get("WorkSuitability_ProductMedicine"),
        "cooling":       row.get("WorkSuitability_Cool"),
        "transporting":  row.get("WorkSuitability_Transport"),
        "farming":       row.get("WorkSuitability_MonsterFarm"),
    }

    food = row.get("FoodAmount")

    breeding = {
        "combiRank":       row.get("CombiRank"),
        "maleProbability": row.get("MaleProbability"),
    }

    behavior = {
        "nocturnal": row.get("Nocturnal"),
        "predator":  row.get("Predator"),
    }

    price = row.get("Price")
    isBoss = row.get("IsBoss")

    passiveSkills = []
    for i in range(1, 5):
        raw = row.get(f"PassiveSkill{i}")
        if raw not in (None, "", "None"):
            ps_id = strip_prefix(raw)
            passiveSkills.append({
                "id":          ps_id,
                "name":        clean_string(get_text(skill_name_rows, f"PASSIVE_{ps_id}"), name_rows) or None,
                "description": clean_string(get_text(ui_text_rows, f"PALRECRUIT_APPEAL_TEXT_DEFAULT_{ps_id}"), name_rows) or None,
            })

    # Partner skill: respect an explicit per-row override, otherwise fall back to tribe base name
    override_ps_id = row.get("OverridePartnerSkillTextID")
    ps_skill_key = override_ps_id if (override_ps_id and override_ps_id != "None") else f"PARTNERSKILL_{base_name}"
    ps_name = clean_string(get_text(skill_name_rows, ps_skill_key), name_rows) or None
    ps_desc = clean_string(get_text(partner_desc_rows, f"PAL_FIRST_SPAWN_DESC_{base_name}"), name_rows, item_name_rows) or None
    icon_vals   = list((icon_rows.get(base_name.lower()) or {}).values())
    icon_id     = icon_vals[0] if icon_vals else None
    icon_square = icon_vals[1] if len(icon_vals) > 1 else None
    partnerSkill = {
        "name":        ps_name,
        "description": ps_desc,
        "icon":        f"T_icon_skill_pal_{icon_id:03d}.png" if icon_id is not None else None,
        "iconSquare":  icon_square,
    }

    activeSkills = []
    for waza in waza_by_pal.get(row_name, []):
        waza_id = strip_prefix(waza.get("WazaID") or "")
        skill_key = f"ACTION_SKILL_{waza_id}"
        waza_data_row = waza_table_index.get(waza_id, {})
        activeSkills.append({
            "level":       waza.get("Level"),
            "id":          waza_id,
            "name":        clean_string(get_text(skill_name_rows, skill_key), name_rows) or None,
            "description": clean_string(get_text(skill_desc_rows, skill_key), name_rows) or None,
            "power":    waza_data_row.get("Power"),
            "cooldown": waza_data_row.get("CoolTime"),
            "range":    waza_data_row.get("MaxRange"),
            "element":  strip_prefix(waza_data_row.get("Element") or "") or None,
            "category": strip_prefix(waza_data_row.get("Category") or "") or None,
        })

    # Spawn locations: try this specific row first, then fall back to the tribe base
    dist_row = distribution_rows.get(row_name) or distribution_rows.get(base_name)
    dist_radius = ((dist_row or {}).get("dayTimeLocations") or {}).get("Radius") or DEFAULT_SPAWN_RADIUS
    spawnLocations = build_spawn_locations(dist_row, dist_radius)

    drops = []
    drop_row = drop_rows.get(f"{row_name}000")
    if drop_row and drop_row.get("CharacterID") == row_name:
        for i in range(1, MAX_DROP_SLOTS + 1):
            item_id = drop_row.get(f"ItemId{i}")
            if not item_id or item_id == "None":
                continue
            item_name = clean_string(get_text(item_name_rows, f"ITEM_NAME_{item_id}"), name_rows) or item_id
            drops.append({
                "itemId": item_id,
                "name":   item_name,
                "rate":   drop_row.get(f"Rate{i}"),
                "min":    drop_row.get(f"min{i}"),
                "max":    drop_row.get(f"Max{i}"),
            })

    return {
        "id":             row_name,
        "name":           palPublicName,
        "title":          title,
        "description":    description,
        "paldexNumber":   paldex_index,
        "paldexSuffix":   suffix,
        "rarity":         rarity,
        "size":           size,
        "genus":          genus,
        "stats":          stats,
        "speed":          speed,
        "work":           work,
        "food":           food,
        "breeding":       breeding,
        "behavior":       behavior,
        "price":          price,
        "isBoss":         isBoss,
        "passiveSkills":  passiveSkills,
        "partnerSkill":   partnerSkill,
        "activeSkills":   activeSkills,
        "spawnLocations": spawnLocations,
        "drops":          drops,
    }


def parse_limit(args):
    """Parse optional tribe count limit from CLI args.  Returns None to process all."""
    if not args:
        return None
    try:
        n = int(args[0])
    except ValueError:
        print(f"Warning: invalid argument '{args[0]}', processing all tribes.")
        return None
    if n <= 0 or n > CLI_LIMIT_MAX:
        print(f"Warning: {n} is out of valid range (1-{CLI_LIMIT_MAX}), processing all tribes.")
        return None
    return n


def main():
    limit = parse_limit(sys.argv[1:])
    sorted_tribes, tribe_members, sources = load_sources()

    if limit is not None:
        sorted_tribes = sorted_tribes[:limit]

    print(f"Processing {len(sorted_tribes)} tribes...")

    output = []
    for tribe_name in sorted_tribes:
        zu, suf, canonical_row = sources["tribe_canonical"][tribe_name]
        print(f"  [{zu}{suf}] {tribe_name}")

        for row_name, row in tribe_members[tribe_name]:
            if any(row_name.startswith(p) for p in OUTPUT_SKIP):
                continue
            if row_name.startswith("BOSS_"):
                marker = "  [boss]"
            elif row_name == canonical_row:
                marker = "        "
            else:
                marker = "       +"
            print(f"    {marker} {row_name}")
            output.append(build_entry(row_name, row, canonical_row, zu, sources))

    with open("PalsOutput.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4)

    print(f"Done. Written {len(output)} entries to PalsOutput.json.")


if __name__ == "__main__":
    main()
