from __future__ import annotations

import io
import json
import math
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.request import Request, urlopen

from riot_auth import _CLIENT_PLATFORM, _riot_http

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps  # type: ignore
    PIL_AVAILABLE = True
except Exception:                                       # pragma: no cover
    PIL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Riot item-type UUIDs
# ---------------------------------------------------------------------------
ITEM_TYPE_AGENTS   = "01bb38e1-da47-4e6a-9b3d-945fe4655707"
ITEM_TYPE_SPRAYS   = "d5f120f8-ff8c-4aac-92ea-f2b5acbe9475"
ITEM_TYPE_BUDDIES  = "dd3bf334-87f3-40bd-b043-682a57a8dc3a"
ITEM_TYPE_CARDS    = "3f296c07-64c3-494c-923b-fe692a4fa1bd"
ITEM_TYPE_TITLES   = "de7caa6b-adf7-4588-bbd1-143831e786c6"
ITEM_TYPE_SKINS    = "e7c63390-eda7-46e0-bb7a-a6abdacd2433"
ITEM_TYPE_VARIANTS = "3ad1b2b2-acdb-4524-852f-954a76ddae0a"

CURRENCY_VP = "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741"
CURRENCY_RP = "e59aa87c-4cbf-517a-5983-6e81511be9b7"
CURRENCY_KC = "85ca954a-41f2-ce94-9b45-8ca3dd39a00d"


# ---------------------------------------------------------------------------
# Generic catalog cache (valorant-api.com)
# ---------------------------------------------------------------------------
_CACHE: dict[str, dict] = {}


def _cache_get(key: str, ttl: float):
    entry = _CACHE.get(key)
    if entry and (time.time() - entry["ts"]) < ttl:
        return entry["data"]
    return None


def _cache_put(key: str, data):
    _CACHE[key] = {"data": data, "ts": time.time()}
    return data


def _val_json(url: str, timeout: int = 10) -> dict:
    """Fetch JSON from valorant-api.com (with SSRF protection)."""
    from riot_auth import _validate_outbound_url
    _validate_outbound_url(url)
    req = Request(url, headers={"User-Agent": "VSTORE/1.0 (+inspector)"})
    with urlopen(req, timeout=min(timeout, 10)) as resp:
        return json.loads(resp.read().decode("utf-8"))



def get_riot_client_version() -> str:
    cached = _cache_get("version", 1800)
    if cached:
        return cached
    parsed = _val_json("https://valorant-api.com/v1/version")
    return _cache_put("version", parsed.get("data", {}).get("riotClientVersion", ""))


# ---------------------------------------------------------------------------
# Weapon skin catalog + classification heuristic (VP vs BP vs default)
# ---------------------------------------------------------------------------
def get_weapon_skin_catalog() -> dict:
    cached = _cache_get("skins", 1800)
    if cached:
        return cached

    weapons = _val_json("https://valorant-api.com/v1/weapons?language=en-US").get("data", [])
    tiers = _val_json("https://valorant-api.com/v1/contenttiers").get("data", [])
    vp_tier_ids = {
        tier.get("uuid")
        for tier in tiers
        if tier.get("uuid")
        and "battlepass" not in (tier.get("devName", "") or "").lower()
        and "no tier" not in (tier.get("devName", "") or "").lower()
    }

    skin_map: dict = {}
    skin_level_map: dict = {}
    skin_chroma_map: dict = {}
    for weapon in weapons:
        category = (weapon.get("category") or "").split("::")[-1].lower()
        weapon_name = weapon.get("displayName", "")
        for skin in weapon.get("skins", []):
            skin_uuid = skin.get("uuid")
            if not skin_uuid:
                continue
            chromas = skin.get("chromas", []) or []
            levels_list = [l for l in (skin.get("levels") or []) if l.get("uuid")]
            chromas_list = [c for c in (skin.get("chromas") or []) if c.get("uuid")]
            # Riot's skin-level displayIcon is often a 512×512 grey "X" placeholder;
            # the real flat gun silhouette lives on level-1 / chroma assets.
            level_icon = levels_list[0].get("displayIcon") if levels_list else None
            chroma_icon = None
            chroma_render = None
            if chromas_list:
                chroma_icon = chromas_list[0].get("displayIcon")
                chroma_render = chromas_list[0].get("fullRender")
            icon_url = level_icon or chroma_icon or chroma_render or skin.get("displayIcon")
            render_icon_url = chroma_render or level_icon or chroma_icon or skin.get("displayIcon")
            tier_uuid = skin.get("contentTierUuid")
            skin_name_lc = (skin.get("displayName") or "").lower()
            levels_count = len(levels_list)
            chromas_count = len(chromas_list)
            # Multi-level (>=2) marks a real VP skin; multi-chroma alone is BP.
            has_upgrades = levels_count >= 2

            is_default = (
                tier_uuid is None
                and not skin.get("themeUuid")
                and any(skin_name_lc.endswith(w) for w in (
                    "vandal", "phantom", "operator", "sheriff", "ghost", "classic",
                    "shorty", "frenzy", "stinger", "spectre", "bucky", "judge",
                    "guardian", "marshal", "ares", "odin", "bulldog", "melee",
                    "knife", "outlaw",
                ))
            )
            is_real_vp = (tier_uuid in vp_tier_ids) and has_upgrades
            is_battlepass = (not is_real_vp) and (not is_default)

            level_uuids = [l["uuid"] for l in levels_list if l.get("uuid")]
            chroma_uuids = [c["uuid"] for c in chromas_list if c.get("uuid")]
            skin_map[skin_uuid] = {
                "uuid": skin_uuid,
                "display_name": skin.get("displayName") or "Unknown Skin",
                "icon_url": icon_url,
                "render_icon_url": render_icon_url,
                "content_tier_uuid": tier_uuid,
                "weapon_name": weapon_name,
                "weapon_category": category,
                "levels_count": levels_count,
                "chromas_count": chromas_count,
                "level_uuids": level_uuids,
                "chroma_uuids": chroma_uuids,
                "is_vp_skin": is_real_vp,
                "is_battlepass": is_battlepass,
                "is_default": is_default,
            }
            for lvl in (skin.get("levels") or []):
                lid = _norm_uuid(lvl.get("uuid"))
                if lid:
                    skin_level_map[lid] = skin_uuid
            for chroma in (skin.get("chromas") or []):
                cid = _norm_uuid(chroma.get("uuid"))
                if cid:
                    skin_chroma_map[cid] = skin_uuid

    return _cache_put("skins", {
        "skins": skin_map,
        "skin_levels": skin_level_map,
        "skin_chromas": skin_chroma_map,
    })


def invalidate_skin_catalog() -> None:
    _CACHE.pop("skins", None)
    for key in list(_CACHE):
        if key.startswith("offers_"):
            _CACHE.pop(key, None)


def _norm_uuid(value: str | None) -> str:
    return (value or "").strip().lower()


def _tier_vp_estimate(dev_name: str) -> int:
    dev = (dev_name or "").lower()
    if "exclusive" in dev:
        return 2475
    if "ultra" in dev:
        return 2475
    if "premium" in dev:
        return 1775
    if "deluxe" in dev:
        return 1275
    if "select" in dev:
        return 875
    return 0


