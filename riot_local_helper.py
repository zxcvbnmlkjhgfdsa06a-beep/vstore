from __future__ import annotations

import base64
import json
import os
import re
import ssl
import subprocess
import time
import unicodedata
import html as html_lib
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HOST = "127.0.0.1"
PORT = int(os.environ.get("VSTORE_LCU_HELPER_PORT", "17321"))
ALLOWED_ORIGIN = os.environ.get("VSTORE_WEB_ORIGIN", "*")

_ssl = ssl.create_default_context()
_ssl.check_hostname = False
_ssl.verify_mode = ssl.CERT_NONE


def _arg(cmd: str, name: str) -> str | None:
    m = re.search(r'(?:^|\s)"?--' + re.escape(name) + r'=([^"\s]+)"?', cmd or "")
    return m.group(1) if m else None


def _powershell_processes() -> list[dict]:
    ps = r'''Get-CimInstance Win32_Process | Where-Object {$_.Name -eq "LeagueClientUx.exe"} | Select-Object Name,ProcessId,CommandLine | ConvertTo-Json -Compress'''
    try:
        cp = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=8, encoding="utf-8", errors="replace",
        )
        raw = (cp.stdout or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        return []


def _lockfile_candidates() -> list[Path]:
    out = []
    env = os.environ.get("LEAGUE_INSTALL_DIR")
    if env:
        out.append(Path(env) / "lockfile")
    for drive in "CDEFG":
        out.extend([
            Path(f"{drive}:/Riot Games/League of Legends/lockfile"),
            Path(f"{drive}:/Games/League of Legends/lockfile"),
        ])
    return out


def discover_lcu() -> dict:
    candidates = []
    for p in _powershell_processes():
        cmd = p.get("CommandLine") or ""
        port = _arg(cmd, "app-port")
        token = _arg(cmd, "remoting-auth-token")
        if port and token:
            candidates.append({"port": int(port), "token": token, "pid": p.get("ProcessId"), "source": "LeagueClientUx.exe"})

    for lf in _lockfile_candidates():
        try:
            if not lf.exists():
                continue
            parts = lf.read_text(encoding="utf-8", errors="ignore").strip().split(":")
            if len(parts) >= 5:
                candidates.append({"port": int(parts[2]), "token": parts[3], "pid": int(parts[1]), "source": str(lf)})
        except Exception:
            continue

    seen = set()
    for c in candidates:
        key = (c["port"], c["token"])
        if key in seen:
            continue
        seen.add(key)
        try:
            status, data, _ = lcu_request(c, "/lol-summoner/v1/current-summoner")
            if status == 200 and isinstance(data, dict):
                c["summoner"] = data
                return c
        except Exception:
            pass
    raise RuntimeError("Không tìm thấy LeagueClientUx/LCU đang hoạt động. Hãy mở League of Legends tới màn hình chính rồi thử lại.")


def lcu_request(lcu: dict, path: str, raw: bool = False):
    url = f"https://127.0.0.1:{lcu['port']}{path}"
    auth = base64.b64encode(f"riot:{lcu['token']}".encode()).decode()
    req = Request(url, headers={"Authorization": f"Basic {auth}", "Accept": "application/json", "User-Agent": "VSTORE-LCU-Helper/99.0"})
    try:
        with urlopen(req, context=_ssl, timeout=8) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "application/octet-stream")
            if raw:
                return resp.status, body, ctype
            if not body:
                return resp.status, None, ctype
            try:
                return resp.status, json.loads(body.decode("utf-8")), ctype
            except Exception:
                return resp.status, body.decode("utf-8", errors="replace"), ctype
    except HTTPError as e:
        body = e.read()
        if raw:
            return e.code, body, e.headers.get("Content-Type", "application/octet-stream")
        try:
            return e.code, json.loads(body.decode("utf-8")), e.headers.get("Content-Type", "")
        except Exception:
            return e.code, body.decode("utf-8", errors="replace"), e.headers.get("Content-Type", "")


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _bool_owned(o: dict) -> bool | None:
    if "owned" in o:
        return bool(o.get("owned"))
    if "isOwned" in o:
        return bool(o.get("isOwned"))
    own = o.get("ownership")
    if isinstance(own, dict):
        if "owned" in own:
            return bool(own.get("owned"))
        if "isOwned" in own:
            return bool(own.get("isOwned"))
    return None


