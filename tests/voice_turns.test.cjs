// node --test tests/voice_turns.test.cjs — fake clock/audio/HTTP, no dependencies.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {TurnDetector} = require('../app/static/js/voice-turns.js');
const config = {
    INITIAL_THINKING_PROMPT_SECONDS: 15, INITIAL_ANSWER_TIMEOUT_SECONDS: 10, END_OF_ANSWER_SILENCE_SECONDS: 5,
    COMMAND_CHECKPOINT_SECONDS: 1,
    AUDIO_SAMPLE_INTERVAL_MS: 50, AUDIO_LEVEL_THRESHOLD: 0.025,
    MIN_ACTIVITY_SECONDS: 0.25, ACTIVITY_GAP_SECONDS: 0.2, PLAYBACK_SETTLE_SECONDS: 0.6,
    MAX_CAPTURE_SECONDS: 600, REQUEST_TIMEOUT_SECONDS: 90,
    SEGMENT_SECONDS: 25, MAX_PENDING_SEGMENTS: 8
};

function speech(detector, start = 0) {
    for (let t = start; t <= start + 300; t += 50) detector.sample(0.1, t);
}

test('managed initial silence expires at ten seconds, speech cancels it', () => {
    const d = new TurnDetector(config, 0, true);
    assert.equal(d.sample(0, 9999), null);
    assert.equal(d.sample(0, 10000), 'unanswered');
    const spoken = new TurnDetector(config, 0, true);
    speech(spoken, 9500);
    assert.equal(spoken.sample(0, 10000), null);
    assert.equal(spoken.sample(0, 14800), 'finish');
});

test('initial thinking never finishes, even after repeated prompts', () => {
    const d = new TurnDetector(config, 0);
    assert.equal(d.sample(0, 14999), null);
    assert.equal(d.sample(0, 15000), 'initial-prompt');
    assert.equal(d.sample(0, 600000), null);
    d.keepThinking(600000);
    assert.equal(d.sample(0, 615000), 'initial-prompt');
    assert.equal(d.hasSpeech, false);
});

test('short impulses are not sustained activity', () => {
    const d = new TurnDetector(config, 0);
    assert.equal(d.sample(0.1, 0), 'sound');
    assert.equal(d.sample(0, 50), null);
    assert.equal(d.sample(0.1, 500), 'sound');
    assert.equal(d.sample(0, 550), null);
    assert.equal(d.hasSpeech, false);
});

test('speech with brief level drops is detected without counting silent gaps as speech', () => {
    const d = new TurnDetector(config, 0, true);
    for (let t = 0; t <= 1000; t += 50) d.sample(t % 200 < 100 ? 0.1 : 0, t);
    assert.equal(d.hasSpeech, true);
    const activity = d.lastActivity;
    assert.ok(activity >= 800);
    assert.notEqual(d.sample(0, activity + 4999), 'finish');
    assert.equal(d.sample(0, activity + 5000), 'finish');
});

test('five seconds of silence ends speech; resumed speech and noise are handled', () => {
    const d = new TurnDetector(config, 0);
    speech(d);
    assert.equal(d.sample(0, 1300), 'checkpoint');
    assert.equal(d.sample(0, 5299), null);
    assert.equal(d.sample(0, 5300), 'finish');
    const resumed = new TurnDetector(config, 0);
    speech(resumed);
    resumed.sample(0, 1300);
    speech(resumed, 4200);
    assert.equal(resumed.sample(0, 5300), null);
    assert.equal(resumed.sample(0, 9500), 'finish');
    const noisy = new TurnDetector(config, 0);
    speech(noisy);
    noisy.sample(0, 1300);
    noisy.sample(.1, 5000); // short accidental sound does not reset silence
    assert.equal(noisy.sample(0, 5300), 'finish');
});

class Target {
    constructor() { this.events = {}; this.dataset = {}; this.disabled = false; this.value = ''; this.textContent = ''; this.hidden = false; }
    addEventListener(type, fn) { (this.events[type] ||= []).push(fn); }
    emit(type, event = {}) { for (const fn of this.events[type] || []) fn(event); }
    click() { if (!this.disabled) this.emit('click'); }
    setCustomValidity() {}
}

