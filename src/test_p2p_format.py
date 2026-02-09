#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import parse_boa_pdf as parser


def run() -> None:
    parser.ParserConfig.load_from_json(
        Path(__file__).resolve().parent.parent / "config" / "boa_config.json"
    )
    cases = [
        (
            '03/27/25 Zelle payment from SMIRTHI MEENAKSHISUNDARAM for "Universal" Conf# bodl7mh6h',
            "Zelle payment from SMIRTHI MEENAKSHISUNDARAM for Universal",
        ),
        (
            "04/09/25 Zelle payment from KSHITIJ GUPTA Conf# myu4w16vu",
            "Zelle payment from KSHITIJ GUPTA",
        ),
        (
            '03/19/25 Zelle payment to KSHITIJ GUPTA for "skiing" Conf# njnxswo78',
            "Zelle payment to KSHITIJ GUPTA for skiing",
        ),
        (
            "04/10/25 Zelle payment to SARVESH PATIL Conf# rafllgt5f",
            "Zelle payment to SARVESH PATIL",
        ),
        (
            '04/10/25 Venmo payment from JENNY P for "dinner" Conf# abc123xyz',
            "Venmo payment from JENNY P for dinner",
        ),
        (
            '04/10/25 Venmo payment to JOHN D for "rent" Conf# mno456pqr',
            "Venmo payment to JOHN D for rent",
        ),
    ]

    for raw, expected in cases:
        got = parser.merchant_clean(raw)
        assert got == expected, f"p2p format mismatch: got={got!r}, expected={expected!r}"

    print("P2P format tests passed.")


if __name__ == "__main__":
    run()
