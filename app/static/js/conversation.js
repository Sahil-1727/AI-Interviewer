/* HTTP conversation controller. Audio hardware/STT remain in voice.js. */
(function (root) {
    const transitions = {
        IDLE: ['PREPARING', 'RECOVERING', 'PAUSED', 'COMPLETED'],
        PREPARING: ['AI_SPEAKING', 'THINKING', 'LISTENING', 'EVALUATING', 'RECOVERING', 'PAUSED', 'ERROR', 'COMPLETED'],
        AI_SPEAKING: ['THINKING', 'PAUSED', 'ERROR', 'RECOVERING'],
        THINKING: ['LISTENING', 'POSSIBLE_END_OF_TURN', 'TRANSCRIBING', 'PREPARING', 'PAUSED', 'ERROR', 'RECOVERING'],
        LISTENING: ['THINKING', 'POSSIBLE_END_OF_TURN', 'TRANSCRIBING', 'PAUSED', 'ERROR', 'RECOVERING'],
        POSSIBLE_END_OF_TURN: ['LISTENING', 'THINKING', 'TRANSCRIBING', 'PREPARING', 'PAUSED', 'ERROR', 'RECOVERING'],
        TRANSCRIBING: ['EVALUATING', 'THINKING', 'LISTENING', 'PREPARING', 'PAUSED', 'ERROR', 'RECOVERING'],
        EVALUATING: ['PREPARING', 'PAUSED', 'RECOVERING', 'COMPLETED', 'ERROR'],
        PAUSED: ['PREPARING', 'THINKING', 'LISTENING', 'TRANSCRIBING', 'EVALUATING', 'RECOVERING', 'ERROR', 'COMPLETED', 'IDLE'],
        RECOVERING: ['PREPARING', 'THINKING', 'TRANSCRIBING', 'EVALUATING', 'PAUSED', 'ERROR', 'COMPLETED', 'IDLE'],
        ERROR: ['RECOVERING', 'PAUSED', 'PREPARING', 'THINKING', 'IDLE'],
        COMPLETED: ['IDLE'],
    };
    class ConversationMachine {
        constructor() { this.state = 'IDLE'; }
        move(next) {
            if (next === this.state) return;
            if (!transitions[this.state]?.includes(next)) throw new Error(`Invalid conversation transition ${this.state} → ${next}`);
            this.state = next;
        }
    }
    if (typeof module !== 'undefined' && module.exports) module.exports = {ConversationMachine};
    else root.ConversationMachine = ConversationMachine;
})(typeof window === 'undefined' ? globalThis : window);

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', () => {
    const $ = id => document.getElementById(id);
    if (!$('start-interview')) return;
    const voice = window.InterviewVoice;
    const config = JSON.parse($('voice-config').textContent);
    const machine = new window.ConversationMachine();
    const answer = $('answer');
    let snapshot = null;
    let active = false;
    let paused = true;
    let playedTurn = null;
    let pending = null;
    let pollTimer = null;
    let operation = 0;
    let starting = false;
    let awaitingPlayback = false;
    let lastEndOfSpeech = null;
    let failureKind = null;
    let bootstrapping = true;
    let terminating = false;
    const metrics = [];
    const completedCaptures = new Set();
    window.voiceMetrics = metrics;

    function metric(stage, ms) {
        if (!Number.isFinite(ms)) return;
        const sample = {stage, ms: Math.round(ms * 100) / 100, turn_id: snapshot?.turn_id, at: new Date().toISOString()};
        metrics.push(sample);
        if (metrics.length > 200) metrics.shift();
        console.info('[voice timing]', sample);
    }

    function setState(next, message) {
        machine.move(next);
        document.body.dataset.conversationState = next;
        // Keep the existing visual audio states and use explicit orchestration
        // states independently from the microphone's short-lived operations.
        $('interviewer-state').textContent = {
            IDLE: 'Ready', PREPARING: 'Preparing', AI_SPEAKING: 'Speaking', THINKING: 'Thinking',
            LISTENING: 'Listening', POSSIBLE_END_OF_TURN: 'Thinking', TRANSCRIBING: 'Transcribing',
            EVALUATING: 'Considering your answer', PAUSED: 'Paused', RECOVERING: 'Reconnecting',
            COMPLETED: 'Complete', ERROR: 'Needs attention'
        }[next];
        if (message) $('voice-status').textContent = message;
        $('start-interview').disabled = terminating || starting || bootstrapping || (active && !paused);
        $('start-interview').textContent = active ? 'Resume Interview' : 'Start Interview';
        $('conversation-recovery').hidden = !['ERROR', 'RECOVERING'].includes(next);
        $('retry-conversation').textContent = snapshot?.status === 'error' ? 'Retry processing' : 'Reconnect / retry';
        $('pause-voice').disabled = terminating || !active || next === 'COMPLETED';
        $('pause-voice').textContent = paused ? 'Resume' : 'Pause Interview';
        $('end-interview').disabled = terminating || !active || next === 'COMPLETED';
        $('end-interview').disabled ||= !snapshot;
        $('manual-mode').disabled = terminating;
        $('microphone-activity').hidden = !['THINKING', 'LISTENING', 'POSSIBLE_END_OF_TURN'].includes(next);
        answer.readOnly = voice.isBusy() || (active && (next === 'EVALUATING' || ['evaluating', 'scoring'].includes(snapshot?.status)));
    }

    function saveDraft() {
        if (!snapshot) return;
        try {
            sessionStorage.setItem('voice-interview:' + snapshot.interview_id,
                JSON.stringify({turn_id: snapshot.turn_id, text: answer.value, pending}));
        } catch (_) { /* Storage may be disabled. Server-accepted answers still persist. */ }
    }

    function restoreDraft(state) {
        try {
            const draft = JSON.parse(sessionStorage.getItem('voice-interview:' + state.interview_id) || 'null');
            if (draft?.turn_id === state.turn_id) {
                answer.value = draft.text || state.draft || '';
                pending = draft.pending || null;
            } else answer.value = state.draft || '';
        } catch (_) { answer.value = state.draft || ''; }
    }

    async function api(path = '', body) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), config.REQUEST_TIMEOUT_SECONDS * 1000);
        try {
            const response = await fetch('/api/conversation' + path, {
                method: body === undefined ? 'GET' : 'POST', signal: controller.signal,
                headers: {'Content-Type': 'application/json'},
                ...(body === undefined ? {} : {body: JSON.stringify(body)})
            });
            const data = await response.json();
            if (!response.ok) throw Object.assign(new Error(data.error || 'Interview service unavailable.'), {status: response.status});
            return data;
        } finally { clearTimeout(timeout); }
    }

    function renderQuestion(state) {
        if (!state.question) return;
        $('interview-question').textContent = state.question.question;
        $('question-count').textContent = state.is_follow_up ? 'FOLLOW-UP QUESTION'
            : `QUESTION ${String(state.question_number).padStart(2, '0')} / ${String(state.total_questions).padStart(2, '0')}`;
        $('question-skill').textContent = state.question.skill || 'General Knowledge';
        $('question-type').textContent = state.question.type.replaceAll('_', ' ');
        $('followup-note').hidden = !state.is_follow_up;
        $('answer-form').action = state.is_follow_up ? '/submit-follow-up' : '/submit-answer';
        $('main-progress').max = state.total_questions;
        $('main-progress').value = state.completed_questions;
        $('progress-count').textContent = `${state.completed_questions} / ${state.total_questions}`;
        $('progress-note').textContent = state.is_follow_up ? "Let's explore your answer a little further." : 'One question at a time. Take the space you need.';
    }

    function recover(error, kind = 'network') {
        if (!active) return;
        failureKind = kind;
        paused = true;
        saveDraft();
        clearTimeout(pollTimer);
        void voice.pause();
        setState(kind === 'network' ? 'RECOVERING' : 'ERROR', error.message + ' Your transcript is kept. Use retry or manual mode.');
    }

    function schedulePoll() {
        clearTimeout(pollTimer);
        const token = operation;
        pollTimer = setTimeout(async () => {
            try {
                const state = await api();
                if (token === operation && active) await receive(state);
            } catch (error) { if (token === operation) recover(error); }
        }, config.POLL_INTERVAL_MS);
    }

    async function playCurrent() {
        if (!active || paused || !snapshot?.question) return;
        awaitingPlayback = true;
        setState('PREPARING', 'Preparing the next question.');
        await voice.speak();
    }

    async function receive(state) {
        if (snapshot && state.version < snapshot.version) return;
        const changed = snapshot && snapshot.turn_id !== state.turn_id;
        if (snapshot && snapshot.interview_id !== state.interview_id) throw new Error('The active interview changed. Return to the setup page.');
        if (changed) {
            pending = null;
            answer.value = '';
            voice.reset();
        }
        snapshot = state;
        renderQuestion(state);
        for (const [stage, ms] of Object.entries(state.timings || {})) metric(stage, ms);
        if (state.control_action) {
            pending = null;
            answer.value = '';
            saveDraft();
            voice.reset();
            if (!paused) {
                setState('PREPARING');
                if (state.control_action === 'repeat') await playCurrent();
                else {
                    awaitingPlayback = false;
                    setState('THINKING', state.control_action === 'complete'
                        ? "I didn't catch an answer yet. Take your time."
                        : 'Of course. Take your time.');
                    await voice.listen();
                }
            }
            return;
        }
        if (state.status === 'completed') {
            pending = null; saveDraft();
            try { sessionStorage.removeItem('voice-ending:' + state.interview_id); } catch (_) {}
            setState('COMPLETED', 'Your interview is complete. Opening your scorecard…');
            await voice.pause();
            window.location.assign(state.scorecard_url);
            return;
        }
        if (state.status === 'error') {
            failureKind = 'processing'; paused = true;
            if (state.draft) answer.value = state.draft;
            saveDraft();
            setState('ERROR', state.error);
            return;
        }
        if (state.status === 'evaluating' || state.status === 'scoring' || state.status === 'ending') {
            if (state.end_requested) {
                terminating = true; paused = true;
                setState('PAUSED', 'Preparing your early interview result.');
                schedulePoll();
                return;
            }
            if (!paused) setState('EVALUATING', state.status === 'scoring' ? 'Preparing your final scorecard.' : 'Considering your answer.');
            else setState('PAUSED', 'Paused. Your saved response is still being processed.');
            schedulePoll();
            return;
        }
        if (state.status === 'ended' || state.end_requested) {
            paused = true; terminating = true;
            setState('PAUSED', 'Interview ended. Preparing your result.');
            schedulePoll();
            return;
        }
        if (state.status === 'manual') return;
        if (state.paused || paused) {
            paused = true;
            setState('PAUSED', 'Paused. Resume when you are ready.');
            return;
        }
        if (pending && pending.turn_id === state.turn_id) {
            await postAnswer(pending); // Exact same key after an ambiguous HTTP failure.
            return;
        }
        if (playedTurn !== state.turn_id) {
            playedTurn = state.turn_id; saveDraft();
            await playCurrent();
        }
    }

    async function postAnswer(payload, retry = false) {
        if (!active || (terminating && !retry)) return;
        pending = payload; saveDraft();
        const token = operation;
        setState('EVALUATING', 'Processing your response.');
        try {
            const state = await api(retry ? '/retry' : '/answer', payload);
            if (token === operation && active) await receive(state);
        } catch (error) {
            if (token !== operation) return;
            if (error.status === 409) {
                try { await receive(await api()); return; } catch (_) { /* Show recoverable failure below. */ }
            }
            recover(error);
        }
    }

    function newPayload() {
        return {interview_id: snapshot.interview_id, turn_id: snapshot.turn_id,
            request_id: crypto.randomUUID().replaceAll('-', ''), answer: answer.value.trim()};
    }

    async function begin() {
        if (starting || bootstrapping || answer.form.dataset.submitting === 'true') return;
        starting = true;
        active = true; paused = false;
        document.body.dataset.interviewMode = 'voice';
        voice.setManaged(true);
        setState('PREPARING', 'Preparing your interview.');
        try {
            await voice.unlock();
            const state = await api('/start', {});
            if (!snapshot) { snapshot = state; restoreDraft(state); }
            if (state.paused) await api('/control', {action: 'resume'});
            await receive({...state, paused: false});
        } catch (error) { recover(error); }
        finally { starting = false; $('start-interview').disabled = terminating || (active && !paused); }
    }

    async function pauseInterview() {
        if (!active) return;
        paused = true;
        await voice.pause(); saveDraft();
        setState('PAUSED', 'Interview paused. Your transcript is kept.');
        try { await receive(await api('/control', {action: 'pause'})); }
        catch (error) { failureKind = 'network'; $('voice-status').textContent = 'Paused locally. Connection unavailable; reconnect before continuing.'; $('conversation-recovery').hidden = false; }
    }

    async function resumeInterview() {
        if (!active) return begin();
        if (voice.hasRecovery()) return retry();
        paused = false;
        setState('PREPARING', 'Resuming your interview.');
        try {
            const state = await api('/control', {action: 'resume'});
            if (state.status === 'ready' && playedTurn === state.turn_id && !pending) {
                snapshot = state; awaitingPlayback = false;
                await voice.listen();
            } else await receive(state);
        } catch (error) { recover(error); }
    }

    async function repeatQuestion() {
        if (!active || !snapshot || ['evaluating', 'scoring'].includes(snapshot.status)) return;
        try {
            if (paused) { snapshot = await api('/control', {action: 'resume'}); paused = false; }
            // voice.speak saves active capture before playback and never emits
            // an answer-complete event for this intentional interruption.
            await voice.pause();
            await playCurrent();
        } catch (error) { recover(error); }
    }

    async function retry() {
        if (!active) return begin();
        try {
            setState('RECOVERING', 'Recovering your saved interview.');
            const state = await api();
            if (terminating && !state.end_requested) return endInterview();
            if (state.status === 'error') {
                snapshot = state;
                answer.value = state.draft || answer.value;
                if (!state.end_requested) await api('/control', {action: 'resume'});
                paused = false;
                await postAnswer(newPayload(), true);
            } else if (state.end_requested) {
                await receive(state);
            } else {
                await api('/control', {action: 'resume'});
                paused = false;
                if (state.status === 'ready' && !pending && playedTurn === state.turn_id) {
                    if (failureKind === 'audio') await playCurrent();
                    else if (voice.hasRecovery()) {
                        setState('TRANSCRIBING', 'Recovering your saved recording.');
                        await voice.retryTranscription();
                    }
                    else { awaitingPlayback = false; setState('THINKING'); await voice.listen(); }
                } else await receive({...state, paused: false});
            }
        } catch (error) { recover(error); }
    }

    $('start-interview').addEventListener('click', () => void (active ? resumeInterview() : begin()));
    $('retry-conversation').addEventListener('click', () => void retry());
    $('manual-mode').addEventListener('click', async () => {
        const wasActive = active;
        try {
            if (active) {
                await pauseInterview();
                const state = await api('/control', {action: 'manual'});
                renderQuestion(state);
            }
            active = false; paused = true; pending = null; ++operation; clearTimeout(pollTimer);
            voice.setManaged(false);
            document.body.dataset.interviewMode = 'manual';
            setState('IDLE', 'Manual mode. Type or record an answer, then review and submit it.');
            $('answer-label').textContent = 'Your answer';
        } catch (error) { if (wasActive) recover(error); }
    });
    async function endInterview() {
        terminating = true;
        paused = true;
        ++operation;
        clearTimeout(pollTimer);
        pending = null;
        try { sessionStorage.setItem('voice-ending:' + snapshot.interview_id, 'true'); } catch (_) {}
        voice.discard();
        setState('PAUSED', 'Ending the interview and preparing your result.');
        try {
            await receive(await api('/control', {action: 'end'}));
        } catch (error) {
            failureKind = 'end';
            setState('RECOVERING', 'Could not reach the interview service. Reconnect to finish ending the interview.');
        }
    }
    $('end-interview').addEventListener('click', () => {
        if (!window.confirm('End the interview early? Only answers already evaluated will be included. The current unprocessed answer will not be scored.')) {
            voice.resetSilence();
            return;
        }
        void endInterview();
    });
    document.addEventListener('voice:toggle-pause', () => void (paused ? resumeInterview() : pauseInterview()));
    document.addEventListener('voice:resume', () => void resumeInterview());
    document.addEventListener('voice:repeat', () => void repeatQuestion());
    document.addEventListener('voice:hide', () => void pauseInterview());
    document.addEventListener('voice:draft', () => { if (active) saveDraft(); });
    document.addEventListener('voice:level', event => { $('microphone-level').value = event.detail.level; });
    document.addEventListener('voice:timing', event => metric(event.detail.stage, event.detail.ms));
    document.addEventListener('voice:endpoint', event => {
        lastEndOfSpeech = event.detail.lastActivity;
        metric('endpoint_ms', event.detail.at - event.detail.lastActivity);
    });
    document.addEventListener('voice:playback', event => {
        awaitingPlayback = false;
        if (lastEndOfSpeech !== null) { metric('end_of_speech_to_playback_ms', event.detail.at - lastEndOfSpeech); lastEndOfSpeech = null; }
    });
    document.addEventListener('voice:state', event => {
        if (!active || terminating) return;
        const {state, message} = event.detail;
        if (machine.state === 'COMPLETED') return;
        if (state === 'error') {
            failureKind = /audio|playback/i.test(message) ? 'audio' : 'microphone';
            paused = true; awaitingPlayback = false; setState('ERROR', message); saveDraft(); return;
        }
        const mapped = {speaking: 'AI_SPEAKING', listening: 'LISTENING', possible_end: 'POSSIBLE_END_OF_TURN',
            thinking: awaitingPlayback ? 'PREPARING' : 'THINKING', transcribing: 'TRANSCRIBING', paused: 'PAUSED'}[state];
        if (mapped && !['EVALUATING', 'RECOVERING', 'ERROR'].includes(machine.state)) setState(mapped, message);
    });
    document.addEventListener('voice:answer', event => {
        if (!active || terminating || paused || !snapshot || pending || snapshot.status !== 'ready') return;
        if (machine.state !== 'TRANSCRIBING' || voice.isBusy() || completedCaptures.has(event.detail.captureId)) return;
        if (!event.detail.transcript.trim()) return;
        completedCaptures.add(event.detail.captureId);
        void postAnswer(newPayload());
    });
    document.addEventListener('voice:empty', event => {
        if (active && !terminating && !paused && machine.state === 'TRANSCRIBING' && !voice.isBusy() && !completedCaptures.has(event.detail.captureId)) {
            completedCaptures.add(event.detail.captureId);
            setState('THINKING', "I didn't catch a clear answer. Please try again; we're on the same question.");
            void voice.listen();
        }
    });
    document.addEventListener('voice:limit', event => {
        if (active && !terminating) { void pauseInterview(); $('voice-status').textContent = event.detail.message; }
    });
    window.addEventListener('pagehide', () => {
        saveDraft(); ++operation; clearTimeout(pollTimer);
    });
    window.addEventListener('pageshow', event => { if (event.persisted) void bootstrap(); });

    async function bootstrap() {
        document.body.dataset.interviewMode = 'idle';
        try {
            const state = await api();
            if (state.status === 'manual' || state.status === 'ended') return;
            snapshot = state; active = true; paused = true; playedTurn = null;
            voice.setManaged(true); restoreDraft(state); renderQuestion(state);
            document.body.dataset.interviewMode = 'voice';
            let endIntent = false;
            try { endIntent = sessionStorage.getItem('voice-ending:' + state.interview_id) === 'true'; } catch (_) {}
            if (state.status === 'completed') await receive(state);
            else {
                setState('PAUSED', 'Your interview is saved. Select Resume Interview to continue.');
                if (endIntent || state.end_requested) {
                    terminating = true;
                    if (!state.end_requested) await endInterview();
                    else await receive(state);
                } else {
                    if (['evaluating', 'scoring'].includes(state.status)) schedulePoll();
                    if (state.status === 'error') await receive(state);
                }
            }
        } catch (error) {
            if (error.status !== 404 && error.status !== 410) $('voice-status').textContent = 'Connection unavailable. Start again when connected, or choose manual mode.';
        } finally {
            bootstrapping = false;
            $('start-interview').disabled = terminating || starting || (active && !paused);
            document.dispatchEvent(new CustomEvent('interview:ready', {detail: {existing: active}}));
        }
    }
    setState('IDLE', 'Start once, then answer naturally. Your interviewer will guide the conversation.');
    void bootstrap();
});
