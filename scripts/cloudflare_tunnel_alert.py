#!/usr/bin/env python3
"""Cloudflare tells Roger when the server's tunnel goes down: the check from OUTSIDE the server (run on the server,
from ~/ingabe).

Every other alarm runs on the Contabo server, so if the whole server dies they all stop with it. Cloudflare sees the
tunnel (ingabe-server) disconnect and notifies by email (ntabukiraniroroger@gmail.com) and by push to the same ntfy
channel as the other alarms (NTFY_TOPIC in .env, through a Cloudflare webhook; ntfy fills in Cloudflare's text).
Fires on down and degraded, and again when healthy. Idempotent: run it again to check or repair.

Needs CLOUDFLARE_API_TOKEN in .env with Account > Notifications > Edit. Neither the token nor the channel is printed.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

TUNNEL_ID = "fa7446d2-7ddb-44fd-b9c9-e4f72e3da305"  # ingabe-server (/etc/cloudflared/config.yml)
EMAIL = "ntabukiraniroroger@gmail.com"
POLICY = "Ingabe server unreachable (tunnel health)"
WEBHOOK = "Ingabe phone alarms (ntfy)"
API = "https://api.cloudflare.com/client/v4"


def env(name: str) -> str:
    for line in open(".env"):
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"{name} is not in .env")


TOKEN, TOPIC = env("CLOUDFLARE_API_TOKEN"), env("NTFY_TOPIC")


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


def ntfy_url() -> str:
    message = ("{{.text}}\nIf it says DOWN: the Contabo server or its connection is unreachable, so the other alarms "
               "cannot reach you either. Open Claude Code and say \"Cloudflare says the server is down\".")
    query = urllib.parse.urlencode({"tpl": "yes", "t": "Server check (from Cloudflare)", "m": message, "p": "5",
                                    "ta": "rotating_light", "click": "https://gis.nozalabs.rw"})
    return f"https://ntfy.sh/{TOPIC}?{query}"


def main() -> None:
    account = cf("GET", "/zones?name=nozalabs.rw")["result"][0]["account"]["id"]
    base = f"/accounts/{account}/alerting/v3"
    hooks = cf("GET", f"{base}/destinations/webhooks")["result"] or []
    hook = next((h for h in hooks if h.get("name") == WEBHOOK), None)
    if hook is None:
        hook_id = cf("POST", f"{base}/destinations/webhooks", {"name": WEBHOOK, "url": ntfy_url()})["result"]["id"]
        print("phone destination: created (Cloudflare sent a test push)")
    else:
        hook_id = hook["id"]
        cf("PUT", f"{base}/destinations/webhooks/{hook_id}", {"name": WEBHOOK, "url": ntfy_url()})
        print("phone destination: updated")

    policy = {"name": POLICY, "alert_type": "tunnel_health_event", "enabled": True,
              "description": "The Contabo server's tunnel is down or degraded (or healthy again); seen from outside.",
              "mechanisms": {"email": [{"id": EMAIL}], "webhooks": [{"id": hook_id}]},
              "filters": {"tunnel_id": [TUNNEL_ID],
                          "new_status": ["TUNNEL_STATUS_TYPE_DOWN", "TUNNEL_STATUS_TYPE_DEGRADED",
                                         "TUNNEL_STATUS_TYPE_HEALTHY"]}}
    existing = next((p for p in cf("GET", f"{base}/policies")["result"] if p.get("name") == POLICY), None)
    if existing is None:
        cf("POST", f"{base}/policies", policy)
        print("tunnel alert: created (email + phone)")
    else:
        cf("PUT", f"{base}/policies/{existing['id']}", policy)
        print("tunnel alert: updated (email + phone)")


if __name__ == "__main__":
    main()
