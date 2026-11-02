"""Read-only review source for the resident consent host, backed by the runtime's own state."""

from __future__ import annotations

from typing import Any

from domoai.domain.household_consent import ConsentReference
from domoai.domain.household_preferences import PreferenceRead
from domoai.domain.models import AuthorityContext, PrincipalRole
from domoai.management.consent import ConsentReviewInput

_PRINCIPAL = "resident-consent-review-source"


class RuntimeReviewSource:
    """Loads the exact target, devices, preferences and control head for one review.

    Nothing is cached and no error is swallowed: the adapter turns every failure
    into the same generic 404, and an absent target is simply ``None``.
    """

    def __init__(
        self,
        *,
        plans: Any,
        rules: Any,
        registry: Any,
        preferences: Any,
        approval_store: Any,
        timezone: str,
        tenant_id: str,
        household_id: str,
    ) -> None:
        self._plans = plans
        self._rules = rules
        self._registry = registry
        self._preferences = preferences
        self._store = approval_store
        self._timezone = timezone
        # Preferences need whole-household authority: the resident identity is
        # narrower on purpose, and its device/area limits are enforced by the adapter.
        self._authority = AuthorityContext(
            tenant_id=tenant_id,
            household_id=household_id,
            household_ids=[household_id],
            principal_id=_PRINCIPAL,
            roles=[PrincipalRole.SERVICE],
        )
        self._tenant_id = tenant_id
        self._household_id = household_id
        self._observation: tuple[Any, Any] | None = None

    def _pin_observation(self, profile: Any) -> Any:
        """Keep ``observed_at`` stable while the bound preference state is unchanged.

        The service stamps every read with the clock, and the review digest
        covers that stamp, so a decision could never match its review. The view
        binds only the preference version and ``next_transition_at``: while both
        are unchanged the first stamp is the one that is reviewed and decided.
        A slow read that finishes late with an older stamp is served as it is
        but never replaces a newer pin.
        """
        key = (profile.version, profile.next_transition_at)
        pinned = self._observation
        if pinned is not None and pinned[0] == key:
            return profile.model_copy(update={"observed_at": pinned[1]})
        if pinned is None or profile.observed_at >= pinned[1]:
            self._observation = (key, profile.observed_at)
        return profile

    async def load(self, identity: Any, reference: ConsentReference) -> ConsentReviewInput | None:
        if reference.kind == "plan":
            target = await self._plans.get(reference.id)
        elif self._rules is None:
            return None
        else:
            record = await self._rules.get(reference.id)
            target = None if record is None else record.rule
        if target is None:
            return None
        profile = self._pin_observation(
            await self._preferences.read(
                PreferenceRead(limit=100, effective_only=True), self._authority
            )
        )
        return ConsentReviewInput(
            target=target,
            devices=list(self._registry.devices),
            preferences=profile,
            control_version=self._store.get_consent_control(self._tenant_id, self._household_id),
            timezone=self._timezone,
        )
