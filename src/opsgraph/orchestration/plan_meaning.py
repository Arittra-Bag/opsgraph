"""Narrow checks for a planner's explicit admission of an unsupported unit assumption.

This is not semantic inference: uncertain wording deliberately produces no match.
The operator must declare missing definitions, and the planner must both admit
missing units and positively assume the requested unit before this check acts.
"""

import re

_QUOTED = re.compile(r'"[^"\n]*"|`[^`\n]*`|(?<!\w)\'[^\'\n]*\'(?!\w)')
_MISSING_UNIT = re.compile(
    r"\b(?:does not|doesn't|did not|has not)\s+(?:provide|specify|define|supply)"
    r"\b[^.!?;\n]{0,100}\bunits?\b"
    r"|\b(?:source |stored |measurement )?units?(?: definitions?)?\s+"
    r"(?:is |are |remain )?"
    r"(?:unknown|unavailable|unspecified|undefined|not provided|not specified)\b"
    r"|\bno\s+(?:source |stored |measurement )?unit(?: definition)?\s+"
    r"(?:is |was )?(?:provided|specified|available)\b"
)
_MISSING_DEFINITIONS = re.compile(
    r"\ball (?:other )?definitions\s+(?:are |remain )?(?:unavailable|unknown|unspecified)\b"
)
_ASSUMED_REQUESTED_UNIT = re.compile(
    r"\b(?:we|i)\s+(?:(?:must|will|can|need to|have to)\s+)?(?:assume|treat)\b"
    r"[^.!?;\n]{0,240}\b(?:requested|target)\s+units?\b"
)
_UNDEFINED_REQUESTED_UNIT = re.compile(
    r"\b(?:requested|target)\s+unit\s*\((?P<unit>[^()\n]{1,40})\)\s+"
    r"(?:is|was)\s+not\s+(?:explicitly\s+)?(?:defined|specified|documented)\s+"
    r"(?:in|by)\s+(?:the\s+)?schema\b"
)
_EXPLICIT_UNIT = re.compile(
    r"\b(?:is|are)\s+(?:stored|measured|recorded|expressed)\s+in\b"
    r"|\b(?:source|stored|measurement|column)\s+units?\s*(?:is|are|=|:)\s*"
    r"(?!(?:unknown|unavailable|unspecified|undefined|not)\b)(?:\w+|[\"'`])"
    r"|\b(?:assume|treat)\b[^.!?;\n]{0,100}\b(?:units?|stored|measured)\b"
)


def _unquoted(value: str) -> str:
    return re.sub(r"\s+", " ", _QUOTED.sub(" ", value)).lower()


def missing_unit_clarification(question: str, rationale: str) -> str | None:
    """Ask for a missing unit only for the explicit contradictory admission above."""
    # A quoted literal can be a legitimate unit definition, not an instruction.
    if _EXPLICIT_UNIT.search(question.lower()):
        return None
    question = _unquoted(question)
    rationale = _unquoted(rationale)
    if _EXPLICIT_UNIT.search(question):
        return None
    if not (_MISSING_UNIT.search(question) or _MISSING_DEFINITIONS.search(question)):
        return None
    missing = _MISSING_UNIT.search(rationale)
    assumed = _ASSUMED_REQUESTED_UNIT.search(rationale)
    missing = missing or _UNDEFINED_REQUESTED_UNIT.search(rationale)
    if missing and not assumed:
        # Bind literal-unit assumptions to quantities actually in the question.
        # The preceding missing-unit admission remains required: a number/unit
        # alone is never treated as evidence of an undefined source meaning.
        requested = re.findall(
            r"(?:\b(?:greater than|less than|above|below|exceeds?|over|under)|[<>]=?)"
            r"\s+\d+(?:\.\d+)?\s+([a-zµ°][\wµ°/^.-]*)",
            question,
        )
        if missing.re is _UNDEFINED_REQUESTED_UNIT:
            requested.append(missing.group("unit").strip())
        for unit in requested:
            assumed = re.search(
                r"\b(?:we|i)\s+(?:(?:must|will|can|need to|have to)\s+)?assume\b"
                r"[^.!?;\n]{0,160}\b\w+\s+(?:column\s+)?"
                r"(?:is|are)\s+(?:already\s+)?(?:stored\s+|measured\s+)?in\s+"
                + re.escape(unit)
                + r"(?=[\s,.;!?)]|$)",
                rationale,
            )
            if assumed:
                break
    if not missing or not assumed or missing.end() > assumed.start():
        return None
    preceding_clause = re.split(r"[.!?;]", rationale[: missing.start()])[-1]
    if re.search(
        r"\b(?:not|never)\s+(?:true|correct|the case)\s+that\b"
        r"|\b(?:false|incorrect)\s+(?:that|to say)\b"
        r"|\b(?:deny|denies|denied)\s+(?:that\s+)?$",
        preceding_clause,
    ):
        return None
    if re.search(r"\b(?:not|no|never|cannot|can't|without)\b", assumed.group()):
        return None
    return (
        "What unit are the source values stored in, and what conversion, if any, "
        "should be used for the requested unit?"
    )
