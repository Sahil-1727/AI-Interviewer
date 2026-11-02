document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-submit-form]").forEach(form => {
        const buttons = Array.from(form.querySelectorAll("button"));
        const initialDisabled = buttons.map(button => button.disabled);

        // A cached Back navigation must not leave the new submission lock active.
        window.addEventListener("pageshow", event => {
            if (!event.persisted || form.dataset.submitting !== "true") return;
            delete form.dataset.submitting;
            form.classList.remove("is-submitting");
            buttons.forEach((button, index) => {
                button.disabled = initialDisabled[index];
                button.removeAttribute("aria-busy");
            });
            form.querySelector(".submit-status").hidden = true;
        });

        form.addEventListener("submit", event => {
            if (event.defaultPrevented || form.dataset.submitting === "true" || form.dataset.voiceBusy === "true") {
                event.preventDefault();
                return;
            }
            const fields = form.querySelectorAll("input[type=text], textarea");
            for (const field of fields) {
                if (!field.value.trim()) {
                    event.preventDefault();
                    field.setCustomValidity("Please enter a response before continuing.");
                    field.reportValidity();
                    return;
                }
            }
            form.dataset.submitting = "true";
            form.classList.add("is-submitting");
            // Keep the live loading message outside an aria-busy subtree.
            form.querySelector('[type="submit"]').setAttribute("aria-busy", "true");
            // Leave named inputs enabled so their values are included in the POST.
            form.querySelectorAll("button").forEach(button => { button.disabled = true; });
            form.querySelector(".submit-status").hidden = false;
        });
        form.querySelectorAll("input[type=text], textarea").forEach(field => {
            field.addEventListener("input", () => field.setCustomValidity(""));
        });
    });

    const resume = document.getElementById("resume");
    if (resume) {
        const area = document.getElementById("upload-area");
        const error = document.getElementById("upload-error");
        function updateFile() {
            const file = resume.files[0];
            const valid = !file || /\.pdf$/i.test(file.name);
            resume.setCustomValidity(valid ? "" : "Please choose a PDF resume.");
            error.hidden = valid;
            error.textContent = valid ? "" : "Please choose a PDF file for your resume.";
            area.classList.toggle("has-file", !!file && valid);
            document.getElementById("file-label").textContent = file ? file.name : "Choose your resume";
            document.getElementById("file-detail").textContent = file && valid ? "PDF selected · Click to choose a different file" : "or drag and drop a PDF here";
        }
        resume.addEventListener("change", updateFile);
        ["dragenter", "dragover"].forEach(type => area.addEventListener(type, () => area.classList.add("is-dragging")));
        ["dragleave", "drop"].forEach(type => area.addEventListener(type, () => area.classList.remove("is-dragging")));
        // The native file input covers the entire drop zone and accepts the drop.
    }

    const cameraStatus = document.getElementById("camera-status");
    if (cameraStatus) {
        const syncCamera = () => {
            const live = cameraStatus.textContent.trim() === "Camera is on";
            document.getElementById("camera-frame").classList.toggle("is-live", live);
            document.getElementById("camera-placeholder").hidden = live;
        };
        new MutationObserver(syncCamera).observe(cameraStatus, {childList: true, subtree: true, characterData: true});
        syncCamera();
    }
    const printButton = document.getElementById("print-report");
    if (printButton) printButton.addEventListener("click", () => window.print());
});
