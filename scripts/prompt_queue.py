"""Prompt Queue for Forge: queue many generations while one runs.

How it works
- "Add to queue" sits beside Generate. It sends the SAME inputs Generate sends, so every setting
  (prompt, size, seed, hires fix, every extension's options, img2img pictures) is captured as it is now.
- A worker thread runs the jobs one by one through Forge's own generate function, so a job behaves
  exactly like pressing Generate, and it waits its turn behind any Generate you press yourself.
- The panel in the page talks to the small JSON API below (GET /sd-queue/api lists it).
"""

import inspect
import json
import os
import sys
import threading
import time
import traceback

import gradio as gr
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from modules import call_queue, script_callbacks, scripts, shared

EXT_DIR = scripts.basedir()
if EXT_DIR not in sys.path:
    sys.path.insert(0, EXT_DIR)

from lib_prompt_queue.store import CANCELLED, DONE, FAILED, RUNNING, WAITING, QueueStore  # noqa: E402

TABS = ("txt2img", "img2img")
store = QueueStore(os.path.join(EXT_DIR, "queue-data"))
runners = {}  # tab -> {"base": raw generate function, "signature": [...], "elem_ids": [...]}
buttons = {}  # tab -> our "Add to queue" button
shows = {}  # tab -> (hidden button, hidden job-id box) that put a finished job's pictures in the tab's gallery
state = {"current": None, "id_task": None, "switched_from": None, "switched_to": None}


class _Request:
    """Stand-in for gr.Request when a queued job runs; Forge only reads .username."""

    username = None


# ---------------------------------------------------------------- UI wiring

def _on_after_component(component, **kwargs):
    elem_id = kwargs.get("elem_id") or getattr(component, "elem_id", None)
    for tab in TABS:
        if elem_id == f"{tab}_generate":
            _attach(tab, component)


def _attach(tab, generate_button):
    queue_button = gr.Button("Add to queue", elem_id=f"{tab}_sdq_add", elem_classes=["sdq-add"],
                             tooltip="Add these exact settings to the queue (Ctrl+Shift+Enter)")
    buttons[tab] = queue_button
    # the page presses this (hidden) button when a queued job is done, so the pictures land in Forge's own gallery
    shows[tab] = (gr.Button("Show queued result", elem_id=f"{tab}_sdq_show", elem_classes=["sdq-hidden"]),
                  gr.Textbox(visible=False, elem_id=f"{tab}_sdq_show_job"))
    original_click = generate_button.click

    def click(*args, **kwargs):
        dependency = original_click(*args, **kwargs)
        try:
            _wire(tab, kwargs)
        except Exception:
            print(f"[Prompt Queue] could not attach to {tab}:", file=sys.stderr)
            traceback.print_exc()
        return dependency

    generate_button.click = click


def _generate_function(tab):
    if tab == "txt2img":
        import modules.txt2img
        return modules.txt2img.txt2img
    import modules.img2img
    return modules.img2img.img2img


def _wire(tab, generate_kwargs):
    if tab in runners:
        return
    inputs = list(generate_kwargs.get("inputs") or [])
    fn = generate_kwargs.get("fn")
    if not inputs or fn is None:
        return
    base = inspect.unwrap(fn)
    # other extensions also hook the Generate button; only Forge's own generate call is the one to copy
    if base is not _generate_function(tab):
        return
    elem_ids = [getattr(c, "elem_id", None) for c in inputs]
    runners[tab] = {
        "base": base,
        "signature": [f"{type(c).__name__}:{getattr(c, 'elem_id', '') or getattr(c, 'label', '') or ''}" for c in inputs],
        "elem_ids": elem_ids,
    }

    def enqueue(*values):
        id_task = values[0] if values else ""
        copies = 1
        if isinstance(id_task, str) and "_n" in id_task:
            try:
                copies = int(id_task.rsplit("_n", 1)[1].rstrip(")"))
            except ValueError:
                copies = 1
        summary = _summarize(tab, elem_ids, values)
        store.add(tab, values, runners[tab]["signature"], summary, copies)

    # queue=False: the add never waits in Gradio's line behind other page events;
    # trigger_mode="multiple": a second press while the first is still on its way is not dropped
    buttons[tab].click(fn=enqueue, _js=f"sdqArgs_{tab}", inputs=inputs, outputs=[], show_progress=False,
                       queue=False, trigger_mode="multiple")

    outputs = list(generate_kwargs.get("outputs") or [])
    if tab in shows and outputs:
        show_button, show_job = shows[tab]
        show_button.click(fn=lambda job_id: _show_outputs(job_id, len(outputs)), _js=f"sdqShowJob_{tab}",
                          inputs=[show_job], outputs=outputs, show_progress=False, queue=False)