def fetch_offer_prices(shard, access_token, entitlements_token) -> dict:
    cache_key = f"offers_{shard}"
    cached = _cache_get(cache_key, 3600)
    if cached is not None:
        return cached

    data = _pd_json(
        "GET",
        f"https://pd.{shard}.a.pvp.net/store/v1/offers/",
        _pd_headers(access_token, entitlements_token),
    )
    prices: dict[str, int] = {}

    def _put(item_id: str | None, cost: int) -> None:
        key = _norm_uuid(item_id)
        if not key:
            return
        prices[key] = max(prices.get(key, 0), int(cost))

    for off in data.get("Offers") or []:
        cost = (off.get("Cost") or {}).get(CURRENCY_VP)
        if cost is None:
            continue
        _put(off.get("OfferID"), cost)
        for rew in off.get("Rewards") or []:
            _put(rew.get("ItemID"), cost)

    return _cache_put(cache_key, prices)


def _resolve_row_price_vp(meta: dict, price_map: dict) -> int | None:
    """Pick the store VP price for one skin row (melee → max match, guns → base level)."""
    if not price_map:
        return None

    skin_uuid = _norm_uuid(meta.get("uuid"))
    level_ids = [_norm_uuid(x) for x in (meta.get("level_uuids") or []) if x]
    chroma_ids = [_norm_uuid(x) for x in (meta.get("chroma_uuids") or []) if x]

    costs: list[int] = []
    for key in [skin_uuid, *level_ids, *chroma_ids]:
        if key in price_map:
            costs.append(price_map[key])

    if not costs:
        return None

    category = (meta.get("weapon_category") or "").lower()
    if category == "melee":
        return max(costs)

    if level_ids and level_ids[0] in price_map:
        return price_map[level_ids[0]]
    return max(costs)


def enrich_skin_rows_with_prices(rows: list, price_map: dict, catalog: dict | None = None) -> None:
    """Attach ``price_vp`` using official Riot store offers API (/store/v1/offers/); API tier estimate only if offer is missing."""
    if not rows:
        return
    catalog = catalog or get_weapon_skin_catalog()
    skins = catalog.get("skins", {})
    tier_catalog = get_content_tier_catalog()

    for row in rows:
        meta = skins.get(row.get("uuid")) or row
        price = _resolve_row_price_vp(meta, price_map) if price_map else None
        if price is None:
            tier_uuid = (meta.get("content_tier_uuid") or row.get("content_tier_uuid") or "").lower()
            tier = tier_catalog.get(tier_uuid) if tier_uuid else None
            if tier and tier.get("vp_estimate"):
                price = tier["vp_estimate"]
        if price is not None:
            row["price_vp"] = price


def _is_melee_row(row: dict) -> bool:
    return (row.get("weapon_category") or "").lower() == "melee"


def sort_skins_for_inspector(rows: list) -> list:
    """Melee first, then guns; VP high→low; battle pass / defaults at the bottom."""
    def _key(row):
        name = (row.get("display_name") or "").lower()
        # 0 = melee, 1 = guns/other (dao luôn trước súng trong cùng nhóm giá)
        weapon_order = 0 if _is_melee_row(row) else 1
        if row.get("is_battlepass"):
            return (3, weapon_order, 0, name)
        if row.get("is_default"):
            return (4, weapon_order, 0, name)
        price = row.get("price_vp")
        if price is None:
            return (1, weapon_order, 0, name)
        return (0, weapon_order, -int(price), name)

    return sorted(rows or [], key=_key)


def resolve_owned_skin_rows(owned_skin_ids, owned_variant_ids, catalog) -> list:
    skin_map = catalog.get("skins", {})
    level_map = catalog.get("skin_levels", {})
    chroma_map = catalog.get("skin_chromas", {})
    resolved, seen = [], set()
    for raw_id in list(owned_skin_ids or []) + list(owned_variant_ids or []):
        raw_key = _norm_uuid(raw_id)
        skin_uuid = raw_id if raw_id in skin_map else raw_key
        if skin_uuid not in skin_map and raw_key not in skin_map:
            skin_uuid = level_map.get(raw_key) or chroma_map.get(raw_key)
        elif raw_key in skin_map:
            skin_uuid = raw_key
        row = skin_map.get(skin_uuid)
        if not row or skin_uuid in seen:
            continue
        seen.add(skin_uuid)
        resolved.append(row)
    return resolved


def get_competitive_tier_catalog() -> dict:
    cached = _cache_get("tiers", 86400)
    if cached is not None:
        return cached
    data = _val_json("https://valorant-api.com/v1/competitivetiers").get("data", []) or []
    latest = data[-1] if data else {}
    tiers = {}
    for tier in latest.get("tiers", []) or []:
        tiers[tier.get("tier")] = {
            "name": tier.get("tierName") or "",
            "color": tier.get("color") or "ffffff",
            "icon": tier.get("smallIcon") or tier.get("largeIcon"),
        }
    return _cache_put("tiers", tiers)


def get_player_card_catalog() -> dict:
    cached = _cache_get("cards", 86400)
    if cached is not None:
        return cached
    parsed = _val_json("https://valorant-api.com/v1/playercards")
    card_map = {}
    for card in parsed.get("data", []) or []:
        if not card.get("uuid"):
            continue
        card_map[card["uuid"]] = {
            "name": card.get("displayName") or "Player Card",
            "icon": card.get("displayIcon"),
            "small": card.get("smallArt"),
            "wide": card.get("wideArt"),
            "large": card.get("largeArt"),
        }
    return _cache_put("cards", card_map)


def get_buddy_catalog() -> dict:
    cached = _cache_get("buddies", 86400)
    if cached is not None:
        return cached
    parsed = _val_json("https://valorant-api.com/v1/buddies")
    buddy_map = {}
    for buddy in parsed.get("data", []) or []:
        if not buddy.get("uuid"):
            continue
        b_info = {
            "uuid": buddy["uuid"],
            "name": buddy.get("displayName") or "Gun Buddy",
            "icon": buddy.get("displayIcon"),
        }
        buddy_map[buddy["uuid"]] = b_info
        for lvl in buddy.get("levels", []) or []:
            if lvl.get("uuid"):
                buddy_map[lvl["uuid"]] = {
                    "uuid": buddy["uuid"],
                    "name": buddy.get("displayName") or "Gun Buddy",
                    "icon": lvl.get("displayIcon") or buddy.get("displayIcon"),
                }
    return _cache_put("buddies", buddy_map)


def get_spray_catalog() -> dict:
    cached = _cache_get("sprays", 86400)
    if cached is not None:
        return cached
    parsed = _val_json("https://valorant-api.com/v1/sprays")
    spray_map = {}
    for spray in parsed.get("data", []) or []:
        if not spray.get("uuid"):
            continue
        spray_map[spray["uuid"]] = {
            "name": spray.get("displayName") or "Spray",
            "icon": spray.get("fullTransparentIcon") or spray.get("displayIcon"),
        }
    return _cache_put("sprays", spray_map)


def get_agent_catalog() -> dict:
    cached = _cache_get("agents_cat", 86400)
    if cached is not None:
        return cached
    parsed = _val_json("https://valorant-api.com/v1/agents?isPlayableCharacter=true")
    agent_map = {}
    for agent in parsed.get("data", []) or []:
        if not agent.get("uuid"):
            continue
        role_obj = agent.get("role") or {}
        agent_map[agent["uuid"]] = {
            "uuid": agent["uuid"],
            "name": agent.get("displayName") or "",
            "icon": agent.get("displayIcon"),
            "portrait": agent.get("fullPortrait") or agent.get("fullPortraitV2") or agent.get("bustPortrait") or agent.get("displayIcon"),
            "killfeed": agent.get("killfeedPortrait"),
            "role": role_obj.get("displayName") or "",
            "role_icon": role_obj.get("displayIcon") or "",
            "background": agent.get("background"),
        }
    return _cache_put("agents_cat", agent_map)


