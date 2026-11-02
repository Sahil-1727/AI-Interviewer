
document.addEventListener("DOMContentLoaded", () => {
    const question = document.getElementById("interview-question");
    const answer = document.getElementById("answer");
    const status = document.getElementById("voice-status");

    const speakBtn = document.getElementById("speak-question");
    const stopSpeakBtn = document.getElementById("stop-speaking");
    const recordBtn = document.getElementById("start-recording");
    const stopBtn = document.getElementById("stop-recording");
    const form = answer.form;
    const submitBtn = form.querySelector('[type="submit"]');
    const interviewerState = document.getElementById("interviewer-state");

    let recorder = null;
    let stream = null;
    let chunks = [];
    let audioPlayer = null;
    let audioUrl = null;
    let audioRequest = null;
    let captureBusy = false;

    function setState(state, label) {
        document.body.dataset.voiceState = state;
        if (interviewerState) interviewerState.textContent = label;
    }

    function setCaptureBusy(busy) {
        captureBusy = busy;
        form.dataset.voiceBusy = String(busy);
        submitBtn.disabled = busy || form.dataset.submitting === "true";
        recordBtn.disabled = busy;
        speakBtn.disabled = busy;
    }

    // Also catches keyboard submission during microphone permission requests.
    form.addEventListener("submit", event => {
        if (captureBusy) event.preventDefault();
        else {
            stopAudio();
            if (["generating", "speaking"].includes(document.body.dataset.voiceState)) {
                setState("ready", "Ready");
                setStatus("Audio stopped.");
            }
        }
    });

    function setStatus(message) {
        status.textContent = message;
    }

    function stopAudio() {
        if (audioRequest) {
            audioRequest.abort();
            audioRequest = null;
        }
        if (audioPlayer) {
            audioPlayer.onended = null;
            audioPlayer.onerror = null;
            audioPlayer.pause();
            audioPlayer.currentTime = 0;
            audioPlayer = null;
        }
        if (audioUrl) {
            URL.revokeObjectURL(audioUrl);
            audioUrl = null;
        }
        stopSpeakBtn.disabled = true;
    }

    // Python TTS
    speakBtn.addEventListener("click", async () => {
        if (captureBusy) return;
        stopAudio();
        const controller = new AbortController();
        audioRequest = controller;
        speakBtn.disabled = true;
        stopSpeakBtn.disabled = false;
        recordBtn.disabled = true;
        setStatus("Preparing question audio...");
        setState("generating", "Generating audio");

        try {
            const response = await fetch("/api/synthesize", {
                method: "POST",
                signal: controller.signal,
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    text: question.textContent.trim()
                })
            });

            if (!response.ok) {
                throw new Error("Voice generation failed");
            }

            const blob = await response.blob();
            if (controller.signal.aborted) return;
            audioUrl = URL.createObjectURL(blob);
            audioPlayer = new Audio(audioUrl);

            audioPlayer.onended = () => {
                stopAudio();
                setState("ready", "Ready");
                setStatus("Question finished.");
            };
            audioPlayer.onerror = () => {
                stopAudio();
                setState("ready", "Ready");
                setStatus("Audio playback failed. You can read the question above.");
            };
            await audioPlayer.play();
            if (controller.signal.aborted) return;
            setState("speaking", "Speaking");
            setStatus("AI is speaking...");
        } catch (error) {
            if (controller.signal.aborted) return;
            stopAudio();
            setState("ready", "Ready");
            setStatus("TTS error: " + error.message);
        } finally {
            if (!audioRequest || audioRequest === controller) {
                speakBtn.disabled = captureBusy || form.dataset.submitting === "true";
                recordBtn.disabled = captureBusy || form.dataset.submitting === "true";
            }
        }
    });

    stopSpeakBtn.addEventListener("click", () => {
        stopAudio();
        speakBtn.disabled = captureBusy || form.dataset.submitting === "true";
        recordBtn.disabled = captureBusy || form.dataset.submitting === "true";
        setState("ready", "Ready");
        setStatus("Audio stopped.");
    });

    // Python Whisper STT
    recordBtn.addEventListener("click", async () => {
        if (captureBusy || form.dataset.submitting === "true") return;
        setCaptureBusy(true);
        try {
            if (!navigator.mediaDevices?.getUserMedia ||
                typeof MediaRecorder === "undefined") {
                throw new Error("Microphone recording not supported.");
            }

            stopAudio();
            setState("permission", "Microphone access");
            setStatus("Allow microphone access in your browser to record.");
            stream = await navigator.mediaDevices.getUserMedia({
                audio: true
            });

            const formats = [
                {mime: "audio/webm;codecs=opus", ext: "webm"},
                {mime: "audio/mp4", ext: "mp4"},
                {mime: "audio/webm", ext: "webm"}
            ];

            const selected = formats.find(
                f => MediaRecorder.isTypeSupported(f.mime)
            );

            recorder = selected
                ? new MediaRecorder(stream, {mimeType: selected.mime})
                : new MediaRecorder(stream);

            chunks = [];

            recorder.ondataavailable = event => {
                if (event.data.size > 0) chunks.push(event.data);
            };

            recorder.onstop = async () => {
                recordBtn.disabled = true;
                stopBtn.disabled = true;

                stream.getTracks().forEach(track => track.stop());
                stream = null;

                const mime = recorder.mimeType;
                const ext = mime.includes("mp4") ? "mp4" :
                            mime.includes("ogg") ? "ogg" : "webm";

                const blob = new Blob(chunks, {type: mime});

                if (!blob.size) {
                    setStatus("No audio captured.");
                    setCaptureBusy(false);
                    setState("ready", "Ready");
                    return;
                }

                const formData = new FormData();
                formData.append("audio", blob, `answer.${ext}`);

                setStatus("Transcribing your answer...");
                setState("transcribing", "Transcribing");

                try {
                    const response = await fetch("/api/transcribe", {
                        method: "POST",
                        body: formData
                    });

                    const data = await response.json();

                    if (!response.ok) {
                        throw new Error(data.error || "Transcription failed");
                    }

                    answer.value = data.transcript.trim();
                    answer.setCustomValidity("");

                    setStatus("Transcription completed. Review your answer.");
                    setState("review", "Ready to submit");
                } catch (error) {
                    setState("ready", "Ready");
                    setStatus("STT error: " + error.message);
                } finally {
                    setCaptureBusy(false);
                }
            };

            recorder.start();
            recordBtn.disabled = true;
            stopBtn.disabled = false;
            setState("recording", "Recording");
            setStatus("Recording your answer. Stop recording when you’re ready.");

        } catch (error) {
            if (stream) {
                stream.getTracks().forEach(track => track.stop());
                stream = null;
            }
            setCaptureBusy(false);
            setState("ready", "Ready");
            stopBtn.disabled = true;
            setStatus(error.name === "NotAllowedError"
                ? "Microphone permission denied. Allow access in your browser, or type your answer."
                : "Microphone error: " + error.message);
        }
    });

    stopBtn.addEventListener("click", () => {
        if (recorder && recorder.state === "recording") {
            stopBtn.disabled = true;
            recorder.stop();
            setState("transcribing", "Processing audio");
            setStatus("Processing recording...");
        }
    });

    window.addEventListener("pagehide", () => {
        stopAudio();
        if (stream) {
            stream.getTracks().forEach(track => track.stop());
        }
    });

    window.addEventListener("pageshow", event => {
        if (event.persisted && form.dataset.submitting === "true") {
            stopAudio();
            setCaptureBusy(false);
            stopBtn.disabled = true;
            setState("ready", "Ready");
            setStatus("Voice ready");
        }
    });
});
