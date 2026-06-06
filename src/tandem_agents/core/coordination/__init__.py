"""coordination/__init__.py -- Coordination store sub-modules.

Sub-modules
-----------
schema  -- SQL DDL constants and schema application helpers
rows    -- Row-to-dict serialization functions for all coordination tables

The full ``CoordinationStore`` class lives in the parent
``src.tandem_agents.core.coordination`` module, which imports from these sub-modules.
All external callers continue to use::

    from src.tandem_agents.core.coordination.coordination import CoordinationStore

No import changes are needed across the rest of the codebase.
"""
from src.tandem_agents.core.coordination.schema import (  # noqa: F401
    SQLITE_SCHEMA,
    POSTGRES_SCHEMA,
    apply_sqlite_schema,
    apply_postgres_schema,
)
from src.tandem_agents.core.coordination.rows import (  # noqa: F401
    row_to_task,
    row_to_run,
    row_to_worker,
    row_to_lease,
    row_to_outbox,
    row_to_external_action_approval,
    row_to_scheduler_event,
)
from src.tandem_agents.core.coordination.constants import (
    DEFAULT_LEASE_TTL_SECONDS,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_OUTBOX_STALE_AFTER_SECONDS,
    DEFAULT_WORKER_STALE_AFTER_SECONDS,
    TASK_STATES,
    COORDINATION_BACKENDS,
)
from src.tandem_agents.core.coordination.tasks import CoordinationTasksMixin
from src.tandem_agents.core.coordination.runners import CoordinationRunnersMixin
from src.tandem_agents.core.coordination.workers import CoordinationWorkersMixin
from src.tandem_agents.core.coordination.leases import CoordinationLeasesMixin
from src.tandem_agents.core.coordination.outbox import CoordinationOutboxMixin
from src.tandem_agents.core.coordination.approvals import CoordinationApprovalsMixin
from src.tandem_agents.core.coordination.scheduler import CoordinationSchedulerMixin
from src.tandem_agents.core.coordination.snapshot import CoordinationSnapshotMixin