def resolve_owned_agent_rows(agent_ids: list) -> list:
    catalog = get_agent_catalog()
    rows = []
    seen = set()
    for aid in (agent_ids or []):
        info = catalog.get(aid) or catalog.get(_norm_uuid(aid))
        if info:
            if aid not in seen:
                seen.add(aid)
                rows.append(info)
        else:
            rows.append({"uuid": aid, "name": "Agent", "icon": None, "portrait": None, "role": "", "role_icon": ""})
    return rows



def resolve_owned_buddy_rows(buddy_ids: list) -> list:
    catalog = get_buddy_catalog()
    rows = []
    seen = set()
    for bid in (buddy_ids or []):
        info = catalog.get(bid) or catalog.get(_norm_uuid(bid))
        if info:
            key = info.get("uuid") or info.get("name")
            if key not in seen:
                seen.add(key)
                rows.append(info)
        else:
            rows.append({"uuid": bid, "name": "Gun Buddy", "icon": None})
    return rows


def resolve_owned_card_rows(card_ids: list) -> list:
    catalog = get_player_card_catalog()
    rows = []
    seen = set()
    for cid in (card_ids or []):
        info = catalog.get(cid) or catalog.get(_norm_uuid(cid))
        if info:
            if cid not in seen:
                seen.add(cid)
                rows.append({
                    "uuid": cid,
                    "name": info.get("name") or "Player Card",
                    "icon": info.get("large") or info.get("displayIcon") or info.get("small"),
                    "large": info.get("large") or info.get("displayIcon"),
                    "wide": info.get("wide"),
                })
        else:
            rows.append({"uuid": cid, "name": "Player Card", "icon": None, "large": None})
    return rows




def resolve_owned_spray_rows(spray_ids: list) -> list:
    catalog = get_spray_catalog()
    rows = []
    seen = set()
    for sid in (spray_ids or []):
        info = catalog.get(sid) or catalog.get(_norm_uuid(sid))
        if info:
            if sid not in seen:
                seen.add(sid)
                rows.append({
                    "uuid": sid,
                    "name": info.get("name") or "Spray",
                    "icon": info.get("icon"),
                })
        else:
            rows.append({"uuid": sid, "name": "Spray", "icon": None})
    return rows




def get_seasons_catalog() -> dict:
    cached = _cache_get("seasons", 86400)
    if cached is not None:
        return cached
    seasons = _val_json("https://valorant-api.com/v1/seasons").get("data", []) or []
    season_map = {s.get("uuid"): s for s in seasons if s.get("uuid")}
    latest = None
    now_iso = datetime.utcnow().isoformat() + "Z"
    for s in seasons:
        if s.get("type") == "EAresSeasonType::Act" and s.get("startTime") and s.get("endTime"):
            if s.get("startTime") <= now_iso <= s.get("endTime"):
                latest = s
                break
    return _cache_put("seasons", {"seasons": season_map, "latest_act": latest})


def get_content_tier_catalog() -> dict:
    cached = _cache_get("ctiers", 86400)
    if cached is not None:
        return cached
    parsed = _val_json("https://valorant-api.com/v1/contenttiers")
    tier_map = {}
    for t in parsed.get("data", []) or []:
        if not t.get("uuid"):
            continue
        dev_name = t.get("devName") or ""
        tier_map[t["uuid"]] = {
            "name": t.get("displayName") or "",
            "dev_name": dev_name,
            "color": t.get("highlightColor") or "ffffffff",
            "icon": t.get("displayIcon"),
            "rank": t.get("rank") or 0,
            "vp_estimate": _tier_vp_estimate(dev_name),
        }
    return _cache_put("ctiers", tier_map)


def format_act_name(latest_act, seasons_map) -> str:
    if not latest_act:
        return ""
    parent = seasons_map.get(latest_act.get("parentUuid")) if latest_act.get("parentUuid") else None
    episode_name = (parent or {}).get("displayName") or ""
    act_name = latest_act.get("displayName") or ""
    if episode_name and act_name:
        return f"{episode_name} // {act_name}"
    return act_name or episode_name


# ---------------------------------------------------------------------------
# Riot store / player endpoints (Cloudflare-gated → via riot_auth._riot_http)
# ---------------------------------------------------------------------------
def _pd_headers(access_token: str, entitlements_token: str) -> dict:
    return {
        "X-Riot-ClientPlatform":   _CLIENT_PLATFORM,
        "X-Riot-ClientVersion":    get_riot_client_version(),
        "X-Riot-Entitlements-JWT": entitlements_token,
        "Authorization":           f"Bearer {access_token}",
        "Accept":                  "application/json",
        "User-Agent":              "ShooterGame/++Ares-Core-shipping-31.00.00.0000000.0000 Windows/10.0.19045.1.256.64bit",
        "Origin":                  "https://playvalorant.com",
        "Referer":                 "https://playvalorant.com/",
    }


def _pd_json(method: str, url: str, headers: dict, *, data: bytes = b"", timeout: int = 20) -> dict:
    body = _riot_http(method, url, headers, data=data, timeout=timeout)
    return json.loads(body) if body else {}


def fetch_owned_item_ids(shard, puuid, item_type_id, access_token, entitlements_token) -> list:
    url = f"https://pd.{shard}.a.pvp.net/store/v1/entitlements/{puuid}/{item_type_id}"
    payload = _pd_json("GET", url, _pd_headers(access_token, entitlements_token))
    item_ids = []
    if isinstance(payload.get("Entitlements"), list):
        for ent in payload["Entitlements"]:
            if ent.get("ItemID"):
                item_ids.append(ent["ItemID"])
    else:
        for row in payload.get("EntitlementsByTypes", []):
            for ent in row.get("Entitlements", []):
                if ent.get("ItemID"):
                    item_ids.append(ent["ItemID"])
    return item_ids


def fetch_owned_item_ids_from_all_types(shard, puuid, target_item_type_id, access_token, entitlements_token) -> list:
    url = f"https://pd.{shard}.a.pvp.net/store/v1/entitlements/{puuid}"
    payload = _pd_json("GET", url, _pd_headers(access_token, entitlements_token))
    item_ids = []
    for row in payload.get("EntitlementsByTypes", []):
        if row.get("ItemTypeID") != target_item_type_id:
            continue
        for ent in row.get("Entitlements", []):
            if ent.get("ItemID"):
                item_ids.append(ent["ItemID"])
    return item_ids


