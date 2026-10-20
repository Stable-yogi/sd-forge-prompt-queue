// Prompt Queue for Forge: the panel. No alert/confirm pop-ups anywhere: inline confirms and Undo toasts.
(function () {
    "use strict";

    const API = "/sd-queue";
    const LS = "sdq.";
    const STATUS_TEXT = { waiting: "Waiting", running: "Running", done: "Done", failed: "Failed", cancelled: "Stopped" };
    let last = null; // last /state reply
    let firstBoot = null; // Forge start stamp seen by this page
    let staleWarned = false;
    let open = false;
    let filter = "all";
    let search = "";
    let pollTimer = null;
    let lastPreviewId = -1;
    let previewSrc = "";
    let progressInfo = null;
    let viewer = null; // {jobs: [...], items: [{job, n}], index}
    let pendingAdd = null; // {tab, before, t0}
    let armed = null; // {key, timer} for two-click confirms
    let dragId = null;
    let prevStatus = null; // job id -> status at the last poll (null until the first poll)
    let mainTask = null; // id_task whose progress bar shows in Forge's own gallery
    const showWant = {}; // tab -> job id the hidden "show" button should put in the gallery

    function lsGet(key, fallback) {
        try {
            const v = localStorage.getItem(LS + key);
            return v === null ? fallback : JSON.parse(v);
        } catch (e) {
            return fallback;
        }
    }
    function lsSet(key, value) {
        try {
            localStorage.setItem(LS + key, JSON.stringify(value));
        } catch (e) {
            /* private window or blocked storage: the panel still works, it just forgets */
        }
    }

    async function call(method, path, body) {
        const res = await fetch(API + path, {
            method,
            headers: body ? { "Content-Type": "application/json" } : {},
            body: body ? JSON.stringify(body) : undefined,
        });
        if (!res.ok) {
            let msg = res.status + " " + res.statusText;
            try {
                msg = (await res.json()).detail || msg;
            } catch (e) {
                /* not JSON */
            }
            throw new Error(msg);
        }
        return res.json();
    }

    // ------------------------------------------------------------ "Add to queue" args
    function queueId(tab) {
        const input = document.getElementById(tab + "_sdq_count");
        let n = parseInt(input ? input.value : "1", 10);
        if (!(n >= 1)) n = 1;
        n = Math.min(n, 100);
        const rand = Math.random().toString(36).slice(2, 10);
        return "task(sdq_" + rand + "_n" + n + ")";
    }
    function makeArgs(tab, args) {
        const res = Array.from(args);
        res[0] = queueId(tab);
        startAddWatch(tab);
        return res;
    }
    window.sdqArgs_txt2img = function () {
        return makeArgs("txt2img", arguments);
    };
    window.sdqArgs_img2img = function () {
        return makeArgs("img2img", arguments);
    };

    // Forge's hidden "show queued result" button asks for the job id here
    window.sdqShowJob_txt2img = function () {
        return [showWant.txt2img || ""];
    };
    window.sdqShowJob_img2img = function () {
        return [showWant.img2img || ""];
    };

    function startAddWatch(tab) {
        // answer at once; the poll below only speaks up again if the job never arrives
        const input = document.getElementById(tab + "_sdq_count");
        const n = Math.min(Math.max(parseInt(input ? input.value : "1", 10) || 1, 1), 100);
        toast("Queued " + n + (n === 1 ? " job." : " jobs.") + (last && last.paused ? " Run automatically is off: press Run next or tick it." : ""));
        pendingAdd = { tab, before: last ? last.jobs.length : 0, t0: Date.now(), quiet: true };
        schedule(250);
    }

    // ------------------------------------------------------------ build the DOM once
    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        return e;
    }

    let root, pill, drawer, list, summaryLine, pauseBtn, noticeBox, toastBox, searchInput, filterBar;

    function build() {
        if (document.getElementById("sdq-root")) return;
        root = el("div");
        root.id = "sdq-root";

        pill = el("button", "sdq-pill");
        pill.type = "button";
        pill.title = "Open the queue (Alt+Q)";
        pill.addEventListener("click", () => setOpen(!open));
        root.appendChild(pill);

        drawer = el("aside", "sdq-drawer");
        drawer.setAttribute("aria-label", "Prompt queue");
        const head = el("div", "sdq-head");
        head.appendChild(el("h3", "sdq-title", "Queue"));
        pauseBtn = el("button", "sdq-btn sdq-pause");
        pauseBtn.type = "button";
        pauseBtn.addEventListener("click", togglePause);
        head.appendChild(pauseBtn);
        const close = el("button", "sdq-btn sdq-icon", "✕");
        close.type = "button";
        close.title = "Close (Esc)";
        close.addEventListener("click", () => setOpen(false));
        head.appendChild(close);
        drawer.appendChild(head);

        summaryLine = el("div", "sdq-summary");
        drawer.appendChild(summaryLine);
        noticeBox = el("div", "sdq-notice");
        drawer.appendChild(noticeBox);

        filterBar = el("div", "sdq-filters");
        for (const [key, label] of [["all", "All"], ["waiting", "Waiting"], ["done", "Done"], ["failed", "Failed"]]) {
            const b = el("button", "sdq-chip", label);
            b.type = "button";
            b.dataset.filter = key;
            b.addEventListener("click", () => {
                filter = key;
                lsSet("filter", key);
                render();
            });
            filterBar.appendChild(b);
        }
        searchInput = el("input", "sdq-search");
        searchInput.type = "search";
        searchInput.placeholder = "Search prompts";
        searchInput.value = search;
        searchInput.addEventListener("input", () => {
            search = searchInput.value;
            lsSet("search", search);
            render();
        });
        filterBar.appendChild(searchInput);
        drawer.appendChild(filterBar);

        list = el("div", "sdq-list");
        drawer.appendChild(list);

        const foot = el("div", "sdq-foot");
        foot.appendChild(confirmButton("Clear finished", "clear-finished", () => clear("finished")));
        foot.appendChild(confirmButton("Clear waiting", "clear-waiting", () => clear("waiting")));
        drawer.appendChild(foot);
        root.appendChild(drawer);

        toastBox = el("div", "sdq-toasts");
        root.appendChild(toastBox);
        document.body.appendChild(root);

        document.addEventListener("keydown", onKey, true);
        setOpen(lsGet("open", false));
    }

    function addCountBoxes() {
        for (const tab of ["txt2img", "img2img"]) {
            const btn = document.getElementById(tab + "_sdq_add");
            if (!btn || document.getElementById(tab + "_sdq_count")) continue;
            const wrap = el("div", "sdq-addrow");
            // Forge lays Interrupt/Skip over the whole generate box while it works, so the row sits below the box
            const box = document.getElementById(tab + "_generate_box");
            if (box && box.contains(btn)) box.parentNode.insertBefore(wrap, box.nextSibling);
            else btn.parentNode.insertBefore(wrap, btn);
            wrap.appendChild(btn);
            const count = el("input", "sdq-count");
            count.id = tab + "_sdq_count";
            count.type = "number";
            count.min = "1";
            count.max = "100";
            count.value = String(lsGet("count_" + tab, 1));
            count.title = "How many copies to queue. With seed -1 every copy gets a new seed.";
            count.addEventListener("change", () => {
                let n = parseInt(count.value, 10);
                if (!(n >= 1)) n = 1;
                count.value = String(Math.min(n, 100));
                lsSet("count_" + tab, parseInt(count.value, 10));
            });
            wrap.appendChild(count);
            const auto = el("label", "sdq-auto");
            auto.title = "Ticked: queued jobs start by themselves. Not ticked: they wait until you press Run next or tick this.";
            const tick = el("input");
            tick.type = "checkbox";
            tick.id = tab + "_sdq_auto";
            tick.checked = !(last && last.paused);
            tick.addEventListener("change", async () => {
                try {
                    await call("POST", "/pause", { paused: !tick.checked });
                    toast(tick.checked ? "Run automatically: on." : "Run automatically: off. New jobs wait for you.");
                } catch (e) {
                    tick.checked = !tick.checked;
                    toast("Could not change it: " + e.message, { error: true });
                }
                refresh();
            });
            auto.appendChild(tick);
            auto.appendChild(el("span", "", "Auto"));
            wrap.appendChild(auto);
        }
    }

    // ------------------------------------------------------------ helpers
    function fmtTime(sec) {
        if (sec === null || sec === undefined || !isFinite(sec)) return "";
        sec = Math.max(0, Math.round(sec));
        if (sec < 60) return sec + " s";
        const m = Math.floor(sec / 60);
        if (m < 60) return m + " min " + (sec % 60) + " s";
        return Math.floor(m / 60) + " h " + (m % 60) + " min";
    }
    function shortModel(model) {
        if (!model || !model.checkpoint) return "";
        return model.checkpoint.replace(/\s*\[[0-9a-f]+\]$/, "").split(/[\\/]/).pop();
    }

    function toast(text, opts) {
        opts = opts || {};
        const t = el("div", "sdq-toast" + (opts.error ? " sdq-error" : ""));
        t.appendChild(el("span", "", text));
        if (opts.undo) {
            const u = el("button", "sdq-btn sdq-undo", "Undo");
            u.type = "button";
            u.addEventListener("click", async () => {
                t.remove();
                try {
                    const r = await call("POST", "/undo", { token: opts.undo });
                    toast(r.restored ? "Brought back " + r.restored + " job(s)." : "Too late to undo.");
                } catch (e) {
                    toast("Undo failed: " + e.message, { error: true });
                }
                refresh();
            });
            t.appendChild(u);
        }
        if (opts.reload) {
            const r = el("button", "sdq-btn sdq-undo", "Reload now");
            r.type = "button";
            r.addEventListener("click", () => location.reload());
            t.appendChild(r);
        }
        toastBox.appendChild(t);
        if (!opts.reload) setTimeout(() => t.remove(), opts.undo ? 8000 : 4000);
    }

    function confirmButton(label, key, action) {
        const b = el("button", "sdq-btn sdq-danger", label);
        b.type = "button";
        b.addEventListener("click", () => {
            if (armed && armed.key === key) {
                clearTimeout(armed.timer);
                armed = null;
                b.textContent = label;
                b.classList.remove("sdq-armed");
                action();
                return;
            }
            if (armed) armed.reset();
            b.textContent = "Click again to confirm";
            b.classList.add("sdq-armed");
            const reset = () => {
                b.textContent = label;
                b.classList.remove("sdq-armed");
                armed = null;
            };
            armed = { key, reset, timer: setTimeout(reset, 3000) };
        });
        return b;
    }

    function busy(btn, promise) {
        btn.disabled = true;
        btn.classList.add("sdq-busy");
        return promise.finally(() => {
            btn.disabled = false;
            btn.classList.remove("sdq-busy");
        });
    }

    // ------------------------------------------------------------ actions
    async function togglePause() {
        if (!last) return;
        try {
            await busy(pauseBtn, call("POST", "/pause", { paused: !last.paused }));
            toast(last.paused ? "Queue running." : "Queue paused. The job that is running now still finishes.");
        } catch (e) {
            toast("Could not change pause: " + e.message, { error: true });
        }
        refresh();
    }
    async function clear(which) {
        try {
            const r = await call("POST", "/clear", { which });
            if (r.count) toast("Cleared " + r.count + " job(s).", { undo: r.undo });
            else toast("Nothing to clear.");
        } catch (e) {
            toast("Could not clear: " + e.message, { error: true });
        }
        refresh();
    }
    async function act(btn, path, okText) {
        try {
            const r = await busy(btn, call("POST", path));
            if (r && r.undo) toast(okText, { undo: r.undo });
            else if (okText) toast(okText);
        } catch (e) {
            toast(e.message, { error: true });
        }
        refresh();
    }
    async function move(id, to) {
        try {
            await call("POST", "/jobs/" + id + "/move", { to });
        } catch (e) {
            toast("Could not move: " + e.message, { error: true });
        }
        refresh();
    }

    function loadSettings(job) {
        // Uses Forge's own "read generation parameters" button, so every field it knows is filled in.
        const tab = job.tab;
        const tabButton = Array.from(document.querySelectorAll("#tabs > .tab-nav button, #tabs button")).find((b) => b.textContent.trim().toLowerCase() === tab);
        if (tabButton) tabButton.click();
        const area = document.querySelector("#" + tab + "_prompt textarea");
        const paste = document.querySelector("#" + tab + "_tools #paste") || document.querySelector("#tab_" + tab + " #paste");
        if (!area || !paste) {
            toast("Could not find the prompt box on the " + tab + " tab.", { error: true });
            return;
        }
        area.value = job.infotext;
        area.dispatchEvent(new Event("input", { bubbles: true }));
        setTimeout(() => paste.click(), 50);
        toast("Settings loaded into the " + tab + " tab.");
    }

    // ------------------------------------------------------------ viewer
    function openViewer(job, n) {
        const items = [];
        for (const j of visibleJobs()) for (let i = 0; i < j.images.length; i++) items.push({ job: j, n: i });
        let index = items.findIndex((x) => x.job.id === job.id && x.n === n);
        if (index < 0) return;
        if (!viewer) {
            const box = el("div", "sdq-viewer");
            box.addEventListener("click", (e) => {
                if (e.target === box) closeViewer();
            });
            const img = el("img", "sdq-viewer-img");
            const prev = el("button", "sdq-btn sdq-nav sdq-prev", "‹");
            const next = el("button", "sdq-btn sdq-nav sdq-next", "›");
            const close = el("button", "sdq-btn sdq-nav sdq-vclose", "✕");
            const count = el("div", "sdq-viewer-count");
            prev.addEventListener("click", () => stepViewer(-1));
            next.addEventListener("click", () => stepViewer(1));
            close.addEventListener("click", closeViewer);
            let x0 = null;
            box.addEventListener("touchstart", (e) => (x0 = e.touches[0].clientX), { passive: true });
            box.addEventListener("touchend", (e) => {
                if (x0 === null) return;
                const dx = e.changedTouches[0].clientX - x0;
                if (Math.abs(dx) > 40) stepViewer(dx < 0 ? 1 : -1);
                x0 = null;
            });
            box.append(img, prev, next, close, count);
            root.appendChild(box);
            viewer = { box, img, count };
        }
        viewer.items = items;
        viewer.index = index;
        showViewer();
    }
    function imageUrl(item) {
        return API + "/jobs/" + item.job.id + "/image/" + item.n;
    }
    function showViewer() {
        const item = viewer.items[viewer.index];
        viewer.img.src = imageUrl(item);
        viewer.count.textContent = viewer.index + 1 + " / " + viewer.items.length;
        const next = viewer.items[viewer.index + 1];
        if (next) new Image().src = imageUrl(next);
    }
    function stepViewer(d) {
        if (!viewer) return;
        viewer.index = (viewer.index + d + viewer.items.length) % viewer.items.length;
        showViewer();
    }
    function closeViewer() {
        if (!viewer) return;
        viewer.box.remove();
        viewer = null;
    }

    // ------------------------------------------------------------ keys
    function onKey(e) {
        if (e.key === "Escape") {
            if (viewer) {
                closeViewer();
                e.stopPropagation();
                return;
            }
            if (open) {
                setOpen(false);
                e.stopPropagation();
                return;
            }
        }
        if (viewer && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
            stepViewer(e.key === "ArrowLeft" ? -1 : 1);
            e.preventDefault();
            return;
        }
        if (e.altKey && !e.ctrlKey && (e.key === "q" || e.key === "Q")) {
            setOpen(!open);
            e.preventDefault();
            return;
        }
        if (e.ctrlKey && e.shiftKey && e.key === "Enter") {
            const tab = currentTab();
            const btn = tab && document.getElementById(tab + "_sdq_add");
            if (btn) {
                btn.click();
                e.preventDefault();
                e.stopPropagation();
            }
        }
    }
    function currentTab() {
        for (const tab of ["txt2img", "img2img"]) {
            const t = document.getElementById("tab_" + tab);
            if (t && t.offsetParent !== null) return tab;
        }
        return null;
    }

    // ------------------------------------------------------------ render
    function setOpen(v) {
        open = !!v;
        lsSet("open", open);
        if (drawer) drawer.classList.toggle("sdq-open", open);
        if (open) refresh();
    }

    function visibleJobs() {
        if (!last) return [];
        const running = last.jobs.filter((j) => j.status === "running");
        const waiting = last.jobs.filter((j) => j.status === "waiting");
        const finished = last.jobs.filter((j) => ["done", "failed", "cancelled"].includes(j.status)).sort((a, b) => (b.finished || 0) - (a.finished || 0));
        let jobs = running.concat(waiting, finished);
        if (filter === "waiting") jobs = running.concat(waiting);
        else if (filter === "done") jobs = jobs.filter((j) => j.status === "done");
        else if (filter === "failed") jobs = jobs.filter((j) => j.status === "failed" || j.status === "cancelled");
        const q = search.trim().toLowerCase();
        if (q) jobs = jobs.filter((j) => (j.summary.prompt || "").toLowerCase().includes(q));
        return jobs;
    }

    function render() {
        if (!last || !root || dragId) return; // never rebuild the list under a drag
        const waiting = last.jobs.filter((j) => j.status === "waiting").length;
        const runningJob = last.jobs.find((j) => j.status === "running");
        const pct = progressInfo && progressInfo.progress ? Math.round(progressInfo.progress * 100) : null;

        pill.className = "sdq-pill" + (runningJob ? " sdq-pill-run" : "") + (last.paused ? " sdq-pill-paused" : "");
        pill.textContent = "Queue" + (runningJob ? " · running" + (pct !== null ? " " + pct + "%" : "") : "") + (waiting ? " · " + waiting + " waiting" : "") + (last.paused ? " · paused" : "");

        for (const tick of document.querySelectorAll(".sdq-auto > input")) tick.checked = !last.paused;
        pauseBtn.textContent = last.paused ? "▶ Resume" : "❚❚ Pause";
        pauseBtn.title = last.paused ? "Start running the waiting jobs" : "Stop starting new jobs (the running one finishes)";
        const left = last.seconds_left ? " · about " + fmtTime(last.seconds_left + (progressInfo && progressInfo.eta ? progressInfo.eta : 0)) + " left" : "";
        summaryLine.textContent = (runningJob ? "1 running · " : "") + waiting + " waiting" + left + (last.paused ? " · paused" : "");
        noticeBox.textContent = last.notice || "";
        noticeBox.style.display = last.notice ? "" : "none";
        for (const b of filterBar.querySelectorAll(".sdq-chip")) b.classList.toggle("sdq-on", b.dataset.filter === filter);
        searchInput.style.display = last.jobs.length >= 20 || search ? "" : "none";

        const jobs = visibleJobs();
        const scroll = list.scrollTop;
        list.textContent = "";
        if (!jobs.length) {
            const empty = el("div", "sdq-empty");
            empty.textContent = last.jobs.length ? "No job matches this filter." : "The queue is empty. Set up a picture and press “Add to queue” next to Generate (Ctrl+Shift+Enter).";
            list.appendChild(empty);
        }
        const waitingIds = last.jobs.filter((j) => j.status === "waiting").map((j) => j.id);
        for (const job of jobs) list.appendChild(card(job, waitingIds));
        list.scrollTop = scroll;
    }

    function button(label, title, onClick, cls) {
        const b = el("button", "sdq-btn" + (cls ? " " + cls : ""), label);
        b.type = "button";
        b.title = title;
        b.addEventListener("click", (e) => {
            e.stopPropagation();
            onClick(b);
        });
        return b;
    }

    function card(job, waitingIds) {
        const c = el("div", "sdq-card sdq-" + job.status);
        c.dataset.id = job.id;
        const top = el("div", "sdq-card-top");
        top.appendChild(el("span", "sdq-badge sdq-b-" + job.status, STATUS_TEXT[job.status] || job.status));
        top.appendChild(el("span", "sdq-tab", job.tab));
        const pos = waitingIds.indexOf(job.id);
        if (pos >= 0) top.appendChild(el("span", "sdq-pos", "#" + (pos + 1)));
        const s = job.summary || {};
        const meta = [];
        if (s.width && s.height) meta.push(s.width + "×" + s.height);
        if (s.steps) meta.push(s.steps + " steps");
        const seed = s.seed_used !== undefined && s.seed_used !== null ? s.seed_used : s.seed;
        if (seed !== null && seed !== undefined) meta.push("seed " + seed);
        if (s.batch_count > 1 || s.batch_size > 1) meta.push((s.batch_count || 1) + "×" + (s.batch_size || 1) + " images");
        top.appendChild(el("span", "sdq-meta", meta.join(" · ")));
        c.appendChild(top);

        const prompt = el("div", "sdq-prompt", s.prompt || "(empty prompt)");
        prompt.title = s.prompt || "";
        c.appendChild(prompt);
        const model = shortModel(s.model);
        if (model) c.appendChild(el("div", "sdq-model", model));

        if (job.status === "running") {
            const bar = el("div", "sdq-bar");
            const fill = el("div", "sdq-fill");
            const p = progressInfo && progressInfo.progress ? progressInfo.progress : 0;
            fill.style.width = Math.round(p * 100) + "%";
            bar.appendChild(fill);
            c.appendChild(bar);
            const eta = progressInfo && progressInfo.eta ? " · " + fmtTime(progressInfo.eta) + " left" : "";
            const label = !last.id_task ? "Waits until the picture Forge makes now is done…" : p > 0 ? Math.round(p * 100) + "% done" : (progressInfo && progressInfo.textinfo) || "Starting…";
            c.appendChild(el("div", "sdq-meta", label + eta));
            if (previewSrc) {
                const pv = el("img", "sdq-preview");
                pv.src = previewSrc;
                pv.alt = "Live preview";
                c.appendChild(pv);
            }
        }
        if (job.status === "done" && job.images.length) {
            const thumbs = el("div", "sdq-thumbs");
            job.images.slice(0, 8).forEach((im, n) => {
                const t = el("img", "sdq-thumb");
                t.loading = "lazy";
                t.src = API + "/jobs/" + job.id + "/thumb/" + n;
                t.alt = "Result " + (n + 1);
                t.addEventListener("click", () => openViewer(job, n));
                thumbs.appendChild(t);
            });
            if (job.images.length > 8) thumbs.appendChild(el("span", "sdq-more", "+" + (job.images.length - 8)));
            c.appendChild(thumbs);
        }
        if (job.error) c.appendChild(el("div", "sdq-err", job.error));
        if (job.note) c.appendChild(el("div", "sdq-note", job.note));
        if (job.finished && job.started) c.appendChild(el("div", "sdq-time", "took " + fmtTime(job.finished - job.started)));

        const row = el("div", "sdq-actions");
        if (job.status === "waiting") {
            c.draggable = true;
            c.addEventListener("dragstart", (e) => {
                dragId = job.id;
                e.dataTransfer.effectAllowed = "move";
                c.classList.add("sdq-dragging");
            });
            c.addEventListener("dragend", () => {
                dragId = null;
                c.classList.remove("sdq-dragging");
            });
            c.addEventListener("dragover", (e) => {
                if (dragId && dragId !== job.id) {
                    e.preventDefault();
                    c.classList.add("sdq-dropzone");
                }
            });
            c.addEventListener("dragleave", () => c.classList.remove("sdq-dropzone"));
            c.addEventListener("drop", (e) => {
                e.preventDefault();
                c.classList.remove("sdq-dropzone");
                if (dragId && dragId !== job.id) move(dragId, waitingIds.indexOf(job.id));
            });
            if (pos > 0) row.appendChild(button("⤒", "Run next", () => move(job.id, 0)));
            if (pos > 0) row.appendChild(button("↑", "Move up", () => move(job.id, pos - 1)));
            if (pos < waitingIds.length - 1) row.appendChild(button("↓", "Move down", () => move(job.id, pos + 1)));
            row.appendChild(button("Remove", "Take this job out of the queue", (b) => act(b, "/jobs/" + job.id + "/stop", "Job removed."), "sdq-danger"));
        } else if (job.status === "running") {
            row.appendChild(button("Stop", "Stop this job now", (b) => act(b, "/jobs/" + job.id + "/stop", "Stopping…"), "sdq-danger"));
        } else {
            row.appendChild(button(job.status === "done" ? "Run again" : "Retry", "Queue the same settings again", (b) => act(b, "/jobs/" + job.id + "/again", "Queued again.")));
            if (job.infotext) row.appendChild(button("Load settings", "Put this job's settings back into the page", () => loadSettings(job)));
            row.appendChild(button("Remove", "Take this job off the list", (b) => act(b, "/jobs/" + job.id + "/stop", "Job removed."), "sdq-danger"));
        }
        c.appendChild(row);
        return c;
    }

    // ------------------------------------------------------------ polling
    async function refresh() {
        try {
            last = await call("GET", "/state");
        } catch (e) {
            if (pill) pill.textContent = "Queue · offline";
            schedule(5000);
            return;
        }
        if (last.boot) {
            if (firstBoot === null) firstBoot = last.boot;
            else if (last.boot !== firstBoot && !staleWarned) {
                // the page outlived a Forge restart: its buttons still use the old settings
                staleWarned = true;
                toast("Forge was restarted. Press F5 to load the page again.", { error: true, reload: true });
            }
        }
        if (pendingAdd) {
            if (last.jobs.length > pendingAdd.before) {
                if (!pendingAdd.quiet) toast("Added " + (last.jobs.length - pendingAdd.before) + " job(s) to the queue.");
                pendingAdd = null;
            } else if (Date.now() - pendingAdd.t0 > 6000) {
                toast("The job did not reach the queue. Check the Forge console for an error.", { error: true });
                pendingAdd = null;
            }
        }
        watchJobs();
        if (last.id_task && open) await pollProgress(last.id_task);
        else if (last.id_task) await pollProgress(last.id_task, true);
        else {
            progressInfo = null;
            previewSrc = "";
            lastPreviewId = -1;
        }
        render();
        const anyWaiting = last.jobs.some((j) => j.status === "waiting");
        schedule(pendingAdd ? 300 : last.id_task || open || anyWaiting ? 1000 : 3000);
    }

    // ------------------------------------------------------------ show queue work in Forge's own result area
    function clip(text, n) {
        text = (text || "").replace(/\s+/g, " ").trim();
        return text.length > n ? text.slice(0, n - 1) + "…" : text;
    }

    function watchJobs() {
        const now = {};
        for (const j of last.jobs) now[j.id] = j.status;
        const waiting = last.jobs.filter((j) => j.status === "waiting").length;
        const running = last.jobs.find((j) => j.status === "running");
        if (prevStatus) {
            // finished since the last look: newest last, so the gallery ends on the latest pictures
            const ended = last.jobs
                .filter((j) => ["running", "waiting"].includes(prevStatus[j.id]) && ["done", "failed", "cancelled"].includes(j.status))
                .sort((a, b) => (a.finished || 0) - (b.finished || 0));
            const shown = {};
            for (const j of ended) {
                const took = j.finished && j.started ? " in " + fmtTime(j.finished - j.started) : "";
                if (j.status === "done") {
                    if (j.images.length) shown[j.tab] = j;
                    toast("Queued job done" + took + "." + (j.images.length ? " The pictures are in the " + j.tab + " gallery." : "") + (!running && !waiting ? " Queue finished." : ""));
                } else if (j.status === "failed") {
                    toast("Queued job failed: " + clip(j.error, 140), { error: true });
                } else {
                    toast("Queued job stopped.");
                }
            }
            for (const tab of Object.keys(shown)) showInGallery(tab, shown[tab]);
        }
        if (running && last.id_task && last.id_task !== mainTask) {
            mainTask = last.id_task;
            showMainProgress(running.tab, last.id_task);
            if (prevStatus) toast("Next queued job started: “" + clip(running.summary && running.summary.prompt, 60) + "”" + (waiting ? " · " + waiting + " more waiting" : "") + ".");
        }
        if (!running) mainTask = null;
        prevStatus = now;
    }

    function showMainProgress(tab, idTask) {
        // Forge's own progress bar + live preview, the same one Generate uses
        const container = document.getElementById(tab + "_gallery_container");
        const gallery = document.getElementById(tab + "_gallery");
        if (typeof requestProgress !== "function" || !container) return;
        requestProgress(idTask, container, gallery, function () {}, null, 0);
    }

    function showInGallery(tab, job) {
        const btn = document.getElementById(tab + "_sdq_show");
        if (!btn) return;
        showWant[tab] = job.id;
        btn.click();
    }

    async function pollProgress(idTask, light) {
        try {
            const res = await fetch("./internal/progress", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id_task: idTask, id_live_preview: light ? -1 : lastPreviewId, live_preview: !light }),
            });
            progressInfo = await res.json();
            if (!light && progressInfo.live_preview) {
                previewSrc = progressInfo.live_preview;
                lastPreviewId = progressInfo.id_live_preview;
            }
        } catch (e) {
            progressInfo = null;
        }
    }

    function schedule(ms) {
        clearTimeout(pollTimer);
        pollTimer = setTimeout(refresh, ms);
    }

    filter = lsGet("filter", "all");
    search = lsGet("search", "");

    onUiLoaded(function () {
        build();
        addCountBoxes();
        refresh();
    });
})();
