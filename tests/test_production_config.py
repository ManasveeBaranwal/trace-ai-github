"""Hosted access retains authentication and explicit trusted-host boundaries."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from access import install_access
from security import Accounts

class ProductionConfig(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='trace-hosted-config-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.accounts = Accounts(self.root/'accounts.sqlite3')

    def build(self, **settings):
        app = FastAPI()
        @contextmanager
        def connection():
            db = sqlite3.connect(':memory:')
            try:
                yield db
            finally:
                db.close()
        environment = {'TRACE_REQUIRE_LOGIN':'1', 'TRACE_ALLOWED_HOSTS':'localhost,127.0.0.1', 'RENDER_EXTERNAL_HOSTNAME':''}
        environment.update(settings)
        with patch.dict(os.environ, environment):
            install_access(app,self.accounts,lambda bid:{},lambda record:None,connection,self.root,{},lambda bid:None)
        @app.get('/health')
        def health():
            return {'status':'ok'}
        return app

    def test_configured_cloud_host_does_not_grant_anonymous_access(self):
        app = self.build(TRACE_ALLOWED_HOSTS='demo.example.com')
        client = TestClient(app,base_url='https://demo.example.com',client=('203.0.113.4',45000))
        with patch.dict(os.environ,{'TRACE_REQUIRE_LOGIN':'1'}):
            self.assertEqual(client.get('/health').status_code,200)
            self.assertEqual(client.get('/api/bills').status_code,401)
            entry = client.get('/super-admin',follow_redirects=False)
            self.assertNotEqual(entry.status_code,200)
            self.assertNotIn('trace_local_master',entry.headers.get('set-cookie',''))
            self.assertIsNone(client.get('/auth/session').json()['user'])

    def test_unknown_host_rejected_and_render_hostname_added(self):
        app = self.build(RENDER_EXTERNAL_HOSTNAME='trace-example.onrender.com')
        unknown = TestClient(app,base_url='https://unknown.example.com')
        self.assertEqual(unknown.get('/health').status_code,400)
        render = TestClient(app,base_url='https://trace-example.onrender.com')
        self.assertEqual(render.get('/health').status_code,200)

    def test_invalid_host_configuration_rejected(self):
        with self.assertRaises(RuntimeError):
            self.build(TRACE_ALLOWED_HOSTS='*')
        with self.assertRaises(RuntimeError):
            self.build(RENDER_EXTERNAL_HOSTNAME='https://wrong.example.com/path')