function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; }
async function flush() { for (let i = 0; i < 30; i++) await Promise.resolve(); }

function browser({auto = true, level = 0, permission, stt, tts, settings = {}} = {}) {
    let now = 0, timerId = 0;
    const timers = new Map();
    const elements = Object.fromEntries(['answer', 'automatic-voice', 'start-recording', 'stop-recording', 'pause-voice',
        'speak-question', 'stop-speaking', 'voice-config', 'interviewer-state', 'voice-status', 'thinking-prompt',
        'thinking-message', 'interview-question', 'keep-thinking'].map(id => [id, new Target()]));
    const form = new Target(), submit = new Target();
    form.querySelector = () => submit;
    elements.answer.form = form;
    elements['automatic-voice'].checked = auto;
    elements['voice-config'].textContent = JSON.stringify({...config, ...settings});
    elements['interview-question'].textContent = 'Example question';
    const document = new Target(), window = new Target();
    document.body = new Target();
    document.getElementById = id => elements[id];
    document.dispatchEvent = event => document.emit(event.type, event);
    let energy = level;
    const streams = [], contexts = [], players = [], requests = [], recorders = [], objectUrls = new Set();
    const stream = () => {
        const track = new Target(); track.stopped = false; track.stop = () => { track.stopped = true; };
        const s = {track, getTracks: () => [track], getAudioTracks: () => [track]};
        streams.push(s); return s;
    };
    const schedule = (fn, ms, repeat) => { const id = ++timerId; timers.set(id, {fn, ms, at: now + ms, repeat}); return id; };
    class Recorder {
        static isTypeSupported(mime) { return mime.includes('webm'); }
        constructor(stream, opts) { this.stream = stream; this.mimeType = opts?.mimeType || 'audio/webm'; this.state = 'inactive'; recorders.push(this); }
        start() { this.state = 'recording'; }
        stop() {
            assert.equal(this.state, 'recording', 'each recording stops only once');
            this.state = 'inactive';
            queueMicrotask(() => { this.ondataavailable({data: new Blob(['segment'])}); this.onstop(); });
        }
    }
    class Context {
        constructor() { this.state = 'running'; contexts.push(this); }
        async resume() {}
        async close() { this.state = 'closed'; }
        createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
        createAnalyser() { return {fftSize: 2048, disconnect() {}, getFloatTimeDomainData(array) { array.fill(energy); }}; }
    }
    window.AudioContext = Context;
    class Player {
        constructor() { players.push(this); }
        async play() { assert.ok(streams.every(s => s.track.stopped), 'microphone is off during TTS'); }
        pause() { this.paused = true; }
    }
    const sandbox = {
        document, window, CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
        performance: {now: () => now}, VoiceTurns: {TurnDetector},
        navigator: {mediaDevices: {getUserMedia: () => permission ? permission.promise : Promise.resolve(stream())}},
        MediaRecorder: Recorder, Blob, FormData, AbortController, Audio: Player,
        URL: {createObjectURL() { const url = `blob:${objectUrls.size}`; objectUrls.add(url); return url; }, revokeObjectURL(url) { objectUrls.delete(url); }},
        setInterval: (fn, ms) => schedule(fn, ms, true), clearInterval: id => timers.delete(id),
        setTimeout: (fn, ms) => schedule(fn, ms, false), clearTimeout: id => timers.delete(id),
        fetch: async (url, options) => {
            requests.push({url, options});
            if (url === '/api/synthesize') {
                if (tts) return tts.promise;
                return {ok: true, blob: async () => new Blob(['tts'])};
            }
            assert.equal(url, '/api/transcribe', 'only STT/TTS endpoints are allowed');
            const result = typeof stt === 'function' ? await stt() : stt ? await stt.promise : {transcript: 'An example answer.', completion_detected: false};
            if (result.mockStatus) return {ok: false, status: result.mockStatus, json: async () => result};
            return {ok: true, status: 200, json: async () => result};
        }
    };
    vm.runInNewContext(fs.readFileSync(require.resolve('../app/static/js/voice.js'), 'utf8'), sandbox);
    document.emit('DOMContentLoaded');
    async function advance(ms) {
        const end = now + ms;
        while (true) {
            const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
            if (!due) break;
            const [id, t] = due;
            now = t.at;
            if (t.repeat) t.at += t.ms; else timers.delete(id);
            t.fn(); await flush();
        }
        now = end; await flush();
    }
    return {elements, form, submit, document, window, streams, contexts, players, requests, recorders, objectUrls,
        stream, advance, level: value => { energy = value; }, state: () => document.body.dataset.voiceState,
        async start() { elements['start-recording'].click(); await flush(); },
        async talk(ms = 500) { energy = .1; await advance(ms); energy = 0; }
    };
}

