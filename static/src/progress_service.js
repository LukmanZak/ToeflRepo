import { registry } from "@web/core/registry";

// Keeps the "Processing..." box on an English Short form up to date while it generates.
// When the short leaves the queued/generating state, the form is reloaded to show the result.
export const englishProgressService = {
    dependencies: ["orm", "action"],
    start(env, { orm, action }) {
        let busy = false;
        setInterval(async () => {
            const box = document.querySelector(".o_english_progress");
            if (!box || busy) {
                return;
            }
            busy = true;
            try {
                const id = parseInt(box.dataset.id, 10);
                if (!id) {
                    return;
                }
                const [rec] = await orm.read("english.short", [id], [
                    "state",
                    "progress",
                    "progress_message",
                ]);
                if (!rec) {
                    return;
                }
                if (rec.state !== "queued" && rec.state !== "generating") {
                    await action.doAction("soft_reload");
                    return;
                }
                const text = box.querySelector(".o_english_progress_text");
                const bar = box.querySelector(".o_english_progress_bar");
                if (text) {
                    text.textContent = rec.progress_message || "Waiting in line...";
                }
                if (bar) {
                    bar.style.width = `${rec.progress || 0}%`;
                }
            } catch {
                // Ignore network hiccups; the next tick tries again.
            } finally {
                busy = false;
            }
        }, 3000);
    },
};

registry.category("services").add("english_progress", englishProgressService);