def _safe(label, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _season_meta(season_id: str, sea: dict, seasons_cache: dict) -> dict:
    for sid in (season_id, (sea or {}).get("SeasonID")):
        if sid and sid in seasons_cache:
            return seasons_cache[sid]
    return {}


def _season_tier_rating(sea: dict) -> tuple[int, int]:
    """Return (CompetitiveTier, RankedRating) from a seasonal MMR entry."""
    if not isinstance(sea, dict):
        return 0, 0
    tier = int(sea.get("CompetitiveTier") or 0)
    rr = int(sea.get("RankedRating") or 0)
    if not tier:
        tier = int(sea.get("Rank") or 0)
    if not tier:
        wins = sea.get("WinsByTier") or {}
        if isinstance(wins, dict) and wins:
            try:
                tier = max(int(k) for k in wins if int(k) > 0)
            except (ValueError, TypeError):
                pass
    return tier, rr


def resolve_competitive_rank(mmr: dict) -> tuple[int, int]:
    """Resolve the best display rank from a Player MMR response.

    Priority:
      1. Current act seasonal rank if non-zero
      2. Most recent act that has a non-zero rank (handles Act resets / game updates)
      3. LatestCompetitiveUpdate (TierAfterUpdate or TierBeforeUpdate)
    """
    if not isinstance(mmr, dict):
        return 0, 0

    latest = mmr.get("LatestCompetitiveUpdate") or {}
    comp = (mmr.get("QueueSkills") or {}).get("competitive") or {}
    seasonal = comp.get("SeasonalInfoBySeasonID") or {}

    catalog = get_seasons_catalog()
    seasons_cache = catalog.get("seasons") or {}
    current_act_id = (catalog.get("latest_act") or {}).get("uuid")

    def _lookup(season_id: str):
        if not season_id:
            return None
        if season_id in seasonal:
            return seasonal[season_id]
        for key, sea in seasonal.items():
            if (sea.get("SeasonID") or key) == season_id:
                return sea
        return None

    # 1. Check current act
    if current_act_id:
        sea = _lookup(current_act_id)
        if sea:
            tier, rr = _season_tier_rating(sea)
            if tier > 0:
                return tier, rr

    # 2. Find most recent season with tier > 0 sorted by start time
    ranked_seasons = []
    for season_id, sea in seasonal.items():
        tier, rr = _season_tier_rating(sea)
        if tier <= 0:
            continue
        meta = _season_meta(season_id, sea, seasons_cache)
        start = meta.get("startTime") or ""
        ranked_seasons.append((start, tier, rr))

    if ranked_seasons:
        ranked_seasons.sort(key=lambda x: x[0], reverse=True)
        return ranked_seasons[0][1], ranked_seasons[0][2]

    # 3. Check LatestCompetitiveUpdate
    t_after = int(latest.get("TierAfterUpdate") or 0)
    t_before = int(latest.get("TierBeforeUpdate") or 0)
    rr = int(latest.get("RankedRatingAfterUpdate") or latest.get("RankedRatingBeforeUpdate") or 0)
    if t_after > 0:
        return t_after, rr
    if t_before > 0:
        return t_before, rr

    return 0, 0


def gather_account_info(shard, puuid, access_token, entitlements_token) -> dict:
    info = {
        "shard": shard, "puuid": puuid,
        "game_name": "", "tag_line": "", "display_name": "",
        "account_level": 0, "account_xp": 0,
        "rank_tier": 0, "ranked_rating": 0,
        "wallet": {"vp": 0, "rp": 0, "kc": 0},
        "counts": {"agents": 0, "sprays": 0, "buddies": 0, "cards": 0, "titles": 0, "skins": 0},
        "loadout": {},
    }
    h = _pd_headers(access_token, entitlements_token)

    name = _safe("name", _pd_json, "PUT",
                 f"https://pd.{shard}.a.pvp.net/name-service/v2/players",
                 {**h, "Content-Type": "application/json"},
                 data=json.dumps([puuid]).encode("utf-8"))
    if isinstance(name, list) and name:
        info["game_name"] = name[0].get("GameName") or ""
        info["tag_line"] = name[0].get("TagLine") or ""
        info["display_name"] = name[0].get("DisplayName") or ""
        if not info["display_name"] and info["game_name"]:
            info["display_name"] = (
                f"{info['game_name']}#{info['tag_line']}" if info["tag_line"] else info["game_name"]
            )

    xp = _safe("xp", _pd_json, "GET", f"https://pd.{shard}.a.pvp.net/account-xp/v1/players/{puuid}", h)
    if isinstance(xp, dict):
        progress = xp.get("Progress") or {}
        info["account_level"] = progress.get("Level") or 0
        info["account_xp"] = progress.get("XP") or 0

    mmr = _safe("mmr", _pd_json, "GET", f"https://pd.{shard}.a.pvp.net/mmr/v1/players/{puuid}", h)
    if isinstance(mmr, dict):
        info["rank_tier"], info["ranked_rating"] = resolve_competitive_rank(mmr)

    if not info["rank_tier"]:
        updates = _safe(
            "mmr_updates", _pd_json, "GET",
            f"https://pd.{shard}.a.pvp.net/mmr/v1/players/{puuid}"
            f"/competitiveupdates?startIndex=0&endIndex=20",
            h,
        )
        if isinstance(updates, dict):
            for match in updates.get("Matches") or []:
                tier = int(match.get("TierAfterUpdate") or match.get("TierBeforeUpdate") or 0)
                if tier > 0:
                    info["rank_tier"] = tier
                    info["ranked_rating"] = int(match.get("RankedRatingAfterUpdate") or match.get("RankedRatingBeforeUpdate") or 0)
                    break


    wallet = _safe("wallet", _pd_json, "GET", f"https://pd.{shard}.a.pvp.net/store/v1/wallet/{puuid}", h)
    if isinstance(wallet, dict):
        balances = wallet.get("Balances") or {}
        info["wallet"]["vp"] = balances.get(CURRENCY_VP) or 0
        info["wallet"]["rp"] = balances.get(CURRENCY_RP) or 0
        info["wallet"]["kc"] = balances.get(CURRENCY_KC) or 0

    loadout = _safe("loadout", _pd_json, "GET",
                    f"https://pd.{shard}.a.pvp.net/personalization/v2/players/{puuid}/playerloadout", h)
    if isinstance(loadout, dict):
        info["loadout"] = loadout
        if not info["account_level"]:
            info["account_level"] = (loadout.get("Identity") or {}).get("AccountLevel") or 0

    for label, type_id in (
        ("agents", ITEM_TYPE_AGENTS), ("sprays", ITEM_TYPE_SPRAYS),
        ("buddies", ITEM_TYPE_BUDDIES), ("cards", ITEM_TYPE_CARDS),
        ("titles", ITEM_TYPE_TITLES),
    ):
        ids = _safe(f"owned_{label}", fetch_owned_item_ids,
                    shard, puuid, type_id, access_token, entitlements_token)
        info["counts"][label] = len(ids or [])

    return info


def mask_riot_display_name(name: str) -> str:
    if not name or "#" not in name:
        if not name:
            return ""
        n = len(name)
        if n <= 4:
            return name[0] + "*" * (n - 1)
        return name[:2] + "*" * (n - 4) + name[-2:]
    prefix, _, tag = name.partition("#")
    n = len(prefix)
    if n <= 4:
        masked = prefix[0] + "*" * (n - 1) if n > 1 else prefix
    else:
        masked = prefix[:2] + "*" * (n - 4) + prefix[-2:]
    return f"{masked}#{tag}"


# ---------------------------------------------------------------------------
# Daily store (storefront rotation)
# ---------------------------------------------------------------------------
def fetch_storefront(shard, puuid, access_token, entitlements_token) -> dict:
    h = _pd_headers(access_token, entitlements_token)
    # Current client uses POST /v3; older accounts still answer GET /v2.
    try:
        return _pd_json("POST", f"https://pd.{shard}.a.pvp.net/store/v3/storefront/{puuid}",
                        {**h, "Content-Type": "application/json"}, data=b"{}")
    except Exception:
        return _pd_json("GET", f"https://pd.{shard}.a.pvp.net/store/v2/storefront/{puuid}", h)


def get_daily_store(shard, puuid, access_token, entitlements_token) -> dict:
    """Return the 4 daily skin offers (resolved rows + VP price) + reset secs."""
    sf = fetch_storefront(shard, puuid, access_token, entitlements_token)
    panel = sf.get("SkinsPanelLayout") or {}
    level_offers = panel.get("SingleItemOffers") or []
    remaining = panel.get("SingleItemOffersRemainingDurationInSeconds") or 0

    # Price: prefer inline offers, else the global price list.
    price_map = {}
    for off in panel.get("SingleItemStoreOffers") or []:
        oid = _norm_uuid(off.get("OfferID"))
        cost = (off.get("Cost") or {}).get(CURRENCY_VP)
        if oid and cost is not None:
            price_map[oid] = int(cost)
    if not price_map:
        try:
            price_map = fetch_offer_prices(shard, access_token, entitlements_token)
        except Exception:
            price_map = {}

    catalog = get_weapon_skin_catalog()
    skins, levels = catalog["skins"], catalog["skin_levels"]
    items = []
    for lvl in level_offers:
        lvl_key = _norm_uuid(lvl)
        skin_uuid = levels.get(lvl) or levels.get(lvl_key) or lvl
        base = skins.get(skin_uuid)
        if not base:
            continue
        row = dict(base)
        row["price_vp"] = price_map.get(lvl_key) or _resolve_row_price_vp(base, price_map)
        items.append(row)
    return {"items": items, "remaining": int(remaining or 0)}


# ---------------------------------------------------------------------------
# High-level scan: fetch inventory + classify + filter
# ---------------------------------------------------------------------------
def build_account_scan(shard, puuid, access_token, entitlements_token,
                       *, include_battlepass: bool = False) -> dict:
    """Fetch the inventory + account info and return everything needed to
    render + persist a scan.

    Returns:
        {
          'account_info': {...},
          'skin_payload': {...},   # full owned list + selection
          'filtered_rows': [...],  # the rows that go into the image by default
        }
    """
    skin_ids = fetch_owned_item_ids(shard, puuid, ITEM_TYPE_SKINS, access_token, entitlements_token)
    if not skin_ids:
        skin_ids = fetch_owned_item_ids_from_all_types(
            shard, puuid, ITEM_TYPE_SKINS, access_token, entitlements_token)
    try:
        variant_ids = fetch_owned_item_ids(shard, puuid, ITEM_TYPE_VARIANTS, access_token, entitlements_token)
        if not variant_ids:
            variant_ids = fetch_owned_item_ids_from_all_types(
                shard, puuid, ITEM_TYPE_VARIANTS, access_token, entitlements_token)
    except Exception:
        variant_ids = []

    catalog = get_weapon_skin_catalog()
    all_skins = resolve_owned_skin_rows(skin_ids, variant_ids, catalog)

    price_map: dict = {}
    try:
        price_map = fetch_offer_prices(shard, access_token, entitlements_token)
    except Exception:
        price_map = {}
    enrich_skin_rows_with_prices(all_skins, price_map, catalog)
    all_skins = sort_skins_for_inspector(all_skins)

    if include_battlepass:
        filtered = list(all_skins)
    else:
        filtered = [s for s in all_skins if not s.get("is_battlepass") and not s.get("is_default")]
    filtered = sort_skins_for_inspector(filtered)

    # Fetch and resolve buddies, cards, sprays, agents
    buddy_ids = _safe("owned_buddies", fetch_owned_item_ids, shard, puuid, ITEM_TYPE_BUDDIES, access_token, entitlements_token) or []
    card_ids = _safe("owned_cards", fetch_owned_item_ids, shard, puuid, ITEM_TYPE_CARDS, access_token, entitlements_token) or []
    spray_ids = _safe("owned_sprays", fetch_owned_item_ids, shard, puuid, ITEM_TYPE_SPRAYS, access_token, entitlements_token) or []
    agent_ids = _safe("owned_agents", fetch_owned_item_ids, shard, puuid, ITEM_TYPE_AGENTS, access_token, entitlements_token) or []

    buddies_list = resolve_owned_buddy_rows(buddy_ids)
    cards_list = resolve_owned_card_rows(card_ids)
    sprays_list = resolve_owned_spray_rows(spray_ids)
    agents_list = resolve_owned_agent_rows(agent_ids)

    account_info = gather_account_info(shard, puuid, access_token, entitlements_token)
    account_info["counts"]["skins"] = len(filtered)
    account_info["counts"]["buddies"] = len(buddies_list)
    account_info["counts"]["cards"] = len(cards_list)
    account_info["counts"]["sprays"] = len(sprays_list)
    account_info["counts"]["agents"] = len(agents_list)

    store = _safe("store", get_daily_store, shard, puuid, access_token, entitlements_token) \
        or {"items": [], "remaining": 0}

    skin_payload = {
        "total_skins": len(all_skins),
        "total_vp_skins": sum(1 for x in all_skins if x.get("is_vp_skin")),
        "owned_skin_ids": skin_ids,
        "owned_variant_ids": variant_ids,
        "owned_buddy_ids": buddy_ids,
        "owned_card_ids": card_ids,
        "owned_spray_ids": spray_ids,
        "owned_agent_ids": agent_ids,
        "selected_skin_ids": [s.get("uuid") for s in filtered if s.get("uuid")],
        "skins": all_skins,
        "buddies": buddies_list,
        "cards": cards_list,
        "sprays": sprays_list,
        "agents": agents_list,
    }

    return {
        "account_info": account_info,
        "skin_payload": skin_payload,
        "filtered_rows": filtered,
        "store": store,
    }



def resolve_selected_rows(skin_payload: dict, selected_ids=None) -> list:
    """Return the skin rows for the currently-selected uuids."""
    all_skins = (skin_payload or {}).get("skins", []) or []
    if selected_ids is None:
        selected_ids = skin_payload.get("selected_skin_ids") or [s.get("uuid") for s in all_skins]
    sel = set(selected_ids)
    rows = [s for s in all_skins if s.get("uuid") in sel]
    return sort_skins_for_inspector(rows)


# ---------------------------------------------------------------------------
# Image rendering
# ---------------------------------------------------------------------------
# Optional bundled fonts (drop the .ttf files in assets/fonts to match the
# site's typography exactly). Falls back to system Arial when absent.
FONT_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
_FONT_FILES = {
    "display": ["Anton-Regular.ttf", "Anton.ttf", "BebasNeue-Regular.ttf"],
    "heading": ["ChakraPetch-Bold.ttf", "ChakraPetch-SemiBold.ttf"],
    "body":    ["ChakraPetch-Medium.ttf", "ChakraPetch-Regular.ttf"],
    "mono":    ["JetBrainsMono-Bold.ttf", "JetBrainsMono-Medium.ttf", "JetBrainsMono-Regular.ttf"],
}
_FONT_FALLBACK = {
    "display": ["arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"],
    "heading": ["arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"],
    "body":    ["arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"],
    "mono":    ["consola.ttf", "arial.ttf", "DejaVuSansMono.ttf"],
}


def _load_font(size: int, role: str = "body"):
    if not PIL_AVAILABLE:
        return None
    for name in _FONT_FILES.get(role, []):
        path = os.path.join(FONT_DIR, name)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    for name in _FONT_FALLBACK.get(role, ["arial.ttf"]):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def get_currency_catalog() -> dict:
    cached = _cache_get("currencies", 86400)
    if cached is not None:
        return cached
    parsed = _val_json("https://valorant-api.com/v1/currencies")
    m = {}
    for c in parsed.get("data", []) or []:
        if c.get("uuid"):
            m[c["uuid"]] = c.get("displayIcon")
    return _cache_put("currencies", m)


def _fetch_image(url: str, mode: str = "RGBA"):
    if not url:
        return None
    try:
        with urlopen(url, timeout=12) as resp:
            raw = resp.read()
        return Image.open(io.BytesIO(raw)).convert(mode)
    except Exception:
        return None


def _hex_to_rgba(hex_color: str, alpha: int = 255):
    s = (hex_color or "").lstrip("#")
    if len(s) == 8:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(s[6:8], 16))
    if len(s) == 6:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), alpha)
    return (255, 255, 255, alpha)


