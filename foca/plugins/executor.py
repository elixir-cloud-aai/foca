"""Bounded thread pool that runs plugin hooks with deadlines."""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from foca.plugins.outcomes import _DeadlineExceededError


class _Executor:
    """Bounded thread pool for hook calls, abandoning calls at deadlines."""

    def __init__(self, workers: int) -> None:
        """Create the pool.

        Args:
            workers: Number of threads.
        """
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="foca-plugin"
        )
        self._slots = threading.BoundedSemaphore(workers)

    def call(self, function: Callable[[], Any], timeout: float) -> Any:
        """Call a function in the pool.

        Args:
            function: Function.
            timeout: Seconds to wait for the result.

        Returns:
            Result of the function.

        Raises:
            _DeadlineExceededError: The timeout passed, or no thread is
                free; the call is abandoned.
        """
        if timeout <= 0:
            raise _DeadlineExceededError("deadline passed before the call")
        if not self._slots.acquire(blocking=False):
            raise _DeadlineExceededError("all plugin workers are busy")
        try:
            future = self._pool.submit(function)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            if future.done():
                raise
            raise _DeadlineExceededError(f"no result after {timeout:.3f} s") from None

    def shutdown(self) -> None:
        """Stop the pool, without waiting for running calls."""
        self._pool.shutdown(wait=False, cancel_futures=True)
