"""Application-level errors raised by read and workflow use cases."""


class ReadError(Exception):
    """Base class for expected plan read failures."""


class InvalidRequest(ReadError):
    """The caller supplied an invalid identifier, date range, or token."""


class PlanNotFound(ReadError):
    """No current plan exists for the supplied plan identifier."""


class SnapshotChanged(ReadError):
    """The supplied shopping identity does not match the current snapshot."""


class NoFeasiblePlan(Exception):
    """The plan generator found no plan satisfying the weekly rules."""
