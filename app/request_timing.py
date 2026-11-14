"""Measure application work, including session reads and writes, before headers."""
import time


class RequestTiming:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        started = time.perf_counter()

        def timed_start(status, headers, exc_info=None):
            elapsed = (time.perf_counter() - started) * 1000
            headers.append(('Server-Timing', f'app;dur={elapsed:.2f}'))
            return start_response(status, headers, exc_info)

        return self.application(environ, timed_start)
