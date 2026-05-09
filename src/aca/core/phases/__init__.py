"""phases/__init__.py -- ACA coding-run phase modules.

Each sub-module owns one well-defined stage of the coding-run lifecycle.
They are imported by runner_core and by tests; they do not import each other.

Modules
-------
context         -- RunContext dataclass carrying all per-run shared state
engine_check    -- Engine health check and repository binding validation
task_intake     -- Task normalization, branch setup, and coordination claim
github_sync     -- GitHub MCP connect/disconnect/claim/finalize helpers
planning        -- Manager prompt, subtask decomposition, pre-satisfaction check
worker_dispatch -- Local worker pool execution and result collection
review_verify   -- Review agent, test agent, verification policy evaluation
repair          -- No-diff / no-proof / should-retry decision helpers
finalize        -- Commit, push, PR creation, final summary and status writes
"""
from src.aca.core.phases.context import RunContext  # noqa: F401
from src.aca.core.phases.engine_check import check_engine_at_startup, check_engine_health  # noqa: F401
from src.aca.core.phases.task_intake import run_task_intake  # noqa: F401
from src.aca.core.phases.github_sync import (  # noqa: F401
    connect_for_intake,
    disconnect_for_coding,
    sync_claim_status,
    finalize_sync,
)
from src.aca.core.phases.planning import run_manager_prompt, pre_screen_subtasks  # noqa: F401
from src.aca.core.phases.worker_dispatch import dispatch_workers  # noqa: F401
from src.aca.core.phases.review_verify import run_review_and_test  # noqa: F401
from src.aca.core.phases.repair import (  # noqa: F401
    RepairDecision,
    check_no_diff,
    check_no_verifiable_proof,
    build_retry_feedback,
)
from src.aca.core.phases.finalize import finalize_completed_run  # noqa: F401
