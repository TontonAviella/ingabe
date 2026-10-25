#!/usr/bin/env python3
"""Put Grafana behind Cloudflare Access at https://grafana.nozalabs.rw (run on the server, from ~/ingabe).

Reads CLOUDFLARE_API_TOKEN from .env (a token limited to Zone DNS: Edit for nozalabs.rw and Access: Apps and
Policies: Edit); the token never appears on a command line or in output. Idempotent: run it again to check or repair.
Order matters, so Grafana is never public without the login in front:
  1. Access: the one-time-PIN login exists; an Access application for grafana.nozalabs.rw allows only ALLOWED_EMAILS.
  2. DNS: grafana.nozalabs.rw -> the server's tunnel (proxied), only after the application exists.
Prints what the tunnel ingress needs (team name and the application's AUD tag), so cloudflared can also refuse any
request without a valid Access token. The rule live in /etc/cloudflared/config.yml (first in `ingress:`, since
2026-10-25; then `sudo systemctl restart cloudflared`, a 2-second blip for the site):

  - hostname: grafana.nozalabs.rw
    service: http://localhost:3000
    originRequest:
      access:
        required: true
        teamName: nozalabs
        audTag:
          - <aud printed by this script>

To let someone else in, add their email to ALLOWED_EMAILS and run this again. Grafana keeps its own sign-in as well
(GRAFANA_ADMIN_PASSWORD in .env); Claude Code reaches Grafana over ssh (scripts/grafana_mcp.sh), not through here.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

HOSTNAME = "grafana.nozalabs.rw"
ZONE = "nozalabs.rw"
TUNNEL_ID = "fa7446d2-7ddb-44fd-b9c9-e4f72e3da305"  # tunnel ingabe-server (/etc/cloudflared/config.yml)
ALLOWED_EMAILS = ["ntabukiraniroroger@gmail.com"]  # Roger, 2026-10-25: only him
API = "https://api.cloudflare.com/client/v4"


def _token() -> str:
    for line in open(".env"):
        if line.startswith("CLOUDFLARE_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit("CLOUDFLARE_API_TOKEN is not in .env")


TOKEN = _token()


def cf(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"{API}{path}", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.load(resp)
    except urllib.error.HTTPError as e:
        out = json.loads(e.read() or b"{}")
    if not out.get("success"):
        errors = "; ".join(f'{x.get("code")}: {x.get("message")}' for x in out.get("errors", []))
        raise SystemExit(f"Cloudflare refused {method} {path.split('?')[0]}: {errors or 'no detail'}")
    return out


def main() -> None:
    cf("GET", "/user/tokens/verify")
    print("token: valid")
    zones = cf("GET", f"/zones?name={ZONE}")["result"]
    if not zones:
        sys.exit(f"the token cannot see the zone {ZONE}")
    zone_id, account_id = zones[0]["id"], zones[0]["account"]["id"]
    print(f"zone {ZONE}: found")

    # Zero Trust organization (team domain) must exist; Cloudflare makes it when Zero Trust is first opened.
    org = cf("GET", f"/accounts/{account_id}/access/organizations")["result"]
    auth_domain = (org or {}).get("auth_domain", "")
    if not auth_domain:
        sys.exit("Zero Trust is not set up on this Cloudflare account yet: open one.dash.cloudflare.com once, "
                 "choose a team name and the Free plan, then run this again")
    team = auth_domain.split(".")[0]
    print(f"Zero Trust team: {team}")

    idps = cf("GET", f"/accounts/{account_id}/access/identity_providers")["result"]
    otp = next((i for i in idps if i.get("type") == "onetimepin"), None)
    if otp is None:
        otp = cf("POST", f"/accounts/{account_id}/access/identity_providers",
                 {"name": "One-time PIN", "type": "onetimepin", "config": {}})["result"]
        print("login method: one-time PIN by email (created)")
    else:
        print("login method: one-time PIN by email")

    apps = cf("GET", f"/accounts/{account_id}/access/apps")["result"]
    app = next((a for a in apps if a.get("domain") == HOSTNAME), None)
    app_body = {"name": "Grafana (Ingabe monitoring)", "domain": HOSTNAME, "type": "self_hosted",
                "session_duration": "24h", "allowed_idps": [otp["id"]], "auto_redirect_to_identity": True,
                "app_launcher_visible": False}
    if app is None:
        app = cf("POST", f"/accounts/{account_id}/access/apps", app_body)["result"]
        print("Access application: created")
    else:
        app = cf("PUT", f"/accounts/{account_id}/access/apps/{app['id']}", app_body)["result"]
        print("Access application: updated")
    policies = cf("GET", f"/accounts/{account_id}/access/apps/{app['id']}/policies")["result"]
    policy_body = {"name": "Allowed people", "decision": "allow", "precedence": 1,
                   "include": [{"email": {"email": e}} for e in ALLOWED_EMAILS]}
    mine = next((p for p in policies if p.get("name") == "Allowed people"), None)
    if mine is None:
        cf("POST", f"/accounts/{account_id}/access/apps/{app['id']}/policies", policy_body)
    else:
        cf("PUT", f"/accounts/{account_id}/access/apps/{app['id']}/policies/{mine['id']}", policy_body)
    others = [p["name"] for p in policies if p.get("name") != "Allowed people"]
    print(f"who may sign in: {', '.join(ALLOWED_EMAILS)}" + (f" (also other policies: {others})" if others else ""))

    # Only now point the name at the tunnel.
    target = f"{TUNNEL_ID}.cfargotunnel.com"
    records = cf("GET", f"/zones/{zone_id}/dns_records?name={HOSTNAME}")["result"]
    record = {"type": "CNAME", "name": HOSTNAME, "content": target, "proxied": True, "ttl": 1,
              "comment": "Grafana via tunnel ingabe-server; protected by Cloudflare Access"}
    if records:
        cf("PUT", f"/zones/{zone_id}/dns_records/{records[0]['id']}", record)
        print("DNS: updated")
    else:
        cf("POST", f"/zones/{zone_id}/dns_records", record)
        print("DNS: created")

    print(json.dumps({"team": team, "aud": app["aud"]}))


if __name__ == "__main__":
    main()
