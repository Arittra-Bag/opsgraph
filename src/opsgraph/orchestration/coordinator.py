"""One local worker, durable events, explicit retry after process interruption."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from contextlib import suppress

from opsgraph.brokers import (
    ConnectorUnavailable,
    EvidenceTooLargeError,
    QueryExecutionFailed,
    UnsafeDatabaseRole,
    UnsafeQuery,
)
from opsgraph.brokers.query import UnsupportedEvidenceTypeError
from opsgraph.orchestration.connected import (
    ClarificationRequired,
    MappingRequiredError,
    ModelAnswerInconsistentError,
    ModelCitationInvalidError,
    ModelOutputInvalidError,
    ModelPlanInconsistentError,
    PlanningContextTooLargeError,
)
from opsgraph.persistence.runs import RunStore, timestamp
from opsgraph.providers import ProviderError, ProviderOutputTruncatedError, ProviderTimeoutError


class RunCancelled(Exception):
    pass


class RunBlocked(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class RunCoordinator:
    def __init__(
        self,
        store: RunStore,
        execute: Callable,
        on_complete: Callable | None = None,
        *,
        workspace_id: str,
        on_terminal: Callable[[str, str, str, str | None], None] | None = None,
    ):
        self.store = store
        self.workspace_id = workspace_id
        self.execute = execute
        self.on_complete = on_complete
        self.on_terminal = on_terminal
        self._guard = None
        self._thread = None
        self._mutex = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._active_cancel = None
        self._failure = None

    def health(self) -> dict[str, str]:
        """Report process-local worker and ownership state without external I/O."""

        with self._mutex:
            thread = self._thread
            guard = self._guard
            failure = self._failure
            stopping = self._stop.is_set()
            try:
                guard_owned = guard is not None and guard.in_transaction
            except sqlite3.Error:
                guard_owned = False
            healthy = (
                thread is not None
                and thread.is_alive()
                and guard_owned
                and failure is None
                and not stopping
            )
        return {
            "status": "ready" if healthy else "unavailable",
            "detail": (
                "Investigation worker and state ownership are active."
                if healthy
                else "Investigation worker or state ownership is unavailable."
            ),
        }

    def _notify_terminal(
        self, workspace: str, run_id: str, status: str, code: str | None = None
    ) -> bool:
        try:
            if self.on_terminal:
                self.on_terminal(workspace, run_id, status, code)
            self.store.mark_terminal_audited(workspace, run_id)
        except Exception:
            with self._mutex:
                self._failure = (
                    "Investigation worker stopped after an audit error. "
                    "Restart OpsGraph after restoring local audit storage."
                )
                self._stop.set()
            return False
        return True

    def notify_terminal(
        self, workspace: str, run_id: str, status: str, code: str | None = None
    ) -> None:
        """Record a terminal transition created outside the worker loop."""

        if not self._notify_terminal(workspace, run_id, status, code):
            raise RuntimeError(self._failure) from None

    def _stop_after_storage_error(self) -> None:
        with self._mutex:
            self._failure = (
                "Investigation worker stopped after a storage error. "
                "Restart OpsGraph after restoring local state storage."
            )
            self._stop.set()

    def start(self):
        with self._mutex:
            if self._failure:
                raise RuntimeError(self._failure)
            if self._thread is not None:
                if self._failure or not self._thread.is_alive():
                    raise RuntimeError(
                        self._failure
                        or "Investigation worker stopped. Restart OpsGraph to recover queued work."
                    )
                return
            # A separate SQLite database holds a process lifetime OS-backed lock.
            # A crash releases it immediately; no stale lease or PID reuse ambiguity.
            guard = sqlite3.connect(
                str(self.store.path) + ".coordinator", timeout=0, check_same_thread=False
            )
            try:
                guard.execute("CREATE TABLE IF NOT EXISTS coordinator (id INTEGER)")
                guard.commit()
                guard.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError:
                guard.close()
                raise RuntimeError(
                    "Another OpsGraph coordinator owns this state database."
                ) from None
            self._guard = guard
            self._stop.clear()
            self._failure = None
            self.store.recover(self.workspace_id)
            for run in self.store.pending_terminal_audits(self.workspace_id):
                error = run.get("error") or {}
                if not self._notify_terminal(
                    self.workspace_id,
                    run["id"],
                    run["status"],
                    error.get("code"),
                ):
                    guard.rollback()
                    guard.close()
                    self._guard = None
                    raise RuntimeError(self._failure) from None
            self._thread = threading.Thread(target=self._work, name="opsgraph-worker", daemon=True)
            self._thread.start()

    def notify(self):
        self._wake.set()

    def bind_cancellation(self, workspace, run_id, callback):
        with self._mutex:
            self._active_cancel = (workspace, run_id, callback)

    def request_cancel(self, workspace, run_id):
        with self._mutex:
            active = self._active_cancel
            if active and active[:2] == (workspace, run_id):
                with suppress(Exception):
                    active[2]()

    def close(self):
        self._stop.set()
        self._wake.set()
        with self._mutex:
            if self._active_cancel:
                with suppress(Exception):
                    self._active_cancel[2]()
        if self._thread:
            self._thread.join(timeout=130)
        if self._thread and self._thread.is_alive():
            return  # Keep ownership until process termination; never run a second worker.
        if self._guard:
            self._guard.rollback()
            self._guard.close()
        self._thread = None
        self._guard = None

    def _work(self):
        while not self._stop.is_set():
            try:
                claimed = self.store.claim(self.workspace_id)
            except Exception:
                self._failure = (
                    "Investigation worker stopped after a storage error. "
                    "Restart OpsGraph to recover queued work."
                )
                return  # Retain process ownership; never accept more work silently.
            if claimed is None:
                self._wake.wait(0.2)
                self._wake.clear()
                continue
            workspace, run = claimed
            run_id = run["id"]
            terminal_notified = False

            def check(workspace=workspace, run_id=run_id):
                if self._stop.is_set():
                    raise RunCancelled()
                if self.store.get(workspace, run_id)["status"] in {"cancelling", "cancelled"}:
                    raise RunCancelled()

            def observe(kind, data, workspace=workspace, run_id=run_id, check=check):
                check()
                changes = {}
                if kind == "plan_completed":
                    changes["plan"] = data["plan"]
                    changes["planning_reported_model"] = data.get("planning_reported_model")
                if kind == "configured":
                    changes["configuration"] = data["configuration"]
                self.store.update(workspace, run_id, kind, data, **changes)

            try:
                check()
                result = self.execute(workspace, run, observe, check, self)
                check()
                completed = self.store.update(
                    workspace,
                    run_id,
                    "completed",
                    status="completed",
                    answer=result["answer"],
                    answer_reported_model=result.get("answer_reported_model"),
                    plan=result["plan"],
                    finished_at=timestamp(),
                )
                if completed["status"] == "cancelling":
                    raise RunCancelled()
                terminal_notified = self._notify_terminal(workspace, run_id, "completed")
                if not terminal_notified:
                    continue
                if self.on_complete:
                    try:
                        self.on_complete(workspace, completed)
                    except Exception:
                        self._stop_after_storage_error()
                        continue
            except Exception as exc:
                try:
                    current = self.store.get(workspace, run_id)
                except Exception:
                    # Keep the durable running state untouched. Startup recovery will
                    # convert it to an interrupted terminal record once storage works.
                    self._stop_after_storage_error()
                    continue
                if self._stop.is_set():
                    # Recovery will accurately mark this run interrupted at next startup.
                    continue
                cancelled = isinstance(exc, RunCancelled) or current["status"] == "cancelling"
                status = (
                    "cancelled"
                    if cancelled
                    else (
                        "blocked"
                        if isinstance(
                            exc,
                            (
                                RunBlocked,
                                ClarificationRequired,
                                MappingRequiredError,
                                PlanningContextTooLargeError,
                            ),
                        )
                        else "failed"
                    )
                )
                message = (
                    "Investigation cancelled."
                    if cancelled
                    else (
                        str(exc)
                        if isinstance(exc, RunBlocked)
                        else (
                            "Investigation could not finish. "
                            "Check source/model configuration and retry. "
                            "Captured evidence is preserved."
                        )
                    )
                )
                code = status
                if not cancelled and not isinstance(exc, RunBlocked):
                    if isinstance(exc, ClarificationRequired):
                        code = "clarification_required"
                        message = "Clarification needed before querying: " + str(exc)
                    elif isinstance(exc, MappingRequiredError):
                        code = "mapping_required"
                        message = str(exc)
                    elif isinstance(exc, PlanningContextTooLargeError):
                        code = "planning_context_too_large"
                        message = str(exc)
                    elif isinstance(exc, UnsupportedEvidenceTypeError):
                        code = "evidence_type_unsupported"
                        message = str(exc)
                    elif isinstance(exc, ModelAnswerInconsistentError):
                        code = "model_answer_inconsistent"
                        message = (
                            "Model answer conflicted with captured collection facts or omitted an "
                            "explicit response requirement after one bounded correction attempt. "
                            "Review preserved captures and retry with a model that follows the "
                            "answer contract; no inconsistent answer was accepted."
                        )
                    elif isinstance(exc, ModelCitationInvalidError):
                        code = "model_citation_invalid"
                        message = (
                            "Model returned missing or unknown evidence citations. "
                            "Review preserved captures and retry with a model that follows "
                            "the citation contract; no unvalidated answer was accepted."
                        )
                    elif isinstance(exc, ModelPlanInconsistentError):
                        code = "model_plan_inconsistent"
                        message = (
                            "Model SQL still contradicts an explicit parent/count "
                            "requirement in the "
                            "question after one correction attempt. No query was executed. "
                            "Retry with a simpler bounded question; "
                            "source permissions are unchanged."
                        )
                    elif isinstance(exc, ModelOutputInvalidError):
                        code = "model_output_invalid"
                        message = (
                            "Model response failed required structure, classification "
                            "or size validation. Review structured-output support and retry; "
                            "no unvalidated answer was accepted."
                        )
                    elif isinstance(exc, ProviderTimeoutError):
                        code = "model_timeout"
                        message = (
                            "Model call exceeded its configured timeout. Warm or choose a smaller "
                            "local model, review reasoning settings, "
                            "or increase the bounded timeout."
                        )
                    elif isinstance(exc, ProviderOutputTruncatedError):
                        code = "model_output_truncated"
                        message = (
                            "Model reached its output-token limit before finishing. "
                            "Narrow the question or choose a model "
                            "suited to concise structured answers."
                        )
                    elif isinstance(exc, ProviderError):
                        code = "model_failed"
                        message = (
                            "Model call failed. Check availability, structured-output "
                            "support and timeout."
                        )
                    elif isinstance(exc, UnsafeDatabaseRole):
                        code = "source_role_rejected"
                        message = (
                            "Database role is not safely read-only. "
                            "Review its grants and inspect again."
                        )
                    elif isinstance(exc, QueryExecutionFailed):
                        code = "query_failed"
                        message = (
                            "PostgreSQL could not execute the proposed query. "
                            "Inspect the recorded query's joins, columns, grouping and value "
                            "types. Clarify the question or source definitions before a fresh "
                            "retry. Earlier captures are preserved."
                        )
                    elif isinstance(exc, ConnectorUnavailable):
                        code = "source_unavailable"
                        message = (
                            "Database read failed. Check connectivity, role permissions "
                            "and statement timeout."
                        )
                    elif isinstance(exc, EvidenceTooLargeError):
                        code = "evidence_too_large"
                        message = (
                            "Evidence exceeds a bounded payload limit. "
                            "Select fewer columns or rows, shorten large values explicitly, "
                            "or narrow the question. Earlier captures are preserved."
                        )
                    elif isinstance(exc, UnsafeQuery):
                        code = "query_rejected"
                        message = (
                            "Proposed query was rejected by read-only policy. "
                            "Narrow the question or review allowed tables."
                        )
                terminal = self.store.update(
                    workspace,
                    run_id,
                    status,
                    status=status,
                    finished_at=timestamp(),
                    error={
                        "code": code,
                        "message": message,
                        "http_status": exc.status_code
                        if isinstance(exc, RunBlocked)
                        else (403 if isinstance(exc, PermissionError) else 422),
                    },
                )
                if not terminal_notified:
                    terminal_notified = self._notify_terminal(
                        workspace,
                        run_id,
                        terminal["status"],
                        terminal.get("error", {}).get("code") if terminal.get("error") else None,
                    )
            finally:
                with self._mutex:
                    if self._active_cancel and self._active_cancel[:2] == (workspace, run_id):
                        self._active_cancel = None
