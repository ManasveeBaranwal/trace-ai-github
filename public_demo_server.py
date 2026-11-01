"""Prepare a public, read-only fictional workspace on ephemeral free hosting.

The normal application never enables this mode by itself. This launcher seeds a
dedicated temporary store, checks it contains no real user accounts, disconnects
all model clients, and only then permits remote sample-manager sessions.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
PUBLIC_KIND = 'trace-ai-public-demo-v1'


def _temporary_folder(folder):
    folder = Path(folder).resolve()
    temporary_roots = {Path(tempfile.gettempdir()).resolve(), Path('/tmp').resolve()}
    if not any(folder != base and folder.is_relative_to(base) for base in temporary_roots):
        raise RuntimeError('A public demo must use its own directory under temporary storage')
    return folder


def validate_public_store(folder, metadata=None, *, seeded=False):
    """Fail closed rather than expose a normal/local production database."""
    folder = _temporary_folder(folder)
    marker = folder/'.public-demo.json'
    try:
        public = json.loads(marker.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError('Public demo storage needs its dedicated marker') from exc
    if public.get('kind') != PUBLIC_KIND or public.get('directory') != str(folder):
        raise RuntimeError('Invalid public demo storage marker')
    if metadata is None:
        try:
            metadata = json.loads((folder/'.boutique-preview.json').read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError('Missing fictional boutique store marker') from exc
    if metadata.get('kind') != 'trace-boutique-fictional-preview-v1':
        raise RuntimeError('Public demo storage is not a fictional boutique store')
    database = folder/'accounts.sqlite3'
    if database.exists():
        with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'users' in tables and db.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
                raise RuntimeError('Public demo storage must not contain real user accounts')
            if 'firms' in tables:
                firms = db.execute('SELECT id,name FROM firms').fetchall()
                if any(fid != metadata['firm_id'] or name != 'Canopy Sustainability · Sample firm'
                       for fid, name in firms):
                    raise RuntimeError('Public demo storage contains a non-sample firm')
                if seeded and len(firms) != 1:
                    raise RuntimeError('The fictional consulting firm was not seeded')
            if 'companies' in tables:
                rows = db.execute('SELECT name,firm_id FROM companies').fetchall()
                expected = {'Cedar Manufacturing', 'Harbor Services', 'Willow Foods'}
                if any(name not in expected or fid != metadata['firm_id'] for name, fid in rows):
                    raise RuntimeError('Public demo storage contains a non-sample company')
                if seeded and {name for name, _ in rows} != expected:
                    raise RuntimeError('The fictional client companies were not seeded')
    elif seeded:
        raise RuntimeError('Public demo storage was not created')
    return folder


class NoHostedModel:
    """Never make an outbound inference request from the free public demo."""
    def __getattr__(self, name):
        raise RuntimeError('AI generation is disabled in the free public demo')


def load_public_demo():
    """Return the FastAPI app only after a safe fictional demo is ready."""
    if os.getenv('TRACE_PUBLIC_DEMO', '0') != '1':
        raise RuntimeError('Set TRACE_PUBLIC_DEMO=1 explicitly to launch the public sample')
    if 'app' in sys.modules:
        raise RuntimeError('Launch the public demo in its own fresh Python process')
    folder = _temporary_folder(os.getenv('TRACE_DATA', str(Path(tempfile.gettempdir())/'trace-ai-public-demo')))
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder/'.public-demo.json'
    if not marker.exists():
        if any(folder.iterdir()):
            raise RuntimeError('Refusing to open existing unmarked data as a public demo')
        marker.write_text(json.dumps({'kind':PUBLIC_KIND, 'directory':str(folder)}))
    else:
        # Existing marked sample stores may survive a process restart. They can
        # never be substituted for unmarked account or evidence directories.
        validate_public_store(folder)
    backend = Path(os.getenv('SUSTIVIO_BACKEND', str(ROOT/'legacy_backend.py'))).resolve()
    if backend != (ROOT/'legacy_backend.py').resolve():
        raise RuntimeError('The public demo uses only its bundled extraction module')
    os.environ['SUSTIVIO_BACKEND'] = str(backend)
    from boutique_server import load_preview
    product = load_preview(folder)
    validate_public_store(folder, seeded=True)
    # No installed model, remote endpoint or per-request fallback can incur
    # inference usage. Retrieval remains available over the fictional records.
    product.legacy.ai = NoHostedModel()
    product.app.state.boutique_ai.ai = None
    product.app.state.boutique_ai.model = None
    product.app.state.coefficient_knowledge.ai = None
    product.app.state.coefficient_knowledge.model = None
    os.environ['TRACE_REQUIRE_LOGIN'] = '1'
    product.app.state.public_demo_active = True
    return product.app