test('automatic initial silence waits without transcription; manual Done overrides energy detection', async () => {
    const b = browser(); await b.start(); await b.advance(60000);
    assert.equal(b.state(), 'thinking'); assert.equal(b.requests.length, 0);
    assert.equal(b.elements['thinking-prompt'].hidden, false);
    b.elements['stop-recording'].click(); await flush();
    assert.equal(b.requests.length, 1); assert.ok(b.streams[0].track.stopped);
    assert.ok(b.contexts.every(c => c.state === 'closed'));
});

test('managed silent capture releases microphone and emits unanswered once', async () => {
    const b = browser();
    const events = [];
    b.document.addEventListener('voice:unanswered', event => events.push(event.detail));
    b.window.InterviewVoice.setManaged(true);
    await b.window.InterviewVoice.listen();
    await b.advance(9950);
    assert.equal(events.length, 0);
    await b.advance(50);
    b.elements['stop-recording'].click();
    await flush();
    assert.equal(events.length, 1);
    assert.equal(b.requests.length, 0);
    assert.ok(b.streams[0].track.stopped);
});

test('five seconds after speech finishes one answer without duplicate uploads', async () => {
    const b = browser(); await b.start(); await b.talk(); await b.advance(1000);
    assert.equal(b.requests.length, 1); assert.equal(b.state(), 'possible_end');
    assert.equal(b.streams[0].track.stopped, false);
    await b.advance(3900);
    assert.equal(b.streams[0].track.stopped, false);
    await b.advance(100);
    assert.equal(b.state(), 'review'); assert.ok(b.streams[0].track.stopped);
    assert.equal(b.requests.length, 1);
    assert.equal(b.elements.answer.value, 'An example answer.');
});

test('a long answer with brief level drops stays open until five seconds of silence', async () => {
    const b = browser(); await b.start();
    for (let i = 0; i < 100; i++) {
        b.level(.1); await b.advance(100);
        b.level(0); await b.advance(100);
    }
    assert.equal(b.streams[0].track.stopped, false);
    await b.advance(5100);
    assert.equal(b.state(), 'review');
    assert.ok(b.streams[0].track.stopped);
});

test('speech resumed before five seconds cancels end-of-turn', async () => {
    const b = browser(); await b.start(); await b.talk(); await b.advance(4000);
    await b.talk(); await b.advance(4500);
    assert.equal(b.streams[0].track.stopped, false);
    await b.advance(500);
    assert.equal(b.state(), 'review'); assert.equal(b.requests.length, 2);
    assert.equal(b.elements.answer.value, 'An example answer. An example answer.');
});

test('VAD-rejected noise after speech does not extend the end-of-turn timer', async () => {
    let calls = 0;
    const b = browser({stt: async () => ++calls === 1
        ? {transcript: 'A real answer.', completion_detected: false}
        : {mockStatus: 400, error: 'No speech detected.'}});
    await b.start(); await b.talk(); await b.advance(2000);
    await b.talk(350); // a non-speech burst long enough for the energy gate
    await b.advance(1000); // server-side VAD rejects the burst
    await b.advance(1650); // five seconds after the real speech
    assert.equal(b.state(), 'review');
    assert.equal(b.elements.answer.value, 'A real answer.');
});

