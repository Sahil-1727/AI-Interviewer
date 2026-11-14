# Latency and measurement

Public static assets and the readiness route bypass browser-session SQL reads and
writes. A candidate cookie on an asset request is neither loaded nor replaced.
Readiness still performs its own explicit database checks. Authenticated pages
and API routes continue loading and verifying their sessions normally.

External scripts use defer in document order so the browser can fetch them in
parallel while rendering. The CSRF wrapper is declared before the controllers;
defer preserves that execution order and runs before DOMContentLoaded.

Responses include Server-Timing: app;dur=... measured from WSGI entry to response
headers, including session loading and saving. Inspect this in browser Network
Timings. It excludes network transit, streaming response bodies and audio playback;
it is not voice response latency.

Use the existing voice metrics for speech completion, LLM first token, TTS first
byte and audible playback. Measure cold startup separately from warm conversation
and report medians and slow-tail values across multiple real interviews. Compare
application timing against browser total duration to identify network overhead.

The existing conversational worker already streams model output, uses preemptive
generation and persists transcripts asynchronously. These mechanisms must remain
outside blocking per-answer Flask polling. Keep scoring separate from speech.
Provider/region/model changes require real-audio measurements and available quota;
no minimum response time or improvement in provider inference is claimed here.

Verification: test_request_latency.py checks that static assets (including 404s)
and readiness perform zero session lookups/writes while dynamic routes still load
saved state. It uses SQLite and mocks, not Neon or paid inference.
