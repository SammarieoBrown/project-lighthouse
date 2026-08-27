"""Record every alert we send, per household and per channel (ALT-02).

Revision ID: 0013_alert_delivery
Revises: 0012_donor_pool_allocations
Create Date: 2026-08-27

ALT-02's acceptance criterion is "per-recipient delivery status recorded" and
there was nowhere to record it. The Alert Agent could draft a cascade and a
Director could sign it, and then the trail stopped.

One row per household per channel per signed cascade, so a fallback is a second
row rather than an overwrite. "We tried WhatsApp, it never confirmed, we sent an
SMS" is three facts and a status column can only hold one of them.

The household is identified by ``phone_hash`` and ``storm_file_id`` and never by
the number. Phone numbers are hashed everywhere except the StormFile row, and a
table of who we messaged is exactly the kind of thing that quietly becomes a
directory.

``approval_id`` is NOT NULL, which makes G1 structural here the way
``disbursement.approval_id`` makes G3 structural: an alert row cannot exist
without the signature that authorised the cascade it belongs to.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0013_alert_delivery"
down_revision = "0012_donor_pool_allocations"
branch_labels = None
depends_on = None


def _table_exists() -> bool:
    return bool(
        op.get_bind()
        .execute(
            text(
                """
                SELECT 1 FROM information_schema.tables
                 WHERE table_schema = current_schema()
                   AND table_name = 'alert_delivery'
                """
            )
        )
        .first()
    )


def upgrade() -> None:
    if _table_exists():
        return
    op.execute(
        """
CREATE TYPE alert_channel AS ENUM ('WHATSAPP', 'SMS');
CREATE TYPE alert_delivery_status AS ENUM (
  'QUEUED', 'SENT', 'CONFIRMED', 'FAILED', 'SUPERSEDED'
);
CREATE TABLE alert_delivery (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  approval_id    uuid NOT NULL REFERENCES approval(id) ON DELETE RESTRICT,
  storm_file_id  uuid NOT NULL REFERENCES storm_file(id) ON DELETE CASCADE,
  phone_hash     text NOT NULL,
  parish         text,
  community      text,
  channel        alert_channel NOT NULL,
  status         alert_delivery_status NOT NULL DEFAULT 'QUEUED',
  simulated      boolean NOT NULL DEFAULT true,
  provider_ref   text,
  failure_reason text,
  attempted_at   timestamptz NOT NULL DEFAULT now(),
  confirmed_at   timestamptz,
  CONSTRAINT alert_delivery_confirmed_chk CHECK (
    (status = 'CONFIRMED') = (confirmed_at IS NOT NULL)
  )
);
CREATE INDEX alert_delivery_approval_idx
  ON alert_delivery (approval_id, storm_file_id);
CREATE UNIQUE INDEX alert_delivery_attempt_uidx
  ON alert_delivery (approval_id, storm_file_id, channel);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS alert_delivery;
        DROP TYPE IF EXISTS alert_delivery_status;
        DROP TYPE IF EXISTS alert_channel;
        """
    )
