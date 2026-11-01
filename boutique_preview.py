"""Explicit loopback-only entry for an isolated fictional boutique workspace."""
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from hierarchy import home, role
from security import SESSION_SECONDS


def install_boutique_preview(app, accounts, root):
    active = os.getenv('TRACE_BOUTIQUE_PREVIEW', '0') == '1'
    public_requested = os.getenv('TRACE_PUBLIC_DEMO', '0') == '1'
    # Only the dedicated public launcher may activate remote sample sessions,
    # after seeding and revalidating its temporary fictional store.
    app.state.public_demo_active = False
    metadata = None
    if active:
        marker = accounts.path.parent / '.boutique-preview.json'
        if not marker.is_file():
            raise RuntimeError('Boutique preview requires its isolated sample-data marker. Use run-boutique.sh.')
        metadata = json.loads(marker.read_text())
        if metadata.get('kind') != 'trace-boutique-fictional-preview-v1':
            raise RuntimeError('This is not an isolated boutique preview store')
        if public_requested:
            from public_demo_server import validate_public_store
            validate_public_store(accounts.path.parent, metadata)
        with accounts.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS boutique_preview_sessions(
                token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at REAL NOT NULL)''')
    app.state.boutique_preview_active = active

    def preview_user():
        with accounts.db() as db:
            firm = db.execute('SELECT * FROM firms WHERE id=? AND active=1', (metadata['firm_id'],)).fetchone()
        if not firm:
            return None
        return {'id': metadata['profile_id'], 'name': 'Aarav · Sample firm manager',
                'email': '', 'role': 'consultant_manager', 'firm_id': firm['id'],
                'company_id': None, 'active': True, 'created_at': metadata['created_at'],
                'local_only': not app.state.public_demo_active,
                'public_demo': bool(app.state.public_demo_active),
                'read_only': bool(app.state.public_demo_active)}

    def resolve(token):
        if not active or not token:
            return None
        with accounts.db() as db:
            row = db.execute('SELECT csrf,expires_at FROM boutique_preview_sessions WHERE token_hash=? AND expires_at>?',
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        user = preview_user() if row else None
        return {'user': user, 'csrf': row['csrf'], 'expires_at': row['expires_at']} if user else None

    def revoke(token):
        if active and token:
            with accounts.db() as db:
                db.execute('DELETE FROM boutique_preview_sessions WHERE token_hash=?',
                           (hashlib.sha256(token.encode()).hexdigest(),))

    app.state.resolve_boutique_session = resolve
    app.state.revoke_boutique_session = revoke

    @app.get('/boutique', include_in_schema=False)
    def boutique_page(request: Request):
        if request.state.user:
            if role(request.state.user) not in {'consultant_manager', 'consultant'}:
                return RedirectResponse(home(request.state.user), 303)
            return FileResponse(Path(root)/'static/boutique.html', headers={'Cache-Control': 'no-store'})
        can_enter = (active and (request.state.local_enabled or app.state.public_demo_active)
                     and request.headers.get('sec-fetch-site') != 'cross-site'
                     and request.headers.get('sec-fetch-dest', 'document') == 'document'
                     and not request.cookies.get('trace_session')
                     and not request.cookies.get('trace_company_manager'))
        if not can_enter:
            return RedirectResponse('/login', 303)
        if not preview_user():
            raise HTTPException(409, 'The sample consulting firm is inactive')
        token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
        with accounts.db() as db:
            db.execute('DELETE FROM boutique_preview_sessions WHERE expires_at<=?', (time.time(),))
            db.execute('INSERT INTO boutique_preview_sessions VALUES(?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), csrf, time.time()+SESSION_SECONDS))
        response = RedirectResponse('/boutique', 303)
        response.set_cookie('trace_boutique_session', token, max_age=SESSION_SECONDS,
                            httponly=True, samesite='strict',
                            secure=request.url.scheme=='https' or os.getenv('TRACE_COOKIE_SECURE')=='1', path='/')
        return response
