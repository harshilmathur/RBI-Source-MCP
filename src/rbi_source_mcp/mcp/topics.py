"""Topic-hint to md_id lookup table shared by search and check_compliance.

Hand-curated mapping from user-visible topic strings (the `topic_hint`
argument on `rbi_search` and `rbi_check_compliance`) to the canonical
md_id for the Master Direction that owns that topic. Expands as more
MDs land.

When a topic is ambiguous (e.g. `kyc` applies to both Commercial Banks
and NBFC KYC MDs), the value is `None` so retrieval runs across the
full corpus and the LLM sorts the entities itself. Topics that map to
exactly one MD use that MD's id.

Before PR 5 this dict was duplicated verbatim across
`mcp/search.py` and `mcp/check_compliance.py`; they drifted in
practice (one carried alternate spellings the other didn't). Both now
import from this module so any future entry lands in one place.
"""

from __future__ import annotations

TOPIC_TO_MD_ID: dict[str, str | None] = {
    # Payment Aggregator MD (12896)
    "payment_aggregator": "12896",
    "pa": "12896",
    "pa_pg": "12896",
    # Commercial Banks KYC MD (13141)
    "kyc_bank": "13141",
    "kyc_commercial_bank": "13141",
    "bank_kyc": "13141",
    # NBFC KYC MD (12943)
    "kyc_nbfc": "12943",
    "nbfc_kyc": "12943",
    # PPIs / Prepaid (12156)
    "ppi": "12156",
    "prepaid": "12156",
    "prepaid_payment_instrument": "12156",
    "wallet": "12156",
    # Cards / Tokenisation (13155 — Commercial Banks)
    "cards": "13155",
    "credit_card": "13155",
    "debit_card": "13155",
    "tokenisation": "13155",
    "tokenization": "13155",
    # E-mandate / Recurring (13374)
    "e_mandate": "13374",
    "emandate": "13374",
    "recurring": "13374",
    "recurring_payment": "13374",
    # Digital Payment Security (12032)
    "digital_payment_security": "12032",
    "payment_security": "12032",
    "afa": "12032",
    "additional_factor_authentication": "12032",
    # Ambiguous topics — return None so search spans full corpus.
    "kyc": None,
}


def md_id_for_topic_hint(hint: str | None) -> str | None:
    """Look up the md_id for a topic hint. Returns None for unknown hints
    AND for explicitly-ambiguous hints (e.g. "kyc")."""
    if not hint:
        return None
    return TOPIC_TO_MD_ID.get(hint.lower())
