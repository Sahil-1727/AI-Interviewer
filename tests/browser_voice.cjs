/* Real Chromium + MediaRecorder/Web Audio with synthetic mic/camera and mocked
 * STT/TTS HTTP. Requires playwright (test dependency), Chrome, and voice_demo.py.
 * Run with NODE_PATH pointing to an installed playwright node_modules directory.
 */
const {chromium} = require('playwright');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const real = process.env.VOICE_REAL_SMOKE === '1';
const baseURL = process.env.VOICE_TEST_URL || 'http://127.0.0.1:5001';
const output = path.resolve('test-results');
fs.mkdirSync(output, {recursive: true});
function wav(seconds, microphone = false) {
    const rate = 16000, count = Math.floor(rate * seconds), data = Buffer.alloc(44 + count * 2);
    data.write('RIFF'); data.writeUInt32LE(data.length - 8, 4); data.write('WAVEfmt ', 8);
    data.writeUInt32LE(16, 16); data.writeUInt16LE(1, 20); data.writeUInt16LE(1, 22);
    data.writeUInt32LE(rate, 24); data.writeUInt32LE(rate * 2, 28); data.writeUInt16LE(2, 32); data.writeUInt16LE(16, 34);
    data.write('data', 36); data.writeUInt32LE(count * 2, 40);
    for (let n = 0; n < count; n++) {
        const t = n / rate;
        const signal = microphone && t > .3 && t < 1.5 ? .15 * Math.sin(2 * Math.PI * (170 + 40 * Math.sin(t * 6)) * t) : 0;
        data.writeInt16LE(Math.round(signal * 32767), 44 + n * 2);
    }
    return data;
}
const audioPath = path.join(os.tmpdir(), `voice-test-${process.pid}.wav`);
fs.writeFileSync(audioPath, real ? fs.readFileSync(process.env.VOICE_REAL_AUDIO) : wav(8, true));
const reports = [];

