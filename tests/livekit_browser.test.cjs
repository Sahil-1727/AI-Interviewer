const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('LiveKit browser starts a room and requests microphone access', async () => {
    const elements = new Map(), listeners = {}, calls = [];
    const element = id => {
        if (!elements.has(id)) elements.set(id, {textContent: '', disabled: false, hidden: true, handlers: {},
            addEventListener(name, fn) { this.handlers[name] = fn; }, appendChild() {}, value: ''});
        return elements.get(id);
    };
    let room;
    class Room {
        constructor() { room = this; this.handlers = {}; this.localParticipant = {identity: 'candidate', setMicrophoneEnabled: async value => calls.push(['mic', value]),
            publishData: async data => calls.push(['control', JSON.parse(new TextDecoder().decode(data)).action])}; }
        on(name, callback) { this.handlers[name] = callback; }
        registerTextStreamHandler(name, handler) { this.transcript = handler; }
        async connect(url, token) { calls.push(['connect', url, token]); }
        async disconnect() {}
    }
    const document = {getElementById: element, body: {appendChild() {}}, addEventListener: (name, fn) => {listeners[name] = fn;}};
    const roomEvents = Object.fromEntries(['TrackSubscribed', 'TrackUnsubscribed', 'Reconnecting', 'Reconnected', 'Disconnected', 'ActiveSpeakersChanged'].map(x => [x, x]));
    const window = {LivekitClient: {Room, RoomEvent: roomEvents, Track: {Kind: {Audio: 'audio'}}}, addEventListener() {}, location: {assign() {}}};
    const fetch = async url => ({ok: true, json: async () => {
        calls.push(['api', url]);
        if (url.endsWith('/livekit/session')) return {url: 'wss://test', token: 'test-token'};
        if (url.endsWith('/start')) return {status: 'ready', paused: false};
        return {status: 'ready', question: {question: 'Test question'}, question_number: 1, total_questions: 2};
    }});
    vm.runInNewContext(fs.readFileSync(require.resolve('../app/static/js/livekit-interview.js'), 'utf8'),
        {document, window, fetch, TextEncoder, setTimeout: () => 1, clearTimeout() {}, console});
    listeners['interview:livekit-start']();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls.find(call => call[0] === 'connect'), ['connect', 'wss://test', 'test-token']);
    assert.deepEqual(calls.find(call => call[0] === 'mic'), ['mic', true]);
    await room.transcript({readAll: async () => 'my answer'}, 'candidate');
    assert.equal(element('answer').value, 'my answer');
    element('stop-recording').handlers.click({stopImmediatePropagation() {}});
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(calls.some(call => call[0] === 'control' && call[1] === 'done'));
    element('pause-voice').handlers.click({stopImmediatePropagation() {}});
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(calls.some(call => call[0] === 'control' && call[1] === 'pause'));
    assert.ok(calls.some(call => call[0] === 'mic' && call[1] === false));
    room.handlers.Disconnected();
    assert.equal(element('conversation-recovery').hidden, false);
    element('retry-conversation').handlers.click({stopImmediatePropagation() {}});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls.filter(call => call[0] === 'connect').length, 2);
});
