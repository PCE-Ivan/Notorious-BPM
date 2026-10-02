"""One shared runner for every long background task.

app.py used to carry ~20 hand-copied blocks of the same shape -- a state
dict, a lock, a thread launcher, a "running"/"error" try/except/finally, and
a progress route -- none of them cancellable, and each reporting progress to
the UI in its own way. This is that shape, once. A Job owns:

  * `state` -- the exact dict shape the existing /api/*/progress routes
    already returned (running/done/total/result/error + whatever extras a
    job declares), so every existing route and front-end poller keeps
    working unchanged; app.py keeps its `_xxx_state` names as aliases of
    these same dict objects.
  * cancellation -- Job.progress() (which every worker already calls once
    per item) raises JobCancelled when the user hit Cancel, so a worker
    needs no cancel-aware rewrite; it just stops at its next progress tick.
  * the Activity tray -- every Job registers itself in REGISTRY, which
    /api/jobs lists, so a long task started from anywhere shows up (with a
    Cancel button) in one place.

JobCancelled derives from BaseException, not Exception, on purpose: most
workers wrap their per-item work in `except Exception: pass` (one bad file
shouldn't kill a whole pass), and a plain Exception subclass would be
swallowed by exactly those handlers instead of stopping the job.
"""
import contextlib
import logging
import threading
import time

log = logging.getLogger("jukebox.jobs")

REGISTRY = {}
RECENT_SECONDS = 600  # how long a finished job stays listed in the Activity tray


class JobCancelled(BaseException):
    pass


class Job:
    def __init__(self, name, label, cancellable=True, progress_extra=None, summarize=None, **extra):
        self.name = name
        self.label = label
        self.cancellable = cancellable
        self._progress_extra = progress_extra
        self._summarize = summarize
        self._extra_defaults = dict(extra)
        self.lock = threading.Lock()
        self._cancel = threading.Event()
        self.state = {}
        self._reset(running=False, started_at=None)
        REGISTRY[name] = self

    # ------------------------------------------------------------ lifecycle
    def _reset(self, running=True, started_at=None, **overrides):
        # In-place update (never rebinding or clear()ing self.state): app.py
        # and the progress routes hold references to this very dict, and a
        # concurrent jsonify() must never catch it half-empty.
        fresh = {
            "running": running, "done": 0, "total": 0, "result": None, "error": None,
            "cancelled": False, "dismissed": False,
            "started_at": started_at if started_at is not None else (time.time() if running else None),
            "finished_at": None,
        }
        fresh.update(self._extra_defaults)
        fresh.update(overrides)
        self._cancel.clear()
        self.state.update(fresh)

    def start(self, target, *args, guard=None, prepare=None, **initial):
        """Runs target(*args) on a daemon thread. Returns False (starting
        nothing) if this job is already running -- an expected outcome the
        caller reports as "Already running", not an error. `guard` is an
        optional lock held around the check-and-start (app.py passes its
        _library_lock, so a library switch and a job start can never
        interleave); `prepare` runs under it just before the thread
        starts."""
        with (guard if guard is not None else contextlib.nullcontext()):
            with self.lock:
                if self.state["running"]:
                    return False
                self._reset(**initial)
                if prepare:
                    prepare()
                threading.Thread(
                    target=self._run, args=(target,) + args, name=f"job-{self.name}", daemon=True,
                ).start()
                return True

    def _run(self, target, *args):
        try:
            target(*args)
        except JobCancelled:
            self.state["cancelled"] = True
            log.info("Job %s cancelled at %s/%s", self.name, self.state.get("done"), self.state.get("total"))
        except Exception as e:
            log.exception("Job %s failed", self.name)
            self.state["error"] = str(e) or e.__class__.__name__
        finally:
            self.state["finished_at"] = time.time()
            self.state["running"] = False

    # --------------------------------------------------------------- workers
    def check_cancel(self):
        if self._cancel.is_set():
            self.state["cancelled"] = True
            raise JobCancelled()

    def progress(self, done, total, extra=None):
        self.state["done"] = done
        self.state["total"] = total
        if extra is not None and self._progress_extra:
            self.state[self._progress_extra] = extra
        self.check_cancel()

    def set(self, **values):
        """Update arbitrary state keys (and honour a pending cancel)."""
        self.state.update(values)
        self.check_cancel()

    # ------------------------------------------------------------- control
    def cancel(self):
        if not (self.cancellable and self.state["running"]):
            return False
        self._cancel.set()
        return True

    def dismiss(self):
        if not self.state["running"]:
            self.state["dismissed"] = True

    @property
    def running(self):
        return bool(self.state["running"])

    # --------------------------------------------------------------- tray
    def is_listed(self):
        s = self.state
        if s.get("started_at") is None or s.get("dismissed"):
            return False
        if s["running"]:
            return True
        finished = s.get("finished_at")
        return bool(finished and time.time() - finished < RECENT_SECONDS)

    def snapshot(self):
        s = dict(self.state)
        total = s.get("total") or 0
        done = s.get("done") or 0
        summary = None
        if not s["running"] and not s.get("error") and not s.get("cancelled") and self._summarize:
            try:
                summary = self._summarize(s)
            except Exception:
                summary = None
        return {
            "name": self.name, "label": self.label, "running": bool(s["running"]),
            "done": done, "total": total,
            "percent": (min(100, round(done * 100 / total)) if total else None),
            "error": s.get("error"), "cancelled": bool(s.get("cancelled")),
            "can_cancel": bool(self.cancellable and s["running"]),
            "started_at": s.get("started_at"), "finished_at": s.get("finished_at"),
            "summary": summary,
        }


def any_running():
    return any(job.running for job in REGISTRY.values())


def list_jobs():
    jobs = [j for j in REGISTRY.values() if j.is_listed()]
    jobs.sort(key=lambda j: (not j.running, -(j.state.get("started_at") or 0)))
    return [j.snapshot() for j in jobs]