def _skin_id(o: dict):
    """Return the canonical LoL skin id when possible.

    Prefer skinId over generic id/itemId so nested chroma/store objects do not
    create duplicate rows for the same base skin.
    """
    for k in ("skinId", "id", "itemId"):
        v = o.get(k)
        if isinstance(v, int) and v > 0:
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def _image_path(o: dict) -> str:
    for k in ("tilePath", "loadoutsIcon", "splashPath", "uncenteredSplashPath", "loadScreenPath", "iconPath"):
        v = o.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _normalize_rarity_text(value) -> str:
    if value is None:
        return ""
    raw = str(value).strip().lower()
    raw = raw.replace("k", "", 1) if raw.startswith("k") else raw
    raw = raw.replace("_", " ").replace("-", " ")
    return raw


def _tier_color(tier: str) -> str:
    return {
        "Transcendent":"#ff8a3d", "Exalted":"#e7d36f", "Ultimate":"#f3c54b",
        "Mythic":"#ef476f", "Legendary":"#f6c445", "Epic":"#a65cff",
        "Rare":"#35a7ff", "Special":"#2fc7d5", "Standard":"#94a3b8",
    }.get(str(tier), "#94a3b8")


def _tier_from(o: dict, price: int | None = None, *, tft: bool = False) -> tuple[str, str]:
    """Use Riot game data / LCU rarity as the source of truth.

    No rarity is inferred from RP. LoL reads rarity/skinTier/rarityName and TFT
    reads TFTRarity; rarityValue is used only for old TFT payloads that omit it.
    """
    fields = ("TFTRarity", "rarity", "rarityName", "skinTier", "tier", "rarityLabel", "collectionRarity") if tft else ("rarity", "rarityName", "skinTier", "tier", "rarityLabel", "collectionRarity")
    values = [str(o.get(k)) for k in fields if o.get(k) not in (None, "")]
    for k in ("rarityGemPath", "rarityGem", "rarityIconPath"):
        if o.get(k): values.append(str(o.get(k)))
    raw = " ".join(values).lower().replace("_", " ").replace("-", " ")
    for needle, label, color in (
        ("transcendent", "Transcendent", "#ff8a3d"),
        ("exalted", "Exalted", "#e7d36f"),
        ("ultimate", "Ultimate", "#f3c54b"),
        ("mythic", "Mythic", "#ef476f"),
        ("prestige", "Mythic", "#ef476f"),
        ("legendary", "Legendary", "#f6c445"),
        ("epic", "Epic", "#a65cff"),
        ("rare", "Rare", "#35a7ff"),
        ("special", "Special", "#2fc7d5"),
    ):
        if needle in raw:
            return label, color
    if any(x in raw for x in ("norarity", "no rarity", "standard", "common")):
        return "Standard", "#94a3b8"
    if tft:
        try: rv = int(o.get("rarityValue"))
        except Exception: rv = None
        if rv is not None:
            if rv >= 4: return "Mythic", "#ef476f"
            if rv == 3: return "Legendary", "#f6c445"
            if rv == 2: return "Epic", "#a65cff"
            if rv == 1: return "Rare", "#35a7ff"
    return "Standard", "#94a3b8"

def _extract_price(o: dict) -> int | None:
    for k, v in o.items():
        kl = str(k).lower()
        if isinstance(v, (int, float)) and ("rp" in kl or kl in ("price", "cost", "storeprice")) and 0 < int(v) < 100000:
            return int(v)
    for v in o.values():
        if isinstance(v, dict):
            p = _extract_price(v)
            if p:
                return p
    return None


def _game_data_skin_map(lcu: dict) -> dict[int, dict]:
    for path in ("/lol-game-data/assets/v1/skins.json", "/lol-game-data/assets/v1/skin-lines.json"):
        status, data, _ = lcu_request(lcu, path)
        if status == 200:
            out = {}
            for o in _walk(data):
                sid = _skin_id(o)
                if sid and (o.get("name") or o.get("displayName")):
                    out.setdefault(sid, {}).update(o)
            if out:
                return out
    return {}


def _catalog_price_map(lcu: dict) -> dict[int, int]:
    out = {}
    for path in ("/lol-store/v1/catalog", "/lol-store/v1/catalog?inventoryType=CHAMPION_SKIN"):
        try:
            status, data, _ = lcu_request(lcu, path)
        except Exception:
            continue
        if status != 200:
            continue
        for o in _walk(data):
            sid = _skin_id(o)
            p = _extract_price(o)
            if sid and p:
                out[sid] = p
        if out:
            break
    return out



