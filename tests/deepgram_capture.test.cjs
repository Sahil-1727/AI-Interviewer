// Simulated streaming events and clock; no network or media devices.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
    let now = 100;
    let socket;
    let nextTimer = 0;
    const timers = new Map();
    const track = {stop() {}, addEventListener() {}};
    const stream = {getTracks: () => [track], getAudioTracks: () => [track]};
    class Recorder {
        static isTypeSupported() { return true; }
        constructor() { this.state = 'inactive'; }
        start() { this.state = 'recording'; }
        stop() { this.state = 'inactive'; this.onstop(); }
    }
    class WS {
        static OPEN = 1;
        static CLOSING = 2;
        constructor() { this.readyState = 1; this.bufferedAmount = 0; this.sent = []; socket = this; }
        send(value) { this.sent.push(value); }
        close() { this.readyState = 3; }
    }
    const window = {};
    const sandbox = {window, WebSocket: WS, MediaRecorder: Recorder,
        navigator: {mediaDevices: {getUserMedia: async () => stream}},
        document: {querySelector: () => ({content: 'csrf'})},
        location: {protocol: 'https:', host: 'example.test'},
        performance: {now: () => now},
        setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, {fn, delay}); return id; },
        clearTimeout: id => timers.delete(id),
        setInterval: () => 1, clearInterval() {}};
    vm.runInNewContext(fs.readFileSync(require.resolve('../app/static/js/deepgram-capture.js'), 'utf8'), sandbox);
    const completed = [], states = [], previews = [];
    const capture = new window.DeepgramCapture({turnId: 'turn', started: 100,
        config: {INITIAL_ANSWER_TIMEOUT_SECONDS: 10, END_OF_ANSWER_SILENCE_SECONDS: 5,
                 DEEPGRAM_SILENCE_FALLBACK_SECONDS: 2.8, DEEPGRAM_UTTERANCE_END_MS: 1500,
                 DEEPGRAM_FINAL_TURN_GRACE_SECONDS: 0.8, MAX_CAPTURE_SECONDS: 600,
                 DEEPGRAM_CONNECT_TIMEOUT_SECONDS: 15, DEEPGRAM_FINALIZE_TIMEOUT_SECONDS: 8},
        onState: state => states.push(state), onTranscript: (final, interim) => previews.push({final, interim}), onError: error => { throw Error(error); },
        onComplete: (text, reason, sawWords) => completed.push({text, reason, sawWords})});
    return {capture, completed, states, previews, timers, socket: () => socket, time: value => { now = value; }};
}

test('interim words are available for preview without becoming finalized text', async () => {
    const f = fixture(); await f.capture.start(); f.socket().onopen();
    f.socket().onmessage({data: JSON.stringify({type: 'ready'})});
    f.socket().onmessage({data: JSON.stringify({type: 'transcript', transcript: 'Final.', interim: 'still speaking', recognized: true})});
    assert.equal(f.previews[0].final, 'Final.');
    assert.equal(f.previews[0].interim, 'still speaking');
    assert.equal(f.capture.text, 'Final.');
});

test('noise and empty interim results do not count as an answer', async () => {
    const f = fixture(); await f.capture.start(); f.socket().onopen();
    f.socket().onmessage({data: JSON.stringify({type: 'ready'})});
    f.socket().onmessage({data: JSON.stringify({type: 'transcript', transcript: '', interim: '', recognized: false})});
    f.time(10099); f.capture.tick(); assert.equal(f.capture.closing, false);
    f.time(10100); f.capture.tick(); assert.equal(f.capture.reason, 'unanswered');
    f.socket().onmessage({data: JSON.stringify({type: 'complete', transcript: ''})});
    assert.equal(f.completed.length, 1);
    assert.equal(f.completed[0].reason, 'unanswered');
});

test('new recognized words reset the five-second timer and finalization waits for complete', async () => {
    const f = fixture(); await f.capture.start(); f.socket().onopen();
    f.socket().onmessage({data: JSON.stringify({type: 'ready'})});
    f.time(1000);
    f.socket().onmessage({data: JSON.stringify({type: 'transcript', transcript: 'First.', interim: '', recognized: true, final: true})});
    f.time(5900);
    f.socket().onmessage({data: JSON.stringify({type: 'transcript', transcript: 'First.', interim: 'second', recognized: true, final: false})});
    f.time(6000); f.capture.tick(); assert.equal(f.capture.closing, false);
    f.time(10900); f.capture.tick(); assert.equal(f.capture.reason, 'silence');
    assert.equal(f.completed.length, 0);
    f.socket().onmessage({data: JSON.stringify({type: 'complete', transcript: 'First. Second.'})});
    assert.equal(f.completed[0].text, 'First. Second.');
});

test('a late complete event after cancellation cannot submit', async () => {
    const f = fixture(); await f.capture.start(); f.socket().onopen();
    f.socket().onmessage({data: JSON.stringify({type: 'ready'})});
    f.capture.cancel();
    f.socket().onmessage({data: JSON.stringify({type: 'complete', transcript: 'Late answer.'})});
    assert.equal(f.completed.length, 0);
});

test('late interim speech is remembered even when no final text arrives', async () => {
    const f = fixture(); await f.capture.start(); f.socket().onopen();
    f.socket().onmessage({data: JSON.stringify({type: 'ready'})});
    f.time(10100); f.capture.tick();
    f.socket().onmessage({data: JSON.stringify({type: 'transcript', transcript: '', interim: 'late speech', recognized: true, final: false})});
    f.socket().onmessage({data: JSON.stringify({type: 'complete', transcript: ''})});
    assert.equal(f.completed[0].sawWords, true);
});

test('Deepgram utterance end finalizes after the configured late-transcript grace', async () => {
    const f = fixture(); await f.capture.start(); f.socket().onopen();
    f.socket().onmessage({data: JSON.stringify({type: 'ready'})});
    f.socket().onmessage({data: JSON.stringify({type: 'transcript', transcript: 'An answer.', interim: '', recognized: true, final: true})});
    f.socket().onmessage({data: JSON.stringify({type: 'utterance_end'})});
    assert.equal(f.capture.closing, false);
    assert.equal([...f.timers.values()].at(-1).delay, 800);
    [...f.timers.values()].at(-1).fn();
    assert.equal(f.capture.reason, 'silence');
    assert.equal(f.capture.closing, true);
});