def _vertical_gradient(width: int, height: int, top_rgba, bottom_rgba):
    if height <= 0:
        return Image.new("RGBA", (width, max(1, height)), (0, 0, 0, 0))
    row = Image.new("RGBA", (1, height), (0, 0, 0, 0))
    for y in range(height):
        t = y / max(1, height - 1)
        row.putpixel((0, y), (
            int(top_rgba[0] + (bottom_rgba[0] - top_rgba[0]) * t),
            int(top_rgba[1] + (bottom_rgba[1] - top_rgba[1]) * t),
            int(top_rgba[2] + (bottom_rgba[2] - top_rgba[2]) * t),
            int(top_rgba[3] + (bottom_rgba[3] - top_rgba[3]) * t),
        ))
    return row.resize((width, height))


# VSTORE tactical palette (matches the website identity, not meefu purple).
_INK_TOP   = (16, 21, 28, 255)
_INK_BOT   = (8, 10, 15, 255)
_RED       = (255, 70, 85)
_RED_DEEP  = (216, 49, 63)
_BONE      = (236, 232, 225)
_BONE_DIM  = (236, 232, 225, 165)
_BONE_MUTE = (236, 232, 225, 100)
_GOLD      = (241, 191, 58)
_TEAL      = (70, 209, 196)
_CARD      = (23, 27, 35, 240)
_CARD_HI   = (30, 35, 45, 240)
_HAIR      = (255, 255, 255, 22)


