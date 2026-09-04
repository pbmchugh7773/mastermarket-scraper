#!/usr/bin/env python3
"""
Mark Lidl aliases that repair_lidl_aliases.py could not repair as unavailable.

Reads a proposal JSON written by repair_lidl_aliases.py and, for every
`unmatched` record whose reason is in --reasons (default: no_match — "nothing
in the sitemap resembled this product"), calls
PATCH /api/product-aliases/{id}/mark-unavailable with reason "persistent_404".
That sets is_active_for_scraping=False so the alias stops eating retry slots.

Proposal-only by default; --apply performs the writes. Reasons like
"ambiguous", "size_mismatch" or "unknown_mm_size" are left for a human because
the product probably still exists and only the match was inconclusive.

Usage:
    python mark_unmatched_unavailable.py /tmp/lidl_repair_proposal_X.json
    python mark_unmatched_unavailable.py /tmp/lidl_repair_proposal_X.json --apply
    python mark_unmatched_unavailable.py proposal.json --reasons no_match,size_mismatch --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

from discover_lidl_aliases import API_URL, API_TIMEOUT, _api_login

UNAVAILABLE_REASON = "persistent_404"
DEFAULT_REASONS = ("no_match",)


def select_unmatched(proposal, reasons):
    """Unmatched records from a repair proposal whose reason is in `reasons`."""
    reasons = set(reasons)
    return [r for r in proposal.get("unmatched", []) if r.get("reason") in reasons]


def _patch_mark_unavailable(alias_id, reason, token):
    resp = requests.patch(
        f"{API_URL}/api/product-aliases/{alias_id}/mark-unavailable",
        json={"reason": reason},
        headers={"Authorization": f"Bearer {token}"},
        timeout=API_TIMEOUT,
    )
    resp.raise_for_status()


def mark_unavailable(records, token, patch_fn=None):
    """PATCH each record's alias. Returns (done, failed); one failure never aborts."""
    patch_fn = patch_fn or _patch_mark_unavailable
    done = failed = 0
    for rec in records:
        try:
            patch_fn(rec["alias_id"], UNAVAILABLE_REASON, token)
            done += 1
        except Exception as exc:  # noqa: BLE001 — continue past one bad alias
            failed += 1
            print(f"  FAILED alias {rec['alias_id']}: {exc}", file=sys.stderr)
    return done, failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("proposal", help="JSON written by repair_lidl_aliases.py")
    parser.add_argument("--apply", action="store_true", help="Perform the PATCH calls.")
    parser.add_argument(
        "--reasons",
        default=",".join(DEFAULT_REASONS),
        help=f"Comma-separated unmatched reasons to act on (default: {','.join(DEFAULT_REASONS)}).",
    )
    args = parser.parse_args(argv)

    proposal = json.loads(Path(args.proposal).read_text())
    reasons = {r.strip() for r in args.reasons.split(",") if r.strip()}
    selected = select_unmatched(proposal, reasons)

    print(f"Unmatched in proposal: {len(proposal.get('unmatched', []))}")
    print(f"Selected for mark-unavailable (reasons={sorted(reasons)}): {len(selected)}")
    for rec in selected:
        print(f"  [{rec.get('reason')}] alias {rec['alias_id']}  {str(rec.get('product_name', ''))[:60]}")

    if not args.apply:
        print("\nProposal-only. Re-run with --apply to mark these unavailable.")
        return 0
    if not selected:
        print("\nNothing to mark.")
        return 0

    token = _api_login()
    done, failed = mark_unavailable(selected, token)
    print(f"\nMarked unavailable: {done} (failures={failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