CACHE_DIR = Path(__file__).resolve().parent / ".cache"
META_CACHE_FILE = CACHE_DIR / "vstore_external_meta.json"
_EXTERNAL_CACHE = None
_CACHE_LOCK = threading.Lock()
_CDRAGON_LOL = None
_CDRAGON_TFT = None


def _load_meta_cache() -> dict:
    global _EXTERNAL_CACHE
    if _EXTERNAL_CACHE is not None:
        return _EXTERNAL_CACHE
    try:
        _EXTERNAL_CACHE = json.loads(META_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        _EXTERNAL_CACHE = {"opgg": {}, "tftskins": {}}
    return _EXTERNAL_CACHE


def _save_meta_cache():
    try:
        with _CACHE_LOCK:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = META_CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(_load_meta_cache(), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(META_CACHE_FILE)
    except Exception:
        pass


def _web_get_text(url: str, timeout: int = 8) -> str:
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    })
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower().replace("&", " and ").replace("'", "").replace("’", "")
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value


def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v)


def _json_scripts(text: str):
    # Next.js / dehydrated page data. Parse whatever valid JSON scripts exist.
    for m in re.finditer(r'<script[^>]*>(.*?)</script>', text, re.I | re.S):
        body = html_lib.unescape(m.group(1)).strip()
        if not body or body[0:1] not in ('{','['):
            continue
        try:
            yield json.loads(body)
        except Exception:
            continue


def _tier_name(v) -> str | None:
    raw = str(v or "").strip().lower()
    options = {
        "transcendent":"Transcendent", "exalted":"Exalted", "ultimate":"Ultimate",
        "mythic":"Mythic", "legendary":"Legendary", "epic":"Epic", "rare":"Rare",
        "common":"Standard", "standard":"Standard",
    }
    for k, label in options.items():
        if k in raw:
            return label
    return None


def _extract_external_fields(obj: dict) -> tuple[str|None, int|None]:
    tier = None; price = None
    for k, v in obj.items():
        kl = str(k).lower()
        if tier is None and any(x in kl for x in ("tier", "rarity", "grade")):
            tier = _tier_name(v)
        if price is None and isinstance(v, (int, float)) and any(x in kl for x in ("price", "rp", "cost")):
            iv = int(v)
            if 0 < iv < 100000:
                price = iv
    return tier, price


def _parse_site_meta(text: str, target_name: str, site: str) -> dict:
    norm_target = _norm_name(target_name)
    best = {"tier": None, "price_rp": None}
    # Prefer structured JSON object whose name/title matches target.
    for root in _json_scripts(text):
        for d in _walk_dicts(root):
            vals = [str(d.get(k) or "") for k in ("name","title","displayName","cosmeticName","skinName")]
            if vals and any(_norm_name(v) == norm_target for v in vals if v):
                tier, price = _extract_external_fields(d)
                if tier: best["tier"] = tier
                if price: best["price_rp"] = price
                if best["tier"] and best["price_rp"]: return best
    # Visible text fallback works with both OP.GG and TFTSkins pages.
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    plain = re.sub(r"\s+", " ", plain)
    known = ["Transcendent","Exalted","Ultimate","Mythic","Legendary","Epic","Rare","Common","Standard"]
    # look near title first, then whole page
    pos = plain.lower().find(target_name.lower()) if target_name else -1
    around = plain[max(0,pos-180):pos+500] if pos >= 0 else plain[:3000]
    for label in known:
        if re.search(r"\b"+re.escape(label)+r"\b", around, re.I):
            best["tier"] = "Standard" if label == "Common" else label
            break
    if site == "opgg":
        # OP.GG detail page places price directly below skin title. Also accept explicit RP.
        m = re.search(r"(?:"+re.escape(target_name)+r")[\s\S]{0,220}?\b(3250|2775|1820|1350|975|750|520|390)\b", plain, re.I)
        if not m:
            m = re.search(r"\b(3250|2775|1820|1350|975|750|520|390)\s*(?:RP)?\b", around, re.I)
        if m: best["price_rp"] = int(m.group(1))
    else:
        m = re.search(r"Price\s*([0-9]{2,5})\s*RP", plain, re.I)
        if not m:
            m = re.search(r"\b([0-9]{2,5})\s*RP\b", around, re.I)
        if m: best["price_rp"] = int(m.group(1))
    return best


