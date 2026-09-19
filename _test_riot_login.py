from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
def main() -> int:
    p = argparse.ArgumentParser(description="Test Riot Sign-On (redirect URL).")
    p.add_argument("redirect_url", help="Pasted localhost/redirect#... URL (or raw access_token)")
    p.add_argument("--shard", default="ap", help="Shard: ap/eu/na/kr")
    args = p.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from riot_auth import RiotAuthError, RIOT_LOGIN_URL, riot_login_from_redirect
    print(f"[i] Link đăng nhập Riot: {RIOT_LOGIN_URL}")
    print(f"[*] Đang xử lý redirect URL (shard={args.shard}) ...")
    try:
        result = riot_login_from_redirect(args.redirect_url, shard=args.shard)
    except RiotAuthError as e:
        print(f"[!] FAIL ({e.code}): {e}")
        return 2
    safe = {k: v for k, v in result.items() if k not in ("access_token", "entitlements_token")}
    safe["access_token"]       = (result.get("access_token") or "")[:24] + "..."
    safe["entitlements_token"] = (result.get("entitlements_token") or "")[:24] + "..."
    print("[+] Thành công:")
    print(json.dumps(safe, indent=2, ensure_ascii=False))
    print()
    print(f"    Game name : {result.get('game_name')}")
    print(f"    Tag line  : {result.get('tag_line')}")
    print(f"    Display   : {result.get('display_name')}")
    print(f"    Shard     : {result.get('shard')}")
    print(f"    PUUID     : {result.get('puuid')}")
    return 0
if __name__ == "__main__":
    sys.exit(main())
