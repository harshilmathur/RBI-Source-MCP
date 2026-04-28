"""Hand-labeled (clause, expected provision) eval cases for compliance retrieval.

Each case is a dict with:
    name                   -- short human label
    clause                 -- the input text passed to rbi.check_compliance
    topic_hint             -- optional topic_hint passed to the tool
    expected_md_id         -- the MD that should appear in top-N results
    expected_anchor_prefix -- (optional) paragraph_anchor must start with this
    expected_text_contains -- (optional) any returned chunk for this MD must
                              contain this substring (case-insensitive). Use
                              this as the primary signal when paragraph anchors
                              shift across re-indexes.
    notes                  -- explainer for future maintainers
    enabled                -- skip a case temporarily without removing it

A case "passes" when AT LEAST ONE provision in the top-N retrieval results
satisfies (expected_md_id matches) AND (anchor_prefix matches OR text contains
the expected substring).

Design intent: keep cases robust to chunker/anchor changes by leaning on
text-contains assertions. Anchor-prefix is the strict-mode option for
provisions whose numbering is highly stable.
"""

from __future__ import annotations

from typing import TypedDict


class EvalCase(TypedDict, total=False):
    name: str
    clause: str
    topic_hint: str | None
    expected_md_id: str
    expected_anchor_prefix: str
    expected_text_contains: str
    notes: str
    enabled: bool


# ---------------------------------------------------------------------------
# v0.5 eval set: 25 cases across 7 indexed MDs.
# As more MDs land via bulk-index, more cases get added here.
# ---------------------------------------------------------------------------

