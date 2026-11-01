"""Launch the boutique design workspace against its own fictional database."""
import json
import os
from pathlib import Path
import sys
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent


def load_preview(storage=None):
    folder = Path(storage or os.getenv('TRACE_BOUTIQUE_DATA', ROOT/'data/boutique-preview')).resolve()
    if folder == (ROOT/'data').resolve():
        raise RuntimeError('The boutique preview cannot use the main data directory')
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder/'.boutique-preview.json'
    if not marker.exists():
        if (folder/'accounts.sqlite3').exists() or (folder/'trace.sqlite3').exists():
            raise RuntimeError('Refusing to seed an existing unmarked database')
        marker.write_text(json.dumps({'kind':'trace-boutique-fictional-preview-v1',
            'firm_id':str(uuid.uuid4()), 'profile_id':str(uuid.uuid4()),
            'created_at':datetime.now(timezone.utc).isoformat()}))
    os.environ.update(TRACE_DATA=str(folder), TRACE_REQUIRE_LOGIN='0', TRACE_TWO_ACCESS='0', TRACE_BOUTIQUE_PREVIEW='1')
    sys.path.insert(0, str(ROOT))
    import app as product
    from boutique_sample import seed_boutique_sample
    seed_boutique_sample(product, json.loads(marker.read_text()))
    return product


if __name__ == '__main__':
    import uvicorn
    product = load_preview()
    print('Boutique workspace: http://localhost:8004/boutique', flush=True)
    print('Separate fictional sample store: '+str(product.DATA), flush=True)
    uvicorn.run(product.app, host='127.0.0.1', port=int(os.getenv('TRACE_BOUTIQUE_PORT','8004')))