def _load_cdragon_lol() -> tuple[dict, dict]:
    global _CDRAGON_LOL
    if _CDRAGON_LOL is not None:
        return _CDRAGON_LOL
    skin_map={}; champ_map={}
    try:
        skins=json.loads(_web_get_text('https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/en_us/v1/skins.json', 12))
        for o in _walk_dicts(skins):
            sid=_skin_id(o)
            if sid and o.get('name'): skin_map[int(sid)] = str(o.get('name'))
    except Exception: pass
    try:
        champs=json.loads(_web_get_text('https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/en_us/v1/champions.json', 12))
        for o in _walk_dicts(champs):
            cid=o.get('id')
            if isinstance(cid,int) and (o.get('alias') or o.get('name')):
                champ_map[cid]=str(o.get('alias') or o.get('name'))
    except Exception: pass
    _CDRAGON_LOL=(skin_map,champ_map)
    return _CDRAGON_LOL


def _load_cdragon_tft() -> dict:
    global _CDRAGON_TFT
    if _CDRAGON_TFT is not None: return _CDRAGON_TFT
    out={}
    try:
        data=json.loads(_web_get_text('https://raw.communitydragon.org/latest/cdragon/tft/en_us.json', 15))
        for o in _walk_dicts(data):
            cid=o.get('contentId') or o.get('contentID')
            nm=o.get('name') or o.get('displayName')
            if cid and nm: out[str(cid).lower()] = str(nm)
    except Exception: pass
    _CDRAGON_TFT=out
    return out


def _opgg_meta(skin_id: int, fallback_name: str, champion_id: int) -> dict:
    cache=_load_meta_cache()["opgg"]
    key=str(skin_id)
    if key in cache: return cache[key]
    skin_names, champs = _load_cdragon_lol()
    name=skin_names.get(int(skin_id)) or fallback_name
    champ=champs.get(int(champion_id)) or ""
    result={"tier":None,"price_rp":None,"source":"OP.GG","source_url":""}
    if name and champ:
        url=f"https://op.gg/lol/skins/{_slugify(champ)}/{_slugify(name)}"
        result["source_url"]=url
        try:
            result.update(_parse_site_meta(_web_get_text(url), name, "opgg"))
        except Exception:
            pass
    cache[key]=result; _save_meta_cache()
    return result


def _tftskins_meta(content_id: str, fallback_name: str) -> dict:
    cache=_load_meta_cache()["tftskins"]
    key=str(content_id or _norm_name(fallback_name))
    if key in cache: return cache[key]
    en_map=_load_cdragon_tft()
    name=en_map.get(str(content_id).lower()) or fallback_name
    result={"tier":None,"price_rp":None,"source":"TFTSkins","source_url":""}
    if name:
        # TFTSkins uses hyphen slugs, not underscore slugs.
        slug=_slugify(name).replace('_','-')
        url=f"https://tftskins.com/cosmetic/{slug}"
        result["source_url"]=url
        try:
            result.update(_parse_site_meta(_web_get_text(url), name, "tftskins"))
        except Exception:
            pass
    cache[key]=result; _save_meta_cache()
    return result

def _norm_name(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (v or "").lower())


def _canonical_lol_key(skin_id: int, meta: dict, name: str, image_path: str) -> tuple:
    champ = meta.get("championId")
    try:
        champ = int(champ) if champ is not None else int(skin_id) // 1000
    except Exception:
        champ = 0
    # Prefer Riot skin id. Secondary fields prevent duplicate wrapper/chroma rows.
    return (int(skin_id), champ, _norm_name(name), (image_path or "").lower())


