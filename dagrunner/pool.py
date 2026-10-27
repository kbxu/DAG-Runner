from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future
from queue import Empty, Queue


class DaemonPool:
    """A small ThreadPoolExecutor stand-in whose worker threads are daemons.

    Interpreter shutdown joins non-daemon threads before atexit handlers run,
    so a long-running task can keep the process alive. Daemon workers avoid
    that, making Ctrl+C / process termination immediate.
    """

    def __init__(self, max_workers: int = 1, thread_name_prefix: str = ""):
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        self._max_workers = max_workers
        self._queue: Queue = Queue()
        self._threads: list[threading.Thread] = []
        self._shutdown = False
        self._lock = threading.Lock()
        for index in range(max_workers):
            thread = threading.Thread(
                target=self._worker,
                name=(
                    f"{thread_name_prefix}_{index}"
                    if thread_name_prefix
                    else f"daemon-pool-{index}"
                ),
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def submit(self, fn: Callable, *args, **kwargs) -> Future:
        future: Future = Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._queue.put((fn, args, kwargs, future))
        return future

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, args, kwargs, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._lock:
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        _, _, _, future = self._queue.get_nowait()
                    except Empty:
                        break
                    future.cancel()
            for _ in self._threads:
                self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join()

    def __enter__(self) -> "DaemonPool":
        return self

    def __exit__(self, *exc_info) -> None:
        self.shutdown(wait=True)
