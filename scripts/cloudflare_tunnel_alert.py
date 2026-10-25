#!/usr/bin/env python3
"""Cloudflare tells Roger when the server's tunnel goes down: the check from OUTSIDE the server (run on the server,
from ~/ingabe).

Every other alarm runs on the Contabo server, so if the whole server dies they all stop with it. Cloudflare sees the
tunnel (ingabe-server) disconnect and notifies by email (ntabukiraniroroger@gmail.com) and by push to the same ntfy
channel as the other alarms (NTFY_TOPIC in .env, through a Cloudflare webhook; ntfy fills in Cloudflare's text).
DOWN is an urgent push (priority 5); back to HEALTHY is a quiet one (priority 3). DEGRADED (some of the tunnel's
connections reconnecting) is left out: cloudflared does that routinely, and it must not wake Roger at night.
Idempotent: run it again to check or repair.

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
# (policy name, tunnel status, webhook name, ntfy title, priority, tag, message)
RULES = [
    ("Ingabe server unreachable (tunnel health)", "TUNNEL_STATUS_TYPE_DOWN", "Ingabe phone alarms (ntfy)",
     "RED: server unreachable (seen by Cloudflare)", "5", "rotating_light",
     "{{.text}}\nThe Contabo server or its connection is down, so the other alarms cannot reach you either. "
     'Open Claude Code and say "Cloudflare says the server is down".'),
    ("Ingabe server reachable again (tunnel health)", "TUNNEL_STATUS_TYPE_HEALTHY", "Ingabe phone (ntfy, back up)",
     "Fixed: server reachable again (seen by Cloudflare)", "3", "white_check_mark",
     "{{.text}}\nThe server's connection to Cloudflare is back."),
]
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


def ntfy_url(title: str, priority: str, tag: str, message: str) -> str:
    query = urllib.parse.urlencode({"tpl": "yes", "t": title, "m": message, "p": priority, "ta": tag,
                                    "click": "https://gis.nozalabs.rw"})
    return f"https://ntfy.sh/{TOPIC}?{query}"


def main() -> None:
    account = cf("GET", "/zones?name=nozalabs.rw")["result"][0]["account"]["id"]
    base = f"/accounts/{account}/alerting/v3"
    hooks = cf("GET", f"{base}/destinations/webhooks")["result"] or []
    policies = cf("GET", f"{base}/policies")["result"] or []
    for policy_name, status, hook_name, title, priority, tag, message in RULES:
        url = ntfy_url(title, priority, tag, message)
        hook = next((h for h in hooks if h.get("name") == hook_name), None)
        if hook is None:
            hook_id = cf("POST", f"{base}/destinations/webhooks", {"name": hook_name, "url": url})["result"]["id"]
            print(f"{hook_name}: created (Cloudflare sent a test push)")
        else:
            hook_id = hook["id"]
            cf("PUT", f"{base}/destinations/webhooks/{hook_id}", {"name": hook_name, "url": url})
            print(f"{hook_name}: updated")
        policy = {"name": policy_name, "alert_type": "tunnel_health_event", "enabled": True,
                  "description": f"Tunnel ingabe-server changed to {status.rsplit('_', 1)[1].lower()}; seen from outside.",
                  "mechanisms": {"email": [{"id": EMAIL}], "webhooks": [{"id": hook_id}]},
                  "filters": {"tunnel_id": [TUNNEL_ID], "new_status": [status]}}
        existing = next((p for p in policies if p.get("name") == policy_name), None)
        if existing is None:
            cf("POST", f"{base}/policies", policy)
            print(f"{policy_name}: created (email + phone)")
        else:
            cf("PUT", f"{base}/policies/{existing['id']}", policy)
            print(f"{policy_name}: updated (email + phone)")


if __name__ == "__main__":
    main()