def _inventory_owned_skin_ids(lcu: dict) -> tuple[set[int], dict]:
    """Return skin ids that are permanently owned according to lol-inventory.

    This endpoint explicitly distinguishes OWNED, RENTED and F2P.  We use it as
    the authoritative ownership source so temporary/rental/free access is not
    reported as part of the user's permanent collection.
    """
    owned: set[int] = set()
    debug = {"status": 0, "owned": 0, "rented": 0, "f2p": 0, "timed": 0, "unknown": 0}
    try:
        st, data, _ = lcu_request(lcu, "/lol-inventory/v2/inventory/CHAMPION_SKIN")
        debug["status"] = st
    except Exception:
        return owned, debug
    if st != 200:
        return owned, debug

    for o in _walk(data):
        if not isinstance(o, dict):
            continue
        iid = o.get("itemId")
        try:
            iid = int(iid)
        except Exception:
            continue
        if iid <= 0:
            continue

        typ = str(o.get("ownershipType") or "").upper().strip()
        rental = o.get("rental")
        f2p = o.get("f2p")
        expiration = str(o.get("expirationDate") or "").strip()

        if typ == "RENTED" or rental is True:
            debug["rented"] += 1
            continue
        if typ == "F2P" or f2p is True:
            debug["f2p"] += 1
            continue
        # A non-empty expiration is a timed entitlement unless Riot explicitly
        # marks the inventory item as permanently OWNED.
        if expiration and typ != "OWNED":
            debug["timed"] += 1
            continue
        if typ == "OWNED":
            owned.add(iid); debug["owned"] += 1
            continue

        # Some client builds return the older DTO without ownershipType.  Only
        # accept it when it explicitly says it is neither rental nor F2P and
        # there is no expiration date.
        if rental is False and f2p is False and not expiration:
            owned.add(iid); debug["owned"] += 1
        else:
            debug["unknown"] += 1
    return owned, debug


def _minimal_skin_permanently_owned(o: dict) -> bool:
    """Strict fallback when lol-inventory is unavailable.

    `ownership.owned` by itself is not enough for our use-case: reject rental,
    free-to-play rewards and any explicit temporary entitlement markers.
    """
    own = o.get("ownership") if isinstance(o.get("ownership"), dict) else {}
    if not bool(own.get("owned", o.get("owned", False))):
        return False
    rental = own.get("rental") if isinstance(own.get("rental"), dict) else {}
    if rental.get("rented") is True or o.get("rental") is True:
        return False
    if own.get("freeToPlayReward") is True or o.get("freeToPlayReward") is True or o.get("f2p") is True:
        return False
    return True


def collect_lol_skins(lcu: dict, summoner: dict) -> list[dict]:
    summoner_id = summoner.get("summonerId") or summoner.get("accountId")
    if not summoner_id:
        return []

    # 1) Metadata / display data from skins-minimal.
    st, payload, _ = lcu_request(lcu, f"/lol-champions/v1/inventories/{summoner_id}/skins-minimal")
    if st != 200 or payload is None:
        return []

    # 2) Authoritative permanent ownership from lol-inventory.
    owned_ids, inv_debug = _inventory_owned_skin_ids(lcu)
    strict_inventory_available = inv_debug.get("status") == 200 and bool(owned_ids)

    gd = _game_data_skin_map(lcu)
    prices = _catalog_price_map(lcu)
    by_id: dict[int, dict] = {}

    for o in _walk(payload):
        if not isinstance(o, dict):
            continue
        # skins-minimal uses `id` in current/legacy schemas.  Some builds also
        # expose skinId, so support both.
        raw_id = o.get("skinId", o.get("id"))
        try:
            skin_id = int(raw_id)
        except Exception:
            continue
        if skin_id <= 0:
            continue
        # Avoid chroma/nested objects accidentally discovered by the walker.
        if not ("championId" in o or "ownership" in o or "isBase" in o):
            continue

        if strict_inventory_available:
            if skin_id not in owned_ids:
                continue
            ownership_source = "lol-inventory:OWNED"
        else:
            if not _minimal_skin_permanently_owned(o):
                continue
            ownership_source = "skins-minimal:fallback"

        game_meta = dict(gd.get(skin_id) or {})
        meta = dict(o)
        meta.update({k: v for k, v in game_meta.items() if v not in (None, "", [])})
        name = meta.get("name") or meta.get("displayName") or meta.get("skinName") or f"Skin {skin_id}"
        image_path = _image_path(meta)
        lname = str(name).strip().lower()
        is_base = bool(meta.get("isBase")) or lname in ("original", "default") or lname.startswith("original ") or skin_id % 1000 == 0
        if is_base:
            continue

        champ_id = meta.get("championId") or skin_id // 1000
        try:
            champ_id = int(champ_id)
        except Exception:
            champ_id = skin_id // 1000
        price = prices.get(skin_id) or _extract_price(meta)
        tier, color = _tier_from(meta, None, tft=False)
        row = {
            "id": skin_id,
            "name": str(name),
            "champion_id": champ_id,
            "price_rp": price,
            "tier": tier,
            "tier_color": color,
            "image_path": image_path,
            "meta_source": "Riot Game Data",
            "source_url": "",
            "riot_rarity": meta.get("rarity") or meta.get("skinTier") or meta.get("rarityName") or "",
            "ownership_source": ownership_source,
        }
        prev = by_id.get(skin_id)
        if prev is None or (not prev.get("image_path") and row.get("image_path")):
            by_id[skin_id] = row

    # Final de-duplication.  Riot skin id is canonical; the secondary keys only
    # protect against malformed duplicate metadata rows.
    out = []
    seen_ids = set(); seen_names = set(); seen_images = set()
    for row in sorted(by_id.values(), key=lambda x: int(x["id"])):
        rid = int(row["id"])
        champ = int(row.get("champion_id") or 0)
        nkey = (champ, _norm_name(row.get("name") or ""))
        ikey = (champ, (row.get("image_path") or "").lower()) if row.get("image_path") else None
        if rid in seen_ids or nkey in seen_names or (ikey and ikey in seen_images):
            continue
        seen_ids.add(rid); seen_names.add(nkey)
        if ikey: seen_images.add(ikey)
        out.append(row)

    order = {"Transcendent":0,"Exalted":1,"Ultimate":2,"Mythic":3,"Legendary":4,"Epic":5,"Special":6,"Rare":7,"Standard":8}
    return sorted(out, key=lambda x: (order.get(x["tier"],99), x["name"].lower(), x["id"]))

