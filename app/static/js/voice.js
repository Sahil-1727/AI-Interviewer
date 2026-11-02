document.addEventListener("DOMContentLoaded", () => {
    const $ = id => document.getElementById(id);
    const answer = $("answer");
    const form = answer.form;
    const automatic = $("automatic-voice");
    const recordBtn = $("start-recording");
    const doneBtn = $("stop-recording");
    const pauseBtn = $("pause-voice");
    const speakBtn = $("speak-question");
    const stopSpeakBtn = $("stop-speaking");
    const config = JSON.parse($("voice-config").textContent);
    let phase = "ready";
    let managed = false;
    let sharedPlayer = null;
    let capture = null;
    let turn = null;
    let acquiring = false;
    let acquiringContext = null;
    let epoch = 0;
    let playback = null;
    let settleTimer = null;

    function closeContext(context) {
        if (context && context.state !== "closed") void context.close().catch(() => {});
    }

    function emit(name, detail = {}) {
        if (typeof document.dispatchEvent === "function") document.dispatchEvent(new CustomEvent(name, {detail}));
    }

    // Hardware capture owns segments/STT. The separate conversation controller
    // owns automatic evaluation, navigation and server turn identifiers.
    function controls() {
        const submitting = form.dataset.submitting === "true";
        const busy = acquiring || !!capture || !!settleTimer || !!turn?.pending || !!turn?.stopping;
        form.dataset.voiceBusy = String(busy);
        form.querySelector('[type="submit"]').disabled = busy || submitting || managed;
        answer.readOnly = busy;
        recordBtn.disabled = busy || !!playback || submitting;
        doneBtn.disabled = !capture || submitting;
        pauseBtn.disabled = submitting || (!capture && !acquiring && !settleTimer && phase !== "paused" && !playback);
        pauseBtn.textContent = phase === "paused" ? "Resume" : "Pause";
        speakBtn.disabled = acquiring || !!turn?.stopping || !!turn?.pending || !!playback || submitting;
        automatic.disabled = busy || !!playback || submitting;
        stopSpeakBtn.disabled = !playback;
    }

    function state(next, message) {
        phase = next;
        document.body.dataset.voiceState = next;
        $("interviewer-state").textContent = {
            ready: "Ready", speaking: "Speaking", thinking: "Thinking",
            listening: "Listening", paused: "Paused", transcribing: "Transcribing",
            error: "Voice error", review: "Ready to submit", possible_end: "Thinking"
        }[next];
        if (message) $("voice-status").textContent = message;
        controls();
        emit("voice:state", {state: next, message});
    }

    function prompt(message) {
        $("thinking-prompt").hidden = !message;
        $("thinking-message").textContent = message || "";
    }

    function stopAudio() {
        if (settleTimer !== null) clearTimeout(settleTimer);
        settleTimer = null;
        if (playback) {
            playback.controller.abort();
            clearTimeout(playback.timeout);
            if (playback.player) {
                playback.player.onended = playback.player.onerror = null;
                playback.player.pause();
                playback.player.src = "";
            }
            if (playback.url) URL.revokeObjectURL(playback.url);
            playback = null;
        }
        controls();
    }

    function releaseCapture() {
        if (!capture) return;
        clearInterval(capture.timer);
        capture.source?.disconnect();
        capture.analyser?.disconnect();
        closeContext(capture.context);
        capture.stream.getTracks().forEach(track => track.stop());
        capture = null;
    }

    function updateTranscript(t) {
        // Fields are read-only while work is in flight, so asynchronous STT
        // cannot overwrite a candidate's edits. A resumed turn appends to them.
        answer.value = [t.baseline, ...t.parts].filter(Boolean).join(" ");
        answer.setCustomValidity("");
        emit("voice:draft", {transcript: answer.value});
    }

    function fail(t, error) {
        if (t !== turn || t.cancelled || t.failed) return;
        t.failed = true;
        t.request?.abort();
        if (capture?.segment.hasActivity) t.recovery.add(capture.segment);
        if (capture?.segment.recorder.state !== "inactive") capture?.segment.recorder.stop();
        releaseCapture();
        prompt(null);
        state("error", "Voice error: " + error.message + " Your existing transcript is kept; resume recording or type your answer.");
        emit("voice:error", {error: error.message});
    }

    function startSegment(t) {
        const formats = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"];
        const mimeType = formats.find(mime => MediaRecorder.isTypeSupported(mime));
        const recorder = mimeType ? new MediaRecorder(capture.stream, {mimeType}) : new MediaRecorder(capture.stream);
        const segment = {recorder, chunks: [], hasActivity: !t.auto, started: performance.now(), sequence: t.nextSequence++};
        segment.ready = new Promise((resolve, reject) => {
            recorder.ondataavailable = event => { if (event.data.size) segment.chunks.push(event.data); };
            recorder.onstop = () => resolve(new Blob(segment.chunks, {type: recorder.mimeType}));
            recorder.onerror = () => {
                reject(new Error("Recording interrupted."));
                fail(t, new Error("Recording interrupted."));
            };
        });
        // Recorder errors can occur before the segment enters the upload queue.
        segment.ready.catch(() => {});
        recorder.start();
        capture.segment = segment;
    }

    function uploadAudio(data, signal) {
        // Fetch has no upload-complete event. XHR measures browser upload time
        // separately from the full STT roundtrip, without inventing a latency.
        if (typeof XMLHttpRequest === "undefined") return fetch("/api/transcribe", {method: "POST", body: data, signal});
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            const started = performance.now();
            const abort = () => xhr.abort();
            xhr.open("POST", "/api/transcribe");
            xhr.responseType = "json";
            xhr.upload.onload = () => emit("voice:timing", {stage: "audio_upload_ms", ms: performance.now() - started});
            xhr.onload = () => resolve({ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, json: async () => xhr.response});
            xhr.onerror = () => reject(new Error("Audio upload failed. Check your connection."));
            xhr.onabort = () => reject(new Error("Audio upload cancelled or timed out."));
            xhr.onloadend = () => signal.removeEventListener("abort", abort);
            signal.addEventListener("abort", abort, {once: true});
            if (signal.aborted) { reject(new Error("Audio upload cancelled.")); return; }
            xhr.send(data);
        });
    }

    function enqueueSegment(t, savedSegment = null) {
        const segment = savedSegment || capture.segment;
        if (!savedSegment) {
            segment.revision = t.revision;
            segment.activityEnd = t.detector.lastActivity;
        }
        t.pending++;
        // Enqueue before stopping: a synchronous Done/timeout/voice-command
        // race still has exactly one owner for each blob.
        t.queue = t.queue.then(async () => {
            const blob = await segment.ready;
            if (t.cancelled || !segment.hasActivity || !blob.size) return;
            if (t.failed) { t.recovery.add(segment); return; }
            const controller = new AbortController();
            t.request = controller;
            const uploadedAt = performance.now();
            const timeout = setTimeout(() => controller.abort(), config.REQUEST_TIMEOUT_SECONDS * 1000);
            try {
                const data = new FormData();
                const mime = segment.recorder.mimeType;
                const ext = mime.includes("mp4") ? "mp4" : mime.includes("ogg") ? "ogg" : "webm";
                data.append("audio", blob, `answer.${ext}`);
                if (t.auto) data.append("detect_completion", "true");
                const response = await uploadAudio(data, controller.signal);
                const result = await response.json();
                if (t.cancelled || t.failed) return;
                // Energy may be noise. Whisper's VAD can reject such a segment.
                if (t.auto && response.status === 400 && result.error === "No speech detected.") {
                    if (capture && t.revision === segment.revision) {
                        if (t.lastValidatedEnd === null) {
                            t.detector.waitForSpeech(performance.now());
                            state("thinking", "Take your time. Waiting for a clear answer.");
                        } else {
                            // A later non-speech burst should not extend the
                            // candidate's last VAD-confirmed speaking time.
                            t.detector.lastActivity = t.lastValidatedEnd;
                        }
                    }
                    return;
                }
                if (!response.ok) throw new Error(result.error || "Transcription failed.");
                if (typeof result.transcript !== "string") throw new Error("Invalid transcription response.");
                t.lastValidatedEnd = segment.activityEnd;
                t.confirmedSpeech = true;
                t.awaitingSpeech = false;
                emit("voice:timing", {stage: "upload_stt_roundtrip_ms", ms: performance.now() - uploadedAt});
                if (typeof result.transcription_ms === "number") emit("voice:timing", {stage: "transcription_ms", ms: result.transcription_ms});
                if (result.transcript.trim()) t.parts.push(result.transcript.trim());
                updateTranscript(t);
                // Never act on an old completion command after speech resumes.
                if (t.auto && !t.stopping && capture && t.revision === segment.revision) {
                    if (result.completion_detected === true) void finish("review");
                    else if (result.command === "think") {
                        t.detector.waitForSpeech(performance.now());
                        t.lastValidatedEnd = null;
                        t.awaitingSpeech = true;
                        prompt("Of course. Take your time — I'm here when you're ready.");
                        state("thinking", "Take your time. Nothing will advance until you continue.");
                    } else if (result.command === "repeat") {
                        if (managed) emit("voice:repeat"); else void speakQuestion();
                    }
                }
            } finally {
                clearTimeout(timeout);
                if (t.request === controller) t.request = null;
            }
        }).catch(error => {
            if (!t.cancelled) t.recovery.add(segment);
            fail(t, error);
        }).finally(() => {
            t.pending--;
            controls();
        });
        if (!savedSegment) segment.recorder.stop();
    }

    async function finish(destination, force = false) {
        if (!capture || !turn || turn.stopping) return;
        const t = turn;
        t.stopping = true;
        prompt(null);
        // A manual completion must work even for quiet speech below the energy
        // threshold. Whisper's VAD, rather than RMS, decides whether it is speech.
        if (force) capture.segment.hasActivity = true;
        enqueueSegment(t);
        releaseCapture(); // Release hardware BEFORE waiting for Python STT.
        state("transcribing", "Transcribing your answer…");
        if (destination === "review") emit("voice:endpoint", {lastActivity: t.detector.lastActivity, at: performance.now()});
        await t.queue;
        t.stopping = false;
        if (turn !== t || t.cancelled) return;
        if (t.failed) { controls(); return; }
        state(destination, destination === "paused"
            ? "Microphone paused. Resume when you're ready; your transcript is kept."
            : t.parts.length ? "Transcript ready. Review and submit it when you're ready, or record more."
                : "No new speech detected. Record again or type your answer.");
        if (destination === "review") {
            const usable = answer.value.trim() && (force || !t.auto || (t.confirmedSpeech && !t.awaitingSpeech));
            emit(usable ? "voice:answer" : "voice:empty", {transcript: usable ? answer.value.trim() : "", captureId: t.id});
        }
    }

    async function startCapture(append = false) {
        if (capture || acquiring || playback || settleTimer !== null || turn?.pending || turn?.stopping || form.dataset.submitting === "true") return;
        const token = ++epoch;
        const t = {id: token, auto: automatic.checked, baseline: append ? answer.value.trim() : "", parts: [],
            queue: Promise.resolve(), pending: 0, revision: 0, stopping: false, cancelled: false, failed: false, noiseFloor: 0,
            recovery: new Set(), nextSequence: 0, lastValidatedEnd: null, confirmedSpeech: false, awaitingSpeech: false};
        turn = t;
        acquiring = true;
        prompt(null);
        state("thinking", "Allow microphone access. Take your time before answering.");
        let stream;
        let context;
        try {
            if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
                throw new Error("Microphone recording is unsupported; type your answer instead.");
            }
            if (t.auto) {
                const AudioContext = window.AudioContext || window.webkitAudioContext;
                if (!AudioContext) throw new Error("Automatic audio detection is unsupported. Turn off automatic mode to record manually.");
                context = new AudioContext();
                acquiringContext = context;
                await context.resume();
            }
            if (token !== epoch) { closeContext(context); return; }
            stream = await navigator.mediaDevices.getUserMedia({audio: {
                echoCancellation: true, noiseSuppression: true, autoGainControl: true
            }});
            // Permission may resolve after Pause, navigation, or a cancelled turn.
            if (token !== epoch) {
                stream.getTracks().forEach(track => track.stop());
                closeContext(context);
                return;
            }
            const now = performance.now();
            capture = {stream, context, started: now};
            acquiringContext = null;
            t.detector = new VoiceTurns.TurnDetector(config, now);
            if (t.auto) {
                capture.source = context.createMediaStreamSource(stream);
                capture.analyser = context.createAnalyser();
                capture.analyser.fftSize = 2048;
                capture.source.connect(capture.analyser); // No connection to speakers.
                capture.samples = new Float32Array(capture.analyser.fftSize);
            }
            startSegment(t);
            stream.getAudioTracks().forEach(track => track.addEventListener("ended", () => {
                if (capture && turn === t) fail(t, new Error("Microphone disconnected."));
            }));
            acquiring = false;
            state(t.auto ? "thinking" : "listening", t.auto
                ? "Listening when you're ready. Initial silence won't finish your answer."
                : "Recording. Select Done Speaking to transcribe.");
            capture.timer = setInterval(() => sample(t), config.AUDIO_SAMPLE_INTERVAL_MS);
        } catch (error) {
            stream?.getTracks().forEach(track => track.stop());
            closeContext(context);
            if (token !== epoch) return;
            acquiring = false;
            releaseCapture();
            state("error", error.name === "NotAllowedError"
                ? "Microphone permission denied. Allow access in your browser or type your answer."
                : error.message);
            emit("voice:error", {error: error.message});
        } finally {
            if (token === epoch) { acquiring = false; acquiringContext = null; controls(); }
        }
    }

    function sample(t) {
        if (!capture || t !== turn || t.stopping || t.failed) return;
        try {
            const now = performance.now();
            if (now - capture.started >= config.MAX_CAPTURE_SECONDS * 1000) {
                void finish("paused");
                emit("voice:limit", {message: "The capture time limit was reached. Your transcript is saved; resume to continue the same answer."});
                return;
            }
            // Bound each independent file even during continuous explanations
            // or indefinite initial silence. Never discard voiced segments.
            if (t.auto && now - capture.segment.started >= config.SEGMENT_SECONDS * 1000) {
                if (t.pending >= config.MAX_PENDING_SEGMENTS) {
                    void finish("paused");
                    emit("voice:limit", {message: "Transcription is falling behind. Your answer is saved; resume to continue."});
                    return;
                }
                enqueueSegment(t);
                startSegment(t);
            }
            if (!t.auto) return;
            capture.analyser.getFloatTimeDomainData(capture.samples);
            const rms = Math.sqrt(capture.samples.reduce((sum, value) => sum + value * value, 0) / capture.samples.length);
            // Adapt only BELOW the base threshold; speech must never train the
            // floor upward. RMS remains a coarse gate, not semantic/real VAD.
            if (rms < config.AUDIO_LEVEL_THRESHOLD && config.NOISE_FLOOR_SMOOTHING) {
                t.noiseFloor += config.NOISE_FLOOR_SMOOTHING * (rms - t.noiseFloor);
            }
            const threshold = Math.max(config.AUDIO_LEVEL_THRESHOLD, t.noiseFloor * (config.NOISE_FLOOR_MULTIPLIER || 1));
            const level = rms >= threshold ? rms : 0;
            emit("voice:level", {level: Math.min(1, rms / config.AUDIO_LEVEL_THRESHOLD)});
            const event = t.detector.sample(level, now);
            if (event === "sound" || event === "activity") {
                t.revision++;
                prompt(null);
                if (event === "activity") {
                    capture.segment.hasActivity = true;
                    if (phase !== "listening") state("listening", "Listening. Natural pauses are welcome.");
                }
            } else if (event === "initial-prompt") {
                prompt("Still thinking? Take your time — nothing will be sent until you speak or select Done Speaking.");
            } else if (event === "checkpoint") {
                state("possible_end", "A pause is fine. Listening for more…");
                // Independent, playable segments are uploaded ONCE, in order.
                // Continue recording during STT so speech isn't lost to latency.
                if (capture.segment.hasActivity) {
                    if (t.pending >= config.MAX_PENDING_SEGMENTS) {
                        void finish("paused");
                        emit("voice:limit", {message: "Transcription is falling behind. Your audio is kept; pause and retry before continuing."});
                        return;
                    }
                    enqueueSegment(t);
                    startSegment(t);
                    controls();
                }
            } else if (event === "finish") {
                void finish("review");
            }
        } catch (error) { fail(t, error); }
    }

    async function speakQuestion() {
        if (playback || acquiring || turn?.pending || turn?.stopping || form.dataset.submitting === "true") return;
        const token = epoch;
        // Repeat saves the captured answer before playback; hardware cannot be
        // open during either synthesis or the interviewer's audio.
        if (capture) await finish("paused");
        if (token !== epoch || document.hidden || form.dataset.submitting === "true") return;
        stopAudio();
        prompt(null);
        const p = {controller: new AbortController(), requestedAt: performance.now()};
        playback = p;
        p.timeout = setTimeout(() => p.controller.abort(), config.REQUEST_TIMEOUT_SECONDS * 1000);
        state("thinking", "Preparing question audio…");
        try {
            const response = await fetch("/api/synthesize", {
                method: "POST", signal: p.controller.signal, headers: {"Content-Type": "application/json"},
                body: JSON.stringify({text: $("interview-question").textContent.trim()})
            });
            if (!response.ok) throw new Error("Question audio generation failed.");
            const blob = await response.blob();
            if (playback !== p) return;
            emit("voice:timing", {stage: "tts_request_ms", ms: performance.now() - p.requestedAt});
            const timing = response.headers?.get("Server-Timing")?.match(/tts;dur=([\d.]+)/);
            if (timing) emit("voice:timing", {stage: "tts_generation_ms", ms: Number(timing[1])});
            clearTimeout(p.timeout);
            p.url = URL.createObjectURL(blob);
            if (!sharedPlayer) sharedPlayer = new Audio();
            p.player = sharedPlayer;
            p.player.src = p.url;
            p.player.onended = () => {
                if (playback !== p) return;
                stopAudio();
                if (automatic.checked) {
                    // Allow speaker/reverb tails to settle before opening the mic.
                    settleTimer = setTimeout(() => {
                        settleTimer = null;
                        void startCapture(true);
                    }, config.PLAYBACK_SETTLE_SECONDS * 1000);
                    state("thinking", "Question finished. Getting ready to listen…");
                } else state("ready", "Question finished. Record or type your answer.");
            };
            p.player.onerror = () => {
                if (playback !== p) return;
                stopAudio();
                state("error", "Audio playback failed. Read the question and select Record answer.");
            };
            const playRequestedAt = performance.now();
            await p.player.play();
            if (playback === p) {
                emit("voice:timing", {stage: "audio_playback_start_ms", ms: performance.now() - playRequestedAt});
                state("speaking", "Your interviewer is speaking. Your microphone is off.");
                emit("voice:playback", {at: performance.now()});
            }
        } catch (error) {
            if (playback !== p) return;
            stopAudio();
            state("error", "Question audio unavailable. Read the question and select Record answer. " + error.message);
        }
    }

    function cancelPendingCapture() {
        ++epoch;
        acquiring = false;
        closeContext(acquiringContext);
        acquiringContext = null;
    }

    async function retryTranscription() {
        const t = turn;
        if (!t?.recovery.size || t.cancelled) return false;
        await t.queue;
        const segments = [...t.recovery].sort((a, b) => a.sequence - b.sequence);
        t.recovery.clear();
        t.baseline = answer.value.trim();
        t.parts = [];
        t.failed = false;
        t.stopping = true;
        t.queue = Promise.resolve();
        state("transcribing", "Retrying your saved audio. Your existing transcript is kept.");
        for (const segment of segments) enqueueSegment(t, segment);
        await t.queue;
        t.stopping = false;
        if (!t.cancelled && turn === t && !t.failed) {
            state("review", "Transcript recovered.");
            emit(answer.value.trim() ? "voice:answer" : "voice:empty", {transcript: answer.value.trim(), captureId: t.id});
        }
        return true;
    }

    async function pause() {
        cancelPendingCapture();
        stopAudio();
        if (capture) await finish("paused");
        else { prompt(null); state("paused", "Paused. Resume when you're ready."); }
    }

    function discard() {
        cancelPendingCapture();
        stopAudio();
        if (turn) {
            turn.cancelled = true;
            turn.request?.abort();
        }
        if (capture?.segment.recorder.state === "recording") capture.segment.recorder.stop();
        releaseCapture();
        prompt(null);
        state("paused", "Interview ended. Preparing your result.");
    }

    automatic.addEventListener("change", () => {
        if (automatic.checked) void speakQuestion();
    });
    speakBtn.addEventListener("click", () => managed ? emit("voice:repeat") : void speakQuestion());
    stopSpeakBtn.addEventListener("click", () => {
        if (managed) { emit("voice:toggle-pause"); return; }
        stopAudio();
        state("paused", "Question audio stopped. Resume or select Record answer when ready.");
    });
    recordBtn.addEventListener("click", () => managed ? emit("voice:resume") : void startCapture(automatic.checked || phase === "paused"));
    doneBtn.addEventListener("click", () => void finish("review", true));
    pauseBtn.addEventListener("click", () => managed ? emit("voice:toggle-pause") : phase === "paused" ? void startCapture(true) : void pause());
    $("keep-thinking").addEventListener("click", () => {
        if (!capture) return;
        turn.detector.keepThinking(performance.now());
        prompt(null);
        state("thinking", "Take your time. Still listening.");
    });

    // Capture listener runs before ui.js's normal submission lock.
    form.addEventListener("submit", event => {
        if (managed || form.dataset.voiceBusy === "true") { event.preventDefault(); return; }
        stopAudio();
    }, true);

    document.addEventListener("visibilitychange", () => {
        // Background timer throttling must not masquerade as a long silence.
        if (document.hidden && (capture || acquiring || playback || settleTimer !== null)) {
            if (managed) emit("voice:hide"); else void pause();
        }
    });
    window.addEventListener("pagehide", () => {
        cancelPendingCapture();
        stopAudio();
        if (turn) {
            turn.cancelled = true;
            turn.request?.abort();
        }
        if (capture?.segment.recorder.state === "recording") capture.segment.recorder.stop();
        releaseCapture();
    });
    window.addEventListener("pageshow", event => {
        if (event.persisted) {
            turn = null;
            prompt(null);
            state("paused", "Voice paused after returning to this page. Resume when ready.");
        }
    });
    window.InterviewVoice = {
        setManaged(value) { managed = value; automatic.checked = value; controls(); },
        async unlock() {
            // Reuse one element across questions, including Safari's playback
            // permission. This silent WAV is played inside the initial click.
            if (!sharedPlayer) sharedPlayer = new Audio();
            sharedPlayer.src = "data:audio/wav;base64,UklGRnQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YVAAAACAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgA==";
            try { await sharedPlayer.play(); } catch (_) { /* Real playback offers recovery. */ }
            sharedPlayer.pause();
            sharedPlayer.src = "";
        },
        speak: speakQuestion,
        listen: () => startCapture(true),
        pause,
        discard,
        resetSilence() { if (capture && turn) turn.detector.keepThinking(performance.now()); },
        finish: () => finish("review", true),
        isCapturing: () => !!capture || acquiring,
        isBusy: () => !!capture || acquiring || !!turn?.pending || !!turn?.stopping,
        hasRecovery: () => !!turn?.recovery.size,
        retryTranscription,
        reset() { turn = null; prompt(null); state("ready", "Ready for the next question."); },
    };
    state("ready", "Start a hands-free interview, or use the manual controls.");
});
