"""coordination/constants.py -- Constants for coordination layer."""
DEFAULT_LEASE_TTL_SECONDS = 300
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30
DEFAULT_OUTBOX_STALE_AFTER_SECONDS = 300
DEFAULT_WORKER_STALE_AFTER_SECONDS = 90
TASK_STATES = {"queued", "claimed", "active", "review", "blocked", "done", "stale"}
COORDINATION_BACKENDS = {"sqlite", "postgres"}