def _tft_inventory_records(lcu: dict, inventory_types) -> tuple[dict[str, dict], dict]:
    """Load authoritative TFT entitlement rows from lol-inventory v2.

    Riot's migration-era lol-cosmetics payload can expose borrowed cosmetics as
    usable. The v2 inventory endpoint is the ownership authority: OWNED means
    permanent collection, LOYALTY means temporary/compensation access, F2P is
    free access and is not counted as owned.
    """
    if isinstance(inventory_types, str):
        inventory_types = [inventory_types]
    debug = {"inventoryType": None, "status": 0, "owned": 0, "loyalty": 0, "f2p": 0, "other": 0}
    for inv_type in inventory_types:
        try:
            st, data, _ = lcu_request(lcu, f"/lol-inventory/v2/inventory/{inv_type}")
        except Exception:
            continue
        if st != 200:
            continue
        rows = {}
        for o in _walk(data):
            if not isinstance(o, dict) or "itemId" not in o:
                continue
            iid = str(o.get("itemId") or "").strip()
            if not iid or iid == "0":
                continue
            typ = str(o.get("ownershipType") or "").upper().strip()
            if typ == "OWNED": debug["owned"] += 1
            elif typ == "LOYALTY": debug["loyalty"] += 1
            elif typ == "F2P": debug["f2p"] += 1
            else: debug["other"] += 1
            # One item id should map to one entitlement row. Prefer an OWNED row
            # if the client ever returns multiple entries for the same item.
            prev = rows.get(iid)
            if prev is None or (str(prev.get("ownershipType") or "").upper() != "OWNED" and typ == "OWNED"):
                rows[iid] = o
        debug["inventoryType"] = inv_type
        debug["status"] = st
        return rows, debug
    return {}, debug


def _tft_entitlement_status(inv: dict | None, endpoint_available: bool, catalog_obj: dict) -> tuple[str, str]:
    """Classify TFT ownership from authoritative inventory entitlement."""
    if isinstance(inv, dict):
        typ = str(inv.get("ownershipType") or "").upper().strip()
        sources = inv.get("loyaltySources") or []
        if isinstance(sources, str):
            sources = [sources]
        sources_text = ",".join(str(x) for x in sources if str(x).strip())
        if typ == "OWNED":
            # IMPORTANT: OWNED wins even if loyaltySources also contains
            # TFT_COMPENSATION. The user's real data confirmed such rows exist.
            return "owned", "ownershipType=OWNED"
        if typ == "LOYALTY":
            return "borrowed", "ownershipType=LOYALTY" + ((";" + sources_text) if sources_text else "")
        if typ == "RENTED":
            return "borrowed", "ownershipType=RENTED"
        if typ == "F2P":
            return "unowned", "ownershipType=F2P"
        return "unknown", "ownershipType=" + (typ or "UNKNOWN")

    # If the authoritative endpoint is healthy and this item has no entitlement
    # row, it is not part of the owned collection.
    if endpoint_available:
        return "unowned", "no-v2-entitlement"

    # No authoritative inventory endpoint: never promote catalog `owned:true`
    # to real ownership. Keep it visible only as unknown for diagnostics.
    if _bool_owned(catalog_obj):
        return "unknown", "v2-inventory-unavailable"
    return "unowned", "owned=false"


