"""Bounded product conversation that cannot invoke database or model tools."""

from __future__ import annotations


def capability_reply(question: str) -> str | None:
    """Answer explicit social and product intents without interpreting source data."""
    normalized = question.strip().lower().rstrip("?.!").strip()
    if normalized in {"hi", "hello", "hey"}:
        return (
            "Hello. I can help investigate your approved PostgreSQL tables. "
            "Describe what looks wrong and the time range you want to check."
        )
    if normalized in {"thanks", "thank you"}:
        return "You're welcome. You can ask another question in this conversation."
    if normalized in {"what are you", "who are you"}:
        return (
            "I'm OpsGraph, a workspace for investigating PostgreSQL application data. "
            "I can read only approved tables, keep the query results, and explain findings "
            "with references you can inspect. I cannot change your database."
        )
    if normalized in {
        "what can you do",
        "so what can you do",
        "what can you help with",
        "how does opsgraph work",
    }:
        return (
            "I can investigate missing events, duplicate records, stuck jobs and unexpected "
            "counts in your approved PostgreSQL tables. Ask a specific question, include "
            "the relevant time range, and explain any business terms. I may ask for missing "
            "details before reading data. Each answer keeps its queries, captured records "
            "and limitations. Follow-ups stay in this conversation. I cannot write data, "
            "run commands, or inspect tables outside your approved scope."
        )
    return None