(async () => {
    const browser = await chromium.launch({headless: true,
        ...(process.env.CHROME_EXECUTABLE ? {executablePath: process.env.CHROME_EXECUTABLE} : {}),
        args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', `--use-file-for-fake-audio-capture=${audioPath}`]
    });
    try {
        async function scenario(name, fault = null) {
            if (process.env.VOICE_TEST_SCENARIO && fault !== process.env.VOICE_TEST_SCENARIO) return;
            const context = await browser.newContext({permissions: ['microphone', 'camera']});
            const page = await context.newPage();
            const errors = [], metrics = [], answerRequests = [], cameraChecks = [];
            let sttCalls = 0, ttsCalls = 0;
            const overlaps = [];
            await page.exposeFunction('__voiceTestCamera', data => cameraChecks.push(data));
            await page.exposeFunction('__voiceTestOverlap', data => overlaps.push(data));
            page.on('pageerror', error => errors.push(error.message));
            page.on('console', async event => {
                if (event.text().startsWith('[voice timing]')) {
                    try { metrics.push(await event.args()[1].jsonValue()); } catch (_) {}
                }
            });
            await page.addInitScript(({deny, duplicate}) => {
                if (duplicate) {
                    let lastAnswer;
                    document.addEventListener('voice:answer', event => { lastAnswer = event.detail; });
                    document.addEventListener('voice:playback', () => {
                        if (lastAnswer) document.dispatchEvent(new CustomEvent('voice:answer', {detail: lastAnswer}));
                    });
                }
                window.__audioTracks = new Set();
                window.__cameraChecks = [];
                window.__states = [];
                window.__audioOverlaps = [];
                const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                navigator.mediaDevices.getUserMedia = async constraints => {
                    if (deny && constraints.audio) throw new DOMException('Test permission denied', 'NotAllowedError');
                    const stream = await original({...constraints, ...(constraints.audio ? {audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: false}} : {})});
                    for (const track of stream.getAudioTracks()) {
                        window.__audioTracks.add(track);
                        const stop = track.stop.bind(track);
                        track.stop = () => { window.__audioTracks.delete(track); stop(); };
                    }
                    return stream;
                };
                const play = HTMLMediaElement.prototype.play;
                HTMLMediaElement.prototype.play = function () {
                    if (this.tagName === 'AUDIO' && window.__audioTracks.size) window.__voiceTestOverlap('TTS while mic active');
                    return play.call(this);
                };
                document.addEventListener('DOMContentLoaded', () => {
                    const video = document.getElementById('candidate-video');
                    const q = document.getElementById('interview-question');
                    if (!q || !video) return;
                    new MutationObserver(() => {
                        const track = video.srcObject?.getVideoTracks()[0];
                        window.__voiceTestCamera({question: q.textContent, id: track?.id, state: track?.readyState});
                    }).observe(q, {childList: true});
                    new MutationObserver(() => {
                        window.__states.push(document.body.dataset.conversationState);
                    }).observe(document.body, {attributes: true, attributeFilter: ['data-conversation-state']});
                });
            }, {deny: fault === 'permission', duplicate: fault === 'duplicates'});
            if (!real) await page.route('**/api/synthesize', async route => {
                ttsCalls++;
                if (fault === 'tts' && ttsCalls === 1) return route.fulfill({status: 503, contentType: 'application/json', body: '{"error":"Simulated TTS failure"}'});
                await route.fulfill({status: 200, contentType: 'audio/wav', headers: {'Server-Timing': 'tts;dur=0'}, body: wav(.15)});
            });
            if (!real) await page.route('**/api/transcribe', async route => {
                sttCalls++;
                if (fault === 'empty' && sttCalls === 1) return route.fulfill({status: 400, contentType: 'application/json', body: '{"error":"No speech detected."}'});
                if (fault === 'stt' && sttCalls === 1) return route.fulfill({status: 500, contentType: 'application/json', body: '{"error":"Simulated STT failure"}'});
                await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({transcript: 'I built a Flask project and tested the API and error handling.', language: 'en', completion_detected: false, transcription_ms: 0})});
            });
            await page.route('**/api/conversation/answer', async route => {
                answerRequests.push(route.request().postDataJSON());
                if (fault === 'lost-response' && answerRequests.length === 1) {
                    await route.fetch(); // Server accepted/evaluated; browser lost the response.
                    return route.abort('failed');
                }
                return route.continue();
            });
            if (real) page.on('request', request => { if (request.url().endsWith('/api/transcribe')) sttCalls++; if (request.url().endsWith('/api/synthesize')) ttsCalls++; });
            await page.goto(baseURL + (real ? '/real-voice-smoke' : '/voice-demo'));
            await page.locator('#interview-instructions').waitFor({state: 'visible'});
            assert.ok(await page.locator('#interview-instructions').isVisible());
            assert.equal(await page.locator('#begin-instructions').isDisabled(), true);
            await page.locator('#instructions-acknowledged').check();
            assert.equal(await page.locator('#begin-instructions').isDisabled(), true);
            await page.locator('#check-microphone').click();
            await page.waitForFunction(() => !document.getElementById('check-microphone').disabled);
            if (fault === 'permission') {
                assert.equal(await page.locator('#begin-instructions').isDisabled(), true);
                assert.match(await page.locator('#readiness-status').textContent(), /Unable to access the microphone/);
                await page.locator('#instructions-manual').click();
                await page.waitForFunction(() => document.body.dataset.interviewMode === 'manual');
                assert.equal(await page.locator('#answer-form [type=submit]').isVisible(), true);
                await page.locator('#answer').fill('I built and tested a Flask project.');
                await page.locator('#answer-form [type=submit]').click();
                await page.waitForURL('**/follow-up');
                await page.waitForFunction(() => document.body.dataset.interviewMode === 'manual');
                assert.equal(await page.locator('#interview-instructions').isVisible(), false);
                assert.equal(await page.locator('#answer-form [type=submit]').isVisible(), true);
                await page.reload();
                await page.waitForFunction(() => document.body.dataset.interviewMode === 'manual');
                await page.locator('#answer').fill('I learned to test error handling.');
                await page.locator('#answer-form [type=submit]').click();
                await page.waitForURL('**/start-interview');
                await page.waitForFunction(() => document.body.dataset.interviewMode === 'manual');
                assert.equal(await page.locator('#answer-form [type=submit]').isVisible(), true);
                await page.locator('#start-interview').click();
                assert.equal(await page.locator('#interview-instructions').isVisible(), true);
                assert.equal(await page.locator('#begin-instructions').isDisabled(), true);
                assert.deepEqual(errors, []);
                reports.push({name, result: 'passed', answerRequests: 0});
                await context.close(); return;
            }
            await page.locator('#check-camera').click();
            await page.waitForFunction(() => !document.getElementById('check-camera').disabled);
            assert.equal(await page.locator('#begin-instructions').isEnabled(), true);
            await page.locator('#begin-instructions').click();
            await page.locator('#start-camera').click();
            await page.waitForFunction(() => document.getElementById('camera-status').textContent === 'Camera is on');
            let originalCamera = await page.locator('#candidate-video').evaluate(video => video.srcObject.getVideoTracks()[0].id);
            cameraChecks.length = 0;
            if (!fault) {
                await page.waitForFunction(() => document.body.dataset.conversationState === 'LISTENING');
                await page.screenshot({path: path.join(output, 'interview-room.png'), fullPage: true});
            }
            if (fault === 'controls') {
                await page.waitForFunction(() => document.body.dataset.conversationState === 'LISTENING');
                await page.locator('#pause-voice').click();
                await page.waitForFunction(() => document.body.dataset.conversationState === 'PAUSED');
                assert.equal(answerRequests.length, 0);
                const savedText = await page.locator('#answer').inputValue();
                assert.ok(savedText);
                await page.locator('#pause-voice').click();
                await page.waitForFunction(() => document.body.dataset.conversationState === 'LISTENING');
                await page.locator('#speak-question').click();
                await page.waitForFunction(() => document.body.dataset.conversationState === 'LISTENING');
                assert.ok((await page.locator('#answer').inputValue()).startsWith(savedText));
                await page.reload();
                await page.waitForFunction(() => document.body.dataset.conversationState === 'PAUSED');
                assert.ok((await page.locator('#answer').inputValue()).startsWith(savedText));
                // Camera restarts only because this test explicitly reloaded.
                await page.locator('#start-camera').click();
                await page.waitForFunction(() => document.getElementById('camera-status').textContent === 'Camera is on');
                cameraChecks.length = 0;
                originalCamera = await page.locator('#candidate-video').evaluate(video => video.srcObject.getVideoTracks()[0].id);
                await page.locator('#start-interview').click();
            }
            if (fault && !['controls', 'empty', 'duplicates'].includes(fault)) {
                await page.waitForFunction(() => ['ERROR', 'RECOVERING'].includes(document.body.dataset.conversationState), {timeout: 20000});
                assert.ok(await page.locator('#interview-question').textContent());
                assert.equal(await page.locator('#candidate-video').evaluate(video => video.srcObject.getVideoTracks()[0].readyState), 'live');
                if (fault === 'permission') {
                    assert.equal(answerRequests.length, 0);
                    await page.locator('#manual-mode').click();
                    await page.waitForFunction(() => document.body.dataset.interviewMode === 'manual');
                    assert.equal(await page.locator('#answer-form [type=submit]').isVisible(), true);
                    assert.deepEqual(errors, []);
                    reports.push({name, result: 'passed', answerRequests: 0});
                    await context.close(); return;
                }
                await page.locator('#retry-conversation').click();
            }
            await page.waitForURL('**/final-scorecard', {timeout: real ? 180000 : 90000});
            if (!real) assert.match(await page.locator('.overall-summary').textContent(), /Mock interview completed/);
            assert.equal(errors.length, 0, errors.join('\n'));
            assert.deepEqual(overlaps, [], 'No audio playback may overlap microphone capture');
            assert.ok(cameraChecks.length >= (real ? 0 : 2));
            assert.ok(cameraChecks.every(check => check.id === originalCamera && check.state === 'live'), JSON.stringify(cameraChecks));
            const state = await (await page.request.get(baseURL + '/api/conversation')).json();
            assert.equal(state.status, 'completed');
            assert.equal(state.completed_questions, real ? 1 : 2);
            if (!real) assert.equal(new Set(answerRequests.map(a => a.turn_id)).size, 3);
            const stats = {name, result: 'passed', camera: originalCamera, sttCalls, ttsCalls,
                answerRequests: answerRequests.length, uniqueTurns: new Set(answerRequests.map(a => a.turn_id)).size, backendTimings: state.timings, metrics};
            reports.push(stats);
            await context.close();
        }
        if (real) { await scenario('scripted real TTS + Whisper + Gemini interview'); } else {
        await scenario('one Start click → follow-up → next question → scorecard');
        await scenario('TTS failure → explicit retry → complete', 'tts');
        await scenario('STT failure → transcript recovery → complete', 'stt');
        await scenario('lost response after server acceptance → recover without duplicate evaluation', 'lost-response');
        await scenario('microphone denied → manual fallback', 'permission');
        await scenario('empty Whisper transcript waits and recovers without evaluation', 'empty');
        await scenario('pause / resume / repeat / refresh preserve transcript and question', 'controls');
        await scenario('late duplicate completion events cannot submit a new question', 'duplicates');
        }
        fs.writeFileSync(path.join(output, real ? 'browser-voice-real.json' : process.env.VOICE_TEST_SCENARIO ? `browser-${process.env.VOICE_TEST_SCENARIO}.json` : 'browser-voice.json'), JSON.stringify(reports, null, 2));
        console.log(JSON.stringify({scenarios: reports.map(({name, result, answerRequests, sttCalls, ttsCalls}) => ({name, result, answerRequests, sttCalls, ttsCalls})), timings: real ? 'test-results/browser-voice-real.json' : 'test-results/browser-voice.json'}, null, 2));
    } finally { await browser.close(); fs.unlinkSync(audioPath); }
})().catch(error => { console.error(error); process.exitCode = 1; });
