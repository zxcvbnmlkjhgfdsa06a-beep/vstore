import base64
import ipaddress
import json
import os
import socket
from typing import Optional
from urllib.parse import urlsplit, parse_qs, unquote_plus


RIOT_LOGIN_URL = (
    "https://auth.riotgames.com/authorize"
    "?redirect_uri=http://localhost/redirect"
    "&client_id=riot-client"
    "&response_type=token%20id_token"
    "&nonce=1"
    "&scope=openid%20link%20ban%20lol_region%20account%20email"
)

RIOT_LOGOUT_URL = "https://auth.riotgames.com/logout"

_CLIENT_PLATFORM = (
    "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjogIldpbmRvd3Mi"
    "LA0KCSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwN"
    "CgkicGxhdGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9"
)


class RiotAuthError(Exception):
    def __init__(self, message: str, *, code: str = "auth_failed"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Strict Outbound SSRF Protection & DNS Validation
# ---------------------------------------------------------------------------
EXACT_ALLOWED_OUTBOUND_DOMAINS = frozenset({
    "auth.riotgames.com",
    "entitlements.auth.riotgames.com",
    "pd.ap.a.pvp.net",
    "pd.eu.a.pvp.net",
    "pd.na.a.pvp.net",
    "pd.kr.a.pvp.net",
    "glz-ap-1.ap.a.pvp.net",
    "glz-eu-1.eu.a.pvp.net",
    "glz-na-1.na.a.pvp.net",
    "glz-kr-1.kr.a.pvp.net",
    "valorant-api.com",
    "media.valorant-api.com",
})

ALLOWED_REDIRECT_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "::1",
    "auth.riotgames.com",
    "vstore.lol",
})


def _validate_resolved_addresses(host: str, port: int = 443) -> None:
    """DNS resolution validation: Ensure all resolved A/AAAA records are global/public IPs."""
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RiotAuthError("Không thể phân giải DNS hostname.", code="ssrf_blocked") from exc

    addresses = {res[4][0].split("%", 1)[0] for res in results}
    if not addresses:
        raise RiotAuthError("Hostname không có địa chỉ IP hợp lệ.", code="ssrf_blocked")

    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
            if not ip.is_global:
                raise RiotAuthError(f"Hostname resolve tới IP nội bộ/không được phép: {ip}", code="ssrf_blocked")
        except ValueError:
            raise RiotAuthError(f"Địa chỉ IP không hợp lệ: {addr}", code="ssrf_blocked")


def _validate_outbound_url(url: str) -> str:
    """SSRF Prevention: Enforce HTTPS, port 443, no userinfo, exact domain whitelist, & post-DNS IP checks."""
    if not url:
        raise RiotAuthError("URL rỗng.", code="ssrf_blocked")

    parsed = urlsplit(url)

    if parsed.scheme != "https":
        raise RiotAuthError("Chỉ hỗ trợ giao thức HTTPS cho outbound requests.", code="ssrf_blocked")

    if parsed.port not in (None, 443):
        raise RiotAuthError(f"Port không được phép: {parsed.port}", code="ssrf_blocked")

    if parsed.username is not None or parsed.password is not None:
        raise RiotAuthError("URL không được chứa userinfo (username/password).", code="ssrf_blocked")

    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or host not in EXACT_ALLOWED_OUTBOUND_DOMAINS:
        raise RiotAuthError(f"Host không nằm trong danh sách cho phép (SSRF Protection): {host}", code="ssrf_blocked")

    # Perform DNS resolution check to prevent DNS rebinding / internal IP access
    _validate_resolved_addresses(host, parsed.port or 443)

    return url


# ---------------------------------------------------------------------------
# Strict Redirect URL & Token Extraction
# ---------------------------------------------------------------------------
def extract_redirect_access_token(raw: str) -> tuple[str, str]:
    """Parse raw redirect string, strictly extract access_token & id_token from fragment.

    Returns (access_token, id_token).
    Throws RiotAuthError on any validation failure without performing network requests.
    """
    raw = (raw or "").strip()
    if not raw:
        raise RiotAuthError("Thiếu Riot redirect URL.", code="bad_input")

    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlsplit(raw)

        if parsed.scheme not in ("http", "https"):
            raise RiotAuthError("Protocol redirect không hợp lệ.", code="invalid_redirect_scheme")

        host = (parsed.hostname or "").rstrip(".").lower()
        if host not in ALLOWED_REDIRECT_HOSTS:
            raise RiotAuthError(
                "Host redirect không hợp lệ. Vui lòng dán đúng link redirect localhost từ Riot OAuth.",
                code="invalid_redirect_host",
            )

        if parsed.username is not None or parsed.password is not None:
            raise RiotAuthError("URL redirect không được chứa userinfo.", code="invalid_redirect_url")

        fragment_qs = parse_qs(parsed.fragment, keep_blank_values=True)
        tokens = fragment_qs.get("access_token", [])

        if len(tokens) != 1 or not tokens[0]:
            raise RiotAuthError(
                "Link redirect không chứa access_token hợp lệ trong URL fragment (#access_token=...). Hãy copy nguyên link localhost/redirect#access_token=... từ Riot.",
                code="no_token",
            )

        id_tokens = fragment_qs.get("id_token", [])
        id_token = id_tokens[0] if id_tokens else ""

        return tokens[0].strip(), id_token.strip()

    # Raw token string handling
    if any(raw.lower().startswith(proto) for proto in ("http:", "https:", "ftp:", "file:", "gopher:", "dict:", "//")):
        raise RiotAuthError("Link redirect không hợp lệ.", code="bad_input")

    parts = raw.split(".")
    if len(parts) != 3:
        raise RiotAuthError("Access token không đúng định dạng JWT.", code="bad_token")

    return raw, ""


