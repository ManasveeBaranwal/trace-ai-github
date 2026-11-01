"""Serve the authenticated product or the explicitly selected fictional demo."""
import os


def load_app():
    if os.getenv('TRACE_PUBLIC_DEMO', '0') == '1':
        from public_demo_server import load_public_demo
        return load_public_demo()
    from app import app
    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(load_app(), host='0.0.0.0', port=int(os.getenv('PORT', '8000')),
                proxy_headers=True,
                forwarded_allow_ips=os.getenv('FORWARDED_ALLOW_IPS', '127.0.0.1'))
