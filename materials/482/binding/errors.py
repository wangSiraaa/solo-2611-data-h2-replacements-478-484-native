"""Distinct error types for the two independent entry points.

BindingError is raised offline (no DB connection) and always carries a
diagnostic path into the typed request. PlanError is raised by the executor
when a serialized BoundPlan is structurally invalid or does not match the
live database. ExecutionError wraps a per-source-row write failure.
"""


class BindingError(ValueError):
    """A typed request cannot be bound; the message names a concrete path."""


class PlanError(ValueError):
    """A serialized plan is corrupt, unknown, or does not match the catalog."""


class ExecutionError(RuntimeError):
    """A source row could not be written; the whole batch is rolled back."""

    def __init__(self, source_no, message, cause=None):
        self.source_no = source_no
        self.cause = cause
        full = "source_no=%d: %s" % (source_no, message)
        if cause is not None:
            full += " (%s: %s)" % (type(cause).__name__, cause)
        super().__init__(full)
