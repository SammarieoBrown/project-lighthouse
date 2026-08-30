"""Register one real household so its Storm File stops being thin.

The replay seeder builds the synthetic registry; this is the same act for a
real phone — the registration an ODPEM officer would capture before a storm.
A registered profile is what lets the verification agent compare reported
damage against a known structure, so a thin file's ``registry_match`` stays
honestly Absent until someone runs this deliberately.

Usage:

    uv run python -m app.registry.register \
      --phone +18761234567 --parish "Saint Elizabeth" --community "Black River" \
      --roof zinc --walls block --built 2004 --floors 1 --household-size 4
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from lighthouse_contracts import ActorKind

from app import ledger
from app.db import session_scope
from app.intake.service import phone_hash
from app.models import StormFile
from app.registry.geography import parish_names
from app.registry.seeder import vulnerability


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.registry.register",
        description="Register or upgrade one household's Storm File profile.",
    )
    parser.add_argument("--phone", required=True, help="E.164 number, e.g. +18761234567")
    parser.add_argument("--parish", required=True)
    parser.add_argument("--community", default=None)
    parser.add_argument("--roof", required=True, help="e.g. zinc, concrete, shingle")
    parser.add_argument("--walls", required=True, help="e.g. block, wood, brick")
    parser.add_argument("--built", type=int, default=None, help="year built")
    parser.add_argument("--floors", type=int, default=1)
    parser.add_argument("--household-size", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    phone = args.phone.strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        print("--phone must be E.164, e.g. +18761234567", file=sys.stderr)
        return 2
    known = parish_names()
    if args.parish not in known:
        print(f"--parish must be one of: {', '.join(known)}", file=sys.stderr)
        return 2

    structure = {
        "roof": args.roof,
        "walls": args.walls,
        "built": args.built,
        "floors": args.floors,
    }
    people = {"total": args.household_size, "children": 0, "elderly": 0, "medical": []}

    with session_scope() as session:
        hashed = phone_hash(phone)
        row = session.scalar(select(StormFile).where(StormFile.phone_hash == hashed))
        created = row is None
        if row is None:
            row = StormFile(phone=phone, phone_hash=hashed, synthetic=False)
            session.add(row)
        row.parish = args.parish
        if args.community:
            row.community = args.community
        row.structure = structure
        row.people = people
        row.vuln_score = vulnerability(structure, people)
        row.thin = False
        session.flush()
        ledger.append(
            session,
            action="storm_file.registered",
            subject_type="storm_file",
            subject_id=row.id,
            payload={
                "created": created,
                "parish": row.parish,
                "community": row.community,
                "structure": structure,
            },
            actor_kind=ActorKind.HUMAN,
        )
        print(
            f"{'Registered' if created else 'Upgraded'} Storm File {row.id} "
            f"({row.parish}{' · ' + row.community if row.community else ''}, "
            f"vulnerability {row.vuln_score})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