test('noise rejected by VAD never submits an existing draft as a new answer', async () => {
    const pending = deferred();
    const b = browser({stt: pending});
    b.elements.answer.value = 'A saved draft.';
    const answers = [], empty = [];
    b.document.addEventListener('voice:answer', event => answers.push(event.detail));
    b.document.addEventListener('voice:empty', event => empty.push(event.detail));
    await b.start(); await b.talk(350); await b.advance(6100);
    pending.resolve({mockStatus: 400, error: 'No speech detected.'});
    await flush(); await flush();
    assert.equal(answers.length, 0);
    assert.equal(empty.length, 1);
    assert.equal(b.elements.answer.value, 'A saved draft.');
});

test('recording continues during slow STT and stale completion is ignored', async () => {
    const pending = deferred(); const b = browser({stt: pending});
    await b.start(); await b.talk(); await b.advance(1000);
    assert.equal(b.requests.length, 1);
    await b.talk();
    pending.resolve({transcript: 'First part', completion_detected: true}); await flush();
    assert.equal(b.state(), 'listening'); assert.equal(b.streams[0].track.stopped, false);
    b.elements['stop-recording'].click(); b.elements['stop-recording'].click(); await flush();
    assert.equal(b.requests.length, 2); assert.equal(b.state(), 'review');
    assert.ok(b.streams[0].track.stopped);
});

test('recognized completion closes capture; double Done cannot duplicate transcription', async () => {
    const pending = deferred(); const b = browser({stt: pending});
    await b.start(); await b.talk(); await b.advance(1000);
    pending.resolve({transcript: 'Complete answer', completion_detected: true}); await flush();
    b.elements['stop-recording'].click(); await flush();
    assert.equal(b.state(), 'review'); assert.equal(b.requests.length, 1);
    assert.equal(b.elements.answer.value, 'Complete answer');
    assert.ok(b.streams[0].track.stopped);
});

test('spoken completion racing Done emits one answer event', async () => {
    const pending = deferred(); let calls = 0;
    const b = browser({stt: async () => ++calls === 1 ? pending.promise : {transcript: ''}});
    const answers = [];
    b.document.addEventListener('voice:answer', event => answers.push(event.detail.transcript));
    await b.start(); await b.talk(); await b.advance(1000);
    b.elements['stop-recording'].click();
    pending.resolve({transcript: 'A complete answer.', completion_detected: true});
    await flush(); await flush();
    assert.deepEqual(answers, ['A complete answer.']);
    assert.equal(b.state(), 'review');
    assert.ok(b.streams[0].track.stopped);
});

test('manual mode needs no Web Audio and records until Done; Pause/Resume appends', async () => {
    const b = browser({auto: false}); b.window.AudioContext = undefined;
    await b.start(); await b.advance(30000);
    assert.equal(b.state(), 'listening'); assert.equal(b.requests.length, 0);
    b.elements['pause-voice'].click(); await flush();
    assert.equal(b.state(), 'paused'); assert.ok(b.streams[0].track.stopped);
    b.elements['pause-voice'].click(); await flush();
    assert.equal(b.state(), 'listening');
    b.elements['stop-recording'].click(); b.elements['stop-recording'].click(); await flush();
    assert.equal(b.state(), 'review'); assert.equal(b.requests.length, 2);
    assert.equal(b.requests[0].options.body.get('detect_completion'), null);
    assert.equal(b.elements.answer.value, 'An example answer. An example answer.');
});

test('TTS keeps mic closed, then auto-listens only after ended plus settle time', async () => {
    const b = browser(); b.elements['speak-question'].click(); await flush();
    assert.equal(b.state(), 'speaking'); assert.equal(b.streams.length, 0);
    b.players[0].onended(); await b.advance(599);
    assert.equal(b.streams.length, 0);
    await b.advance(1); assert.equal(b.streams.length, 1); assert.equal(b.state(), 'thinking');
    assert.equal(b.objectUrls.size, 0);
});