def _companion_variant_key(o: dict, name: str) -> tuple:
    """Group Lv1/Lv2/Lv3 of the same Little Legend variant."""
    base = _norm_name(name)
    base = re.sub(r"(?:lv|level|cap|cấp|sao|star)[123]$", "", base, flags=re.I)
    family = _norm_name(str(o.get("species") or o.get("groupName") or o.get("groupId") or ""))
    ctype = _norm_name(str(o.get("companionType") or ""))
    return (base, family, ctype)


def collect_tft(lcu: dict) -> list[dict]:
    # Catalog endpoint + authoritative entitlement inventory type(s).
    kinds = [
        ("companion", "companions", ["COMPANION"]),
        ("arena", "map-skins", ["TFT_MAP_SKIN", "MAP_SKIN"]),
        ("boom", "damage-skins", ["TFT_DAMAGE_SKIN", "DAMAGE_SKIN"]),
        ("zoom", "zoom-skins", ["TFT_ZOOM_SKIN", "ZOOM_SKIN"]),
        ("augment", "augment-pillars", ["TFT_AUGMENT_PILLAR", "AUGMENT_PILLAR"]),
    ]

    raw_rows = []
    seen_exact = set()
    inv_debug = {}

    for kind, endpoint, inv_types in kinds:
        entitlements, dbg = _tft_inventory_records(lcu, inv_types)
        inv_debug[kind] = dbg
        endpoint_available = dbg.get("status") == 200

        st, data, _ = lcu_request(lcu, f"/lol-cosmetics/v1/inventories/tft/{endpoint}")
        if st != 200:
            continue
        for o in _walk(data):
            if not isinstance(o, dict):
                continue
            iid = str(o.get("itemId") or "").strip()
            cid = str(o.get("contentId") or iid or "")
            name = o.get("name")
            if not iid or iid == "0" or not name:
                continue

            inv = entitlements.get(iid)
            status, reason = _tft_entitlement_status(inv, endpoint_available, o)
            if status == "unowned":
                continue

            try:
                level = int(o.get("level") or 0)
            except Exception:
                level = 0

            ek = (kind, iid, cid.lower(), _norm_name(str(name)), level, status)
            if ek in seen_exact:
                continue
            seen_exact.add(ek)
            raw_rows.append((kind, o, inv, cid, iid, str(name), level, status, reason))

    # One card per Little Legend variant. Prefer permanent OWNED over borrowed,
    # then unknown; within the chosen status keep the highest level.
    status_priority = {"owned": 0, "borrowed": 1, "unknown": 2}
    chosen = {}
    passthrough = []
    for row in raw_rows:
        kind, o, inv, cid, iid, name, level, status, reason = row
        if kind != "companion":
            passthrough.append(row)
            continue
        variant = (kind,) + _companion_variant_key(o, name)
        prev = chosen.get(variant)
        if prev is None:
            chosen[variant] = row
            continue
        prev_status, prev_level = prev[7], prev[6]
        if status_priority.get(status, 9) < status_priority.get(prev_status, 9):
            chosen[variant] = row
        elif status == prev_status and level > prev_level:
            chosen[variant] = row

    final_raw = passthrough + list(chosen.values())

    rows = []
    seen_final = set()
    for kind, o, inv, cid, iid, name, level, status, reason in final_raw:
        tier, color = _tier_from(o, None, tft=True)
        fkey = (kind, iid if kind != "companion" else _companion_variant_key(o, name), status)
        if fkey in seen_final:
            continue
        seen_final.add(fkey)
        src = inv if isinstance(inv, dict) else {}
        rows.append({
            "id": cid,
            "item_id": iid,
            "name": name,
            "type": kind,
            "tier": tier,
            "tier_color": color,
            "tft_rarity": o.get("TFTRarity") or o.get("rarity") or "",
            "rarity_value": o.get("rarityValue"),
            "level": level or None,
            "image_path": _image_path(o),
            "price_rp": _extract_price(o),
            "meta_source": "Riot Game Data",
            "source_url": "",
            "ownership_status": status,
            "ownership_reason": reason,
            "ownership_type": src.get("ownershipType") or "",
            "loyalty": o.get("loyalty"),
            "loyalty_sources": src.get("loyaltySources") or o.get("loyaltySources") or [],
            "purchase_date": src.get("purchaseDate") or o.get("purchaseDate") or "",
        })

    order = {"Mythic":0, "Legendary":1, "Epic":2, "Rare":3, "Standard":4}
    own_order = {"owned":0, "borrowed":1, "unknown":2}
    rows = sorted(rows, key=lambda x:(own_order.get(x.get("ownership_status"),9), x["type"], order.get(x["tier"],9), x["name"].lower(), str(x["id"])))
    return rows

