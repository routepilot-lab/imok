#!/usr/bin/env python3
"""Emit illustrative fw4 rules for an offline review with synthetic addresses."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
from mplane import fw4_rule_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("masque", "worker"), default="masque")
    args = parser.parse_args()
    worker = args.backend == "worker"
    proxy = "rp-backup" if worker else "rp-host"
    devices = [
        dict(
            scope="device",
            lan_interface="br-lan",
            mac=f"02:00:00:00:00:{number}",
            ip=f"192.168.50.{number}",
            mode="bypass_cn",
            egress_guard=protected,
        )
        for number, protected in ((10, False), (11, True))
    ]
    print(
        json.dumps(
            {
                "default": fw4_rule_text(devices, "eth1", proxy, not worker).splitlines(),
                "candidate": fw4_rule_text(
                    devices, "eth1", proxy, not worker, preview_offload=True
                ).splitlines(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
