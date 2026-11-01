"""Provision the first owner in a local terminal; never pass passwords as arguments."""
import getpass
import os
import re
from pathlib import Path
from security import Accounts

if __name__ == '__main__':
    accounts = Accounts(Path(os.getenv('TRACE_DATA',str(Path(__file__).resolve().parent/'data')))/'accounts.sqlite3')
    if accounts.has_master():
        raise SystemExit('A master account already exists. Use /login to sign in.')
    print('Create the Trace AI master owner. Password input is hidden.')
    email = input('Email: ').strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise SystemExit('Enter a valid email address.')
    name = input('Display name: ').strip()
    password = getpass.getpass('Password (12–256 characters): ')
    if password != getpass.getpass('Confirm password: '):
        raise SystemExit('Passwords do not match. No account created.')
    try:
        accounts.create_master(email, name or 'Master owner', password)
    except ValueError as exc:
        raise SystemExit(str(exc))
    print('Master owner created. Sign in at http://127.0.0.1:8002/admin')