def _show_outputs(job_id, count):
    """Values for Generate's outputs (gallery, video, generation info, info html, log) from a finished job."""
    out = [gr.update() for _ in range(count)]
    job = store.get(str(job_id or ""))
    if not job or job.get("status") != DONE:
        return out
    from PIL import Image

    images = []
    for im in job.get("images") or []:
        try:
            img = Image.open(im["path"])
        except OSError:
            continue
        img.already_saved_as = im["path"]  # Forge then serves the saved file instead of writing a copy
        images.append(img)
    if not images:
        return out
    out[0] = images
    if count >= 5:  # Forge's layout: gallery, video, generation info, info html, log
        if job.get("gen_info"):
            out[2] = job["gen_info"]
        if job.get("html_info"):
            out[3] = job["html_info"]
    return out


def _summarize(tab, elem_ids, values):
    def pick(name, default=None):
        key = f"{tab}_{name}"
        if key in elem_ids:
            return values[elem_ids.index(key)]
        return default

    def num(v):
        return v if isinstance(v, (int, float)) else None

    return {
        "prompt": str(pick("prompt", "") or "")[:2000],
        "negative": str(pick("neg_prompt", "") or "")[:500],
        "width": num(pick("width")), "height": num(pick("height")),
        "steps": num(pick("steps")), "cfg": num(pick("cfg_scale")),
        "seed": num(pick("seed")),
        "batch_count": num(pick("batch_count")), "batch_size": num(pick("batch_size")),
        "model": _model_snapshot(),
    }


def _model_snapshot():
    opts = shared.opts
    return {
        "checkpoint": getattr(opts, "sd_model_checkpoint", "") or "",
        "modules": list(getattr(opts, "forge_additional_modules", []) or []),
        "dtype": getattr(opts, "forge_unet_storage_dtype", None),
    }


# ---------------------------------------------------------------- model switch

def _use_model(snap):
    """Load the model the job was queued with (like ComfyUI). Returns a note for the card, or ''."""
    if not snap:
        return ""
    try:
        from modules_forge import main_entry
    except ImportError:
        return ""
    before = _model_snapshot()
    changed = False
    if snap.get("checkpoint") and snap["checkpoint"] != before["checkpoint"]:
        changed |= main_entry.checkpoint_change(snap["checkpoint"], None, save=False)
    if sorted(snap.get("modules") or []) != sorted(before["modules"]):
        changed |= main_entry.modules_change(snap.get("modules") or [], None, save=False)
    if snap.get("dtype") and snap["dtype"] != before["dtype"]:
        main_entry.dtype_change(snap["dtype"], None, save=False)
        changed = True
    if changed:
        if state["switched_from"] is None:
            state["switched_from"] = before
        state["switched_to"] = _model_snapshot()
        return "Loaded the model this job was queued with."
    return ""


def _restore_model():
    """Queue went idle: put back the model the page shows, unless someone changed it meanwhile."""
    prior, ours = state["switched_from"], state["switched_to"]
    state["switched_from"] = state["switched_to"] = None
    if prior is None or _model_snapshot() != ours:
        return
    try:
        from modules_forge import main_entry
        main_entry.checkpoint_change(prior["checkpoint"], None, save=False, refresh=False)
        main_entry.modules_change(prior["modules"], None, save=False, refresh=False)
        if prior.get("dtype"):
            main_entry.dtype_change(prior["dtype"], None, save=False, refresh=False)
        main_entry.refresh_model_loading_parameters()
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------- worker