test('repeat saves speech and releases microphone before any interviewer audio', async () => {
    const b = browser(); await b.start(); await b.talk();
    b.elements['speak-question'].click(); await flush();
    assert.equal(b.state(), 'speaking'); assert.ok(b.streams[0].track.stopped);
    assert.equal(b.elements.answer.value, 'An example answer.');
    b.elements['stop-speaking'].click(); await flush(); await b.advance(1000);
    assert.equal(b.state(), 'paused'); assert.equal(b.streams.length, 1);
});

test('late microphone permission is released after Pause', async () => {
    const permission = deferred(); const b = browser({permission}); await b.start();
    b.elements['pause-voice'].click(); await flush();
    permission.resolve(b.stream()); await flush();
    assert.equal(b.state(), 'paused'); assert.ok(b.streams[0].track.stopped);
    assert.ok(b.contexts.every(c => c.state === 'closed')); assert.equal(b.recorders.length, 0);
});

test('navigation aborts queued work, releases resources, and does not upload stopped recorder', async () => {
    const b = browser(); await b.start(); await b.talk();
    b.window.emit('pagehide'); await flush();
    assert.ok(b.streams.every(s => s.track.stopped)); assert.ok(b.contexts.every(c => c.state === 'closed'));
    assert.equal(b.requests.length, 0);
    b.window.emit('pageshow', {persisted: true}); assert.equal(b.state(), 'paused');
});

test('hidden page pauses instead of misreading throttled timers as silence', async () => {
    const b = browser(); await b.start(); await b.talk();
    b.document.hidden = true; b.document.emit('visibilitychange'); await flush();
    assert.equal(b.state(), 'paused'); assert.ok(b.streams[0].track.stopped);
});

test('keyboard submission blocked while permission or transcription is in flight', async () => {
    const pending = deferred(); const b = browser({stt: pending}); await b.start(); await b.talk();
    b.elements['stop-recording'].click(); await flush();
    let prevented = false;
    b.form.emit('submit', {preventDefault() { prevented = true; }});
    assert.equal(prevented, true); assert.equal(b.state(), 'transcribing');
    assert.ok(b.streams[0].track.stopped);
    pending.resolve({transcript: 'Answer', completion_detected: false}); await flush();
    assert.equal(b.submit.disabled, false);
});

test('STT failure releases capture and preserves existing transcript', async () => {
    const pending = deferred(); const b = browser({stt: pending});
    b.elements.answer.value = 'Existing edited answer.';
    await b.start(); await b.talk(); await b.advance(1000);
    pending.reject(new Error('Simulated unavailable STT')); await flush();
    assert.equal(b.state(), 'error'); assert.ok(b.streams[0].track.stopped);
    assert.equal(b.elements.answer.value, 'Existing edited answer.');
    assert.equal(b.submit.disabled, false);
});

test('permission denial restores manual typing and leaves no open audio context', async () => {
    const permission = deferred(); const b = browser({permission}); await b.start();
    permission.reject(Object.assign(new Error('denied'), {name: 'NotAllowedError'})); await flush();
    assert.equal(b.state(), 'error'); assert.equal(b.elements.answer.readOnly, false);
    assert.ok(b.contexts.every(c => c.state === 'closed'));
    assert.equal(b.elements['start-recording'].disabled, false);
});

test('navigation during repeat transcription cannot start late TTS', async () => {
    const pending = deferred(); const b = browser({stt: pending}); await b.start(); await b.talk();
    b.elements['speak-question'].click(); await flush();
    b.window.emit('pagehide');
    pending.resolve({transcript: 'Answer'}); await flush();
    assert.equal(b.requests.filter(r => r.url === '/api/synthesize').length, 0);
    assert.ok(b.streams.every(s => s.track.stopped));
});

