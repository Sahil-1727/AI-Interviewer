/* Local test server uses test-only mocked Nova, Aura, and Gemini responses. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

(async () => {
    const browser = await chromium.launch({headless: true,
        ...(process.env.CHROME_EXECUTABLE ? {executablePath: process.env.CHROME_EXECUTABLE} : {}),
        args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream']});
    try {
        const base = process.env.VOICE_TEST_URL || 'http://127.0.0.1:5019';
        async function start(scenario) {
            const context = await browser.newContext({permissions: ['microphone', 'camera']});
            const page = await context.newPage();
            const errors = [], requests = [], sockets = [], overlaps = [];
            await page.exposeFunction('__recordOverlap', value => overlaps.push(value));
            await page.addInitScript(() => {
                window.__audioTracks = new Set();
                window.__audioOverlaps = [];
                const getMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                navigator.mediaDevices.getUserMedia = async constraints => {
                    const stream = await getMedia(constraints);
                    for (const track of stream.getAudioTracks()) {
                        window.__audioTracks.add(track);
                        const stop = track.stop.bind(track);
                        track.stop = () => { window.__audioTracks.delete(track); stop(); };
                    }
                    return stream;
                };
                const play = HTMLMediaElement.prototype.play;
                HTMLMediaElement.prototype.play = function () {
                    if (this.tagName === 'AUDIO' && window.__audioTracks.size) window.__recordOverlap('audio playback with microphone open');
                    return play.call(this);
                };
            });
            page.on('pageerror', error => errors.push(error.message));
            page.on('request', request => requests.push(request.url()));
            page.on('websocket', ws => sockets.push(ws.url()));
            await page.goto(base + '/voice-demo?scenario=' + scenario);
            await page.locator('#instructions-acknowledged').check();
            await page.locator('#check-microphone').click();
            await page.waitForFunction(() => !document.getElementById('begin-instructions').disabled);
            await page.locator('#begin-instructions').click();
            return {context, page, errors, requests, sockets, overlaps};
        }
        async function report(test) {
            await test.page.waitForURL('**/final-scorecard', {timeout: 90000});
            const response = await test.page.request.get(base + '/api/conversation/transcript');
            assert.equal(response.status(), 200);
            assert.equal(test.requests.filter(url => url.endsWith('/api/transcribe')).length, 0);
            assert.deepEqual(test.errors, []);
            assert.deepEqual(test.overlaps, []);
            return (await response.json()).messages;
        }
        const normal = await start('normal');
        const messages = await report(normal);
        assert.match(await normal.page.locator('.overall-summary').textContent(), /Mock interview completed/);
        assert.equal(messages.filter(message => message.kind === 'answered').length, 3);
        assert.ok(normal.sockets.length >= 3);
        await normal.context.close();
        if (process.env.DEEPGRAM_NORMAL_ONLY === '1') {
            console.log('Mocked browser streaming interview completed.');
            return;
        }
        for (const scenario of ['silence', 'noise']) {
            const test = await start(scenario);
            const events = await report(test);
            assert.equal(events.filter(event => event.kind === 'answered').length, 0);
            assert.equal(events.filter(event => event.kind === 'unanswered').length, 2);
            assert.match(await test.page.locator('body').textContent(), /Insufficient evidence/);
            await test.context.close();
        }
        const interrupted = await start('disconnect');
        await interrupted.page.waitForFunction(() => document.body.dataset.conversationState === 'ERROR');
        assert.match(await interrupted.page.locator('#answer').inputValue(), /I built a Flask project/);
        assert.equal(interrupted.requests.filter(url => url.endsWith('/api/conversation/answer')).length, 0);
        await interrupted.page.locator('#retry-conversation').click();
        const recovered = await report(interrupted);
        assert.match(recovered.find(event => event.kind === 'answered').text, /I built a Flask project.*I tested the API/s);
        await interrupted.context.close();
        const pausedSpeech = await start('pauses');
        const pauseEvents = await report(pausedSpeech);
        assert.ok(pauseEvents.filter(event => event.kind === 'answered').every(event => /First part.*Second part/s.test(event.text)));
        await pausedSpeech.context.close();
        const done = await start('command_done');
        const doneEvents = await report(done);
        assert.ok(!doneEvents.find(event => event.kind === 'answered').text.includes("I'm done"));
        await done.context.close();
        const repeat = await start('command_repeat');
        const repeatEvents = await report(repeat);
        assert.equal(repeatEvents.filter(event => event.kind === 'answered').length, 3);
        assert.ok(repeat.sockets.length >= 4);
        await repeat.context.close();
        const manual = await start('normal');
        await manual.page.waitForFunction(() => document.getElementById('answer').value.includes('Flask'));
        await manual.page.locator('#stop-recording').click();
        const manualEvents = await report(manual);
        assert.equal(manualEvents.filter(event => event.kind === 'answered').length, 3);
        await manual.context.close();
        const early = await start('normal');
        early.page.on('dialog', dialog => dialog.accept());
        await early.page.waitForFunction(() => document.body.dataset.conversationState === 'LISTENING');
        await early.page.locator('#end-interview').click();
        const earlyEvents = await report(early);
        assert.equal(earlyEvents.filter(event => event.kind === 'answered').length, 0);
        await early.context.close();
        const concurrent = await Promise.all([start('candidate_a'), start('candidate_b')]);
        const both = await Promise.all(concurrent.map(report));
        assert.ok(both.every(events => events.filter(event => event.kind === 'answered').length === 3));
        assert.ok(both[0].filter(event => event.kind === 'answered').every(event => event.text.includes('alpha') && !event.text.includes('beta')));
        assert.ok(both[1].filter(event => event.kind === 'answered').every(event => event.text.includes('beta') && !event.text.includes('alpha')));
        await Promise.all(concurrent.map(test => test.context.close()));
        console.log('Mocked streaming interview, silence, noise, reconnect, and concurrent candidates passed.');
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
