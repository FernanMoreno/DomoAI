"""Import smoke tests for the final package ownership boundaries."""

from __future__ import annotations


def test_production_and_lab_boundaries_import_without_ordering_hacks() -> None:
    import domoai.adapters
    import domoai.application
    import domoai.config
    import domoai.domain
    import domoai.lab
    import domoai.optimizer
    import domoai.persistence
    import domoai.runtime

    assert all(
        package.__name__.startswith("domoai.")
        for package in (
            domoai.adapters,
            domoai.application,
            domoai.config,
            domoai.domain,
            domoai.lab,
            domoai.optimizer,
            domoai.persistence,
            domoai.runtime,
        )
    )
