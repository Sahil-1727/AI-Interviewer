// Chromium early-termination tests. All STT/TTS and Gemini decisions are mocked.
const {chromium} = require('playwright');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const base = 'http://127.0.0.1:5001';
fs.mkdirSync('test-results', {recursive: true});
function wav(seconds, input = false) {
    const rate = 16000, count = Math.floor(rate * seconds), data = Buffer.alloc(44 + count * 2);
    data.write('RIFF'); data.writeUInt32LE(data.length - 8, 4); data.write('WAVEfmt ', 8);
    data.writeUInt32LE(16, 16); data.writeUInt16LE(1, 20); data.writeUInt16LE(1, 22);
    data.writeUInt32LE(rate, 24); data.writeUInt32LE(rate * 2, 28); data.writeUInt16LE(2, 32); data.writeUInt16LE(16, 34);
    data.write('data', 36); data.writeUInt32LE(count * 2, 40);
    for (let n = 0; n < count; n++) {
        const t = n / rate;
        data.writeInt16LE(input && t > .3 && t < 1.5 ? Math.round(4000 * Math.sin(2 * Math.PI * 190 * t)) : 0, 44 + n * 2);
    }
    return data;
}
const audioPath = path.join(os.tmpdir(), `voice-early-${process.pid}.wav`);
fs.writeFileSync(audioPath, wav(8, true));
(async () => {
    const browser = await chromium.launch({headless: true, executablePath: process.env.CHROME_EXECUTABLE,
        args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', `--use-file-for-fake-audio-capture=${audioPath}`]});
    try {
        for (const name of ['recording', 'speaking', 'after-answer', 'evaluating', 'five-seconds', 'done', 'spoken-done']) {
            if (process.env.VOICE_TEST_SCENARIO && name !== process.env.VOICE_TEST_SCENARIO) continue;
            const context = await browser.newContext({permissions: ['microphone', 'camera']});
            const page = await context.newPage();
            const errors = [], requests = [], stops = [], pauses = [];
            page.on('pageerror', error => errors.push(error.message));
            await page.exposeFunction('__testAudioStopped', () => stops.push('stopped'));
            await page.exposeFunction('__testPlaybackPaused', () => pauses.push('paused'));
            await page.addInitScript(() => {
                const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                navigator.mediaDevices.getUserMedia = async constraints => {
                    // Camera readiness is unavailable: it must never prevent starting.
                    if (constraints.video) throw new DOMException('No camera', 'NotFoundError');
                    const stream = await original({audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: false}});
                    for (const track of stream.getAudioTracks()) {
                        const stop = track.stop.bind(track);
                        track.stop = () => { window.__testAudioStopped(); stop(); };
                    }
                    return stream;
                };
                const originalPause = HTMLMediaElement.prototype.pause;
                HTMLMediaElement.prototype.pause = function () {
                    if (this.tagName === 'AUDIO') window.__testPlaybackPaused();
                    return originalPause.call(this);
                };
            });
            await page.route('**/api/synthesize', route => route.fulfill({status: 200, contentType: 'audio/wav', body: wav(name === 'speaking' ? 5 : .2)}));
            await page.route('**/api/transcribe', route => route.fulfill({status: 200, contentType: 'application/json',
                body: JSON.stringify({transcript: 'I built and tested a project.', completion_detected: name === 'spoken-done'})}));
            await page.route('**/api/conversation/answer', route => { requests.push(route.request().postDataJSON()); return route.continue(); });
            await page.goto(base + '/voice-demo');
            await page.locator('#interview-instructions').waitFor({state: 'visible'});
            if (name === 'recording') await page.screenshot({path: 'test-results/pre-interview-top.png'});
            await page.locator('#instructions-acknowledged').check();
            await page.locator('#check-microphone').click();
            await page.waitForFunction(() => !document.getElementById('check-microphone').disabled);
            await page.locator('#check-camera').click();
            await page.waitForFunction(() => document.getElementById('readiness-status').textContent.includes('Unable'));
            assert.equal(await page.locator('#begin-instructions').isEnabled(), true);
            if (name === 'recording') await page.screenshot({path: 'test-results/pre-interview-instructions.png'});
            await page.locator('#begin-instructions').click();
            if (['five-seconds', 'done', 'spoken-done'].includes(name)) {
                await page.waitForFunction(() => document.body.dataset.conversationState === 'LISTENING');
                if (name === 'done') await page.locator('#stop-recording').click();
                await page.waitForFunction(() => window.voiceMetrics?.some(sample => sample.stage === 'endpoint_ms'), {timeout: 15000});
                const endpoint = await page.evaluate(() => window.voiceMetrics.find(sample => sample.stage === 'endpoint_ms').ms);
                if (name === 'five-seconds') assert.ok(endpoint >= 5000 && endpoint < 7000, `actual silence: ${endpoint}ms`);
                else assert.ok(endpoint < 5000, `completion should not wait five seconds: ${endpoint}ms`);
                await page.waitForFunction(() => document.body.dataset.conversationState === 'EVALUATING');
                await page.waitForTimeout(100);
                assert.equal(requests.length, 1);
                assert.deepEqual(errors, []);
                console.log(`PASS ${name}: one automatic answer; endpoint ${endpoint}ms`);
                await context.close();
                continue;
            }
            if (name === 'recording') {
                await page.waitForFunction(() => document.body.dataset.conversationState === 'LISTENING');
                page.once('dialog', dialog => dialog.dismiss());
                await page.locator('#end-interview').click();
                assert.equal(requests.length, 0, 'cancel does not submit or end');
                await page.waitForFunction(() => document.body.dataset.conversationState === 'LISTENING');
            } else if (name === 'speaking') {
                await page.waitForFunction(() => document.body.dataset.conversationState === 'AI_SPEAKING');
                assert.equal(requests.length, 0);
            } else if (name === 'after-answer') {
                await page.waitForFunction(() => document.getElementById('question-count').textContent.includes('FOLLOW-UP'));
                assert.equal(requests.length, 1);
            } else {
                await page.waitForFunction(() => document.body.dataset.conversationState === 'EVALUATING');
                // Wait until the server has accepted the job, while the mock worker is held.
                await page.waitForFunction(async () => {
                    const r = await fetch('/api/conversation');
                    return r.ok && (await r.json()).status === 'evaluating';
                });
                assert.equal(requests.length, 1);
            }
            const stopsBeforeEnd = stops.length;
            const pausesBeforeEnd = pauses.length;
            page.once('dialog', dialog => dialog.accept());
            await page.locator('#end-interview').click();
            await page.waitForURL('**/final-scorecard', {timeout: 20000});
            assert.match(await page.locator('h1').textContent(), /Interview Ended Early/);
            if (name === 'recording') await page.screenshot({path: 'test-results/early-insufficient-evidence.png'});
            const body = await page.locator('body').textContent();
            if (name === 'after-answer') assert.match(body, /1 of 2 planned questions/);
            else {
                assert.match(body, /0 of 2 planned questions/);
                assert.match(body, /Insufficient evidence/);
                assert.doesNotMatch(body, /\/ 10/);
            }
            if (name === 'recording') assert.ok(stops.length > stopsBeforeEnd, 'active microphone stops at confirmation');
            if (name === 'speaking') assert.ok(pauses.length > pausesBeforeEnd, 'question playback stops at confirmation');
            const snapshot = await (await page.request.get(base + '/api/conversation')).json();
            assert.equal(snapshot.status, 'completed'); assert.equal(snapshot.early_ended, true);
            if (requests.length) {
                const repeat = await page.request.post(base + '/api/conversation/answer', {data: requests[0]});
                assert.equal(repeat.status(), 409);
            }
            await page.reload();
            assert.match(await page.locator('h1').textContent(), /Interview Ended Early/);
            assert.deepEqual(errors, []);
            console.log(`PASS ${name}: early scorecard, confirmation, camera optional, no extra submission`);
            await context.close();
        }
    } finally { await browser.close(); fs.unlinkSync(audioPath); }
})().catch(error => { console.error(error); process.exitCode = 1; });
