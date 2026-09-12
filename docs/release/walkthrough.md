# Investigation walkthrough

This guide describes the product flow; it is not a recorded demonstration or
performance benchmark. Follow the [quick start](../quickstart.md) using an
authorized read-only PostgreSQL source and explicitly selected tables.

1. Launch OpsGraph and connect the browser to the workspace.
2. In Sources, save the explicit schema/table scope and inspect the discovered
   columns. Supply needed business meanings and relationships in the question;
   schema names alone do not establish units or status semantics.
3. In Settings, run the actual structured model probe. Resolve configuration
   errors before starting an investigation.
4. Ask a bounded question. Progress reflects backend events; a connection
   heartbeat does not mean a query completed.
5. Open a finding and its cited capture to inspect source identity, timestamp,
   SQL, records and collection limits. Check the interpretation against the rows.
6. Export only records you are authorized to share. Exports may contain sensitive
   data even though credentials are excluded.
7. Use history to reopen original captures. Follow-up and fresh retry create
   linked attempts with newly collected evidence. Cancellation waits for an
   active operation to exit; interrupted work is not silently replayed.

A hash identifies captured bytes, not the truth of an explanation. If business
meaning is unavailable, provide clarification rather than interpreting a raw
number as an assumed unit. See the [support matrix](support-matrix.md) for tested
and unverified configurations.
