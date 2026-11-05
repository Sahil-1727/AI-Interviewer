/* Experimental LiveKit transport. Interview state remains in Flask. */
(() => {
    let room = null, active = false, timer = null, expiryTimer = null, ending = false, scorecardRetryAvailable = false, muted = false, livekitMode = false;
    let agentSpeaking = false, firstAgentAudioLogged = false, roomConnectStartedAt = null;
    const agentTranscriptSegments = new Map();
    const candidateTranscriptSegments = new Map();
    const $ = id => document.getElementById(id);
    const status = message => { $('voice-status').textContent = message; };
    function setAgentSpeaking(speaking) {
        const content = $('question-content');
        const panel = $('question-panel');
        const question = $('interview-question');
        const count = $('question-count');
        if (agentSpeaking === speaking) {
            if (!speaking) {
                panel.hidden = true;
                content.style.visibility = 'hidden';
                question.hidden = true;
                count.hidden = true;
                question.textContent = '';
            }
            return;
        }
        agentSpeaking = speaking;
        panel.hidden = !speaking;
        content.style.visibility = speaking ? 'visible' : 'hidden';
        question.hidden = !speaking;
        count.hidden = !speaking;
        if (speaking) {
            count.textContent = 'AI INTERVIEWER';
            question.textContent = [...agentTranscriptSegments.values()].join('');
        } else {
            question.textContent = '';
            agentTranscriptSegments.clear();
        }
    }
    function updateTranscript(segments, segmentId, text, target) {
        segments.set(segmentId, text);
        const transcript = [...segments.values()].join('');
        if ('value' in target) target.value = transcript;
        else target.textContent = transcript;
    }
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
        // Flask status polling is only needed while finalization is running.
        // Normal conversation is driven directly by the LiveKit agent.
        if (!active || !ending) return;
        try {
            const state = await api('');
            if (state.status === 'completed') {
                active = false;
                await room?.disconnect();
                window.location.assign(state.scorecard_url);
                return;
            }
            if (state.status === 'error') {
                status(state.error || 'Interview processing failed.');
                $('scorecard-loading').hidden = true;
                if (state.end_requested) {
                    scorecardRetryAvailable = true;
                    $('end-interview').disabled = false;
                    $('end-interview').textContent = 'Retry finalization';
                    $('conversation-recovery').hidden = true;
                } else {
                    $('conversation-recovery').hidden = false;
                }
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
        setAgentSpeaking(false);
        candidateTranscriptSegments.clear();
        $('answer').value = '';
        $('question-count').textContent = 'AI INTERVIEWER';
        livekitMode = true;
        document.dispatchEvent(new Event('interview:livekit-own-voice'));
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
            let finalizeAck;
            const finalizeReady = connection.agent_mode === 'conversational'
                ? new Promise(resolve => { finalizeAck = resolve; }) : null;
            if (finalizeReady) room.on(RoomEvent.DataReceived, (payload, participant, kind, topic) => {
                if (topic !== 'interview.control.ack') return;
                try {
                    const message = JSON.parse(new TextDecoder().decode(payload));
                    if (message.action === 'finalize-ready') finalizeAck();
                } catch (_) {}
            });
            room.finalizeReady = finalizeReady;
            let candidateSpeaking = false, lastSpeechEnd = null;
            room.on(RoomEvent.ActiveSpeakersChanged, speakers => {
                const now = performance.now();
                if (!firstAgentAudioLogged && speakers.some(speaker => speaker.identity !== room.localParticipant.identity)) {
                    firstAgentAudioLogged = true;
                    if (roomConnectStartedAt !== null) {
                        console.info('[voice timing]', {stage: 'room_connect_to_first_agent_audio_ms',
                            ms: Math.round(now - roomConnectStartedAt)});
                    }
                }
                const candidate = speakers.some(speaker => speaker.identity === room.localParticipant.identity);
                setAgentSpeaking(speakers.some(speaker => speaker.identity !== room.localParticipant.identity));
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
                    const identity = typeof participant === 'string' ? participant : participant?.identity;
                    const isCandidate = identity === room.localParticipant.identity;
                    const attributes = reader.info?.attributes || {};
                    const segmentId = attributes['lk.segment_id'] || reader.info?.id || `${identity}-${Date.now()}`;
                    let text = '';
                    for await (const chunk of reader) {
                        text += chunk;
                        if (isCandidate) updateTranscript(candidateTranscriptSegments, segmentId, text, $('answer'));
                        else {
                            updateTranscript(agentTranscriptSegments, segmentId, text, $('interview-question'));
                            if (agentSpeaking) $('interview-question').hidden = false;
                        }
                    }
                } catch (_) { status('Transcript interrupted. Please repeat any missing words.'); }
            });
            firstAgentAudioLogged = false;
            roomConnectStartedAt = performance.now();
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
        } catch (error) {
            active = false;
            await room?.disconnect(); room = null;
            status(error.message + ' Reconnect or use manual mode to continue.');
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
        const retryingScorecard = scorecardRetryAvailable;
        if (ending && !retryingScorecard) return;
        scorecardRetryAvailable = false;
        ending = true;
        $('end-interview').disabled = true;
        $('scorecard-loading').hidden = false;
        status('Preparing your scorecard…');
        try {
            if (!retryingScorecard && room?.finalizeReady) {
                await room.localParticipant.setMicrophoneEnabled(false);
                await control('finalize');
                await Promise.race([room.finalizeReady, new Promise((_, reject) => setTimeout(() => reject(new Error('Transcript flush timed out.')), 15000))]);
            }
            if (!retryingScorecard) await room?.disconnect();
            await api('/control', {action: 'end'});
            clearTimeout(timer); void poll();
        } catch (_) {
            if (!retryingScorecard) ending = false;
            scorecardRetryAvailable = retryingScorecard;
            $('end-interview').disabled = false;
            $('scorecard-loading').hidden = true;
            $('end-interview').textContent = retryingScorecard ? 'Retry finalization' : 'End Interview';
            status('Could not finish preparing the scorecard. Retry when the connection is available.');
        }
    }
    $('end-interview').addEventListener('click', event => {
        if (!active || $('end-interview').disabled) return;
        event.stopImmediatePropagation();
        const message = scorecardRetryAvailable
            ? 'Retry the saved final evaluation and scorecard? Successfully saved evaluations will not be repeated.'
            : 'End the interview early? Only completed evaluations will be included.';
        if (!window.confirm(message)) return;
        void endInterview();
    }, true);
    async function restoreFinalizationAfterRefresh() {
        try {
            const state = await api('');
            if (state.status === 'completed') {
                window.location.assign(state.scorecard_url);
                return;
            }
            if (!state.end_requested) return;
            active = true;
            ending = true;
            livekitMode = true;
            if ($('interview-instructions').open) $('interview-instructions').close();
            if (state.status === 'error') {
                scorecardRetryAvailable = true;
                $('scorecard-loading').hidden = true;
                $('end-interview').disabled = false;
                $('end-interview').textContent = 'Retry finalization';
                $('conversation-recovery').hidden = true;
                status(state.error || 'Scorecard processing failed. Retry when ready.');
            } else {
                $('end-interview').disabled = true;
                $('scorecard-loading').hidden = false;
                status('Preparing your scorecard…');
                void poll();
            }
        } catch (_) {
            // A missing interview is normal on a fresh setup page.
        }
    }
    if (document.body.dataset.hasLivekitSession === '1') void restoreFinalizationAfterRefresh();
    window.addEventListener('pagehide', () => { active = false; clearTimeout(timer); clearTimeout(expiryTimer); void room?.disconnect(); });
})();
