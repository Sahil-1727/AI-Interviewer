const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('LiveKit browser starts a room and requests microphone access', async () => {
    const elements = new Map(), listeners = {}, calls = [];
    const scheduled = [];
    let conversationState = {status: 'ready', end_requested: false};
    const element = id => {
        if (!elements.has(id)) elements.set(id, {textContent: '', disabled: false, hidden: true, style: {}, handlers: {},
            addEventListener(name, fn) { this.handlers[name] = fn; }, appendChild() {}, ...(id === 'answer' ? {value: ''} : {})});
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
    const document = {getElementById: element, body: {dataset: {hasLivekitSession: '0'}, appendChild() {}}, addEventListener: (name, fn) => {listeners[name] = fn;},
        dispatchEvent(event) { calls.push(['event', event.type]); }};
    const roomEvents = Object.fromEntries(['TrackSubscribed', 'TrackUnsubscribed', 'Reconnecting', 'Reconnected', 'Disconnected', 'ActiveSpeakersChanged', 'DataReceived'].map(x => [x, x]));
    const window = {LivekitClient: {Room, RoomEvent: roomEvents, Track: {Kind: {Audio: 'audio'}}}, addEventListener() {}, location: {assign() {}}, confirm: () => true};
    const fetch = async (url, options = {}) => ({ok: true, json: async () => {
        calls.push(['api', url, options.method]);
        if (url.endsWith('/livekit/session')) return {url: 'wss://test', token: 'test-token', agent_mode: 'conversational'};
        if (url.endsWith('/start')) return {status: 'ready', paused: false};
        if (url.endsWith('/control')) {
            conversationState = {status: 'scoring', end_requested: true};
            return conversationState;
        }
        if (url === '/api/conversation' && options.method === 'GET') return conversationState;
        return {status: 'ready', question: {question: 'Test question'}, question_number: 1, total_questions: 2};
    }});
    vm.runInNewContext(fs.readFileSync(require.resolve('../app/static/js/livekit-interview.js'), 'utf8'),
        {document, window, fetch, Event, TextEncoder, TextDecoder, performance,
            setTimeout: (fn, delay) => { scheduled.push({fn, delay}); return scheduled.length; }, clearTimeout() {}, console});
    listeners['interview:livekit-start']();
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(calls.find(call => call[0] === 'event' && call[1] === 'interview:livekit-own-voice'));
    assert.equal(calls.filter(call => call[0] === 'api' && call[1] === '/api/conversation' && call[2] === 'GET').length, 0,
        'LiveKit conversation should not poll Flask between turns');
    assert.deepEqual(calls.find(call => call[0] === 'connect'), ['connect', 'wss://test', 'test-token']);
    assert.deepEqual(calls.find(call => call[0] === 'mic'), ['mic', true]);
    assert.equal(element('question-panel').hidden, true);
    assert.equal(element('question-content').style.visibility, 'hidden');
    await room.transcript({async *[Symbol.asyncIterator]() { yield 'my answer'; }}, 'candidate');
    assert.equal(element('answer').value, 'my answer');
    room.handlers.ActiveSpeakersChanged([{identity: 'agent'}]);
    assert.equal(element('question-panel').hidden, false);
    assert.equal(element('question-content').style.visibility, 'visible');
    await room.transcript({info: {id: 'stream-1', attributes: {'lk.segment_id': 'segment-1'}},
        async *[Symbol.asyncIterator]() { yield 'Please explain '; yield 'your project.'; }}, {identity: 'agent'});
    assert.equal(element('interview-question').textContent, 'Please explain your project.');
    room.handlers.ActiveSpeakersChanged([{identity: 'candidate'}]);
    assert.equal(element('question-panel').hidden, true);
    assert.equal(element('question-content').style.visibility, 'hidden');
    assert.equal(element('interview-question').hidden, true);
    assert.equal(element('interview-question').textContent, '');
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
    element('end-interview').handlers.click({stopImmediatePropagation() {}});
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(calls.some(call => call[0] === 'control' && call[1] === 'finalize'));
    assert.ok(calls.some(call => call[0] === 'mic' && call[1] === false));
    room.handlers.DataReceived(new TextEncoder().encode(JSON.stringify({action: 'finalize-ready'})), null, null, 'interview.control.ack');
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(calls.some(call => call[0] === 'api' && call[1] === '/api/conversation/control'));
    conversationState = {status: 'error', end_requested: true, error: 'Scorecard processing failed.'};
    scheduled.filter(item => item.delay === 1000).at(-1).fn();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(element('scorecard-loading').hidden, true);
    assert.equal(element('end-interview').textContent, 'Retry finalization');
    assert.equal(element('end-interview').disabled, false);
    element('end-interview').handlers.click({stopImmediatePropagation() {}});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls.filter(call => call[0] === 'api' && call[1] === '/api/conversation/control').length, 3);
});