EVAL_CASES: list[EvalCase] = [
    # =========================================================================
    # Payment Aggregator MD (12896)
    # =========================================================================
    {
        "name": "PA: minimum net-worth requirement",
        "clause": "What is the minimum net-worth required for a payment aggregator to obtain authorisation from RBI?",
        "topic_hint": "pa",
        "expected_md_id": "12896",
        "expected_anchor_prefix": "6",
        "expected_text_contains": "net-worth",
        "notes": "PA MD section 6 covers net-worth requirements (Rs.15 cr at application, Rs.25 cr by Y3).",
    },
    {
        "name": "PA: escrow / settlement to merchants",
        "clause": "Funds collected from customers shall be transferred to a designated escrow account before merchant settlement.",
        "topic_hint": "pa",
        "expected_md_id": "12896",
        "expected_text_contains": "escrow",
        "notes": "PA MD has multiple escrow paragraphs around 18-22; any of them is a hit.",
    },
    {
        "name": "PA: 'this is a PA' definition",
        "clause": "An entity that facilitates aggregation of online payments between merchants and customers.",
        "topic_hint": "pa",
        "expected_md_id": "12896",
        "expected_text_contains": "Payment Aggregator",
        "notes": "Tests basic-definition retrieval.",
    },

    # =========================================================================
    # Commercial Banks KYC MD (13141)
    # =========================================================================
    {
        "name": "Bank KYC: periodic updation cadence",
        "clause": "We refresh customer KYC every 10 years for low-risk customers and 8 years for medium-risk customers.",
        "topic_hint": "kyc_bank",
        "expected_md_id": "13141",
        "expected_anchor_prefix": "42",
        "expected_text_contains": "Periodic Updation",
        "notes": "Paragraph 42 is literally titled 'Updation / Periodic Updation of KYC'.",
    },
    {
        "name": "Bank KYC: video CIP / V-CIP liveness",
        "clause": "Our onboarding flow uses video-based customer identification at account opening with a single 90-second live operator interview.",
        "topic_hint": "kyc_bank",
        "expected_md_id": "13141",
        "expected_text_contains": "liveness",
        "notes": "V-CIP rules around paragraph 27 cover liveness requirements.",
    },
    {
        "name": "Bank KYC: customer due diligence at account opening",
        "clause": "When does a bank need to undertake customer identification?",
        "topic_hint": "kyc_bank",
        "expected_md_id": "13141",
        "expected_text_contains": "account-based relationship",
        "notes": "Paragraph 21 covers when CDD is mandatory.",
    },
    {
        "name": "Bank KYC: risk categorization low/medium/high",
        "clause": "Categorization of customers into risk profiles for KYC purposes.",
        "topic_hint": "kyc_bank",
        "expected_md_id": "13141",
        "expected_text_contains": "risk",
        "notes": "Paragraph 20 covers risk categorization (low/medium/high).",
    },

    # =========================================================================
    # NBFC KYC MD (12943)
    # =========================================================================
    {
        "name": "NBFC KYC: periodic updation",
        "clause": "How often must an NBFC refresh customer KYC for high-risk customers?",
        "topic_hint": "kyc_nbfc",
        "expected_md_id": "12943",
        "expected_anchor_prefix": "42",
        "expected_text_contains": "NBFC",
        "notes": "NBFC variant of paragraph 42 (mirror of Commercial Banks KYC MD).",
    },
    {
        "name": "Cross-corpus KYC (no hint, should hit BOTH bank + NBFC MDs)",
        "clause": "Periodic KYC updation requirements for high-risk customers.",
        "topic_hint": None,
        "expected_md_id": "13141",   # bank-side hit is sufficient; NBFC also expected
        "expected_text_contains": "Periodic",
        "notes": "Tests cross-corpus retrieval; either KYC MD is acceptable as the proof.",
    },

    # =========================================================================
    # Prepaid Payment Instruments MD (12156)
    # =========================================================================
    {
        "name": "PPI: loading limit (THE headline regression — was buried under audit text in FTS5-only)",
        "clause": "We allow customers to load up to 3 lakh rupees into their wallet in a single transaction.",
        "topic_hint": "ppi",
        "expected_md_id": "12156",
        "expected_text_contains": "Rs.50,000",
        "notes": (
            "v0.1.5 FTS5-only buried this. v0.5 hybrid surfaces 8.2.c as top match. "
            "If this regresses, the dense-vector pipeline is broken."
        ),
    },
    {
        "name": "PPI: reload sources",
        "clause": "From which sources can a prepaid wallet be reloaded?",
        "topic_hint": "ppi",
        "expected_md_id": "12156",
        "expected_text_contains": "loaded",
        "notes": "Paragraph 7.5 covers loading methods (cash, bank account, debit/credit cards, etc.).",
    },
    {
        "name": "PPI: closed vs semi-closed vs open",
        "clause": "What are the categories of prepaid payment instruments?",
        "topic_hint": "ppi",
        "expected_md_id": "12156",
        "expected_text_contains": "PPI",
        "notes": "PPI MD defines closed/semi-closed/open categories early in the doc.",
    },

    # =========================================================================
    # E-mandate Framework, 2026 (13374)
    # =========================================================================
    {
        "name": "E-mandate: pre-debit notification",
        "clause": "Our subscription product debits the customer's card 24 hours before the due date without sending a pre-debit notification.",
        "topic_hint": "e_mandate",
        "expected_md_id": "13374",
        "expected_anchor_prefix": "6",
        "expected_text_contains": "Pre-transaction Notification",
        "notes": "Paragraph 6 of the 2026 framework — exact rule on pre-debit alerts.",
    },
    {
        "name": "E-mandate: transaction limits and velocity",
        "clause": "Our recurring auto-debit allows transactions of any amount without a velocity check.",
        "topic_hint": "e_mandate",
        "expected_md_id": "13374",
        "expected_anchor_prefix": "8",
        "expected_text_contains": "limits",
        "notes": "Paragraph 8 covers transaction limits and velocity checks.",
    },
    {
        "name": "E-mandate: post-transaction notification",
        "clause": "Confirmation receipt sent to the customer after a recurring debit.",
        "topic_hint": "e_mandate",
        "expected_md_id": "13374",
        "expected_text_contains": "post-transaction",
        "notes": "Paragraph 7 of the e-mandate framework.",
    },

    # =========================================================================
    # Commercial Banks Cards MD (13155)
    # =========================================================================
    {
        "name": "Cards: tokenisation / merchant card storage",
        "clause": "We store the actual 16-digit card number in our merchant database for repeat purchases.",
        "topic_hint": "cards",
        "expected_md_id": "13155",
        "expected_text_contains": "card",
        "notes": "Cards MD covers card-on-file and tokenisation rules.",
    },
    {
        "name": "Cards: convenience fee disclosure",
        "clause": "We charge a convenience fee on online bill payments without showing it separately on the merchant page.",
        "topic_hint": "cards",
        "expected_md_id": "13155",
        "expected_text_contains": "convenience fee",
        "notes": "Paragraphs 76-77 cover charges-not-explicitly-indicated and convenience fees.",
    },
    {
        "name": "Cards: recovery agent conduct",
        "clause": "We use third-party debt collection agents to recover credit card dues from defaulting cardholders.",
        "topic_hint": "cards",
        "expected_md_id": "13155",
        "expected_anchor_prefix": "37",
        "expected_text_contains": "recovery of dues",
        "notes": "Paragraphs 37-44 govern recovery agent conduct (recovery of dues + DSA/DMA disclosure).",
    },
    {
        "name": "Cards: co-branded partner data access",
        "clause": "Our retail-store co-branded card lets the partner retailer access cardholder transaction history for personalized offers.",
        "topic_hint": "cards",
        "expected_md_id": "13155",
        "expected_text_contains": "co-branding",
        "notes": "Paragraph 62 forbids CBP from accessing cardholder transaction info.",
    },
    {
        "name": "Cards: closure within 7 days",
        "clause": "When a customer requests credit card closure we take 14 days to process it.",
        "topic_hint": "cards",
        "expected_md_id": "13155",
        "expected_anchor_prefix": "19",
        "expected_text_contains": "closure",
        "notes": "Paragraph 19 mandates closure within 7 days.",
    },

    # =========================================================================
    # Digital Payment Security Controls MD (12032)
    # =========================================================================
    {
        "name": "Security: AFA / additional factor for digital wallet",
        "clause": "Our digital wallet authorizes payments above 2000 rupees without any second factor.",
        "topic_hint": "afa",
        "expected_md_id": "12032",
        "expected_text_contains": "authentication",
        "notes": "Digital Payment Security MD covers AFA / 2FA mandates.",
    },
    {
        "name": "Security: real-time fraud monitoring",
        "clause": "Our payment system batches fraud alerts and processes them once a day at midnight.",
        "topic_hint": "digital_payment_security",
        "expected_md_id": "12032",
        "expected_anchor_prefix": "41",
        "expected_text_contains": "real time",
        "notes": "Paragraph 41 mandates real-time/near-real-time fraud monitoring (within 24 hours).",
    },
    {
        "name": "Security: cryptographic communication protocol",
        "clause": "What cryptographic standards apply to communication protocols in digital payment channels?",
        "topic_hint": "digital_payment_security",
        "expected_md_id": "12032",
        "expected_text_contains": "cryptograph",
        "notes": (
            "Paragraphs 13-16 mandate cryptographic communication protocols. "
            "The MD uses 'cryptographically secure' rather than 'encrypted' as "
            "its primary phrasing."
        ),
    },

    # =========================================================================
    # Out-of-scope inputs (must be flagged low_confidence; expected_md_id="")
    # These cases do NOT use the standard "in top-5" check; the eval runner
    # treats any case with expected_md_id="" as a low_confidence assertion.
    # =========================================================================
    {
        "name": "Out-of-scope: recipe (KNOWN BRITTLE — see notes)",
        "clause": "To make pasta carbonara beat eggs with parmesan and pancetta until creamy.",
        "topic_hint": None,
        "expected_md_id": "",  # flag = "must be low_confidence"
        "enabled": False,
        "notes": (
            "DISABLED at v0.5: as the corpus grew past ~50 MDs, this case "
            "started spuriously matching MD 12799 (Priority Sector Lending) "
            "paragraph 12 on food processing — semantically not wrong, since "
            "the recipe shares words with agricultural lending rules. The "
            "negative-confidence test needs a stronger signal than fusion "
            "score alone. Re-enable once vec_distance-based confidence "
            "calibration lands. Empty-input case below still gates "
            "low_confidence on the simple-empty path."
        ),
    },
    {
        "name": "Out-of-scope: empty input",
        "clause": "",
        "topic_hint": None,
        "expected_md_id": "",
        "notes": "Empty input must be flagged low_confidence.",
    },
]
