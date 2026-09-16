"""Who sees what — applied when the pack is built, never when it is displayed.

A plant manager's brief does not contain another plant's numbers. Not hidden behind a role check in the
console, not filtered out of the rendered page: **absent from the evidence pack**, which means absent from the
prompt, which means the model could not mention it if it tried. Filtering at the point of display leaves the
data in the prompt, in the model's context, in the logs, and one bug away from the reader (Project A §6.3).

Roles are data, not code paths. Adding a role is a row here; it is never an `if role == ...` in a renderer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ALL_PLANTS: tuple[str, ...] = ()


class PermissionError_(PermissionError):
    """Raised when a brief is asked for on behalf of somebody who cannot be identified."""


@dataclass(frozen=True)
class Audience:
    """One reader of a brief: their role, and the plants they may see."""

    user: str
    role: str
    plants: tuple[str, ...] = ALL_PLANTS   # empty means every plant: leadership, not a wildcard for everyone

    @property
    def sees_everything(self) -> bool:
        return self.role in UNRESTRICTED_ROLES and not self.plants

    def may_see(self, segment: dict[str, str]) -> bool:
        """Whether one segment belongs to this audience.

        A segment with no plant — the company total, a distribution centre, a customer lane spanning plants —
        belongs to nobody in particular, so only an unrestricted audience sees it. The alternative is
        showing a plant manager a company number they can neither act on nor check.
        """
        if self.sees_everything:
            return True
        plant = segment.get("plant")
        return plant is not None and plant in self.plants


UNRESTRICTED_ROLES = frozenset({"leadership", "supply_chain_vp", "ops"})

# The directory. In production this is a table with the same columns; the shape is what matters here.
DIRECTORY: dict[str, Audience] = {
    "vp": Audience(user="vp", role="supply_chain_vp"),
    "ops": Audience(user="ops", role="ops"),
    "plt01": Audience(user="plt01", role="plant_manager", plants=("PLT-01",)),
    "plt02": Audience(user="plt02", role="plant_manager", plants=("PLT-02",)),
    "plt03": Audience(user="plt03", role="plant_manager", plants=("PLT-03",)),
    "east": Audience(user="east", role="regional_manager", plants=("PLT-01", "PLT-02")),
}


def audience_for(user: str) -> Audience:
    """Look a reader up. An unknown user is refused rather than defaulted to a safe-looking role.

    Defaulting an unrecognised user to "plant manager for nothing" produces an empty brief, which looks like a
    quiet morning rather than a broken permission check — the failure that hides itself.
    """
    found = DIRECTORY.get(user.strip().lower())
    if found is None:
        raise PermissionError_(f"no audience is configured for {user!r}")
    return found


@dataclass
class Redaction:
    """What a filter removed, for the ops record. The brief never says it; someone auditing can see it."""

    audience: str
    considered: int
    removed: int
    segments: list[str] = field(default_factory=list)

    @property
    def kept(self) -> int:
        return self.considered - self.removed


def plants_for(audience: Audience) -> list[str] | None:
    """What `build_pack` needs: the plants to build for, or None for an audience that sees everything."""
    return None if audience.sees_everything else list(audience.plants)
