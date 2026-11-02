
document.addEventListener("DOMContentLoaded", () => {
    const question = document.getElementById("interview-question");
    const answer = document.getElementById("answer");
    const status = document.getElementById("voice-status");

    const speakBtn = document.getElementById("speak-question");
    const stopSpeakBtn = document.getElementById("stop-speaking");
    const recordBtn = document.getElementById("start-recording");
    const stopBtn = document.getElementById("stop-recording");

    let recorder = null;
    let stream = null;
    let chunks = [];
    let audioPlayer = null;
    let audioUrl = null;

    function setStatus(message) {
        status.textContent = message;
    }

    function stopAudio() {
        if (audioPlayer) {
            audioPlayer.pause();
            audioPlayer.currentTime = 0;
        }
        if (audioUrl) {
            URL.revokeObjectURL(audioUrl);
            audioUrl = null;
        }
    }

    // Python TTS
    speakBtn.addEventListener("click", async () => {
        stopAudio();
        speakBtn.disabled = true;
        setStatus("Generating AI voice...");

        try {
            const response = await fetch("/api/synthesize", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    text: question.textContent.trim()
                })
            });

            if (!response.ok) {
                throw new Error("Voice generation failed");
            }

            const blob = await response.blob();
            audioUrl = URL.createObjectURL(blob);
            audioPlayer = new Audio(audioUrl);

            audioPlayer.onended = () => setStatus("Question finished.");
            await audioPlayer.play();

            setStatus("AI is speaking...");
        } catch (error) {
            setStatus("TTS error: " + error.message);
        } finally {
            speakBtn.disabled = false;
        }
    });

    stopSpeakBtn.addEventListener("click", () => {
        stopAudio();
        setStatus("Audio stopped.");
    });

    // Python Whisper STT
    recordBtn.addEventListener("click", async () => {
        try {
            if (!navigator.mediaDevices?.getUserMedia ||
                typeof MediaRecorder === "undefined") {
                throw new Error("Microphone recording not supported.");
            }

            stopAudio();
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
                    recordBtn.disabled = false;
                    return;
                }

                const formData = new FormData();
                formData.append("audio", blob, `answer.${ext}`);

                setStatus("Whisper is transcribing...");

                try {
                    const response = await fetch("/api/transcribe", {
                        method: "POST",
                        body: formData
                    });

                    const data = await response.json();
                    console.log("Whisper response:", data.transcript);

                    if (!response.ok) {
                        throw new Error(data.error || "Transcription failed");
                    }

                    answer.value = data.transcript.trim();

                    setStatus("Transcription completed. Review your answer.");
                } catch (error) {
                    setStatus("STT error: " + error.message);
                } finally {
                    recordBtn.disabled = false;
                }
            };

            recorder.start();
            recordBtn.disabled = true;
            stopBtn.disabled = false;
            setStatus("🔴 Recording your answer...");

        } catch (error) {
            if (stream) {
                stream.getTracks().forEach(track => track.stop());
                stream = null;
            }
            recordBtn.disabled = false;
            stopBtn.disabled = true;
            setStatus("Microphone error: " + error.message);
        }
    });

    stopBtn.addEventListener("click", () => {
        if (recorder && recorder.state === "recording") {
            recorder.stop();
            setStatus("Processing recording...");
        }
    });

    window.addEventListener("pagehide", () => {
        stopAudio();
        if (stream) {
            stream.getTracks().forEach(track => track.stop());
        }
    });
});
