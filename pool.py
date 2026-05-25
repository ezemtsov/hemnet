"""Shared CDP worker-pool helper.

Three scripts in this repo need the same pattern: spread CDP-driven work
across N Chromium tabs, where N is controlled by `HEMNET_CDP_PORTS`. This
module is the one place that pattern lives — enrich.py / scrape_allabrf.py /
history.py call into it instead of repeating the boilerplate.

Usage:
    from pool import cdp_ports, run_parallel

    def process(cdp, task) -> Any: ...

    run_parallel(tasks, cdp_ports(), process,
                 on_result=lambda t, r: ...,
                 label="enrich")
"""
import os
import queue
import threading
import time
from typing import Callable, Iterable, Any

from cdp import CDP, find_tab


def cdp_ports() -> list[int]:
    """Read pool size from env. `HEMNET_CDP_PORTS` (comma-sep) takes
    precedence; else `HEMNET_CDP_PORT` single-port; else default 9223."""
    raw = os.environ.get("HEMNET_CDP_PORTS")
    if raw:
        return [int(p) for p in raw.split(",") if p.strip()]
    return [int(os.environ.get("HEMNET_CDP_PORT", "9223"))]


def run_parallel(
    tasks: Iterable[Any],
    ports: list[int],
    fn: Callable[[CDP, Any], Any],
    *,
    tab_substring: str = "hemnet.se",
    on_result: Callable[[Any, Any], None] | None = None,
    label: str = "task",
    log_every: int = 25,
    delay_s: float = 0.5,
    log_result: bool = False,
) -> None:
    """Run `fn(cdp, task)` over `tasks` with one worker per port.

    - Each worker locates a tab via `find_tab(tab_substring, port)` and
      reuses that one tab for every task it handles.
    - `on_result(task, result)` is called under a shared lock so it can
      mutate shared state (writing into a results list, persisting to
      disk, updating a dict) without races.
    - Errors in `fn` are caught, logged, and counted as a None result;
      one bad task can't kill the worker.
    - If a worker can't even attach to its tab (port wrong / Chromium
      down) it logs and exits; remaining workers continue to drain the
      queue, so the run completes at reduced parallelism.

    `tab_substring` defaults to `hemnet.se` to match the most common
    caller; pass `"allabrf"` (or `"://"` for anything) for cross-site work.
    """
    q: queue.Queue = queue.Queue()
    n = 0
    for t in tasks:
        q.put(t)
        n += 1
    if n == 0:
        return
    counter = {"done": 0, "n": n}
    lock = threading.Lock()

    def worker(port: int) -> None:
        try:
            cdp = CDP(find_tab(tab_substring, f"http://localhost:{port}"))
        except Exception as e:
            with lock:
                print(f"[{label} worker :{port} init err] {e}", flush=True)
            return
        while True:
            try:
                task = q.get_nowait()
            except queue.Empty:
                return
            try:
                result = fn(cdp, task)
            except Exception as e:
                with lock:
                    print(f"[{label} err] {task}: {e}", flush=True)
                result = None
            with lock:
                if on_result is not None:
                    on_result(task, result)
                counter["done"] += 1
                # log_every=0 disables built-in progress (caller does its own).
                if log_every and (counter["done"] % log_every == 0 or counter["done"] <= 3):
                    suffix = f" → {result}" if log_result else ""
                    print(f"  [{label} {counter['done']}/{counter['n']}] {task}{suffix}", flush=True)
            time.sleep(delay_s)

    threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in ports]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
