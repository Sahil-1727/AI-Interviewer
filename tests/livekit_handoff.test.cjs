const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('switching to LiveKit cancels an in-flight legacy start', async () => {
    const nodes = new Map(), events = {}, calls = [];
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, {
            textContent: id === 'voice-config' ? JSON.stringify({REQUEST_TIMEOUT_SECONDS: 30}) : '',
            dataset: {}, style: {}, disabled: false, hidden: false, value: '',
            addEventListener(name, fn) { this[name] = fn; },
            form: {dataset: {interviewInstance: 'test'}},
        });
        return nodes.get(id);
    };
    const document = {body: {dataset: {livekitPrimary: '1'}}, getElementById: node,
        addEventListener(name, fn) { (events[name] ||= []).push(fn); },
        dispatchEvent(event) { for (const fn of events[event.type] || []) fn(event); }};
    let releaseUnlock;
    const voice = {isBusy: () => false, setManaged() {}, discard() { calls.push('discard'); },
        unlock: () => new Promise(resolve => { releaseUnlock = resolve; }), speak: () => calls.push('speak')};
    const window = {InterviewVoice: voice, addEventListener() {}, location: {assign() {}}};
    const fetch = async url => { calls.push(url); return {ok: false, status: 404, json: async () => ({})}; };
    class CustomEvent { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } }
    vm.runInNewContext(fs.readFileSync(require.resolve('../app/static/js/conversation.js'), 'utf8'),
        {window, document, fetch, CustomEvent, AbortController, setTimeout: () => 1, clearTimeout() {},
            sessionStorage: {getItem() { return null; }, setItem() {}, removeItem() {}}, console});
    document.dispatchEvent(new CustomEvent('DOMContentLoaded'));
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(!calls.includes('/api/conversation'), 'LiveKit-primary page should not call the legacy state endpoint before start');
    node('start-interview').click();
    document.dispatchEvent(new CustomEvent('interview:livekit-own-voice'));
    releaseUnlock();
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(calls.includes('discard'));
    assert.ok(!calls.includes('/api/conversation/start'));
    assert.ok(!calls.includes('speak'));
});

for (const scenario of [
    {name: 'HTML outage', response: {ok: false, status: 503, json: async () => {throw new SyntaxError('HTML');}}, expected: /temporarily unavailable/},
    {name: 'expired session', response: {ok: false, status: 401, json: async () => null}, expected: /session expired/},
    {name: 'rate limit', response: {ok: false, status: 429, json: async () => ({})}, expected: /Too many requests/},
    {name: 'invalid success body', response: {ok: true, json: async () => null}, expected: /unexpected response/},
    {name: 'timeout', error: Object.assign(new Error(), {name: 'AbortError'}), expected: /timed out/},
]) test(`conversation recovers from ${scenario.name} without clearing typed answers`, async () => {
    const nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, {
            textContent: id === 'voice-config' ? JSON.stringify({REQUEST_TIMEOUT_SECONDS: 30}) : '',
            dataset: {}, style: {}, value: id === 'answer' ? 'My saved answer' : '',
            addEventListener(name, callback) {this[name] = callback;},
            form: {dataset: {interviewInstance: 'test'}},
        });
        return nodes.get(id);
    };
    const voice = {isBusy: () => false, setManaged() {}, unlock: async () => {}, pause: async () => {}};
    const window = {InterviewVoice: voice, addEventListener() {}};
    const document = {body: {dataset: {}}, getElementById: node,
        addEventListener(name, callback) {if (name === 'DOMContentLoaded') this.ready = callback;}, dispatchEvent() {}};
    const fetch = async url => {
        if (url === '/api/conversation') return {ok: false, status: 404, json: async () => ({})};
        if (scenario.error) throw scenario.error;
        return scenario.response;
    };
    vm.runInNewContext(fs.readFileSync('app/static/js/conversation.js', 'utf8'), {
        window, document, fetch, AbortController, setTimeout: () => 1, clearTimeout() {},
        CustomEvent: class {}, sessionStorage: {getItem() {return null;}, setItem() {}}, console,
    });
    document.ready();
    await new Promise(resolve => setImmediate(resolve));
    node('start-interview').click();
    await new Promise(resolve => setImmediate(resolve));
    assert.match(node('voice-status').textContent, scenario.expected);
    assert.equal(node('answer').value, 'My saved answer');
    assert.equal(document.body.dataset.conversationState, 'RECOVERING');
    assert.equal(node('start-interview').disabled, false);
});
