/* Add the session token only to same-origin state-changing browser requests. */
(() => {
    const token = document.querySelector('meta[name="csrf-token"]')?.content;
    const originalFetch = window.fetch;
    window.fetch = function(input, options = {}) {
        const url = new URL(input instanceof Request ? input.url : input, location.href);
        const method = (options.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
        if (url.origin === location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
            const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
            headers.set('X-CSRF-Token', token);
            options = {...options, headers};
        }
        return originalFetch.call(this, input, options);
    };
    const open = XMLHttpRequest.prototype.open;
    const send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        this._csrfRequired = new URL(url, location.href).origin === location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase());
        return open.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function(...args) {
        if (this._csrfRequired) this.setRequestHeader('X-CSRF-Token', token);
        return send.apply(this, args);
    };
})();
