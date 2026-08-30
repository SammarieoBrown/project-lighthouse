"""Typed settings from the environment.

Reads the repo-root ``.env``. Nothing in here has a default that would let the
app start pointed at the wrong database — an unset ``DATABASE_URL`` is a startup
failure, not a fallback to localhost.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_DIR = REPO_ROOT / "packages" / "contracts"
SCHEMA_SQL = CONTRACTS_DIR / "schema.sql"

#: The committed Hurricane Melissa advisory cache. Committed rather than fetched
#: so the replay is byte-identical everywhere and makes no network calls on
#: stage; see data/replay/README.md.
REPLAY_CACHE = REPO_ROOT / "data" / "replay" / "cache"
MELISSA = REPLAY_CACHE / "al132025"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", extra="ignore", case_sensitive=False
    )

    database_url: str
    environment: str = "local"
    log_level: str = "INFO"

    # Signs the operator session cookie. No default on purpose: a generated
    # fallback would invalidate every session on each deploy, and a constant one
    # would let anyone holding this source forge a Director session. Unset means
    # operator sign-in returns 503 rather than silently accepting forgeries.
    session_secret: str | None = None
    public_base_url: str = "http://localhost:8000"
    replay_seed: int = 20251028

    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    # Media downloads should use a restricted API key in production. The
    # Account SID/AuthToken pair remains the webhook-signing secret and is only
    # accepted by the fetcher in local/demo environments.
    twilio_api_key_sid: str | None = None
    twilio_api_key_secret: str | None = None
    twilio_whatsapp_from: str | None = None
    # Explicitly binds live inbound claims to one hazard. Production commonly
    # has several historical/open rows, so choosing "latest" is not safe.
    intake_hazard_external_ref: str | None = None

    # Private, content-addressed intake media. These are worker-only values;
    # the console and webhook edge never need object-store credentials.
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_bucket: str = "lighthouse-media"

    # Disabled is a deliberate local default: a worker may not silently make
    # a paid inference call just because some unrelated API credential exists.
    intake_transcription_provider: str = "disabled"
    intake_transcription_model: str = "@cf/openai/whisper-large-v3-turbo"
    cloudflare_ai_api_token: str | None = None

    # Money execution is fail-closed.  The only implementation in this release
    # is an in-process demo simulator; it never contacts a bank, mobile-money
    # rail, voucher issuer, or other payment provider.  Operators must opt in
    # explicitly rather than getting simulated confirmations by accident.
    disbursement_executor_mode: Literal["disabled", "simulated"] = "disabled"

    # Disabled is a deliberate local default, same reasoning as the
    # transcription provider above: an agent must not silently start making
    # paid vision calls just because a key happens to be set.
    damage_assessment_provider: Literal["disabled", "anthropic"] = "disabled"

    # Whether the intake agent answers the household over WhatsApp — an
    # acknowledgment with the claim reference, then at most three follow-up
    # questions for missing fields. Disabled by default for the same reason as
    # every other sender in this file: a message to a real phone must be a
    # deliberate deployment decision, never a side effect of a key being set.
    intake_reply_mode: Literal["disabled", "live"] = "disabled"

    # ALT-02. There is no live sender in this release and "simulated" is the
    # only implemented mode. The registry is synthetic and so are its phone
    # numbers; a real message to a real number is the one mistake in this
    # system that cannot be taken back.
    alert_channel_mode: Literal["simulated"] = "simulated"
    anthropic_api_key: str | None = None
    # Which Claude model the assessor calls. Env-configured so changing tier
    # is a deployment decision, not a code change; the default is the cheapest
    # vision-capable tier because triaging a handful of photos does not need a
    # frontier model.
    damage_assessment_model: str = "claude-haiku-4-5"

    @property
    def sqlalchemy_url(self) -> str:
        """SQLAlchemy needs the driver named explicitly.

        Neon hands out a libpq-style URL. We drive it with psycopg 3, which
        SQLAlchemy only selects if the scheme says so.
        """
        url = self.database_url
        if url.startswith("postgresql+"):
            return url
        return url.replace("postgresql://", "postgresql+psycopg://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
