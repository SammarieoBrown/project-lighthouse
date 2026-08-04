"""Release proof: signed household intake through truthful demo settlement.

This is intentionally one broad real-Postgres test.  Narrow unit tests cover
the individual policies; this test proves that their actual service seams fit
together without a network, a payment rail, or an invented verification fact.
"""

from __future__ import annotations

import importlib
import uuid
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from lighthouse_contracts import (
    AgentName,
    AppRole,
    ClaimStatus,
    DisbursementChannel,
    DisbursementStatus,
    Event,
    PayerRoute,
    Posture,
    ResourceKind,
    StormFileState,
    Verdict,
)

from app import ledger, public_ledger
from app.approval_credentials import issue_human_credential, set_human_password
from app.approvals import AllocationApprovalRequest, approve_claim_allocation
from app.disbursements import (
    BatchSignRequest,
    SimulatedExecutionRequest,
    execute_simulated_disbursement,
    sign_disbursement_batch,
)
from app.human_auth import AuthenticatedHuman, authenticate_human
from app.intake.service import process_intake_job
from app.intake.twilio import signature_for
from app.models import AgentJob, Claim, HazardEvent, StormFile
from app.settlement_executor import SimulatedDemoExecutor
from app.verification_service import record_review_decision, run_verification
from app.web import app

from factories import make_user


TWILIO_TOKEN = "release-flow-primary-auth-token"
PUBLIC_BASE_URL = "https://lighthouse.example"
WHATSAPP_PATH = "/webhooks/twilio/whatsapp"
HOUSEHOLD_PHONE = "+18765550123"
HOUSEHOLD_REPORT = "Mi roof gone and we need water and tarpaulin."


def _human(session, role: AppRole) -> AuthenticatedHuman:
    """Issue and authenticate the same short-lived credential used by routes."""
    user = make_user(session, role)
    password = f"release proof {role.value.lower()} password"
    set_human_password(session, email=user.email, password=password)
    issued = issue_human_credential(
        session,
        email=user.email,
        password=password,
    )
    return authenticate_human(
        session,
        f"Bearer {issued.token}",
        allowed_roles={role},
    )


def _client(monkeypatch, session, *, event_ref: str) -> TestClient:
    intake_router = importlib.import_module("app.intake.router")

    @contextmanager
    def scoped_session():
        yield session
        session.flush()

    monkeypatch.setattr(intake_router, "session_scope", scoped_session)
    monkeypatch.setattr(public_ledger, "session_scope", scoped_session)
    monkeypatch.setattr(
        intake_router,
        "get_settings",
        lambda: SimpleNamespace(
            twilio_auth_token=TWILIO_TOKEN,
            public_base_url=PUBLIC_BASE_URL,
            intake_hazard_external_ref=event_ref,
        ),
    )
    ledger.clear_verify_chain_cache()
    public_ledger.clear_aggregate_cache()
    return TestClient(app, raise_server_exceptions=False)