def _validation_examples(tft_rows: list[dict]) -> dict:
    """Report the two user-provided reference cosmetics without hardcoding classification."""
    checks = {
        "Thủy Thần Vạc Phép": {"expected":"owned", "tokens":["thuy","than","vac","phep"]},
        "Briar Huyết Nguyệt": {"expected":"borrowed", "tokens":["briar","huyet","nguyet"]},
    }
    out={}
    for label,cfg in checks.items():
        matches=[]
        for row in tft_rows:
            if row.get("type") != "companion": continue
            norm = unicodedata.normalize("NFKD", str(row.get("name") or "")).encode("ascii","ignore").decode().lower()
            if all(tok in norm for tok in cfg["tokens"]):
                matches.append({k:row.get(k) for k in ("name","level","ownership_status","ownership_reason","loyalty","loyalty_sources","purchase_date","tft_rarity")})
        actual = matches[0].get("ownership_status") if matches else None
        out[label]={"expected":cfg["expected"],"actual":actual,"ok":actual==cfg["expected"] if actual else None,"matches":matches}
    return out

def collection_payload() -> dict:
    lcu = discover_lcu()
    summoner = lcu.get("summoner") or {}
    lol = collect_lol_skins(lcu, summoner)
    tft_all = collect_tft(lcu)
    tft_owned = [x for x in tft_all if x.get("ownership_status") == "owned"]
    tft_borrowed = [x for x in tft_all if x.get("ownership_status") == "borrowed"]
    tft_unknown = [x for x in tft_all if x.get("ownership_status") == "unknown"]
    validation = _validation_examples(tft_all)
    return {
        "ok": True,
        "account": {
            "gameName": summoner.get("gameName") or summoner.get("displayName") or "",
            "tagLine": summoner.get("tagLine") or "",
            "level": summoner.get("summonerLevel") or summoner.get("level") or 0,
            "profileIconId": summoner.get("profileIconId"),
        },
        "lol": lol,
        # Keep all accessible TFT rows so the UI can show borrowed/unknown in a
        # separate filter, but collection counts/export default to true owned.
        "tft": tft_all,
        "counts": {
            "lol": len(lol),
            "tft_owned": len(tft_owned),
            "tft_borrowed": len(tft_borrowed),
            "tft_unknown": len(tft_unknown),
            "companions": sum(x["type"] == "companion" for x in tft_owned),
            "arenas": sum(x["type"] == "arena" for x in tft_owned),
            "booms": sum(x["type"] == "boom" for x in tft_owned),
        },
        "ownership_validation": validation,
        "helper": {"version": "101.0", "port": PORT, "ownership_mode":"lol-inventory-v2-authoritative"},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "VSTORELCUHelper/101.0"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._json(200, {"ok": True, "service": "VSTORE LCU Helper", "port": PORT})
        if u.path == "/api/collection":
            try:
                return self._json(200, collection_payload())
            except Exception as e:
                return self._json(503, {"ok": False, "error": str(e)})
        if u.path == "/asset":
            path = (parse_qs(u.query).get("path") or [""])[0]
            if not path.startswith("/lol-game-data/assets/"):
                return self._json(400, {"ok": False, "error": "Chỉ cho phép proxy asset LoL game-data."})
            try:
                lcu = discover_lcu()
                st, body, ctype = lcu_request(lcu, path, raw=True)
                self.send_response(st)
                self._cors()
                self.send_header("Content-Type", ctype or "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json(502, {"ok": False, "error": str(e)})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt, *args):
        return


def main():
    print(f"VSTORE LCU Helper đang chạy tại http://{HOST}:{PORT}")
    print("Giữ cửa sổ này mở khi dùng LoL + TFT Collection trên web.")
    print("Helper chỉ đọc LCU local; auth token không được gửi lên website.")
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