test('Pause closes the pending audio context even before permission resolves', async () => {
    const permission = deferred(); const b = browser({permission}); await b.start();
    b.elements['pause-voice'].click(); await flush();
    assert.equal(b.contexts[0].state, 'closed');
    permission.resolve(b.stream()); await flush(); assert.ok(b.streams[0].track.stopped);
});

test('more-time command disarms endpointing until fresh speech begins', async () => {
    const b = browser({stt: async () => ({transcript: '', command: 'think'})});
    await b.start(); await b.talk(); await b.advance(1000);
    assert.equal(b.state(), 'thinking');
    await b.advance(60000);
    assert.equal(b.streams[0].track.stopped, false);
    assert.equal(b.requests.length, 1);
    assert.equal(b.elements.answer.value, '');
});

test('failed STT retries the saved audio without rerecording or losing previous text', async () => {
    let calls = 0;
    const b = browser({stt: async () => {
        if (++calls === 1) throw new Error('Temporary STT failure');
        return {transcript: 'Recovered speech.', completion_detected: false};
    }});
    b.elements.answer.value = 'Prior edited text.';
    await b.start(); await b.talk(); await b.advance(1000);
    assert.equal(b.state(), 'error'); assert.equal(b.window.InterviewVoice.hasRecovery(), true);
    await b.window.InterviewVoice.retryTranscription(); await flush();
    assert.equal(b.state(), 'review');
    assert.equal(b.elements.answer.value, 'Prior edited text. Recovered speech.');
    assert.equal(b.streams.length, 1, 'retry uses saved audio, not a fresh mic');
    assert.equal(b.requests.length, 2);
});

test('continuous speech rotates bounded segments without ending the answer', async () => {
    const b = browser(); await b.start(); b.level(.1); await b.advance(26000);
    assert.equal(b.requests.length, 1);
    assert.equal(b.state(), 'listening');
    assert.equal(b.streams[0].track.stopped, false);
    b.elements['pause-voice'].click(); await flush();
    assert.equal(b.requests.length, 2);
    assert.equal(b.state(), 'paused');
});

test('slow STT queue is bounded and pauses rather than truncating recorded speech', async () => {
    const pending = deferred();
    const b = browser({stt: pending, settings: {SEGMENT_SECONDS: 1, MAX_PENDING_SEGMENTS: 2}});
    await b.start(); b.level(.1); await b.advance(3100);
    assert.ok(b.streams[0].track.stopped);
    pending.resolve({transcript: 'A segment.'}); await flush(); await flush();
    assert.equal(b.state(), 'paused'); assert.equal(b.requests.length, 3);
    assert.equal(b.elements.answer.value, 'A segment. A segment. A segment.');
});

test('capture duration limit pauses initial silence without sending an empty turn', async () => {
    const b = browser({settings: {MAX_CAPTURE_SECONDS: 2}}); await b.start(); await b.advance(2100);
    assert.equal(b.state(), 'paused'); assert.equal(b.requests.length, 0);
    assert.ok(b.streams[0].track.stopped);
});

const {ConversationMachine} = require('../app/static/js/conversation.js');
test('explicit conversation state machine covers a full loop and rejects invalid jumps', () => {
    const m = new ConversationMachine();
    for (const next of ['PREPARING', 'AI_SPEAKING', 'THINKING', 'LISTENING', 'POSSIBLE_END_OF_TURN',
        'LISTENING', 'TRANSCRIBING', 'EVALUATING', 'PREPARING', 'AI_SPEAKING', 'THINKING',
        'PAUSED', 'PREPARING', 'THINKING', 'LISTENING', 'TRANSCRIBING', 'EVALUATING', 'COMPLETED']) m.move(next);
    assert.equal(m.state, 'COMPLETED');
    assert.throws(() => m.move('LISTENING'), /Invalid conversation transition/);
});

test('duplicate playback-ended callbacks open the microphone once', async () => {
    const b = browser(); b.elements['speak-question'].click(); await flush();
    const ended = b.players[0].onended;
    ended(); ended(); await b.advance(600);
    assert.equal(b.streams.length, 1);
});
