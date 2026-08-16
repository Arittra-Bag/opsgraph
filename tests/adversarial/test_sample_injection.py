from opsgraph.orchestration.sample import run_sample


def test_stored_prompt_injection_remains_evidence_only():
    result = run_sample("Investigate the webhook incident")
    hostile = next(item for item in result.evidence if item.id == "EV-UNTRUSTED-01")
    assert "ignore policy" in hostile.excerpt
    assert all(hostile.id not in finding.evidence_ids for finding in result.findings)
    assert all("token" not in finding.statement.lower() for finding in result.findings)
