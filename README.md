# Prompt Queue for Forge Neo

Queue up many generations while one is running, the way ComfyUI does.

Press **Add to queue** next to **Generate**. Every setting on the page is captured
at that moment: prompt, size, seed, sampler, hires fix, the options of every other
extension, and (on img2img) the input pictures. You can change the page right
away and queue the next one. Jobs run one after another in the background.

## Features

- **Add to queue** on txt2img and img2img, with a copies box (×N). With seed -1
  every copy gets its own seed.
- **Auto** tick box next to the button: ticked = jobs start by themselves;
  not ticked = jobs wait until you press Resume.
- A queued job shows in the tab's own gallery, like Generate: progress bar and
  live preview while it runs, then the pictures and their info text.
  Short notices tell you when a job starts, ends or fails.
- Keyboard: **Ctrl+Shift+Enter** adds to the queue, **Alt+Q** opens the queue,
  **Esc** closes it.
- Queue panel (the "Queue" button, bottom right):
  - live progress bar, time left and live preview of the running job
  - pause / resume the whole queue (the running job always finishes)
  - stop the running job
  - reorder waiting jobs: drag and drop, or Run next / up / down
  - remove a job, clear finished, clear waiting — every removal has **Undo**
  - finished jobs show thumbnails; click for a full-size viewer
    (arrow keys, swipe, Esc)
  - **Run again** / **Retry** with the exact same settings
  - **Load settings** puts a finished job's settings back into the page
  - filter (All / Waiting / Done / Failed) and search
- Each job remembers the model it was queued with and loads it when it runs.
  When the queue goes idle the page's own model selection is put back.
- No pop-up dialogs: confirms are inline ("click again"), messages are toasts.
- The queue survives a page reload and a Forge restart. After a restart the
  waiting jobs carry on by themselves. Untick **Auto** to hold them.
- A page left open over a Forge restart asks you to press F5.
- A job you queue waits its turn behind a Generate you press yourself, and the
  other way round. Nothing runs twice at the same time.

## Install

Extensions tab → Install from URL → paste this repository's URL → Install,
then restart Forge. Or clone it into `extensions/`.

## Tested on

- Forge Neo **2.29** (`neo` branch, commit 8a6bc1d), in daily use since October 2026.
- Forge Neo **neo-2.30.1** (`neo` branch, commit ad60793, 21 October 2026):
  5 queued jobs ran one after another and landed in the gallery, with no errors.

If your Forge is older and the extension does not load, update Forge first.

## If Forge Neo adds a queue itself

- You update Forge: keep the extension or remove it, your choice. If the two
  clash, remove this extension's folder from `extensions/`.
- You keep your current Forge: keep the extension. It keeps working.

## JSON API

Everything the panel does is plain HTTP. `GET /sd-queue/api` lists the routes:

| Method | Path | What |
|---|---|---|
| GET | `/sd-queue/state` | all jobs, paused flag, running job, time estimate |
| POST | `/sd-queue/pause` | `{"paused": true}` or `false` |
| POST | `/sd-queue/jobs/{id}/stop` | stop the running job, or remove a waiting one |
| POST | `/sd-queue/jobs/{id}/move` | `{"to": 0}` = runs next |
| POST | `/sd-queue/jobs/{id}/again` | queue a finished job again |
| POST | `/sd-queue/remove` | `{"ids": [...]}` |
| POST | `/sd-queue/clear` | `{"which": "finished"}` or `"waiting"` |
| POST | `/sd-queue/undo` | `{"token": "..."}` from a remove/clear reply |
| GET | `/sd-queue/jobs/{id}/thumb/{n}` | thumbnail of result n |
| GET | `/sd-queue/jobs/{id}/image/{n}` | full image of result n |

The routes have no login of their own, the same as Forge's `/internal/*` routes.
Do not expose Forge to an untrusted network.

## Good to know

- Queued settings are stored in `queue-data/` inside this extension folder.
- If you add, remove or update an extension after queuing, the page layout
  changes and old waiting jobs can no longer be read. They fail with a clear
  message; queue them again.
- Results are saved by Forge as usual (your normal output folder). If saving is
  turned off, the queue keeps its own copy until you remove the job.

## Tests

```
python tests/test_store.py
```

## License

MIT
