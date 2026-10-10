"""Pure business rules, shared by bot and tests."""
import hashlib
import hmac
import re
from urllib.parse import urlparse

ADMIN_DEFAULT = 8051325598
DAILY_LIMIT = 50
COUNTS = (5, 10)
STAGES = ('Не связывался', 'Написал', 'Ответил', 'Не подходит', 'Клиент')

def key_digest(key, secret):
    return hmac.new(secret.encode(), key.strip().upper().encode(), hashlib.sha256).hexdigest()

def org_id(url):
    p = urlparse(url)
    if p.scheme != 'https' or p.hostname not in ('yandex.ru', 'yandex.com'):
        return None
    m = re.match(r'^/maps/org/(?:[^/]+/)?(\d+)(?:/|$)', p.path)
    return m.group(1) if m else None

def canonical_url(url):
    oid = org_id(url)
    return f'https://yandex.ru/maps/org/{oid}/' if oid else None

def allowed_count(value, used):
    return value in COUNTS and 0 <= used <= DAILY_LIMIT and value <= DAILY_LIMIT-used

def spreadsheet_cell(value):
    # Avoid formula execution if a business name/note is opened in Excel.
    value = str(value or '')
    return "'"+value if value.lstrip().startswith(('=', '+', '-', '@')) else value

def classify(card):
    """Fail closed: unknown layout, missing contacts, closed org never become leads."""
    if card.get('blocked'):
        return 'blocked'
    if not card.get('loaded') or not card.get('name') or not card.get('address'):
        return 'unknown'
    if card.get('closed') or card.get('website') or card.get('booking'):
        return 'reject'
    if not card.get('contacts_loaded') or not card.get('overview_loaded'):
        return 'unknown'
    return 'accept'
