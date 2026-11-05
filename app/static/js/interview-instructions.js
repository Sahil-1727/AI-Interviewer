document.addEventListener('DOMContentLoaded', () => {
    const dialog = document.getElementById('interview-instructions');
    const start = document.getElementById('start-interview');
    const begin = document.getElementById('begin-instructions');
    const question = document.getElementById('question-content');
    function revealQuestion() { question.style.visibility = ''; }
    const status = document.getElementById('readiness-status');
    let generation = 0;
    let checking = false;
    let microphoneReady = false;
    const acknowledged = document.getElementById('instructions-acknowledged');
    function buttons() {
        begin.disabled = start.disabled || !microphoneReady || !acknowledged.checked;
        const livekit = document.getElementById('begin-livekit');
        if (livekit) livekit.disabled = begin.disabled;
        document.getElementById('check-microphone').disabled = checking;
        document.getElementById('check-camera').disabled = checking;
    }
    new MutationObserver(buttons).observe(start, {attributes: true, attributeFilter: ['disabled']});
    async function check(kind) {
        if (!navigator.mediaDevices?.getUserMedia) {
            status.textContent = kind === 'microphone'
                ? 'Microphone access is unavailable in this browser. Use manual mode.'
                : 'Camera access is unavailable. The camera is optional.';
            return;
        }
        const token = ++generation;
        if (kind === 'microphone') microphoneReady = false;
        checking = true; buttons();
        status.textContent = `Checking ${kind} access…`;
        try {
            const stream = await navigator.mediaDevices.getUserMedia(kind === 'microphone' ? {audio: true, video: false} : {video: true, audio: false});
            const available = (kind === 'microphone' ? stream.getAudioTracks() : stream.getVideoTracks()).some(track => track.readyState === 'live');
            stream.getTracks().forEach(track => track.stop());
            if (token === generation && kind === 'microphone') microphoneReady = available;
            if (token === generation) status.textContent = available
                ? `${kind === 'microphone' ? 'Microphone' : 'Camera'} access is available.`
                : kind === 'microphone'
                    ? 'No microphone was detected. Check again or use manual mode.'
                    : 'No camera was detected. The camera is optional.';
        } catch (_) {
            if (token === generation) status.textContent = kind === 'microphone'
                ? 'Unable to access the microphone. Allow access and check again, or use manual mode.'
                : 'Unable to access the camera. You can still continue; the camera is optional.';
        } finally {
            if (token === generation) { checking = false; buttons(); }
        }
    }
    acknowledged.addEventListener('change', buttons);
    document.getElementById('check-microphone').addEventListener('click', () => void check('microphone'));
    document.getElementById('check-camera').addEventListener('click', () => void check('camera'));
    begin.addEventListener('click', () => { ++generation; dialog.close(); revealQuestion(); start.click(); });
    document.getElementById('begin-livekit')?.addEventListener('click', () => {
        ++generation; dialog.close(); revealQuestion();
        document.dispatchEvent(new CustomEvent('interview:livekit-start'));
    });
    start.addEventListener('click', event => {
        // Manual mode can be chosen without a microphone. Starting voice from
        // there must still go through the acknowledgment and readiness gate.
        if (document.body.dataset.interviewMode !== 'voice' && (!microphoneReady || !acknowledged.checked)) {
            event.preventDefault();
            event.stopImmediatePropagation();
            if (typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal();
            buttons();
        }
    }, true);
    document.getElementById('instructions-manual').addEventListener('click', () => {
        ++generation; dialog.close(); revealQuestion(); document.getElementById('manual-mode').click();
    });
    dialog.addEventListener('cancel', event => event.preventDefault());
    document.addEventListener('interview:ready', event => {
        if (event.detail.existing) revealQuestion();
        if (!event.detail.existing && typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal();
        buttons();
    });
    window.addEventListener('pagehide', () => { ++generation; });
    buttons();
});
