"""Create a partner organization and invite its first admin (WorkOS emails the invite).

    docker compose exec app python scripts/invite_partner.py --name "BK Insurance" --admin-email admin@example.rw

The admin then signs in and adds the rest of their staff on the app's
Organization members page (/settings/organization). Run again with the same
name to send another admin invitation; the organization is reused.
"""

from __future__ import annotations

import argparse
import json
import sys

from src.services import workos_auth


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help="organization name, e.g. 'BK Insurance'")
    parser.add_argument("--admin-email", required=True, help="email of the partner's first admin")
    args = parser.parse_args()
    if not workos_auth.enabled():
        print("WorkOS is not configured (AUTH_PROVIDER=workos and WORKOS_* keys).", file=sys.stderr)
        return 1
    if "@" not in args.admin_email:
        print("--admin-email must be an email address", file=sys.stderr)
        return 1
    print(json.dumps(workos_auth.create_partner(args.name, args.admin_email), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
