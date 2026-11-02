// node --test tests/voice_turns.test.cjs — fake clock/audio/HTTP, no dependencies.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {TurnDetector} = require('../app/static/js/voice-turns.js');
const config = {
    INITIAL_THINKING_PROMPT_SECONDS: 15, MID_ANSWER_PAUSE_SECONDS: 5,
    LONG_PAUSE_CONFIRMATION_SECONDS: 10, TURN_CONFIRMATION_GRACE_SECONDS: 10,
    AUDIO_SAMPLE_INTERVAL_MS: 50, AUDIO_LEVEL_THRESHOLD: 0.025,
    MIN_ACTIVITY_SECONDS: 0.25, PLAYBACK_SETTLE_SECONDS: 0.6,
    MAX_CAPTURE_SECONDS: 600, REQUEST_TIMEOUT_SECONDS: 90
};

function speech(detector, start = 0) {
    for (let t = start; t <= start + 300; t += 50) detector.sample(0.1, t);
}

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

test('5s checkpoint, 10s prompt, further 10s grace; speech or button resets finish', () => {
    const d = new TurnDetector(config, 0);
    speech(d);
    assert.equal(d.sample(0, 5299), null);
    assert.equal(d.sample(0, 5300), 'checkpoint');
    assert.equal(d.sample(0, 10300), 'confirmation');
    assert.equal(d.sample(0, 20299), null);
    assert.equal(d.sample(0, 20300), 'finish');
    d.keepThinking(20300);
    assert.equal(d.sample(0, 20301), null);
    assert.equal(d.sample(0, 30300), 'confirmation');
    speech(d, 31000);
    assert.equal(d.sample(0, 31301), null);
    assert.equal(d.sample(0, 36300), 'checkpoint');
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

function browser({auto = true, level = 0, permission, stt, tts} = {}) {
    let now = 0, timerId = 0;
    const timers = new Map();
    const elements = Object.fromEntries(['answer', 'automatic-voice', 'start-recording', 'stop-recording', 'pause-voice',
        'speak-question', 'stop-speaking', 'voice-config', 'interviewer-state', 'voice-status', 'thinking-prompt',
        'thinking-message', 'interview-question', 'keep-thinking'].map(id => [id, new Target()]));
    const form = new Target(), submit = new Target();
    form.querySelector = () => submit;
    elements.answer.form = form;
    elements['automatic-voice'].checked = auto;
    elements['voice-config'].textContent = JSON.stringify(config);
    elements['interview-question'].textContent = 'Example question';
    const document = new Target(), window = new Target();
    document.body = new Target();
    document.getElementById = id => elements[id];
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
        document, window, performance: {now: () => now}, VoiceTurns: {TurnDetector},
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
            const result = stt ? await stt.promise : {transcript: 'An example answer.', completion_detected: false};
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

test('natural pause does not finish; confirmation grace prepares transcript without submitting', async () => {
    const b = browser(); await b.start(); await b.talk(); await b.advance(5000);
    assert.equal(b.requests.length, 1); assert.equal(b.state(), 'thinking');
    assert.equal(b.streams[0].track.stopped, false);
    await b.advance(5000);
    assert.equal(b.elements['thinking-prompt'].hidden, false);
    assert.equal(b.submit.disabled, true);
    await b.advance(10000);
    assert.equal(b.state(), 'review'); assert.ok(b.streams[0].track.stopped);
    assert.equal(b.requests.length, 1, 'silent tail is never transcribed twice');
    assert.equal(b.elements.answer.value, 'An example answer.');
    assert.equal(b.form.dataset.submitting, undefined);
});

test('Still thinking resets the grace window; resumed speech remains captured', async () => {
    const b = browser(); await b.start(); await b.talk(); await b.advance(19000);
    b.elements['keep-thinking'].click(); await b.advance(2000);
    assert.equal(b.streams[0].track.stopped, false);
    await b.talk(); await b.advance(20000);
    assert.equal(b.state(), 'review'); assert.equal(b.requests.length, 2);
    assert.equal(b.elements.answer.value, 'An example answer. An example answer.');
});

test('recording continues during slow STT and stale completion is ignored', async () => {
    const pending = deferred(); const b = browser({stt: pending});
    await b.start(); await b.talk(); await b.advance(5000);
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
    await b.start(); await b.talk(); await b.advance(5000);
    pending.resolve({transcript: 'Complete answer', completion_detected: true}); await flush();
    b.elements['stop-recording'].click(); await flush();
    assert.equal(b.state(), 'review'); assert.equal(b.requests.length, 1);
    assert.equal(b.elements.answer.value, 'Complete answer');
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
    await b.start(); await b.talk(); await b.advance(5000);
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
