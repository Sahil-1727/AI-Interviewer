/* Experimental LiveKit transport. Interview state remains in Flask. */
(() => {
    let room = null, active = false, timer = null, expiryTimer = null, ending = false, muted = false, livekitMode = false;
    const $ = id => document.getElementById(id);
    const status = message => { $('voice-status').textContent = message; };
    async function api(path, data) {
        const response = await fetch('/api/conversation' + path, {
            method: data === undefined ? 'GET' : 'POST',
            headers: {'Content-Type': 'application/json'},
            ...(data === undefined ? {} : {body: JSON.stringify(data)})
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Interview connection failed.');
        return result;
    }
    async function poll() {
        if (!active) return;
        try {
            const state = await api('');
            if (state.question) {
                $('interview-question').textContent = state.question.question;
                $('question-count').textContent = state.is_follow_up ? 'FOLLOW-UP QUESTION' : `QUESTION ${state.question_number} / ${state.total_questions}`;
            }
            if (state.status === 'completed') {
                active = false;
                await room?.disconnect();
                window.location.assign(state.scorecard_url);
                return;
            }
            if (state.status === 'error') {
                status(state.error || 'Interview processing failed.');
                $('conversation-recovery').hidden = false;
            } else if (state.status === 'evaluating') status('Considering your answer…');
            else if (state.status === 'scoring' || state.status === 'ending') status('Preparing your scorecard…');
            timer = setTimeout(poll, 1000);
        } catch (_) {
            status('Interview connection unavailable. Reconnect or use manual mode.');
            $('conversation-recovery').hidden = false;
        }
    }
    async function start() {
        if (active) return;
        livekitMode = true;
        active = true;
        ending = false;
        $('conversation-recovery').hidden = true;
        status('Connecting to the interview…');
        try {
            let state = await api('/start', {});
            if (state.paused) state = await api('/control', {action: 'resume'});
            const connection = await api('/livekit/session', {});
            if (connection.expires_at) {
                clearTimeout(expiryTimer);
                expiryTimer = setTimeout(() => {
                    if (active) void endInterview();
                }, Math.max(0, Date.parse(connection.expires_at) - Date.now()));
            }
            const {Room, RoomEvent, Track} = window.LivekitClient;
            room = new Room();
            let candidateSpeaking = false, lastSpeechEnd = null;
            room.on(RoomEvent.ActiveSpeakersChanged, speakers => {
                const now = performance.now();
                const candidate = speakers.some(speaker => speaker.identity === room.localParticipant.identity);
                if (candidateSpeaking && !candidate) lastSpeechEnd = now;
                candidateSpeaking = candidate;
                if (lastSpeechEnd !== null && speakers.some(speaker => speaker.identity !== room.localParticipant.identity)) {
                    const ms = Math.round(now - lastSpeechEnd);
                    console.info('[voice timing]', {stage: 'browser_speech_end_to_remote_speaker_ms', ms});
                    lastSpeechEnd = null;
                }
            });
            room.on(RoomEvent.TrackSubscribed, track => {
                if (track.kind === Track.Kind.Audio) document.body.appendChild(track.attach());
            });
            room.on(RoomEvent.TrackUnsubscribed, track => track.detach().forEach(element => element.remove()));
            room.on(RoomEvent.Reconnecting, () => status('Reconnecting to the interview…'));
            room.on(RoomEvent.Reconnected, () => status('Reconnected. Continue your answer.'));
            room.on(RoomEvent.Disconnected, () => {
                if (active && !ending) {
                    active = false;
                    clearTimeout(timer);
                    status('Disconnected. Select Reconnect / retry to continue.');
                    $('conversation-recovery').hidden = false;
                }
            });
            room.registerTextStreamHandler('lk.transcription', async (reader, participant) => {
                try {
                    const text = await reader.readAll();
                    if (participant === room.localParticipant.identity) $('answer').value = text;
                } catch (_) { status('Transcript interrupted. Please repeat any missing words.'); }
            });
            await room.connect(connection.url, connection.token);
            await room.localParticipant.setMicrophoneEnabled(true);
            muted = false;
            $('start-interview').disabled = true;
            $('manual-mode').disabled = true;
            $('pause-voice').disabled = false;
            $('pause-voice').textContent = 'Pause Interview';
            $('stop-recording').disabled = false;
            $('end-interview').disabled = false;
            status('Connected. Listen to the interviewer.');
            void poll();
        } catch (error) {
            active = false;
            await room?.disconnect(); room = null;
            status(error.message + ' Use the existing voice or manual mode to continue.');
            $('start-interview').disabled = false;
            $('manual-mode').disabled = false;
        }
    }
    document.addEventListener('interview:livekit-start', () => void start());
    $('retry-conversation').addEventListener('click', event => {
        if (!livekitMode || active) return;
        event.stopImmediatePropagation();
        void start();
    }, true);
    async function control(action) {
        await room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({action})),
            {reliable: true, topic: 'interview.control'});
    }
    $('stop-recording').addEventListener('click', event => {
        if (!active || ending || muted) return;
        event.stopImmediatePropagation();
        void control('done').catch(() => status('Could not finish the answer. Please retry.'));
    }, true);
    $('pause-voice').addEventListener('click', event => {
        if (!active) return;
        event.stopImmediatePropagation();
        void (async () => {
            try {
                const next = !muted;
                if (next) {
                    await room.localParticipant.setMicrophoneEnabled(false);
                    await api('/control', {action: 'pause'});
                    await control('pause');
                } else {
                    await api('/control', {action: 'resume'});
                    await room.localParticipant.setMicrophoneEnabled(true);
                    await control('resume');
                }
                muted = next;
                $('pause-voice').textContent = muted ? 'Resume Interview' : 'Pause Interview';
                $('stop-recording').disabled = muted;
                status(muted ? 'Interview paused.' : 'Interview resumed.');
            } catch (_) { status('Could not change interview state. Reconnect and retry.'); }
        })();
    }, true);
    async function endInterview() {
        ending = true;
        $('scorecard-loading').hidden = false;
        status('Preparing your scorecard…');
        try {
            await room?.disconnect();
            await api('/control', {action: 'end'});
            clearTimeout(timer); void poll();
        } catch (_) {
            $('scorecard-loading').hidden = true;
            status('Could not end the interview. Reconnect and try again.');
        }
    }
    $('end-interview').addEventListener('click', event => {
        if (!active) return;
        event.stopImmediatePropagation();
        if (!window.confirm('End the interview early? Only completed evaluations will be included.')) return;
        void endInterview();
    }, true);
    window.addEventListener('pagehide', () => { active = false; clearTimeout(timer); clearTimeout(expiryTimer); void room?.disconnect(); });
})();
