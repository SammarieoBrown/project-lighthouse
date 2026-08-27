"""Act 3 stops at an authenticated allocation and says so exactly."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from lighthouse_contracts import (
    ActorKind,
    AppRole,
    ClaimStatus,
    Event,
    GateKind,
    PayerRoute,
    ResourceKind,
    StormFileState,
    Verdict,
)

from app import approvals, ledger, public_ledger
from app.approval_credentials import (
    hash_human_token,
    issue_human_credential,
    set_human_password,
)
from app.human_auth import authenticate_human
from app.models import (
    Allocation,
    AllocationPlan,
    Approval,
    Disbursement,
    HumanCredential,
    DonationPool,
    LedgerEntry,
    StockItem,
    Verification,
    Warehouse,
)
from app.donations_service import create_pool, record_donation
from app.web import BoundedApprovalBodyMiddleware, _MAX_APPROVAL_BODY_BYTES, app

from factories import (
    make_claim,
    make_event,
    make_storm_file,
    make_user,
    make_verification,
)


BODY = {
    "resource": "CASH",
    "amount": "45000.00",
    "currency": "JMD",
    "payer_route": "GOV_RELIEF",
    "note": "Director reviewed the synthetic claim",
}


def test_direct_approval_body_is_bounded_before_parsing_or_authentication():
    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    middleware = BoundedApprovalBodyMiddleware(downstream)
    chunks = [
        b"x" * (_MAX_APPROVAL_BODY_BYTES // 2),
        b"y" * (_MAX_APPROVAL_BODY_BYTES // 2 + 1),
    ]
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": f"/v1/claims/{uuid.uuid4()}/allocations/approve",
        "raw_path": b"",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 443),
    }

    asyncio.run(middleware(scope, receive, send))

    assert called is False
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def _credential(session, role: AppRole = AppRole.DIRECTOR):
    user = make_user(session, role)
    password = "correct horse lighthouse"
    set_human_password(session, email=user.email, password=password)
    issued = issue_human_credential(session, email=user.email, password=password)
    return user, issued


def _client(monkeypatch, session) -> TestClient:
    @contextmanager
    def scoped():
        yield session
        session.flush()

    monkeypatch.setattr(approvals, "session_scope", scoped)
    monkeypatch.setattr(public_ledger, "session_scope", scoped)
    return TestClient(app)


def _verified_claim(session):
    storm_file = make_storm_file(session, state=StormFileState.VERIFIED)
    event = make_event(session)
    claim = make_claim(session, storm_file, event, status=ClaimStatus.VERIFIED)
    make_verification(session, claim)
    return storm_file, claim


def _approve(client, claim, token, *, body=BODY, key: str | None = None):
    return client.post(
        f"/v1/claims/{claim.id}/allocations/approve",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key or str(uuid.uuid4()),
        },
    )


def test_credential_is_recent_hashed_and_role_scoped(session):
    director, issued = _credential(session)
    stored = session.scalar(
        select(HumanCredential).where(HumanCredential.id == issued.credential_id)
    )

    assert stored is not None
    assert stored.token_hash == hash_human_token(issued.token)
    assert issued.token not in stored.token_hash
    assert stored.expires_at - stored.reauthenticated_at == timedelta(minutes=5)

    authenticated = authenticate_human(
        session,
        f"Bearer {issued.token}",
        allowed_roles={AppRole.DIRECTOR},
    )
    assert authenticated.user.id == director.id


def test_approval_route_requires_authentication_and_director_role(
    session, monkeypatch
):
    _, claim = _verified_claim(session)
    _, reviewer_token = _credential(session, AppRole.REVIEW_CLERK)
    client = _client(monkeypatch, session)
    path = f"/v1/claims/{claim.id}/allocations/approve"

    missing = client.post(path, json=BODY, headers={"Idempotency-Key": "missing-auth"})
    forbidden = client.post(
        path,
        json=BODY,
        headers={
            "Authorization": f"Bearer {reviewer_token.token}",
            "Idempotency-Key": "wrong-role",
        },
    )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert forbidden.status_code == 403
    assert session.scalar(select(func.count()).select_from(Approval)) == 0


def test_verified_claim_approval_is_atomic_explicit_and_creates_no_disbursement(
    session, monkeypatch
):
    _, claim = _verified_claim(session)
    director, issued = _credential(session)
    client = _client(monkeypatch, session)

    response = client.post(
        f"/v1/claims/{claim.id}/allocations/approve",
        json=BODY,
        headers={
            "Authorization": f"Bearer {issued.token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["approval"]["gate"] == "ALLOCATION_PLAN"
    assert body["approval"]["approved_by"] == {
        "id": str(director.id),
        "display_name": director.display_name,
        "role": "DIRECTOR",
    }
    assert body["allocation"] == {
        "id": body["allocation"]["id"],
        "plan_id": body["allocation"]["plan_id"],
        "claim_id": str(claim.id),
        "resource": "CASH",
        "amount": "45000.00",
        "currency": "JMD",
        "payer_route": "GOV_RELIEF",
        "state": "APPROVED_NOT_DISBURSED",
    }
    assert body["ledger"]["action"] == "allocation.approved"
    assert body["money_movement"] == {
        "status": "NOT_INITIATED",
        "disbursement_id": None,
        "external_ref": None,
    }
    assert body["idempotent_replay"] is False
    assert session.scalar(select(func.count()).select_from(Disbursement)) == 0
    assert ledger.verify_chain(session) is True


def test_exact_retry_returns_same_durable_records_and_changed_request_conflicts(
    session, monkeypatch
):
    _, claim = _verified_claim(session)
    _, issued = _credential(session)
    client = _client(monkeypatch, session)
    key = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {issued.token}",
        "Idempotency-Key": key,
    }
    path = f"/v1/claims/{claim.id}/allocations/approve"

    first = client.post(path, json=BODY, headers=headers)
    replay = client.post(path, json=BODY, headers=headers)
    changed = client.post(path, json={**BODY, "note": "different"}, headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert changed.status_code == 409
    assert replay.json()["idempotent_replay"] is True
    for section in ("approval", "allocation", "ledger", "money_movement"):
        assert replay.json()[section] == first.json()[section]
    assert session.scalar(select(func.count()).select_from(Approval)) == 1
    assert session.scalar(select(func.count()).select_from(Allocation)) == 1
    assert session.scalar(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.action == "allocation.approved")
    ) == 1


def test_unverified_claim_and_duplicate_distinct_key_are_refused(session, monkeypatch):
    storm_file = make_storm_file(session)
    event = make_event(session)
    filed = make_claim(session, storm_file, event)
    _, issued = _credential(session)
    client = _client(monkeypatch, session)
    auth = {"Authorization": f"Bearer {issued.token}"}

    unverified = client.post(
        f"/v1/claims/{filed.id}/allocations/approve",
        json=BODY,
        headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert unverified.status_code == 409

    filed.status = ClaimStatus.VERIFIED
    storm_file.state = StormFileState.VERIFIED
    session.flush()
    make_verification(session, filed)
    first = client.post(
        f"/v1/claims/{filed.id}/allocations/approve",
        json=BODY,
        headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
    )
    duplicate = client.post(
        f"/v1/claims/{filed.id}/allocations/approve",
        json=BODY,
        headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert first.status_code == 201
    assert duplicate.status_code == 409


def test_database_role_guard_and_approval_immutability(session, monkeypatch):
    _, claim = _verified_claim(session)
    _, issued = _credential(session)
    reviewer = make_user(session, AppRole.REVIEW_CLERK)

    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            Approval(
                gate=GateKind.ALLOCATION_PLAN,
                subject_type="allocation_plan",
                subject_id=uuid.uuid4(),
                approved_by=reviewer.id,
                role_at_time=reviewer.role,
                reauth_at=datetime.now(UTC),
            )
        )
        session.flush()

    client = _client(monkeypatch, session)
    created = client.post(
        f"/v1/claims/{claim.id}/allocations/approve",
        json=BODY,
        headers={
            "Authorization": f"Bearer {issued.token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    approval_id = created.json()["approval"]["id"]
    original = session.execute(
        select(Approval.note, Approval.request_hash).where(Approval.id == approval_id)
    ).one()

    session.execute(
        text("UPDATE approval SET note = 'tampered' WHERE id = :id"),
        {"id": approval_id},
    )
    session.execute(text("DELETE FROM approval WHERE id = :id"), {"id": approval_id})

    preserved = session.execute(
        select(Approval.note, Approval.request_hash).where(Approval.id == approval_id)
    ).one()
    assert preserved == original


def test_public_ledger_is_allowlisted_pii_safe_and_reports_full_chain(
    session, monkeypatch
):
    _, claim = _verified_claim(session)
    _, issued = _credential(session)
    client = _client(monkeypatch, session)
    created = client.post(
        f"/v1/claims/{claim.id}/allocations/approve",
        json=BODY,
        headers={
            "Authorization": f"Bearer {issued.token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    ledger.append(
        session,
        action="claim.internal_test",
        subject_type="claim",
        subject_id=claim.id,
        actor_kind=ActorKind.SYSTEM,
        payload={"phone": "+18765551234", "head_name": "Never Publish"},
    )

    response = client.get("/v1/public/ledger?after_seq=0&limit=50")

    assert response.status_code == 200
    body = response.json()
    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert entry["allocation"] == {
        "resource": "CASH",
        "amount": "45000.00",
        "currency": "JMD",
        "payer_route": "GOV_RELIEF",
        "synthetic": True,
    }
    assert entry["approval"] == {"gate": "ALLOCATION_PLAN"}
    assert entry["money_movement"] == {"status": "NOT_INITIATED_AT_APPROVAL"}
    assert body["chain"]["valid"] is True
    assert body["chain"]["scope"] == "FULL_INTERNAL_LEDGER"
    assert body["chain"]["head_seq"] > entry["seq"]
    assert "+18765551234" not in response.text
    assert "Never Publish" not in response.text
    assert str(claim.id) not in response.text
    assert created.json()["allocation"]["id"] not in response.text
    assert "Saint Elizabeth" not in response.text
    assert "ROOF_DAMAGE" not in response.text
    assert "recorded_on" in entry
    assert "recorded_at" not in entry
    assert "subject_id" not in entry
    assert "id" not in entry


def test_public_ledger_withholds_every_entry_when_chain_verification_fails(
    session, monkeypatch
):
    _, claim = _verified_claim(session)
    _, issued = _credential(session)
    client = _client(monkeypatch, session)
    client.post(
        f"/v1/claims/{claim.id}/allocations/approve",
        json=BODY,
        headers={
            "Authorization": f"Bearer {issued.token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    ledger.clear_verify_chain_cache()
    monkeypatch.setattr(
        public_ledger.ledger,
        "cached_verify_chain",
        lambda _session, **_kwargs: False,
    )

    response = client.get("/v1/public/ledger")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "ledger integrity check failed; public entries withheld"
    }
    assert "allocation.approved" not in response.text


@pytest.mark.parametrize(
    "invalid_body",
    [
        {
            "resource": "ITEM",
            "currency": "JMD",
            "payer_route": "GOV_RELIEF",
            "sku": "TARP",
            "quantity": 1,
        },
        {**BODY, "payer_route": "INSURER"},
        {**BODY, "payer_route": "BOTH"},
        {**BODY, "payer_route": "DONOR_POOL"},
        {**BODY, "amount": "44999.99"},
        {**BODY, "currency": "USD"},
    ],
)
def test_release_policy_rejects_every_non_fixed_grant_shape(
    session, monkeypatch, invalid_body
):
    _, claim = _verified_claim(session)
    _, issued = _credential(session)
    response = _approve(_client(monkeypatch, session), claim, issued.token, body=invalid_body)

    assert response.status_code == 422
    assert session.scalar(select(func.count()).select_from(Allocation)) == 0
    assert session.scalar(select(func.count()).select_from(Approval)) == 0


def test_storm_file_must_also_be_verified_or_settled(session, monkeypatch):
    storm_file = make_storm_file(session, state=StormFileState.AFFECTED)
    event = make_event(session)
    claim = make_claim(session, storm_file, event, status=ClaimStatus.VERIFIED)
    make_verification(session, claim)
    _, issued = _credential(session)

    response = _approve(_client(monkeypatch, session), claim, issued.token)

    assert response.status_code == 409
    assert "storm file" in response.json()["detail"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda signals: signals.pop("media_integrity"),
        lambda signals: signals.update({"free_text": {"present": True, "score": 1}}),
        lambda signals: signals["media_integrity"].update({"score": True}),
        lambda signals: signals["media_integrity"].update({"score": 1.1}),
        lambda signals: signals["media_integrity"].update(
            {"present": False, "score": 0.4}
        ),
    ],
)
def test_latest_verification_with_malformed_signal_contract_is_rejected(
    session, monkeypatch, mutate
):
    _, claim = _verified_claim(session)
    signals = {
        name: {"present": True, "score": 0.9, "evidence": {}}
        for name in (
            "hazard_sufficiency",
            "satellite_change",
            "neighbour_corroboration",
            "registry_match",
            "media_integrity",
        )
    }
    mutate(signals)
    make_verification(
        session,
        claim,
        signals=signals,
        created_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    _, issued = _credential(session)

    response = _approve(_client(monkeypatch, session), claim, issued.token)

    assert response.status_code == 409
    assert session.scalar(select(func.count()).select_from(Allocation)) == 0


@pytest.mark.parametrize(
    ("verdict", "confidence", "capped"),
    [
        (Verdict.REVIEW, 0.90, False),
        (Verdict.AUTO_VERIFIED, 0.84, False),
        (Verdict.AUTO_VERIFIED, 0.90, True),
        (Verdict.FLAGGED, 0.20, False),
    ],
)
def test_latest_ineligible_verification_overrides_older_eligible_evidence(
    session, monkeypatch, verdict, confidence, capped
):
    _, claim = _verified_claim(session)
    make_verification(
        session,
        claim,
        verdict=verdict,
        confidence=confidence,
        capped=capped,
        created_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    _, issued = _credential(session)

    response = _approve(_client(monkeypatch, session), claim, issued.token)

    assert response.status_code == 409


def test_human_review_clerk_approval_is_eligible_and_bound_to_receipt(
    session, monkeypatch
):
    _, claim = _verified_claim(session)
    reviewer = make_user(session, AppRole.REVIEW_CLERK)
    verification = make_verification(
        session,
        claim,
        verdict=Verdict.APPROVED,
        confidence=0.40,
        actor=reviewer,
        created_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    _, issued = _credential(session)

    response = _approve(_client(monkeypatch, session), claim, issued.token)

    assert response.status_code == 201
    allocation = session.get(Allocation, response.json()["allocation"]["id"])
    entry = session.get(LedgerEntry, response.json()["ledger"]["seq"])
    assert allocation is not None and entry is not None
    assert allocation.verification_id == verification.id
    assert allocation.verification_snapshot_hash == verification.snapshot_hash
    assert entry.payload["verification_id"] == str(verification.id)
    assert entry.payload["verification_snapshot_hash"] == verification.snapshot_hash


def test_verification_snapshot_is_database_generated_refreshed_and_deterministic(session):
    storm_file = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(
        session,
        storm_file,
        make_event(session),
        status=ClaimStatus.VERIFIED,
    )
    verification = make_verification(session, claim)
    expected = session.scalar(
        text(
            "SELECT verification_snapshot_digest(v) "
            "FROM verification v WHERE v.id = :id"
        ),
        {"id": verification.id},
    )

    assert verification.snapshot_hash == expected
    assert len(verification.snapshot_hash) == 64


def test_verification_authority_and_evidence_are_database_enforced(session):
    storm_file = make_storm_file(session, state=StormFileState.VERIFIED)
    claim = make_claim(
        session,
        storm_file,
        make_event(session),
        status=ClaimStatus.VERIFIED,
    )
    director = make_user(session, AppRole.DIRECTOR)
    signals = {
        name: {"present": True, "score": 0.9}
        for name in (
            "hazard_sufficiency",
            "satellite_change",
            "neighbour_corroboration",
            "registry_match",
            "media_integrity",
        )
    }

    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            Verification(
                claim_id=claim.id,
                signals=signals,
                confidence=0.9,
                verdict=Verdict.APPROVED,
                actor_kind=ActorKind.HUMAN,
                actor_id=director.id,
            )
        )
        session.flush()

    verification = make_verification(session, claim)
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text("UPDATE verification SET rationale = 'tampered' WHERE id = :id"),
            {"id": verification.id},
        )
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text("DELETE FROM verification WHERE id = :id"),
            {"id": verification.id},
        )


def test_signed_plan_and_allocation_reject_divergent_mutation_or_addition(
    session, monkeypatch
):
    _, claim = _verified_claim(session)
    director, issued = _credential(session)
    created = _approve(_client(monkeypatch, session), claim, issued.token)
    assert created.status_code == 201
    allocation = session.get(Allocation, created.json()["allocation"]["id"])
    assert allocation is not None

    for statement, row_id in (
        ("UPDATE allocation_plan SET proposed_by = 'changed' WHERE id = :id", allocation.plan_id),
        ("DELETE FROM allocation_plan WHERE id = :id", allocation.plan_id),
        ("UPDATE allocation SET amount = 1 WHERE id = :id", allocation.id),
        ("DELETE FROM allocation WHERE id = :id", allocation.id),
    ):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(text(statement), {"id": row_id})

    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            Allocation(
                plan_id=allocation.plan_id,
                claim_id=allocation.claim_id,
                resource=ResourceKind.CASH,
                amount=Decimal("45000.00"),
                currency="JMD",
                payer_route=PayerRoute.GOV_RELIEF,
                verification_id=allocation.verification_id,
                verification_snapshot_hash=allocation.verification_snapshot_hash,
            )
        )
        session.flush()

    # A valid-looking direct addition still cannot commit without its matching
    # receipt; this catches bypasses outside the API service.
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text(
                "SET CONSTRAINTS signed_plan_complete_trigger, "
                "allocation_ledger_complete_trigger DEFERRED"
            )
        )
        plan_id = uuid.uuid4()
        approval = Approval(
            gate=GateKind.ALLOCATION_PLAN,
            subject_type="allocation_plan",
            subject_id=plan_id,
            approved_by=director.id,
            role_at_time=director.role,
            reauth_at=datetime.now(UTC),
        )
        session.add(approval)
        session.flush()
        plan = AllocationPlan(
            id=plan_id,
            hazard_event_id=claim.hazard_event_id,
            proposed_by="direct_bypass",
            approval_id=approval.id,
        )
        session.add(plan)
        session.flush()
        direct = Allocation(
            plan_id=plan.id,
            claim_id=allocation.claim_id,
            resource=ResourceKind.CASH,
            amount=Decimal("45000.00"),
            currency="JMD",
            payer_route=PayerRoute.GOV_RELIEF,
            verification_id=allocation.verification_id,
            verification_snapshot_hash=allocation.verification_snapshot_hash,
        )
        session.add(direct)
        session.flush()
        session.execute(
            text(
                "SET CONSTRAINTS signed_plan_complete_trigger, "
                "allocation_ledger_complete_trigger IMMEDIATE"
            )
        )


def test_plan_rejects_approval_for_a_different_subject(session):
    director = make_user(session, AppRole.DIRECTOR)
    event = make_event(session)
    approval = Approval(
        gate=GateKind.ALLOCATION_PLAN,
        subject_type="allocation_plan",
        subject_id=uuid.uuid4(),
        approved_by=director.id,
        role_at_time=director.role,
        reauth_at=datetime.now(UTC),
    )
    session.add(approval)
    session.flush()

    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            AllocationPlan(
                id=uuid.uuid4(),
                hazard_event_id=event.id,
                proposed_by="mismatched",
                approval_id=approval.id,
            )
        )
        session.flush()


def test_unknown_household_classification_is_safely_coarsened_not_denied(
    session, monkeypatch
):
    storm_file = make_storm_file(session, state=StormFileState.VERIFIED)
    storm_file.parish = "Resident Jane Doe near the blue shop"
    claim = make_claim(
        session,
        storm_file,
        make_event(session),
        status=ClaimStatus.VERIFIED,
        damage_type="private free-form detail",
    )
    make_verification(session, claim)
    _, issued = _credential(session)

    response = _approve(_client(monkeypatch, session), claim, issued.token)

    assert response.status_code == 201
    entry = session.get(LedgerEntry, response.json()["ledger"]["seq"])
    assert entry is not None
    assert entry.payload["parish"] == "UNSPECIFIED"
    assert entry.payload["need_category"] == "OTHER_DAMAGE"
    public = _client(monkeypatch, session).get("/v1/public/ledger")
    assert public.status_code == 200
    assert "Resident Jane Doe" not in public.text
    assert "private free-form detail" not in public.text
    assert "UNSPECIFIED" not in public.text
    assert "OTHER_DAMAGE" not in public.text


# ---------------------------------------------------------------------------
# PAY-06's goods half. Cash stays flat; goods carry a SKU, a count, and the
# shelf they leave — and the shelf moves when a Director signs.
# ---------------------------------------------------------------------------


def _stocked_warehouse(session, sku="tarpaulin", quantity=5):
    warehouse = Warehouse(name="St Elizabeth Depot", parish="St Elizabeth")
    session.add(warehouse)
    session.flush()
    session.add(StockItem(warehouse_id=warehouse.id, sku=sku, quantity=quantity))
    session.flush()
    return warehouse


def _goods_body(warehouse, *, sku="tarpaulin", quantity=2):
    return {
        "resource": "ITEM",
        "payer_route": "GOV_RELIEF",
        "sku": sku,
        "quantity": quantity,
        "warehouse_id": str(warehouse.id),
        "note": "Roof cover for a household with the roof off.",
    }


def test_signing_goods_releases_stock_and_decrements_the_shelf(session, monkeypatch):
    storm_file, claim = _verified_claim(session)
    warehouse = _stocked_warehouse(session, quantity=5)
    director, issued = _credential(session)
    client = _client(monkeypatch, session)

    response = _approve(client, claim, issued.token, body=_goods_body(warehouse))

    assert response.status_code == 201, response.text
    allocation = session.scalar(
        select(Allocation).where(Allocation.resource == ResourceKind.ITEM)
    )
    assert allocation is not None
    assert allocation.sku == "tarpaulin"
    assert allocation.quantity == 2
    assert allocation.amount is None
    assert allocation.warehouse_id == warehouse.id
    # LGX-01: decremented by approved allocations.
    assert session.scalar(select(StockItem.quantity)) == 3


def test_a_goods_receipt_names_the_sku_and_never_an_amount(session, monkeypatch):
    storm_file, claim = _verified_claim(session)
    warehouse = _stocked_warehouse(session)
    director, issued = _credential(session)
    client = _client(monkeypatch, session)

    _approve(client, claim, issued.token, body=_goods_body(warehouse))

    entry = session.scalar(
        select(LedgerEntry)
        .where(LedgerEntry.action == str(Event.ALLOCATION_APPROVED))
        .order_by(LedgerEntry.seq.desc())
        .limit(1)
    )
    assert entry.payload["resource"] == "ITEM"
    assert entry.payload["sku"] == "tarpaulin"
    assert entry.payload["quantity"] == "2"
    # An amount on a goods row would be a valuation nobody made.
    assert "amount" not in entry.payload


def test_signing_for_stock_that_is_not_there_is_refused(session, monkeypatch):
    storm_file, claim = _verified_claim(session)
    warehouse = _stocked_warehouse(session, quantity=1)
    director, issued = _credential(session)
    client = _client(monkeypatch, session)

    response = _approve(
        client, claim, issued.token, body=_goods_body(warehouse, quantity=4)
    )

    assert response.status_code == 409
    assert "only 1" in response.text
    assert session.scalar(select(StockItem.quantity)) == 1


def test_cash_is_still_exactly_the_flat_grant(session, monkeypatch):
    """Widening the path for goods must not have loosened the cash half."""
    storm_file, claim = _verified_claim(session)
    director, issued = _credential(session)
    client = _client(monkeypatch, session)

    inflated = {**BODY, "amount": "90000.00"}
    response = _approve(client, claim, issued.token, body=inflated)

    assert response.status_code == 422
    assert session.scalar(select(func.count()).select_from(Allocation)) == 0


def test_a_goods_request_without_a_warehouse_is_refused(session, monkeypatch):
    storm_file, claim = _verified_claim(session)
    director, issued = _credential(session)
    client = _client(monkeypatch, session)

    body = {"resource": "ITEM", "payer_route": "GOV_RELIEF", "sku": "tarpaulin",
            "quantity": 1}
    response = _approve(client, claim, issued.token, body=body)

    assert response.status_code == 422
    assert "warehouse_id" in response.text


# ---------------------------------------------------------------------------
# DON-03. A donation pool is a payer source, and signing draws it down.
# ---------------------------------------------------------------------------


def _pool_with(session, amount="90000.00"):
    pool = create_pool(
        session, name="St Elizabeth pool", scope_kind="PARISH", scope_value="St Elizabeth"
    )
    record_donation(
        session, pool_id=pool.id, donor_handle="diaspora-42", amount=Decimal(amount)
    )
    return pool


def test_a_donor_pool_can_fund_a_grant_and_is_drawn_down(session, monkeypatch):
    storm_file, claim = _verified_claim(session)
    pool = _pool_with(session)
    director, issued = _credential(session)
    client = _client(monkeypatch, session)

    response = _approve(
        client,
        claim,
        issued.token,
        body={**BODY, "payer_route": "DONOR_POOL", "pool_id": str(pool.id)},
    )

    assert response.status_code == 201, response.text
    allocation = session.scalar(select(Allocation))
    assert allocation.payer_route is PayerRoute.DONOR_POOL
    assert allocation.pool_id == pool.id
    # 90,000 donated, one 45,000 grant released.
    assert session.scalar(select(DonationPool.balance)) == Decimal("45000.00")


def test_the_receipt_names_the_pool_that_paid(session, monkeypatch):
    storm_file, claim = _verified_claim(session)
    pool = _pool_with(session)
    director, issued = _credential(session)
    client = _client(monkeypatch, session)

    _approve(
        client,
        claim,
        issued.token,
        body={**BODY, "payer_route": "DONOR_POOL", "pool_id": str(pool.id)},
    )

    entry = session.scalar(
        select(LedgerEntry)
        .where(LedgerEntry.action == str(Event.ALLOCATION_APPROVED))
        .order_by(LedgerEntry.seq.desc())
        .limit(1)
    )
    assert entry.payload["payer_route"] == "DONOR_POOL"
    assert entry.payload["pool_id"] == str(pool.id)


def test_a_pool_that_cannot_cover_the_grant_is_refused(session, monkeypatch):
    storm_file, claim = _verified_claim(session)
    pool = _pool_with(session, amount="1000.00")
    director, issued = _credential(session)
    client = _client(monkeypatch, session)

    response = _approve(
        client,
        claim,
        issued.token,
        body={**BODY, "payer_route": "DONOR_POOL", "pool_id": str(pool.id)},
    )

    assert response.status_code == 409
    assert session.scalar(select(DonationPool.balance)) == Decimal("1000.00")
    assert session.scalar(select(func.count()).select_from(Allocation)) == 0


def test_relief_funding_may_not_name_a_pool_and_pool_funding_must(session, monkeypatch):
    """A relief-funded allocation naming a pool would claim a donor paid for
    something a donor did not."""
    storm_file, claim = _verified_claim(session)
    pool = _pool_with(session)
    director, issued = _credential(session)
    client = _client(monkeypatch, session)

    named = _approve(
        client, claim, issued.token, body={**BODY, "pool_id": str(pool.id)}
    )
    assert named.status_code == 422

    unnamed = _approve(
        client, claim, issued.token, body={**BODY, "payer_route": "DONOR_POOL"}
    )
    assert unnamed.status_code == 422


def test_an_insurer_may_not_fund_a_relief_allocation(session, monkeypatch):
    """Routing a claim to a carrier and funding a basket are different
    questions that happen to share an enum."""
    storm_file, claim = _verified_claim(session)
    director, issued = _credential(session)
    client = _client(monkeypatch, session)

    response = _approve(client, claim, issued.token, body={**BODY, "payer_route": "INSURER"})

    assert response.status_code == 422
