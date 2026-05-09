"""coordination/__init__.py -- Coordination store sub-modules.

Sub-modules
-----------
schema  -- SQL DDL constants and schema application helpers
rows    -- Row-to-dict serialization functions for all coordination tables

The full ``CoordinationStore`` class lives in the parent
``src.aca.core.coordination`` module, which imports from these sub-modules.
All external callers continue to use::

    from src.aca.core.coordination.coordination import CoordinationStore

No import changes are needed across the rest of the codebase.
"""
from src.aca.core.coordination.schema import (  # noqa: F401
    SQLITE_SCHEMA,
    POSTGRES_SCHEMA,
    apply_sqlite_schema,
    apply_postgres_schema,
)
from src.aca.core.coordination.rows import (  # noqa: F401
    row_to_task,
    row_to_run,
    row_to_worker,
    row_to_lease,
    row_to_outbox,
    row_to_scheduler_event,
)
from src.aca.core.coordination.constants import (
    DEFAULT_LEASE_TTL_SECONDS,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_OUTBOX_STALE_AFTER_SECONDS,
    DEFAULT_WORKER_STALE_AFTER_SECONDS,
    TASK_STATES,
    COORDINATION_BACKENDS,
)
from src.aca.core.coordination.tasks import CoordinationTasksMixin
from src.aca.core.coordination.runners import CoordinationRunnersMixin
from src.aca.core.coordination.workers import CoordinationWorkersMixin
from src.aca.core.coordination.leases import CoordinationLeasesMixin
from src.aca.core.coordination.outbox import CoordinationOutboxMixin
from src.aca.core.coordination.scheduler import CoordinationSchedulerMixin
from src.aca.core.coordination.snapshot import CoordinationSnapshotMixin