def _run(job):
    runner = runners.get(job["tab"])
    if runner is None:
        return store.finish(job["id"], FAILED, error=f"The {job['tab']} tab is not ready yet.")
    if job["signature"] != runner["signature"]:
        return store.finish(job["id"], FAILED, error="The settings layout changed since this job was queued "
                            "(an extension was added, removed or updated). Queue it again from the page.")
    values = store.load_args(job["id"])
    id_task = f"task(sdq-{job['id']})"
    values[0] = id_task
    state["current"] = job["id"]
    note_box = {}

    def generate(*args, **kwargs):
        # runs inside Forge's GPU lock; until here the job may wait behind a Generate press
        state["id_task"] = id_task
        store.mark_started(job["id"])
        note_box["note"] = _use_model(job["summary"].get("model"))
        return runner["base"](*args, **kwargs)

    wrapped = call_queue.wrap_gradio_gpu_call(generate, extra_outputs=[None, None, "", ""])
    try:
        result = list(wrapped(id_task, _Request(), *values[1:]))
    finally:
        state["current"] = state["id_task"] = None

    if store.get(job["id"]) and store.get(job["id"]).get("cancel"):
        return store.finish(job["id"], CANCELLED, error="Stopped by you.", note=note_box.get("note", ""))
    gallery = _update_value(result[0]) if result else None
    video = _update_value(result[1]) if len(result) > 4 else None
    if not gallery and isinstance(video, str) and os.path.isfile(video):
        # ponytail: video results are only named in the note; a player in the drawer if people ask for it
        return store.finish(job["id"], DONE, note=f"Video saved as {os.path.basename(video)}.")
    if not gallery:
        log = str(result[-1]) if result else ""
        return store.finish(job["id"], FAILED, error=_plain(log) or "Forge returned no image.", note=note_box.get("note", ""))
    images = _keep_images(job["id"], gallery)
    infotext, seed = "", None
    try:
        info = json.loads(result[2])
        infotext = (info.get("infotexts") or [""])[0]
        seed = info.get("seed")
    except Exception:
        pass
    summary = dict(job["summary"])
    if seed is not None:
        summary["seed_used"] = seed
    gen_info = result[2] if len(result) > 3 and isinstance(result[2], str) else ""
    html_info = result[3] if len(result) > 4 and isinstance(result[3], str) else ""
    return store.finish(job["id"], DONE, images=images, infotext=infotext, summary=summary, note=note_box.get("note", ""),
                        gen_info=gen_info, html_info=html_info)


def _update_value(output):
    """Forge returns gr.update(value=...) dicts for the gallery and video; plain lists are passed through."""
    if isinstance(output, dict):
        return output.get("value")
    return output


def _plain(html_text):
    import html
    import re
    text = html.unescape(re.sub(r"<[^>]+>", " ", html_text))
    return re.sub(r"\s+", " ", text).strip()[:600]


def _keep_images(job_id, gallery):
    from PIL import Image

    kept = []
    for n, item in enumerate(gallery):
        img = item[0] if isinstance(item, (tuple, list)) else item
        path = None
        if isinstance(img, str):
            path = os.path.abspath(img)
            try:
                img = Image.open(path)
            except OSError:
                continue
        if not isinstance(img, Image.Image):
            continue
        saved = getattr(img, "already_saved_as", None)
        if saved:
            path = os.path.abspath(saved)
        if not path or not os.path.isfile(path):
            path = os.path.join(store.results_dir, f"{job_id}_{n}.png")
            img.save(path)
        thumb = img.copy()
        thumb.thumbnail((320, 320))
        thumb.convert("RGB").save(os.path.join(store.thumbs_dir, f"{job_id}_{n}.jpg"), quality=85)
        kept.append({"path": path, "w": img.width, "h": img.height})
    return kept


