"""Content-versioned asset URLs: reuse downloads without stale release assets."""
import hashlib
from pathlib import Path

from flask import request


def init_assets(app):
    root = Path(app.static_folder)
    versions = {}
    for path in root.rglob('*'):
        if not path.is_file() or path.suffix not in {'.js', '.css', '.svg', '.woff', '.woff2'}:
            continue
        name = path.relative_to(root).as_posix()
        try:
            versions[name] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except OSError:
            # Optional fingerprinting must not prevent the application booting.
            # Unversioned files retain normal Flask cache validation/404 handling.
            app.logger.warning('Asset fingerprint unavailable: %s', name)


    @app.url_defaults
    def version_static(endpoint, values):
        if endpoint == 'static' and values.get('filename') in versions:
            values.setdefault('v', versions[values['filename']])

    @app.after_request
    def cache_versioned_assets(response):
        filename = (request.view_args or {}).get('filename')
        if (app.config.get('VERSIONED_ASSET_CACHE', False) and request.endpoint == 'static'
                and response.status_code in (200, 304) and filename in versions
                and request.args.get('v') == versions[filename]):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return response
