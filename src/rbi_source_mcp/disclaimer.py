"""Single source of truth for the legal/affiliation disclaimer.

Every MCP tool response is wrapped with this disclaimer in server.py so it
travels with the data, regardless of which tool the consuming LLM called.
The text is also referenced in README and CHANGELOG so wording stays in sync.

Two forms:
    DISCLAIMER       — full text. Returned in the response payload.
    DISCLAIMER_SHORT — one-liner. Used in tool descriptions / log lines.

The wording is deliberate:
    - Lead with "NOT LEGAL ADVICE" in caps so an LLM summarizing the
      response can't easily drop it without obviously dropping a header.
    - State what the MCP does (source-grounded retrieval) so the user
      understands what they're getting.
    - State the freshness caveat (provisions may have changed since refresh).
    - State the action (verify with a qualified compliance reviewer).
    - State affiliation status (unofficial, not RBI-affiliated).

Changing this text in production requires a CHANGELOG entry; the text is
how the project draws the line between "source layer" and "regulatory
advisor," and that line is load-bearing for the project's legal posture.
"""

DISCLAIMER = (
    "NOT LEGAL ADVICE. RBI Source MCP returns source-grounded retrieval over "
    "RBI publications (Master Directions, Circulars, Master Circulars, Press "
    "Releases, FAQs); it does not issue compliance verdicts or regulatory "
    "opinions. Provisions cited may have been amended, withdrawn, or superseded "
    "after this corpus's last refresh; check the official_url on each provision "
    "for the live RBI page. Always verify with a qualified compliance reviewer "
    "before acting on any provision returned. This is an unofficial, community-"
    "maintained open-source tool and is not affiliated with, endorsed by, or "
    "sponsored by the Reserve Bank of India."
)

DISCLAIMER_SHORT = (
    "NOT LEGAL ADVICE — source-grounded retrieval only. Verify with a qualified "
    "compliance reviewer."
)

# Instruction embedded in the response so the consuming LLM knows what to do
# with the disclaimer when summarizing for the end user. LLMs are inconsistent
# about following description-level instructions; embedding the instruction in
# the data is more reliable.
LLM_INSTRUCTION = (
    "When presenting results to the user, ALWAYS surface this disclaimer "
    "verbatim or in a paraphrase that preserves all five points: (1) not legal "
    "advice, (2) retrieval-only — no compliance verdicts, (3) provisions may "
    "have been amended/withdrawn since the last corpus refresh, (4) verify with "
    "a qualified human compliance reviewer before acting, (5) unofficial — not "
    "affiliated with the Reserve Bank of India. Never omit the disclaimer."
)
