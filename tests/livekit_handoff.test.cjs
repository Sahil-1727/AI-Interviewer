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