def _worker():
    while True:
        job = store.claim_next()
        if job is None:
            if state["switched_from"] is not None and not store.waiting():
                with call_queue.queue_lock:
                    _restore_model()
            store.wake.wait(1.0)
            store.wake.clear()
            continue
        try:
            _run(job)
        except Exception as e:
            traceback.print_exc()
            store.finish(job["id"], FAILED, error=f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- API

ROUTES = [
    ("GET", "/sd-queue/api", "this list"),
    ("GET", "/sd-queue/state", "all jobs, paused flag, the running job and time estimates"),
    ("POST", "/sd-queue/pause", "body {paused: true|false}"),
    ("POST", "/sd-queue/jobs/{id}/stop", "stop the running job, or remove a waiting one (returns an undo token)"),
    ("POST", "/sd-queue/jobs/{id}/move", "body {to: n}, n = place among the waiting jobs, 0 = next"),
    ("POST", "/sd-queue/jobs/{id}/again", "queue a finished job again with the same settings"),
    ("POST", "/sd-queue/remove", "body {ids: [...]} remove finished or waiting jobs (returns an undo token)"),
    ("POST", "/sd-queue/clear", "body {which: finished|waiting} (returns an undo token)"),
    ("POST", "/sd-queue/undo", "body {token}"),
    ("GET", "/sd-queue/jobs/{id}/thumb/{n}", "small JPEG of result n"),
    ("GET", "/sd-queue/jobs/{id}/image/{n}", "full image of result n"),
]


# changes on every Forge start; a page that saw another value is still the page from before the restart
BOOT = repr(time.time())


def _on_app_started(_demo, app: FastAPI):
    # ponytail: no auth of its own, same as Forge's /internal/* routes; add a check here if you expose Forge on a network
    @app.get("/sd-queue/api")
    def api_list():
        return [{"method": m, "path": p, "what": w} for m, p, w in ROUTES]

    @app.get("/sd-queue/state")
    def api_state():
        snap = store.snapshot()
        avg = store.average_seconds()
        waiting = sum(1 for j in snap["jobs"] if j["status"] == WAITING)
        snap["boot"] = BOOT
        snap.update(running=state["current"], id_task=state["id_task"], average_seconds=avg,
                    seconds_left=(avg * waiting) if avg else None, ready_tabs=sorted(runners))
        for j in snap["jobs"]:
            j.pop("signature", None)
            j.pop("gen_info", None)
            j.pop("html_info", None)
            j["images"] = [{"w": i.get("w"), "h": i.get("h")} for i in j.get("images", [])]
        return snap

    @app.post("/sd-queue/pause")
    def api_pause(body: dict = Body(...)):
        store.set_paused(bool(body.get("paused")))
        return {"paused": store.paused}

    @app.post("/sd-queue/jobs/{job_id}/stop")
    def api_stop(job_id: str):
        job = _job_or_404(job_id)
        if job["status"] == RUNNING:
            job["cancel"] = True
            shared.state.interrupt()
            return {"stopping": True}
        return {"undo": store.remove([job_id])}

    @app.post("/sd-queue/jobs/{job_id}/move")
    def api_move(job_id: str, body: dict = Body(...)):
        _job_or_404(job_id)
        if not store.move(job_id, int(body.get("to", 0))):
            raise HTTPException(409, "Only waiting jobs can move.")
        return {"ok": True}

    @app.post("/sd-queue/jobs/{job_id}/again")
    def api_again(job_id: str):
        _job_or_404(job_id)
        new = store.requeue(job_id)
        if new is None:
            raise HTTPException(409, "Only finished jobs can run again.")
        return {"id": new["id"]}

    @app.post("/sd-queue/remove")
    def api_remove(body: dict = Body(...)):
        ids = [str(i) for i in (body.get("ids") or [])]
        return {"undo": store.remove(ids)}

    @app.post("/sd-queue/clear")
    def api_clear(body: dict = Body(...)):
        token, count = store.clear(str(body.get("which", "")))
        return {"undo": token, "count": count}

    @app.post("/sd-queue/undo")
    def api_undo(body: dict = Body(...)):
        return {"restored": store.undo(str(body.get("token", "")))}

    @app.get("/sd-queue/jobs/{job_id}/thumb/{n}")
    def api_thumb(job_id: str, n: int):
        _job_or_404(job_id)
        path = os.path.join(store.thumbs_dir, f"{job_id}_{int(n)}.jpg")
        if not os.path.isfile(path):
            raise HTTPException(404, "No picture.")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/sd-queue/jobs/{job_id}/image/{n}")
    def api_image(job_id: str, n: int):
        job = _job_or_404(job_id)
        images = job.get("images") or []
        if not 0 <= n < len(images) or not os.path.isfile(images[n]["path"]):
            raise HTTPException(404, "This picture is no longer on disk.")
        return FileResponse(images[n]["path"])  # only paths this extension recorded, never a path from the request

    if not state.get("worker_started"):
        state["worker_started"] = True
        threading.Thread(target=_worker, name="prompt-queue-worker", daemon=True).start()


def _job_or_404(job_id):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job.")
    return job


script_callbacks.on_after_component(_on_after_component)
script_callbacks.on_app_started(_on_app_started)
