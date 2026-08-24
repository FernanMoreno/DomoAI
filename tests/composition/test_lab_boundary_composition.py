"""Replay and twin are explicit lab capabilities, not runtime kernel code."""

from __future__ import annotations


def test_replay_and_twin_are_owned_by_lab() -> None:
    from domoai.lab.replay import PlanReplayer
    from domoai.lab.twin import DigitalTwin

    assert PlanReplayer.__module__ == "domoai.lab.replay"
    assert DigitalTwin.__module__ == "domoai.lab.twin"
