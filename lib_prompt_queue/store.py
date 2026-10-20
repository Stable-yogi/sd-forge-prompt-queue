"""Job store for the prompt queue. Pure Python, no Forge imports, so it can be unit-tested alone.

Layout on disk (inside the extension's queue-data folder):
    jobs.json         list of job records + the paused flag
    args/<id>.pkl     the exact UI values captured when the job was queued
    thumbs/<id>_<n>.jpg
    results/<id>_<n>.png   only for images Forge itself did not save
"""

import json
import os
import pickle
import threading
import time
import uuid

WAITING = "waiting"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
FINISHED = (DONE, FAILED, CANCELLED)


def _new_id():
    return uuid.uuid4().hex[:12]


class QueueStore:
    def __init__(self, root):
        self.root = root
        self.args_dir = os.path.join(root, "args")
        self.thumbs_dir = os.path.join(root, "thumbs")
        self.results_dir = os.path.join(root, "results")
        for d in (self.root, self.args_dir, self.thumbs_dir, self.results_dir):
            os.makedirs(d, exist_ok=True)
        self.path = os.path.join(root, "jobs.json")
        self.lock = threading.RLock()
        self.jobs = []
        self.paused = False
        self.notice = ""
        # removed jobs, kept so an Undo can bring them back
        # ponytail: lives in memory only; after a restart the undo is gone and the files are swept
        self.trash = {}
        self.wake = threading.Event()
        self._load()

    # ---------- disk ----------
    def _load(self):
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.jobs = data.get("jobs", [])
                self.paused = bool(data.get("paused", False))
            except (OSError, ValueError) as e:
                broken = self.path + ".broken-%d" % int(time.time())
                os.replace(self.path, broken)
                self.notice = "The queue file could not be read (%s). It was kept as %s." % (e, os.path.basename(broken))
                self.jobs = []
        stopped = False
        for job in self.jobs:
            if job.get("status") == RUNNING:
                job["status"] = FAILED
                job["error"] = "Forge stopped while this job ran. Press Retry to run it again."
                job["finished"] = time.time()
                stopped = True
        # owner 2026-10-15: "Auto" is always on after a restart; waiting jobs carry on by themselves
        self.paused = False
        waiting = sum(1 for j in self.jobs if j.get("status") == WAITING)
        if waiting:
            self.notice = "Forge restarted. %d waiting job(s) carry on now. Untick Auto to hold them." % waiting
        self._sweep_orphans()
        self.save()

    def _sweep_orphans(self):
        keep = {j["id"] for j in self.jobs}
        for folder in (self.args_dir, self.thumbs_dir, self.results_dir):
            for name in os.listdir(folder):
                if name.split(".")[0].split("_")[0] not in keep:
                    try:
                        os.remove(os.path.join(folder, name))
                    except OSError:
                        pass  # a file still open elsewhere; the next start tries again

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"jobs": self.jobs, "paused": self.paused}, f, indent=1)
            os.replace(tmp, self.path)

    def args_path(self, job_id):
        return os.path.join(self.args_dir, job_id + ".pkl")

    def load_args(self, job_id):
        # ponytail: pickle, because the values hold PIL images and numpy arrays; we only ever read files we wrote
        with open(self.args_path(job_id), "rb") as f:
            return pickle.load(f)

    # ---------- reads ----------
    def get(self, job_id):
        with self.lock:
            for j in self.jobs:
                if j["id"] == job_id:
                    return j
        return None

    def waiting(self):
        with self.lock:
            return [j for j in self.jobs if j["status"] == WAITING]

    def snapshot(self):
        with self.lock:
            return {"paused": self.paused, "notice": self.notice, "jobs": [dict(j) for j in self.jobs]}

    def average_seconds(self, tab=None):
        with self.lock:
            times = [j["finished"] - j["started"] for j in self.jobs
                     if j["status"] == DONE and j.get("started") and j.get("finished") and (tab is None or j["tab"] == tab)]
        # median of the last 10, so one slow job (a model load) does not skew the estimate
        times = sorted(times[-10:])
        return times[len(times) // 2] if times else None

    # ---------- writes ----------
    def add(self, tab, values, signature, summary, copies=1):
        copies = max(1, min(int(copies), 100))
        added = []
        with self.lock:
            for _ in range(copies):
                job_id = _new_id()
                with open(self.args_path(job_id), "wb") as f:
                    pickle.dump(list(values), f, protocol=pickle.HIGHEST_PROTOCOL)
                job = {
                    "id": job_id, "tab": tab, "status": WAITING, "created": time.time(),
                    "started": None, "finished": None, "signature": signature,
                    "summary": summary, "error": "", "images": [], "infotext": "", "note": "",
                }
                self.jobs.append(job)
                added.append(job)
            self.save()
        self.wake.set()
        return added

    def claim_next(self):
        """Mark the first waiting job as running and return it, or None."""
        with self.lock:
            if self.paused:
                return None
            for j in self.jobs:
                if j["status"] == WAITING:
                    j["status"] = RUNNING
                    j["started"] = time.time()
                    j["cancel"] = False
                    self.save()
                    return j
        return None

    def mark_started(self, job_id):
        """The job got the GPU (it may have waited behind a Generate press): its time starts now."""
        with self.lock:
            j = self.get(job_id)
            if j is not None:
                j["started"] = time.time()

    def finish(self, job_id, status, **fields):
        with self.lock:
            j = self.get(job_id)
            if j is None:
                return None
            j.update(fields)
            j["status"] = status
            j["finished"] = time.time()
            j.pop("cancel", None)
            self.save()
            return j

    def set_paused(self, paused):
        with self.lock:
            self.paused = bool(paused)
            self.notice = ""
            self.save()
        self.wake.set()

    def move(self, job_id, to_index):
        """Move a waiting job to position to_index among the waiting jobs (0 = runs next)."""
        with self.lock:
            j = self.get(job_id)
            if j is None or j["status"] != WAITING:
                return False
            waiting = [w for w in self.jobs if w["status"] == WAITING and w is not j]
            to_index = max(0, min(int(to_index), len(waiting)))
            self.jobs.remove(j)
            if to_index < len(waiting):
                anchor = self.jobs.index(waiting[to_index])
            else:
                anchor = (self.jobs.index(waiting[-1]) + 1) if waiting else len(self.jobs)
            self.jobs.insert(anchor, j)
            self.save()
            return True

    def remove(self, job_ids):
        """Remove jobs (not the running one). Returns an undo token, or None if nothing was removed."""
        with self.lock:
            removed = []
            for job_id in job_ids:
                j = self.get(job_id)
                if j is None or j["status"] == RUNNING:
                    continue
                removed.append((self.jobs.index(j), j))
                self.jobs.remove(j)
            if not removed:
                return None
            token = _new_id()
            self.trash[token] = removed
            self.save()
            return token

    def undo(self, token):
        with self.lock:
            batch = self.trash.pop(token, None)
            if not batch:
                return 0
            for index, j in sorted(batch, key=lambda x: x[0]):
                self.jobs.insert(min(index, len(self.jobs)), j)
            self.save()
        self.wake.set()
        return len(batch)

    def requeue(self, job_id):
        """Run a finished job again: a fresh waiting copy at the end, same settings."""
        with self.lock:
            j = self.get(job_id)
            if j is None or j["status"] not in FINISHED:
                return None
            new = self.add(j["tab"], self.load_args(job_id), j["signature"], j["summary"])[0]
            return new

    def clear(self, which):
        with self.lock:
            if which == "finished":
                ids = [j["id"] for j in self.jobs if j["status"] in FINISHED]
            elif which == "waiting":
                ids = [j["id"] for j in self.jobs if j["status"] == WAITING]
            else:
                return None, 0
            return self.remove(ids), len(ids)