def _decode_jwt_payload(token: str) -> dict:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    pad = "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8"))
    except Exception:
        return {}


VALID_SHARDS = frozenset({"ap", "na", "eu", "kr"})


def _extract_username_from_id_token(id_token: str) -> str:
    """Decode id_token JWT and extract lol[0].uname (login username)."""
    payload = _decode_jwt_payload(id_token)
    lol_list = payload.get("lol") or []
    if lol_list and isinstance(lol_list, list):
        return lol_list[0].get("uname") or ""
    return ""


RIOT_PROXY_URL = "socks5h://127.0.0.1:40000"


def _resolve_proxy() -> str:
    return (os.environ.get("RIOT_PROXY_URL") or RIOT_PROXY_URL or "socks5h://127.0.0.1:40000").strip()


def _proxies_for_api() -> Optional[dict]:
    proxy = _resolve_proxy()
    return {"http": proxy, "https": proxy} if proxy else None


def _riot_http(method: str, url: str, headers: dict, *, data: bytes = b"",
               timeout: int = 5) -> str:
    _validate_outbound_url(url)
    try:
        from curl_cffi import requests as cffi_requests  # type: ignore
        kwargs: dict = dict(
            headers=headers, data=data, timeout=(3, 5),
            impersonate="chrome131",
            allow_redirects=False,
        )
        proxies = _proxies_for_api()
        if proxies:
            kwargs["proxies"] = proxies
        r = cffi_requests.request(method, url, **kwargs)
        if r.status_code >= 400:
            raise RiotAuthError(
                f"Riot API HTTP {r.status_code} ({url}): {(r.text or '')[:200]}",
                code="riot_api",
            )
        return r.text or ""
    except RiotAuthError:
        raise
    except Exception:
        _validate_outbound_url(url)
        from urllib.request import Request, urlopen
        req = Request(url, data=data or None, method=method, headers=headers)
        with urlopen(req, timeout=5) as resp:
            return resp.read().decode("utf-8")


def fetch_entitlements_token(access_token: str) -> str:
    body = _riot_http(
        "POST",
        "https://entitlements.auth.riotgames.com/api/token/v1",
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {access_token}",
            "Accept":        "application/json",
            "Origin":        "https://authenticate.riotgames.com",
            "Referer":       "https://authenticate.riotgames.com/",
        },
        data=b"{}",
    )
    return (json.loads(body) if body else {}).get("entitlements_token", "")


def fetch_name(shard: str, puuid: str, access_token: str, entitlements_token: str) -> dict:
    url = f"https://pd.{shard}.a.pvp.net/name-service/v2/players"
    headers = {
        "Authorization":           f"Bearer {access_token}",
        "X-Riot-Entitlements-JWT": entitlements_token,
        "X-Riot-ClientPlatform":   _CLIENT_PLATFORM,
        "X-Riot-ClientVersion":    "release-09.10-shipping-9-3349788",
        "Content-Type":            "application/json",
        "Accept":                  "application/json",
        "User-Agent":              "ShooterGame/++Ares-Core-shipping-31.00.00.0000000.0000 Windows/10.0.19045.1.256.64bit",
    }
    body = _riot_http("PUT", url, headers, data=json.dumps([puuid]).encode("utf-8"))
    payload = json.loads(body) if body else []
    if isinstance(payload, list) and payload:
        gn = payload[0].get("GameName") or ""
        tl = payload[0].get("TagLine")  or ""
        return {
            "game_name":    gn,
            "tag_line":     tl,
            "display_name": f"{gn}#{tl}" if tl else gn,
        }
    return {"game_name": "", "tag_line": "", "display_name": ""}


def riot_login_from_redirect(redirect_url: str, *, shard: str = "ap") -> dict:
    access_token, id_token = extract_redirect_access_token(redirect_url)

    shard = (shard or "").strip().lower()
    if shard not in VALID_SHARDS:
        raise RiotAuthError(
            f"Shard không hợp lệ: {shard!r}. Chọn ap, eu, na hoặc kr.",
            code="bad_shard",
        )

    payload = _decode_jwt_payload(access_token)
    puuid = payload.get("sub") or ""
    if not puuid:
        raise RiotAuthError(
            "Token không hợp lệ hoặc đã hết hạn (không decode được PUUID).",
            code="bad_token",
        )

    entitlements_token = fetch_entitlements_token(access_token)
    if not entitlements_token:
        raise RiotAuthError("Không lấy được entitlements_token.", code="no_entitlements")

    name = fetch_name(shard, puuid, access_token, entitlements_token)
    username = _extract_username_from_id_token(id_token) if id_token else ""

    return {
        "access_token":       access_token,
        "entitlements_token": entitlements_token,
        "puuid":              puuid,
        "shard":              shard,
        "game_name":          name["game_name"],
        "tag_line":           name["tag_line"],
        "display_name":       name["display_name"],
        "username":           username,
    }


