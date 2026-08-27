"""Finance signature, demo execution, confirmation, and public proof."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from lighthouse_contracts import (
    AppRole,
    ClaimStatus,
    DisbursementChannel,
    DisbursementStatus,
    StormFileState,
)

from app import approvals, disbursements, ledger, public_ledger
from app.approval_credentials import issue_human_credential, set_human_password
from app.models import (
    Approval,
    Disbursement,
    DisbursementBatch,
    LedgerEntry,
)
from app.web import app

from factories import make_claim, make_event, make_storm_file, make_user, make_verification


ALLOCATION_BODY = {
    "resource": "CASH",
    "amount": "45000.00",
    "currency": "JMD",
    "payer_route": "GOV_RELIEF",
}
SIGN_BODY = {
    "channel": "BANK",
    "executor_provenance": "SIMULATED_DEMO",
    "note": "Finance reviewed the signed allocation",
}
EXECUTE_BODY = {
    "executor_provenance": "SIMULATED_DEMO",
    "acknowledge_no_real_money": True,
}


def _credential(session, role: AppRole):
    user = make_user(session, role)
    password = f"correct horse {role.value.lower()}"
    set_human_password(session, email=user.email, password=password)
    issued = issue_human_credential(session, email=user.email, password=password)
    return user, issued


def _client(monkeypatch, session, *, execution_enabled: bool = False) -> TestClient:
    @contextmanager
    def scoped():
        yield session
        session.flush()

    monkeypatch.setattr(approvals, "session_scope", scoped)
    monkeypatch.setattr(disbursements, "session_scope", scoped)
    monkeypatch.setattr(public_ledger, "session_scope", scoped)
    monkeypatch.setattr(
        disbursements,
        "get_settings",
        lambda: SimpleNamespace(
            disbursement_executor_mode=(
                "simulated" if execution_enabled else "disabled"
            )
        ),
    )
    ledger.clear_verify_chain_cache()
    public_ledger.clear_aggregate_cache()
    return TestClient(app)


def _verified_claim(session):
    storm_file = make_storm_file(session, state=StormFileState.VERIFIED)
    event = make_event(session)
    claim = make_claim(
        session,
        storm_file,
        event,
        status=ClaimStatus.VERIFIED,
    )
    make_verification(session, claim)
    return storm_file, claim


def _approved_allocation(client, session):
    storm_file, claim = _verified_claim(session)
    _, director = _credential(session, AppRole.DIRECTOR)
    response = client.post(
        f"/v1/claims/{claim.id}/allocations/approve",
        json=ALLOCATION_BODY,
        headers={
            "Authorization": f"Bearer {director.token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 201, response.text
    return storm_file, claim, response.json()["allocation"]["id"]


def _sign(client, allocation_id: str, token: str, *, key: str | None = None, body=None):
    return client.post(
        f"/v1/allocations/{allocation_id}/disbursements/sign",
        json=body or SIGN_BODY,
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key or str(uuid.uuid4()),
        },
    )


def _execute(client, disbursement_id: str, token: str, *, key: str | None = None, body=None):
    return client.post(
        f"/v1/disbursements/{disbursement_id}/execute",
        json=body or EXECUTE_BODY,
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key or str(uuid.uuid4()),
        },
    )


def test_finance_signature_is_atomic_bound_and_idempotent(session, monkeypatch):
    client = _client(monkeypatch, session)
    _, _, allocation_id = _approved_allocation(client, session)
    finance, token = _credential(session, AppRole.FINANCE_OFFICER)
    key = str(uuid.uuid4())

    first = _sign(client, allocation_id, token.token, key=key)
    replay = _sign(client, allocation_id, token.token, key=key)
    changed = _sign(
        client,
        allocation_id,
        token.token,
        key=key,
        body={**SIGN_BODY, "channel": "MOBILE_MONEY"},
    )
    duplicate = _sign(client, allocation_id, token.token)

    assert first.status_code == 201, first.text
    assert replay.status_code == 200
    assert changed.status_code == 409
    assert duplicate.status_code == 409
    body = first.json()
    assert body["approval"]["gate"] == "DISBURSEMENT_BATCH"
    assert body["approval"]["approved_by"] == {
        "id": str(finance.id),
        "display_name": finance.display_name,
        "role": "FINANCE_OFFICER",
    }
    assert body["batch"]["total"] == "45000.00"
    assert body["batch"]["channel"] == "BANK"
    assert len(body["batch"]["snapshot_hash"]) == 64
    assert body["disbursement"]["status"] == "PENDING"
    assert body["disbursement"]["simulated"] is True
    assert body["disbursement"]["executor_provenance"] == "SIMULATED_DEMO"
    assert len(body["disbursement"]["snapshot_hash"]) == 64
    assert body["money_movement"] == "NOT_INITIATED"
    assert body["no_real_money_moved"] is True
    assert replay.json()["idempotent_replay"] is True
    assert session.scalar(select(func.count()).select_from(DisbursementBatch)) == 1
    assert session.scalar(select(func.count()).select_from(Disbursement)) == 1
    assert session.scalar(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.action == "disbursement.batch_signed")
    ) == 1
    assert ledger.verify_chain(session) is True


def test_batch_signing_requires_finance_role_and_supported_cash_channel(
    session, monkeypatch
):
    client = _client(monkeypatch, session)
    _, _, allocation_id = _approved_allocation(client, session)
    _, director = _credential(session, AppRole.DIRECTOR)

    forbidden = _sign(client, allocation_id, director.token)
    invalid_channel = _sign(
        client,
        allocation_id,
        director.token,
        body={**SIGN_BODY, "channel": "GOODS"},
    )

    assert forbidden.status_code == 403
    # Body validation intentionally happens before route authentication in
    # FastAPI; either way no row may cross the gate.
    assert invalid_channel.status_code in {403, 422}
    assert session.scalar(select(func.count()).select_from(Disbursement)) == 0


def test_execution_is_disabled_by_default_and_requires_explicit_acknowledgement(
    session, monkeypatch
):
    client = _client(monkeypatch, session, execution_enabled=False)
    _, _, allocation_id = _approved_allocation(client, session)
    _, finance = _credential(session, AppRole.FINANCE_OFFICER)
    signed = _sign(client, allocation_id, finance.token)
    disbursement_id = signed.json()["disbursement"]["id"]

    disabled = _execute(client, disbursement_id, finance.token)
    missing_ack = _execute(
        client,
        disbursement_id,
        finance.token,
        body={"executor_provenance": "SIMULATED_DEMO"},
    )

    assert disabled.status_code == 503
    assert "disabled" in disabled.json()["detail"]
    assert missing_ack.status_code == 422
    disbursement = session.get(Disbursement, disbursement_id)
    assert disbursement is not None
    assert disbursement.status is DisbursementStatus.PENDING
    assert disbursement.external_ref is None
    assert session.scalar(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.action == "disbursement.executed")
    ) == 0


def test_simulated_confirmation_settles_claim_and_file_only_after_receipt(
    session, monkeypatch
):
    client = _client(monkeypatch, session, execution_enabled=True)
    storm_file, claim, allocation_id = _approved_allocation(client, session)
    finance_user, finance = _credential(session, AppRole.FINANCE_OFFICER)
    signed = _sign(client, allocation_id, finance.token)
    disbursement_id = signed.json()["disbursement"]["id"]
    key = str(uuid.uuid4())

    executed = _execute(client, disbursement_id, finance.token, key=key)
    replay = _execute(client, disbursement_id, finance.token, key=key)
    changed_key = _execute(client, disbursement_id, finance.token)

    assert executed.status_code == 200, executed.text
    body = executed.json()
    assert body["disbursement"]["status"] == "CONFIRMED"
    assert body["disbursement"]["simulated"] is True
    assert body["disbursement"]["executor_provider"] == (
        "LIGHTHOUSE_DEMO_EXECUTOR_V1"
    )
    assert body["provider_confirmation_ref"].startswith("DEMO-")
    assert len(body["provider_confirmation_hash"]) == 64
    assert body["money_movement"] == "SIMULATED_CONFIRMATION_ONLY"
    assert body["no_real_money_moved"] is True
    assert body["execution_ledger"]["action"] == "disbursement.executed"
    assert body["confirmation_ledger"]["action"] == "disbursement.confirmed"
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["provider_confirmation_ref"] == body["provider_confirmation_ref"]
    assert changed_key.status_code == 409

    session.refresh(claim)
    session.refresh(storm_file)
    assert claim.status is ClaimStatus.SETTLED
    assert claim.settled_at is not None
    assert storm_file.state is StormFileState.SETTLED
    disbursement = session.get(Disbursement, disbursement_id)
    assert disbursement is not None
    assert disbursement.execution_requested_by == finance_user.id
    assert disbursement.confirmed_at is not None
    assert disbursement.external_ref == body["provider_confirmation_ref"]
    assert ledger.verify_chain(session) is True


def test_public_ledger_distinguishes_three_states_and_aggregates_without_identity(
    session, monkeypatch
):
    client = _client(monkeypatch, session, execution_enabled=True)
    storm_file, claim, allocation_id = _approved_allocation(client, session)
    _, finance = _credential(session, AppRole.FINANCE_OFFICER)
    signed = _sign(client, allocation_id, finance.token)
    disbursement_id = signed.json()["disbursement"]["id"]
    confirmed = _execute(client, disbursement_id, finance.token)
    assert confirmed.status_code == 200

    response = client.get("/v1/public/ledger?after_seq=0&limit=100")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [entry["action"] for entry in body["entries"]] == [
        "allocation.approved",
        "disbursement.executed",
        "disbursement.confirmed",
    ]
    assert body["entries"][0]["money_movement"]["status"] == (
        "NOT_INITIATED_AT_APPROVAL"
    )
    assert body["entries"][1]["money_movement"]["status"] == (
        "SIMULATION_EXECUTED_NO_REAL_FUNDS"
    )
    assert body["entries"][2]["money_movement"]["status"] == (
        "SIMULATED_CONFIRMATION_RECORDED_NO_REAL_FUNDS"
    )
    assert body["aggregate"] == {
        "scope": "CONFIRMED_SIMULATED_RELIEF_ONLY",
        "count": 1,
        "amount": "45000.00",
        "currency": "JMD",
        "no_real_money_moved": True,
        # No claim.created receipt in a factory-built claim, so T2R has no
        # start point and honestly reports none rather than guessing one.
        "median_time_to_relief_hours": None,
        "time_to_relief_sample": 0,
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
    text_body = response.text
    for private in (
        str(claim.id),
        claim.claim_ref,
        str(storm_file.id),
        allocation_id,
        disbursement_id,
        confirmed.json()["provider_confirmation_ref"],
        storm_file.parish,
        claim.damage_type,
    ):
        assert private not in text_body
    assert "disbursement.batch_signed" not in text_body
    assert "recorded_at" not in text_body
    assert "subject_id" not in text_body


def test_protected_settlement_queue_tracks_each_truthful_state(session, monkeypatch):
    client = _client(monkeypatch, session, execution_enabled=True)
    _, _, allocation_id = _approved_allocation(client, session)
    _, finance = _credential(session, AppRole.FINANCE_OFFICER)
    headers = {"Authorization": f"Bearer {finance.token}"}

    waiting = client.get("/v1/settlements", headers=headers)
    signed = _sign(client, allocation_id, finance.token)
    pending = client.get("/v1/settlements", headers=headers)
    _execute(client, signed.json()["disbursement"]["id"], finance.token)
    confirmed = client.get("/v1/settlements", headers=headers)

    assert waiting.json()["settlements"][0]["state"] == (
        "AWAITING_FINANCE_SIGNATURE"
    )
    assert pending.json()["settlements"][0]["state"] == (
        "SIGNED_PENDING_SIMULATED_EXECUTION"
    )
    assert confirmed.json()["settlements"][0]["state"] == "SIMULATED_CONFIRMED"
    assert confirmed.json()["execution"] == {
        "enabled": True,
        "executor_provenance": "SIMULATED_DEMO",
        "no_real_payment_provider": True,
    }


def test_database_refuses_unsigned_batch_lifecycle_skip_and_binding_mutation(
    session, monkeypatch
):
    client = _client(monkeypatch, session, execution_enabled=True)
    _, _, allocation_id = _approved_allocation(client, session)
    finance_user, finance = _credential(session, AppRole.FINANCE_OFFICER)

    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            DisbursementBatch(
                channel=DisbursementChannel.BANK,
                total=45000,
                approval_id=uuid.uuid4(),
            )
        )
        session.flush()

    signed = _sign(client, allocation_id, finance.token)
    batch_id = signed.json()["batch"]["id"]
    disbursement_id = signed.json()["disbursement"]["id"]

    for statement, parameters in (
        (
            "UPDATE disbursement_batch SET total = 1 WHERE id = :id",
            {"id": batch_id},
        ),
        (
            "UPDATE disbursement SET allocation_id = gen_random_uuid() WHERE id = :id",
            {"id": disbursement_id},
        ),
        (
            "UPDATE disbursement SET status = 'CONFIRMED', "
            "execution_requested_by = :actor, execution_idempotency_key = 'x', "
            "execution_request_hash = repeat('a',64), executed_at = now(), "
            "confirmed_at = now(), external_ref = 'DEMO-AAAAAAAAAAAAAAAAAAAAAAAA', "
            "provider_confirmation_hash = repeat('b',64) WHERE id = :id",
            {"id": disbursement_id, "actor": finance_user.id},
        ),
    ):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(text(statement), parameters)

    disbursement = session.get(Disbursement, disbursement_id)
    assert disbursement is not None
    assert disbursement.status is DisbursementStatus.PENDING


def test_database_refuses_fabricated_confirmation_ledger_while_pending(
    session, monkeypatch
):
    client = _client(monkeypatch, session)
    _, _, allocation_id = _approved_allocation(client, session)
    _, finance = _credential(session, AppRole.FINANCE_OFFICER)
    signed = _sign(client, allocation_id, finance.token)
    disbursement_id = signed.json()["disbursement"]["id"]

    with pytest.raises(DBAPIError), session.begin_nested():
        ledger.append(
            session,
            action="disbursement.confirmed",
            subject_type="disbursement",
            subject_id=uuid.UUID(disbursement_id),
            payload={"money_movement": "REAL_MONEY_MOVED"},
        )


def test_execution_idempotency_key_cannot_cross_disbursements(session, monkeypatch):
    client = _client(monkeypatch, session, execution_enabled=True)
    _, _, first_allocation = _approved_allocation(client, session)
    _, _, second_allocation = _approved_allocation(client, session)
    _, finance = _credential(session, AppRole.FINANCE_OFFICER)
    first = _sign(client, first_allocation, finance.token)
    second = _sign(client, second_allocation, finance.token)
    key = str(uuid.uuid4())

    first_execution = _execute(
        client, first.json()["disbursement"]["id"], finance.token, key=key
    )
    second_execution = _execute(
        client, second.json()["disbursement"]["id"], finance.token, key=key
    )

    assert first_execution.status_code == 200
    assert second_execution.status_code == 409
    assert "different disbursement" in second_execution.json()["detail"]


def test_settlement_approval_is_stored_as_exact_finance_gate(session, monkeypatch):
    client = _client(monkeypatch, session)
    _, _, allocation_id = _approved_allocation(client, session)
    finance_user, finance = _credential(session, AppRole.FINANCE_OFFICER)
    response = _sign(client, allocation_id, finance.token)

    approval = session.get(Approval, response.json()["approval"]["id"])
    assert approval is not None
    assert str(approval.gate) == "DISBURSEMENT_BATCH"
    assert approval.subject_type == "disbursement_batch"
    assert approval.subject_id == uuid.UUID(response.json()["batch"]["id"])
    assert approval.approved_by == finance_user.id
    assert approval.role_at_time is AppRole.FINANCE_OFFICER
