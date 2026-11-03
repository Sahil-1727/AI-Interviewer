/* Browser capture only; the API key stays on the server. */
class DeepgramCapture {
    constructor(options) {
        this.options = options;
        this.cancelled = false;
        this.closing = false;
        this.text = '';
        this.interim = '';
        this.lastWords = null;
        this.sawWords = false;
        this.started = options.started || performance.now();
    }

    async start() {
        const o = this.options;
        this.stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
        if (this.cancelled) { this.stream.getTracks().forEach(t => t.stop()); return; }
        this.stream.getAudioTracks().forEach(track => track.addEventListener('ended', () => {
            if (!this.cancelled && !this.closing) this.fail('Microphone disconnected. Resume to retry.');
        }));
        const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'].find(x => MediaRecorder.isTypeSupported(x));
        if (!mime) throw new Error('Streaming audio is unsupported by this browser. Use manual mode.');
        this.recorder = new MediaRecorder(this.stream, {mimeType: mime});
        this.pendingAudio = [];
        this.pendingBytes = 0;
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.socket = new WebSocket(`${protocol}//${location.host}/api/voice/stream`);
        this.socket.binaryType = 'arraybuffer';
        this.recorder.ondataavailable = event => {
            if (!event.data.size || this.cancelled) return;
            if (this.providerReady && this.socket.readyState === WebSocket.OPEN) {
                if (this.socket.bufferedAmount > 1024 * 1024) return this.fail('Speech connection is too slow. Resume and repeat any missing words.');
                this.socket.send(event.data);
            }
            else {
                this.pendingBytes += event.data.size;
                if (this.pendingBytes > 1024 * 1024) return this.fail('Speech connection is too slow. Resume to retry.');
                this.pendingAudio.push(event.data);
            }
        };
        this.recorder.onstop = () => {
            this.releaseMicrophone();
            if (!this.cancelled && this.providerReady && this.socket.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({type: 'finish'}));
        };
        this.recorder.start(250);
        this.socket.onopen = () => this.socket.send(JSON.stringify({csrf: document.querySelector('meta[name="csrf-token"]').content, turn_id: o.turnId}));
        this.socket.onerror = () => this.fail('Speech streaming connection failed. Resume to retry.');
        this.socket.onclose = () => { if (!this.cancelled) this.fail('Speech streaming disconnected. Resume to retry.'); };
        this.socket.onmessage = event => {
            if (this.cancelled) return;
            let data;
            try { data = JSON.parse(event.data); } catch (_) { return this.fail('Invalid speech service response.'); }
            if (data.type === 'ready') {
                if (this.closing) return;
                clearTimeout(this.timeout);
                this.providerReady = true;
                for (const audio of this.pendingAudio) this.socket.send(audio);
                this.pendingAudio = [];
                this.pendingBytes = 0;
                // Allow buffered speech time to be recognized after a slow handshake.
                this.started = Math.max(this.started, performance.now() - Math.max(0, o.config.INITIAL_ANSWER_TIMEOUT_SECONDS - 3) * 1000);
                o.onState('thinking', 'Listening for your answer.');
                this.timer = setInterval(() => this.tick(), 100);
            } else if (data.type === 'transcript') {
                if (typeof data.transcript !== 'string' || typeof data.interim !== 'string') return this.fail('Invalid speech service response.');
                const changed = data.transcript !== this.text || (data.interim && data.interim !== this.interim);
                this.text = data.transcript;
                this.interim = data.interim;
                if (data.recognized) this.sawWords = true;
                if (data.recognized && changed && !this.closing) {
                    this.lastWords = performance.now();
                    o.onState('listening', 'Listening. Natural pauses are welcome.');
                }
                o.onTranscript(this.text, this.interim);
                if (!this.closing && data.final && data.command_confident && /(?:^|[.!?]\s+)(?:i['’]m done|i am done|repeat the question|please repeat the question|can you repeat the question)[.!?\s]*$/i.test(this.text)) this.finish('command');
            } else if (data.type === 'complete') {
                if (!this.closing || typeof data.transcript !== 'string') return this.fail('Unexpected speech service response.');
                this.cancelled = true;
                clearTimeout(this.timeout);
                this.socket.close();
                o.onComplete(data.transcript, this.reason, this.sawWords);
                this.resolveFinished?.();
            } else if (data.type === 'error') this.fail(data.message || 'Speech streaming failed.');
        };
        this.timeout = setTimeout(() => this.fail('Speech streaming timed out. Resume to retry.'), (o.config.DEEPGRAM_CONNECT_TIMEOUT_SECONDS + 5) * 1000);
    }

    tick() {
        if (this.closing || this.cancelled) return;
        const now = performance.now(), c = this.options.config;
        if (now - this.started >= c.MAX_CAPTURE_SECONDS * 1000) return this.fail('Recording limit reached. Resume to continue.');
        if (this.lastWords === null && now - this.started >= c.INITIAL_ANSWER_TIMEOUT_SECONDS * 1000) this.finish('unanswered');
        else if (this.lastWords !== null && now - this.lastWords >= c.END_OF_ANSWER_SILENCE_SECONDS * 1000) this.finish('silence');
    }

    finish(reason) {
        if (this.closing || this.cancelled) return;
        this.closing = true;
        this.reason = reason;
        clearInterval(this.timer);
        this.options.onState('transcribing', 'Finishing your transcript…');
        this.timeout = setTimeout(() => this.fail('Speech finalization timed out. Resume to retry.'), (this.options.config.DEEPGRAM_FINALIZE_TIMEOUT_SECONDS + 2) * 1000);
        if (this.recorder?.state === 'recording') this.recorder.stop();
        else this.fail('Speech streaming was not ready. Resume to retry.');
    }

    releaseMicrophone() { this.stream?.getTracks().forEach(t => t.stop()); }
    fail(message) {
        if (this.cancelled) return;
        this.cancel();
        this.options.onError(message);
        this.resolveFinished?.();
    }
    finishAndWait(reason) {
        if (this.cancelled) return Promise.resolve();
        if (this.closing) return new Promise(resolve => { this.resolveFinished = resolve; });
        if (!this.providerReady || !this.recorder || this.recorder.state !== 'recording') { this.cancel(); return Promise.resolve(); }
        return new Promise(resolve => { this.resolveFinished = resolve; this.finish(reason); });
    }
    cancel() {
        this.cancelled = true;
        clearInterval(this.timer);
        clearTimeout(this.timeout);
        if (this.recorder?.state === 'recording') this.recorder.stop();
        this.releaseMicrophone();
        if (this.socket && this.socket.readyState < WebSocket.CLOSING) this.socket.close();
    }
}
window.DeepgramCapture = DeepgramCapture;
