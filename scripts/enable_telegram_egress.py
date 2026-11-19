"""Extend the existing private loopback route to Telegram data centers on TCP 443."""

import argparse
import copy
import json
import os
from pathlib import Path

# Official https://core.telegram.org/resources/cidr.txt, checked 2026-11-16.
TELEGRAM_NETWORKS = [
    "91.108.56.0/22", "91.108.4.0/22", "91.108.8.0/22", "91.108.16.0/22",
    "91.108.12.0/22", "149.154.160.0/20", "91.105.192.0/23", "91.108.20.0/22",
    "185.76.151.0/24", "2001:b28:f23d::/48", "2001:b28:f23f::/48",
    "2001:67c:4e8::/48", "2001:b28:f23c::/48", "2a0a:f280::/32",
]


def extend(config):
    result = copy.deepcopy(config)
    rules = result["routing"]["rules"]
    model = next(rule for rule in rules if rule.get("domain") == ["full:openrouter.ai"])
    tags = model["inboundTag"]
    if len(tags) != 1 or model.get("port") != "443":
        raise ValueError("Expected the existing restricted model route")
    inbound = next(row for row in result["inbounds"] if row.get("tag") == tags[0])
    if inbound.get("listen") != "127.0.0.1" or inbound.get("protocol") != "http":
        raise ValueError("Expected a private loopback HTTP listener")
    telegram = {
        "type": "field", "inboundTag": tags, "ip": TELEGRAM_NETWORKS,
        "network": "tcp", "port": "443", "outboundTag": model["outboundTag"],
    }
    # Idempotent replacement of our exact route, preserving all other restrictions.
    rules[:] = [r for r in rules if r.get("ip") != TELEGRAM_NETWORKS]
    rules.insert(rules.index(model) + 1, telegram)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    path = args.config
    config = json.loads(path.read_text())
    updated = extend(config)
    if updated == config:
        print("Telegram route already configured")
        return
    stat = path.stat()
    backup = path.with_name("config.before-telegram.json")
    if not backup.exists():
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream)
        os.chown(backup, stat.st_uid, stat.st_gid)
    temporary = path.with_name(".config.telegram.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(updated, stream)
    os.chown(temporary, stat.st_uid, stat.st_gid)
    os.replace(temporary, path)
    print("Private Telegram TCP 443 route configured; other targets remain blocked")


if __name__ == "__main__":
    main()