def _hgradient_mask(width: int, height: int, fade_frac: float = 0.45) -> "Image.Image":
    """L-mode mask that fades 0→255 left→right over the first `fade_frac`."""
    fade_w = max(1, int(width * fade_frac))
    rowimg = Image.new("L", (width, 1), 255)
    for x in range(width):
        rowimg.putpixel((x, 0), int(255 * min(1.0, x / fade_w)))
    return rowimg.resize((width, height))


def _rounded(size, radius, fill, border=None, border_w=0):
    w, h = size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=fill)
    if border and border_w:
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, outline=border, width=border_w)
    return layer


def _fmt_countdown(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m:02d}m"


# Content Tier Color mapping to keep tiers exact and beautiful
TIER_COLOR_MAP = {
    "12683d76-48d7-840a-f0f4-c085b1350850": (90, 159, 226),   # Select (Blue)
    "0cebbd32-4112-6ad1-218d-944d47e326e2": (0, 189, 162),   # Deluxe (Green)
    "607fae73-81a2-47a6-bc0d-47ef9d21a487": (209, 84, 141),  # Premium (Purple)
    "11e0f608-43f1-acf2-c973-158a7e584f5c": (241, 184, 45),  # Ultra (Yellow)
    "e04bde6b-4369-969c-73b5-0c937040b128": (255, 140, 70),  # Exclusive (Orange)
}


def _draw_radial_gradient(tile, center, radius, color):
    r, g, b, a_max = color
    cx, cy = center
    sz = 64
    mask = Image.new("L", (sz, sz), 0)
    mdraw = ImageDraw.Draw(mask)
    c = sz // 2
    mdraw.ellipse((c - 8, c - 8, c + 8, c + 8), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(12))
    dest_sz = radius * 2
    resampling = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
    mask_resized = mask.resize((dest_sz, dest_sz), resampling)
    glow_color = Image.new("RGBA", (dest_sz, dest_sz), (r, g, b, a_max))
    glow_color.putalpha(mask_resized)
    tile.alpha_composite(glow_color, (cx - radius, cy - radius))


def _draw_drop_shadow(canvas, img, pos):
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    alpha = img.split()[-1]
    shadow = Image.new("RGBA", img.size, (0, 0, 0, int(255 * 0.85)))
    shadow.putalpha(alpha)
    pad = 8
    padded = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2), (0, 0, 0, 0))
    padded.alpha_composite(shadow, (pad, pad + 5))
    blurred = padded.filter(ImageFilter.GaussianBlur(4))
    canvas.alpha_composite(blurred, (pos[0] - pad, pos[1] - pad))


