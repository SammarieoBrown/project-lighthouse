"""Drafting targeted alert cascades (ALT-01). Propose only — gate G1 sends.

Nothing in this module sends anything, and there is nothing here that could:
the outbound channel does not exist yet, which ``intake/service.py`` says
plainly. What this produces is a set of drafts and a raw record of them, and a
Director's signature at G1 is what makes them sendable. That ordering is not a
staging decision — an alert is the one agent output that reaches a household
directly, and ALT-01 makes the signature non-negotiable for exactly that
reason.

**Scope is geography times risk band.** A cascade is drafted per parish and
community, and only for households the Risk Mapper actually put at risk on the
current advisory. Alerting an entire parish because a storm threatens the
island is how people learn to ignore alerts, and the registry knows better
than that.

**Two languages, always both.** English and Patois are drafted together rather
than one being a translation toggle. A household that reads the Patois line
first should not be reading a worse message.

**What is honestly missing.** ``nearest_shelter`` is always None: LGX-04's
shelter registry is P1 and unbuilt, so there is no shelter to name. Inventing
one would be the single most dangerous thing this file could do, so it does
not, and the gap is visible in every draft rather than hidden.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lighthouse_contracts import (
    ActorKind,
    AgentName,
    DamageBand,
    Event,
    Posture,
    StormFileState,
)
from lighthouse_contracts.agents import AlertAgentOutput, AlertDraft

from app import ledger
from app.models import Advisory, HazardEvent, RiskAssessment, StormFile

#: Bands worth waking someone for. NONE is the Risk Mapper saying it expects
#: this household to come through it, and a cascade that reaches those people
#: teaches everyone else to ignore the next one.
ALERTABLE_BANDS = (DamageBand.MINOR, DamageBand.MAJOR, DamageBand.DESTROYED)

#: QUIET is absent on purpose: there is no cascade to draft when nothing is
#: coming, and an agent that drafts one anyway is an agent that will eventually
#: send one.
_MESSAGES: dict[Posture, tuple[str, str, tuple[str, ...]]] = {
    Posture.WATCH: (
        "A tropical storm is tracking toward Jamaica and may affect {area}. "
        "There is time to prepare. Check on neighbours who live alone.",
        "Storm a track toward Jamaica an it can affect {area}. Yuh have time fi "
        "get ready. Check pon di neighbour dem weh live alone.",
        (
            "Clear drains and yard debris",
            "Charge phones and power banks",
            "Set aside drinking water and non-perishable food",
        ),
    ),
    Posture.READY: (
        "A hurricane watch covers {area}. Damaging wind is possible within 48 "
        "hours. Secure your roof today and decide now where you will go if you "
        "have to move.",
        "Hurricane watch deh pon {area}. Bad wind can reach wi inna 48 hour. "
        "Secure yuh roof today, an decide now weh yuh a go if yuh haffi move.",
        (
            "Secure roof sheeting and shutters",
            "Move important papers and medicine into a waterproof bag",
            "Agree a meeting place with your household",
            "Fill containers with drinking water",
        ),
    ),
    Posture.ACT: (
        "Hurricane warning for {area}. Hurricane-force wind is expected within "
        "36 hours. If your home is in a low-lying area or your roof is weak, "
        "move to safety now. Do not wait for the wind.",
        "Hurricane warning fi {area}. Hurricane wind a come inna 36 hour. If "
        "yuh inna low-lying place or yuh roof weak, move to safety now. Nuh "
        "wait pon di wind.",
        (
            "Move now if you are in a flood-prone or weak structure",
            "Carry identification, medicine and phone chargers",
            "Turn off electricity at the main switch before you leave",
            "Tell a neighbour or relative where you are going",
        ),
    ),
}


class AlertServiceError(RuntimeError):
    """Base class for safe, non-PII alert failures."""


class NothingToAlert(AlertServiceError):
    pass


@dataclass(frozen=True, slots=True)
class AlertProposal:
    output: AlertAgentOutput
    ledger_entry_id: uuid.UUID
    posture: Posture


def _area_label(parish: str | None, community: str | None) -> str:
    if parish and community:
        return f"{community}, {parish}"
    return community or parish or "your area"


def _at_risk_groups(session: Session, advisory_id: uuid.UUID):
    """Households the Risk Mapper put at risk, grouped by where they live.

    Registered-but-unaffected files are included as long as the assessment
    puts them in an alertable band: an alert is about what is coming, so the
    Storm File does not need to have been touched yet. Files already SETTLED
    from an earlier event are excluded — they have been through this.
    """
    return session.execute(
        select(
            StormFile.parish,
            StormFile.community,
            func.count(RiskAssessment.id).label("recipients"),
            func.max(RiskAssessment.p34).label("worst_p34"),
        )
        .join(StormFile, StormFile.id == RiskAssessment.storm_file_id)
        .where(
            RiskAssessment.advisory_id == advisory_id,
            RiskAssessment.predicted_band.in_(ALERTABLE_BANDS),
            StormFile.state != StormFileState.SETTLED,
        )
        .group_by(StormFile.parish, StormFile.community)
        .order_by(func.count(RiskAssessment.id).desc(), StormFile.parish)
    ).all()


def build_cascade(
    session: Session, event: HazardEvent, advisory: Advisory
) -> AlertAgentOutput:
    """Draft one cascade per at-risk community. Pure of writes."""
    posture = event.current_posture
    if posture is Posture.QUIET:
        raise NothingToAlert("posture is QUIET; there is no cascade to draft")
    groups = _at_risk_groups(session, advisory.id)
    if not groups:
        raise NothingToAlert("no registered household is in an alertable band")

    english, patois, steps = _MESSAGES[posture]
    drafts = [
        AlertDraft(
            parish=group.parish or "UNSPECIFIED",
            community=group.community,
            recipient_count=group.recipients,
            text_en=english.format(area=_area_label(group.parish, group.community)),
            text_patois=patois.format(area=_area_label(group.parish, group.community)),
            # The voice variant is the Patois line as written. It is a script
            # for a human or a TTS pass, and it is stored so that whoever
            # records it is reading the text a Director actually approved.
            voice_script_patois=patois.format(
                area=_area_label(group.parish, group.community)
            ),
            # LGX-04 is unbuilt. There is no shelter registry to consult, and
            # naming a shelter we cannot confirm is open would be worse than
            # naming none.
            nearest_shelter=None,
            preparation_steps=list(steps),
        )
        for group in groups
    ]
    recipients = sum(draft.recipient_count for draft in drafts)
    return AlertAgentOutput(
        drafts=drafts,
        rationale=(
            f"Posture {posture} on advisory {advisory.advisory_number}: "
            f"{len(drafts)} cascade(s) covering {recipients} registered "
            "household(s) in an alertable band; no shelter named because the "
            "shelter registry (LGX-04) does not exist yet"
        ),
    )


def propose_cascade(
    session: Session, event: HazardEvent, advisory: Advisory
) -> AlertProposal:
    """Draft a cascade and store it raw. Sends nothing."""
    output = build_cascade(session, event, advisory)
    entry = ledger.append(
        session,
        action=str(Event.ALERT_CASCADE_PROPOSED),
        subject_type="hazard_event",
        subject_id=event.id,
        payload={
            "hazard_event_id": str(event.id),
            "advisory_id": str(advisory.id),
            "advisory_number": advisory.advisory_number,
            "posture": str(event.current_posture),
            "cascade_count": len(output.drafts),
            "recipient_count": sum(d.recipient_count for d in output.drafts),
            "requires_approval": output.requires_approval,
            "shelter_registry_available": False,
            "cascade": output.model_dump(mode="json"),
        },
        actor_kind=ActorKind.AGENT,
        agent=AgentName.ALERT_AGENT,
    )
    return AlertProposal(
        output=output, ledger_entry_id=entry.id, posture=event.current_posture
    )


__all__ = [
    "ALERTABLE_BANDS",
    "AlertProposal",
    "AlertServiceError",
    "NothingToAlert",
    "build_cascade",
    "propose_cascade",
]
