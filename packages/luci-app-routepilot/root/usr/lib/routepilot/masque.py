#!/usr/bin/env python3
"""Entry point for the imOK routing service."""

from control import control as control, main as main
from lan_discovery import lan_interfaces as lan_interfaces
from rules_update import (
    commit_rules as commit_rules,
    finish_interrupted_update as finish_interrupted_update,
    rule_snapshot as rule_snapshot,
    update_rules as update_rules,
)
from service import Service as Service, preflight as preflight, routing_digest as routing_digest
from supervisor import output as output


if __name__ == "__main__":
    main()
