"""Release gates for the multi-storm replay library."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from app.console import library


def _payload(ref: str, *, complete: bool = True) -> dict:
    districts = [
        {"id": 0, "parish": "Kingston", "district": "A", "n": 1, "structures": 7},
        {"id": 1, "parish": "Kingston", "district": "B", "n": 1, "structures": 9},
    ]
    frames = [{"n": "1"}, {"n": "2"}]
    if complete:
        for frame in frames:
            frame["district_exposed"] = [[0, 0, 0], [1, 2, 3]]
    return {
        "event": {"id": ref.upper(), "name": "Test", "advisory_count": 2},
        "districts": districts,
        "frames": frames,
    }


def test_release_gate_requires_exposure_on_every_frame() -> None:
    with pytest.raises(library.ReleaseGateError, match="mapped exposure is incomplete"):
        library.require_releaseable(
            _payload("al992020", complete=False), external_ref="al992020"
        )


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (None, "measured"),
        (["measured"], "measured"),
        (["modelled"], "modelled"),
        (["measured", "modelled"], "mixed"),
        (["mixed"], "mixed"),
        (["measured", "unavailable"], "unavailable"),
        (["mixed", "not_applicable"], "mixed"),
        (["not_applicable"], "unavailable"),
        (["unexpected"], "unavailable"),
    ],
)
def test_size_source_rollup_preserves_uncertainty(values, expected) -> None:
    assert library._rollup_size_source(values) == expected


def test_export_all_honours_directory_and_uses_explicit_complete_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "known-good.json"
    fallback.write_text(json.dumps(_payload("al132025")))
    output = tmp_path / "nested" / "replay"

    monkeypatch.setattr(library, "replayable", lambda _session: ["al132025"])
    monkeypatch.setattr(
        library,
        "build_replay",
        lambda _session, *, external_ref: _payload(external_ref, complete=False),
    )
    monkeypatch.setattr(
        library,
        "_describe",
        lambda _session, ref: {
            "id": ref,
            "name": "Hurricane Melissa",
            "advisories": 2,
            "from": "2025-01-01T00:00:00Z",
            "to": "2025-01-01T06:00:00Z",
            "file": f"{ref}.json",
            "kind": "advisory",
            "size_source": "measured",
        },
    )

    entries = library.export_all(
        object(),
        directory=output,
        fallbacks={
            "al132025": library.TrustedArtifact(
                fallback,
                hashlib.sha256(fallback.read_bytes()).hexdigest(),
            )
        },
    )

    assert entries[0]["artifact_source"] == "last_known_good"
    assert (output / "index.json").exists()
    emitted = json.loads((output / "al132025.json").read_text())
    assert all("district_exposed" in frame for frame in emitted["frames"])
    assert json.loads((output / "index.json").read_text())["storms"] == entries


def test_export_all_skips_incomplete_artifact_without_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(library, "replayable", lambda _session: ["al992020"])
    monkeypatch.setattr(
        library,
        "build_replay",
        lambda _session, *, external_ref: _payload(external_ref, complete=False),
    )

    entries = library.export_all(object(), directory=tmp_path, fallbacks={})

    assert entries == []
    assert not (tmp_path / "al992020.json").exists()
    assert json.loads((tmp_path / "index.json").read_text())["storms"] == []


def test_fallback_requires_its_pinned_digest(tmp_path: Path) -> None:
    fallback = tmp_path / "fallback.json"
    fallback.write_text(json.dumps(_payload("al132025")))

    with pytest.raises(library.ReleaseGateError, match="expected"):
        library._read_releaseable(
            library.TrustedArtifact(fallback, "0" * 64),
            external_ref="al132025",
        )


def test_committed_default_library_artifact_keeps_mapped_exposure() -> None:
    artifact = library.REPLAY_DIR / "al132025.json"
    payload = json.loads(artifact.read_text())
    library.require_releaseable(payload, external_ref="al132025")
