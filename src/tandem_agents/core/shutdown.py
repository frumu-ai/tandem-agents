"""shutdown.py -- Graceful shutdown interception for ACA.

Captures SIGINT / SIGTERM securely, allowing the engine loop and worker
pools to safely abort polling loops and drop leases instead of unceremonious
hard kills that leave state dangling.
"""
from __future__ import annotations

import logging
import signal
import threading
from typing import Any

logger = logging.getLogger("aca.core.shutdown")

class ShutdownHandler:
    """Handles SIGINT/SIGTERM gracefully using a threading.Event."""
    
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self._original_sigint: Any = None
        self._original_sigterm: Any = None
        
    def hook(self) -> None:
        """Register signal handlers."""
        if threading.current_thread() is not threading.main_thread():
            return
            
        def signal_handler(signum: int, frame: Any) -> None:
            logger.info("Shutdown signal %d received. Initiating graceful shutdown...", signum)
            self.stop_event.set()
            
        self._original_sigint = signal.signal(signal.SIGINT, signal_handler)
        self._original_sigterm = signal.signal(signal.SIGTERM, signal_handler)
        
    def unhook(self) -> None:
        """Restore original signal handlers."""
        if threading.current_thread() is not threading.main_thread():
            return
        if self._original_sigint is not None:
            signal.signal(signal.SIGINT, self._original_sigint)
        if self._original_sigterm is not None:
            signal.signal(signal.SIGTERM, self._original_sigterm)
            
    def is_shutting_down(self) -> bool:
        """Return True if a shutdown has been requested."""
        return self.stop_event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the shutdown event. Returns True if shutdown was requested."""
        return self.stop_event.wait(timeout=timeout)