def render_inspector_image(account_info: dict, skin_rows: list, output_path: str,
                           *, mask_id: bool = False, logo_path: Optional[str] = None,
                           banner_art_path: Optional[str] = None) -> bool:
    """Render the shareable VSTORE inspector card (tactical / clean). True on success.

    Note: the daily store is intentionally NOT drawn here — it's shown live on
    the web (it rotates every 24h), so baking it into a static image would go
    stale. See ``get_daily_store`` for the web-facing data.
    """
    if not PIL_AVAILABLE:
        return False

    skin_rows = sort_skins_for_inspector(list(skin_rows or []))

    tier_catalog = _safe("tiers", get_competitive_tier_catalog) or {}
    season_catalog = _safe("seasons", get_seasons_catalog) or {"latest_act": None, "seasons": {}}
    card_catalog = _safe("cards", get_player_card_catalog) or {}
    content_tier_catalog = _safe("ctiers", get_content_tier_catalog) or {}
    currency_catalog = _safe("currencies", get_currency_catalog) or {}

    # Load custom card frame
    _card_frame_img = None
    _frames_dir = os.path.join(os.path.dirname(__file__), "assets", "frames")
    
    fpath = os.path.join(_frames_dir, "vstore-card-210x150.png")
    if os.path.exists(fpath):
        try:
            _card_frame_img = Image.open(fpath).convert("RGBA")
        except Exception:
            pass

    info = account_info or {}
    wallet = info.get("wallet") or {}
    counts = info.get("counts") or {}
    loadout = info.get("loadout") or {}
    identity = loadout.get("Identity") or {}

    # ===== Layout =====
    W = 1600
    PAD = 36
    BANNER_H = 184
    INFO_H = 120
    RANK_H = 92
    FOOTER_H = 56
    GAP = 16
    inner_w = W - PAD * 2

    cols = 7
    tile_w = 210
    tile_h = 150
    tgap = (inner_w - cols * tile_w) // (cols - 1)
    n_skins = len(skin_rows or [])
    rows_count = math.ceil(n_skins / cols) if n_skins else 0
    grid_h = rows_count * (tile_h + tgap) - (tgap if rows_count else 0)

    H = (PAD + BANNER_H + GAP + INFO_H + GAP + RANK_H + GAP
         + (grid_h + GAP if rows_count else 0) + FOOTER_H + PAD)

    resampling = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS

    # ===== Background: ink gradient + diagonal weave + red corner glow =====
    canvas = Image.new("RGBA", (W, H), _INK_TOP[:3] + (255,))
    canvas.alpha_composite(_vertical_gradient(W, H, _INK_TOP, _INK_BOT), (0, 0))

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    for r, a in [(760, 26), (520, 30), (320, 34)]:
        gdraw.ellipse((-260, -300, -260 + r * 2, -300 + r * 2), fill=_RED + (a,))
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(70)))

    weave = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    wd = ImageDraw.Draw(weave)
    for i in range(0, W + H, 46):
        wd.line([(i, 0), (i - H, H)], fill=(255, 255, 255, 5), width=1)
    canvas.alpha_composite(weave)

    draw = ImageDraw.Draw(canvas)

    # ===== Fonts (role-based; bundled fonts in assets/fonts win) =====
    # Anton (display) ONLY for fixed ASCII headings + digits — it has no
    # diacritics and odd vertical metrics. Everything else uses Chakra Petch
    # (heading, diacritics-safe) / JetBrains Mono (labels).
    f_brand   = _load_font(50, "display")
    f_num     = _load_font(38, "display")
    f_name    = _load_font(34, "heading")
    f_head    = _load_font(24, "heading")
    f_sub     = _load_font(19, "heading")
    f_val     = _load_font(19, "heading")
    f_name_sm = _load_font(15, "heading")   # per-skin name on grid cards
    f_mono    = _load_font(13, "mono")
    f_lab     = _load_font(11, "mono")
    f_watermark = _load_font(34, "display")
    f_card_title = _load_font(14, "heading")
    f_card_type = _load_font(10, "mono")
    f_tiny_v    = _load_font(7, "mono")

    def tx(x, cy, s, font, fill, anchor="lm"):
        """Draw text positioned by vertical center (anchor-based) so layout is
        stable regardless of each font's ascent/line-gap."""
        draw.text((x, cy), s, font=font, fill=fill, anchor=anchor)

    def red_accent(x, y, h):
        """Signature 4px red left bar for the tactical look."""
        draw.rectangle((x, y + 8, x + 3, y + h - 8), fill=_RED)

    # ============ BANNER ============
    by = PAD
    banner = _rounded((inner_w, BANNER_H), 18, _CARD, border=_HAIR, border_w=1)
    bgrad = _vertical_gradient(inner_w, BANNER_H, (28, 33, 43, 255), (15, 18, 24, 255))
    bmask = Image.new("L", (inner_w, BANNER_H), 0)
    ImageDraw.Draw(bmask).rounded_rectangle((0, 0, inner_w - 1, BANNER_H - 1), radius=18, fill=255)
    bgrad.putalpha(bmask)
    banner.alpha_composite(bgrad)

    # Right-side decorative art: player wide card art, else a banner asset.
    art = None
    card_meta = card_catalog.get(identity.get("PlayerCardID")) or {}
    art = _fetch_image(card_meta.get("wide") or card_meta.get("large"))
    if art is None and banner_art_path and os.path.exists(banner_art_path):
        try:
            art = Image.open(banner_art_path).convert("RGBA")
        except Exception:
            art = None
    if art is not None:
        art_w = int(inner_w * 0.46)
        art_fit = ImageOps.fit(art, (art_w, BANNER_H), method=resampling)
        art_fit.putalpha(_hgradient_mask(art_w, BANNER_H, fade_frac=0.6))
        art_layer = Image.new("RGBA", (inner_w, BANNER_H), (0, 0, 0, 0))
        art_layer.alpha_composite(art_fit, (inner_w - art_w, 0))
        art_layer.putalpha(Image.composite(art_layer.split()[-1], Image.new("L", (inner_w, BANNER_H), 0), bmask))
        banner.alpha_composite(art_layer)
        # red hairline where the art begins
        ImageDraw.Draw(banner).line(
            [(inner_w - art_w, 18), (inner_w - art_w, BANNER_H - 18)], fill=_RED + (120,), width=1)

    ImageDraw.Draw(banner).rounded_rectangle(
        (0, 0, inner_w - 1, BANNER_H - 1), radius=18, outline=_HAIR, width=1)
    canvas.alpha_composite(banner, (PAD, by))
    red_accent(PAD, by, BANNER_H)

    # Logo + wordmark
    text_x = PAD + 40
    if logo_path and os.path.exists(logo_path):
        try:
            logo_img = Image.open(logo_path).convert("RGBA")
            logo_img.thumbnail((132, 132), resampling)
            canvas.alpha_composite(logo_img, (PAD + 34, by + (BANNER_H - logo_img.height) // 2))
            text_x = PAD + 188
        except Exception:
            pass

    tx(text_x, by + 52, "VSTORE", f_sub, _RED)
    tx(text_x - 1, by + 90, "INSPECTOR", f_brand, _BONE)
    tx(text_x, by + 130, "VALORANT   SKIN   INSPECTOR", f_lab, _BONE_MUTE)
    tx(PAD + inner_w - 28, by + BANNER_H - 26, "vstore.lol", f_mono, _GOLD, anchor="rm")

    # ============ INFO STRIP ============
    iy = by + BANNER_H + GAP
    canvas.alpha_composite(_rounded((inner_w, INFO_H), 16, _CARD, border=_HAIR, border_w=1), (PAD, iy))
    red_accent(PAD, iy, INFO_H)

    av = 84
    ax = PAD + 28
    ay = iy + (INFO_H - av) // 2
    card_img = _fetch_image(card_meta.get("icon") or card_meta.get("small"))
    canvas.alpha_composite(_rounded((av, av), 12, _CARD_HI, border=_RED + (140,), border_w=2), (ax, ay))
    if card_img is not None:
        inner = av - 8
        fitted = ImageOps.fit(card_img, (inner, inner), method=resampling)
        m = Image.new("L", (inner, inner), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, inner - 1, inner - 1), radius=9, fill=255)
        fitted.putalpha(m)
        canvas.alpha_composite(fitted, (ax + 4, ay + 4))

    nx = ax + av + 22
    display_name = info.get("display_name") or ""
    if mask_id:
        display_name = mask_riot_display_name(display_name)
    tx(nx, iy + 34, "RIOT ID", f_lab, _RED)
    tx(nx, iy + 62, display_name or "Unknown", f_name, _BONE)

    now_str = (datetime.utcnow() + timedelta(hours=7)).strftime("%H:%M · %d/%m/%Y")
    meta = f"REGION {(info.get('shard') or '—').upper()}     CẬP NHẬT {now_str}"
    tx(nx, iy + INFO_H - 24, meta, f_mono, _BONE_DIM)

    # LEVEL block on the right (replaces the old price box — clean)
    level = info.get("account_level") or identity.get("AccountLevel") or 0
    lx = PAD + inner_w - 28
    tx(lx, iy + 36, "LEVEL", f_lab, _BONE_MUTE, anchor="rm")
    lvl_txt = str(level)
    lvl_w = draw.textlength(lvl_txt, font=f_num)
    tx(lx, iy + 70, lvl_txt, f_num, _BONE, anchor="rm")
    draw.rectangle((lx - lvl_w, iy + 92, lx, iy + 95), fill=_RED)

    # ============ RANK + STATS ============
    ry = iy + INFO_H + GAP
    canvas.alpha_composite(_rounded((inner_w, RANK_H), 16, _CARD, border=_HAIR, border_w=1), (PAD, ry))
    red_accent(PAD, ry, RANK_H)

    rank_meta = tier_catalog.get(info.get("rank_tier") or 0) or tier_catalog.get(0) or {}
    rank_color = _hex_to_rgba(rank_meta.get("color"), 255) if rank_meta.get("color") else _BONE
    rank_icon = _fetch_image(rank_meta.get("icon")) if rank_meta.get("icon") else None
    rx = PAD + 28
    off = 0
    if rank_icon:
        rank_icon.thumbnail((60, 60), resampling)
        canvas.alpha_composite(rank_icon, (rx, ry + (RANK_H - rank_icon.height) // 2))
        off = 70

    act_name = format_act_name(season_catalog.get("latest_act") or {}, season_catalog.get("seasons") or {}) or "VALORANT"
    rank_name = (rank_meta.get("name") or "UNRATED").upper()
    tx(rx + off, ry + 34, act_name.upper(), f_lab, _BONE_MUTE)
    tx(rx + off, ry + 58, rank_name, f_head, rank_color[:3])
    if info.get("ranked_rating"):
        rrw = draw.textlength(rank_name, font=f_head)
        tx(rx + off + rrw + 12, ry + 60, f"{info['ranked_rating']} RR", f_mono, _BONE_DIM)

    # Stat blocks, right-aligned. Wallet blocks carry currency icons.
    blocks = [
        ("AGENTS", counts.get("agents", 0), _BONE, None),
        ("SPRAYS", counts.get("sprays", 0), _BONE, None),
        ("BUDDIES", counts.get("buddies", 0), _BONE, None),
        ("CARDS", counts.get("cards", 0), _BONE, None),
        ("TITLES", counts.get("titles", 0), _BONE, None),
        ("VP", wallet.get("vp", 0), _GOLD, CURRENCY_VP),
        ("RP", wallet.get("rp", 0), _TEAL, CURRENCY_RP),
        ("KC", wallet.get("kc", 0), _RED, CURRENCY_KC),
    ]
    bw, bh, bgap = 86, 60, 6
    total_bw = bw * len(blocks) + bgap * (len(blocks) - 1)
    bx0 = PAD + inner_w - 24 - total_bw
    byy = ry + (RANK_H - bh) // 2
    for i, (label, val, color, cur_uuid) in enumerate(blocks):
        x = bx0 + i * (bw + bgap)
        canvas.alpha_composite(_rounded((bw, bh), 10, _CARD_HI, border=_HAIR, border_w=1), (x, byy))
        icon = None
        if cur_uuid and currency_catalog.get(cur_uuid):
            icon = _fetch_image(currency_catalog[cur_uuid])
        if icon is not None:
            icon.thumbnail((14, 14), resampling)
            tlw = draw.textlength(label, font=f_lab)
            gw = icon.width + 4 + tlw
            ix = x + (bw - gw) // 2
            canvas.alpha_composite(icon, (int(ix), byy + 14))
            tx(ix + icon.width + 4, byy + 21, label, f_lab, _BONE_MUTE)
        else:
            tx(x + bw // 2, byy + 21, label, f_lab, _BONE_MUTE, anchor="mm")
        vtxt = f"{val:,}" if isinstance(val, int) and val >= 1000 else str(val)
        tx(x + bw // 2, byy + 42, vtxt, f_val, color, anchor="mm")

    # ----- shared skin-card renderer (used by daily store + owned grid) -----
    def fit_text(s, font, max_w):
        """Truncate `s` with an ellipsis so it fits within `max_w` px."""
        if draw.textlength(s, font=font) <= max_w:
            return s
        while s and draw.textlength(s + "…", font=font) > max_w:
            s = s[:-1]
        return (s.rstrip() + "…") if s else ""

    def render_card(cx, cy, cw, ch, row):
        if _card_frame_img:
            # Resize the template to match cell size (cw, ch)
            tile = _card_frame_img.resize((cw, ch), resampling)
            tile_draw = ImageDraw.Draw(tile)
        else:
            # Fallback to manual drawing if template is not found
            tile = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            tile_draw = ImageDraw.Draw(tile)
            bg_grad = _vertical_gradient(cw, ch, (33, 35, 40, 255), (18, 20, 25, 255))
            tile.alpha_composite(bg_grad, (0, 0))
            # Watermark VSTORE (centered and faint)
            tile_draw.text((cw // 2, ch // 2), "VSTORE", font=f_watermark, fill=(255, 255, 255, 3), anchor="mm")

        # ── Skin Image: Centered with drop shadow ─────────────────────────
        icon_url = row.get("render_icon_url") or row.get("icon_url") if row else None
        gimg = _fetch_image(icon_url) if icon_url else None
        if gimg is not None:
            gimg.thumbnail((170, 80), resampling)
            ix = (cw - gimg.width) // 2
            iy = (ch - gimg.height) // 2
            _draw_drop_shadow(tile, gimg, (ix, iy))
            tile.alpha_composite(gimg, (ix, iy))

        # ── Card Bottom: Skin Name and Type ───────────────────────────────
        name = row.get("display_name") or "Unknown Skin" if row else "Unknown Skin"
        name_truncated = fit_text(name, f_card_title, cw - 24)
        tile_draw.text((12, ch - 26), name_truncated, font=f_card_title, fill=(244, 244, 244, 255), anchor="lm")

        # Skin classification label
        if row:
            is_melee = row.get("weapon_category", "").lower() == "melee"
            cat_text = "MELEE" if is_melee else "WEAPON"
        else:
            cat_text = "WEAPON"
        tile_draw.text((12, ch - 12), cat_text, font=f_card_type, fill=_BONE_DIM, anchor="lm")

        # ── Card Border outline (only if no template loaded)
        if not _card_frame_img:
            tile_draw.rounded_rectangle(
                (0, 0, cw - 1, ch - 1),
                radius=14,
                outline=(255, 255, 255, 22),
                width=1
            )

        # ── Apply clipping mask and alpha composite onto canvas ───────────
        tile_mask = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(tile_mask).rounded_rectangle((0, 0, cw - 1, ch - 1), radius=14, fill=255)
        tile.putalpha(tile_mask)
        canvas.alpha_composite(tile, (cx, cy))

    # ============ OWNED SKIN GRID ============
    if rows_count:
        gy = ry + RANK_H + GAP
        for idx, row in enumerate(skin_rows or []):
            render_card(PAD + (idx % cols) * (tile_w + tgap),
                        gy + (idx // cols) * (tile_h + tgap),
                        tile_w, tile_h, row)

    # ============ FOOTER ============
    fy = H - PAD - FOOTER_H
    canvas.alpha_composite(_rounded((inner_w, FOOTER_H), 12, _CARD, border=_HAIR, border_w=1), (PAD, fy))
    brand = "VSTORE"
    domain = "   ·   vstore.lol"
    tail = "   ·   PUBLIC SKIN INSPECTOR"
    bw_ = draw.textlength(brand, font=f_sub)
    dw_ = draw.textlength(domain, font=f_mono)
    tw_ = draw.textlength(tail, font=f_mono)
    start = (W - (bw_ + dw_ + tw_)) // 2
    fcy = fy + FOOTER_H // 2
    tx(start, fcy, brand, f_sub, _RED)
    tx(start + bw_, fcy, domain, f_mono, _GOLD)
    tx(start + bw_ + dw_, fcy, tail, f_mono, _BONE_DIM)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    canvas.convert("RGB").save(output_path, format="PNG", optimize=True, compress_level=6)
    return True