def test_signed_intake_review_and_simulated_settlement_release_flow(
    session, monkeypatch
):
    event_ref = f"release-flow-{uuid.uuid4()}"
    event = HazardEvent(
        name="Synthetic release-flow storm",
        external_ref=event_ref,
        current_posture=Posture.ACT,
        replay=True,
    )
    session.add(event)
    session.flush()
    client = _client(monkeypatch, session, event_ref=event_ref)

    # 1. The household report crosses the real signed Twilio edge and becomes
    # a provider-minimal durable job before any claim logic runs.
    message_sid = "SM" + uuid.uuid4().hex
    form = {
        "MessageSid": message_sid,
        "From": f"whatsapp:{HOUSEHOLD_PHONE}",
        "To": "whatsapp:+14155238886",
        "Body": HOUSEHOLD_REPORT,
        "NumMedia": "0",
    }
    signature = signature_for(
        PUBLIC_BASE_URL + WHATSAPP_PATH,
        form,
        TWILIO_TOKEN,
    )
    webhook = client.post(
        WHATSAPP_PATH,
        data=form,
        headers={"X-Twilio-Signature": signature},
    )
    assert webhook.status_code == 200, webhook.text
    assert webhook.text == "<Response></Response>"

    intake_job = session.scalar(
        select(AgentJob).where(
            AgentJob.job_type == str(AgentName.INTAKE_AGENT),
            AgentJob.payload["provider_message_sid"].astext == message_sid,
        )
    )
    assert intake_job is not None
    intake = process_intake_job(session, dict(intake_job.payload))
    claim = session.get(Claim, intake.claim_id)
    storm_file = session.get(StormFile, intake.storm_file_id)
    assert claim is not None and storm_file is not None
    assert claim.status is ClaimStatus.FILED
    assert storm_file.state is StormFileState.AFFECTED
    assert claim.damage_type == "roof_damage"
    assert claim.reported_needs == ["tarpaulin", "water"]
    assert intake.verification_state == "QUEUED"

    # Parish classification is a registry/human enrichment, not an inference
    # from the message.  The deliberately thin file still lacks four other
    # independent evidence families and therefore may not auto-verify.
    storm_file.parish = "St Elizabeth"
    storm_file.community = "Newmarket"
    session.flush()
    verification = run_verification(session, claim.id)
    assert verification.output.verdict is Verdict.REVIEW
    assert verification.output.capped is True
    assert verification.transitioned is False
    assert "satellite_change" in verification.output.missing_signals
    assert claim.status is ClaimStatus.FILED
    assert storm_file.state is StormFileState.AFFECTED

    # 2. A credentialed Review Clerk appends, rather than mutates, the human
    # decision and is the only authority that advances this incomplete case.
    clerk = _human(session, AppRole.REVIEW_CLERK)
    reviewed = record_review_decision(
        session,
        claim_id=claim.id,
        verification_id=verification.verification.id,
        clerk_id=clerk.user.id,
        verdict=Verdict.APPROVED,
        rationale="Review Clerk inspected the redacted evidence and approved assistance.",
    )
    assert reviewed.verification.overrides_id == verification.verification.id
    assert reviewed.output.verdict is Verdict.APPROVED
    assert claim.status is ClaimStatus.VERIFIED
    assert storm_file.state is StormFileState.VERIFIED

    # 3. Separate recently authenticated Director and Finance Officer
    # signatures bind the fixed allocation and its simulated demo batch.
    director = _human(session, AppRole.DIRECTOR)
    allocation = approve_claim_allocation(
        session,
        human=director,
        claim_id=claim.id,
        request=AllocationApprovalRequest(
            resource=ResourceKind.CASH,
            amount=Decimal("45000.00"),
            currency="JMD",
            payer_route=PayerRoute.GOV_RELIEF,
            note="Director approved the fixed synthetic release grant.",
        ),
        idempotency_key=f"release-director-{uuid.uuid4()}",
    )
    assert allocation.idempotent_replay is False
    assert allocation.ledger_entry.action == str(Event.ALLOCATION_APPROVED)

    finance = _human(session, AppRole.FINANCE_OFFICER)
    signed = sign_disbursement_batch(
        session,
        human=finance,
        allocation_id=allocation.allocation.id,
        request=BatchSignRequest(
            channel=DisbursementChannel.BANK,
            executor_provenance="SIMULATED_DEMO",
            note="Finance signed the explicitly simulated batch.",
        ),
        idempotency_key=f"release-finance-sign-{uuid.uuid4()}",
    )
    assert signed.disbursement.status is DisbursementStatus.PENDING
    assert signed.disbursement.external_ref is None
    assert signed.disbursement.simulated is True

    # Execution is an explicit, acknowledged demo action.  The adapter makes
    # no network call and records execution and confirmation as distinct facts.
    settled = execute_simulated_disbursement(
        session,
        human=finance,
        disbursement_id=signed.disbursement.id,
        request=SimulatedExecutionRequest(
            executor_provenance="SIMULATED_DEMO",
            acknowledge_no_real_money=True,
        ),
        idempotency_key=f"release-finance-execute-{uuid.uuid4()}",
        executor=SimulatedDemoExecutor(),
    )
    assert settled.disbursement.status is DisbursementStatus.CONFIRMED
    assert settled.executed_entry.action == str(Event.DISBURSEMENT_EXECUTED)
    assert settled.confirmed_entry.action == str(Event.DISBURSEMENT_CONFIRMED)
    assert (
        settled.executed_entry.payload["money_movement"]
        == "SIMULATION_EXECUTED_NO_REAL_FUNDS"
    )
    assert (
        settled.confirmed_entry.payload["money_movement"]
        == "SIMULATED_CONFIRMATION_RECORDED_NO_REAL_FUNDS"
    )
    session.refresh(claim)
    session.refresh(storm_file)
    assert claim.status is ClaimStatus.SETTLED
    assert claim.settled_at is not None
    assert storm_file.state is StormFileState.SETTLED
    assert ledger.verify_chain(session) is True

    # 4. The public projection contains exactly the three truthful release
    # stages, an aggregate of confirmed simulated relief, and no household or
    # internal settlement identity.
    ledger.clear_verify_chain_cache()
    public_ledger.clear_aggregate_cache()
    response = client.get("/v1/public/ledger?after_seq=0&limit=100")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [entry["action"] for entry in body["entries"]] == [
        "allocation.approved",
        "disbursement.executed",
        "disbursement.confirmed",
    ]
    assert [entry["money_movement"]["status"] for entry in body["entries"]] == [
        "NOT_INITIATED_AT_APPROVAL",
        "SIMULATION_EXECUTED_NO_REAL_FUNDS",
        "SIMULATED_CONFIRMATION_RECORDED_NO_REAL_FUNDS",
    ]
    assert body["aggregate"] == {
        "scope": "CONFIRMED_SIMULATED_RELIEF_ONLY",
        "count": 1,
        "amount": "45000.00",
        "currency": "JMD",
        "no_real_money_moved": True,
        "by_channel": [
            {
                "channel": "BANK",
                "count": 1,
                "amount": "45000.00",
                "currency": "JMD",
                "executor_provenance": "SIMULATED_DEMO",
            }
        ],
    }
    assert body["chain"]["valid"] is True
    assert all(
        entry.get("settlement", {}).get("simulated") is True
        for entry in body["entries"][1:]
    )

    public_text = response.text
    for private_value in (
        HOUSEHOLD_PHONE,
        HOUSEHOLD_REPORT,
        claim.claim_ref,
        str(claim.id),
        str(storm_file.id),
        str(allocation.allocation.id),
        str(signed.disbursement.id),
        settled.disbursement.external_ref,
        storm_file.parish,
        storm_file.community,
        claim.damage_type,
    ):
        assert private_value not in public_text
    assert "subject_id" not in public_text
    assert "disbursement.batch_signed" not in public_text
