"""Read public/nonsecret scanner observations only. Never reads private alert state."""
import re
import requests


def read_scanner(url, key, session=None):
    url = url.rstrip('/')
    if not re.fullmatch(r'https://[a-z0-9-]+\.supabase\.co', url) or not key:
        raise ValueError('Scanner project URL and publishable/read key required')
    session = session or requests
    headers = {'apikey': key}
    if not key.startswith(('sb_publishable_', 'sb_secret_')):
        headers['Authorization'] = 'Bearer '+key
    records = []
    # Stable order and pagination avoid the standard 1,000-row response limit.
    for offset in range(0, 4000, 500):
        response = session.get(url+'/rest/v1/ict_scanner_observations', headers=headers,
            params={'select': 'checked_at_utc,market,timeframe,record',
                    'order': 'checked_at_utc.desc,market.asc,timeframe.asc', 'limit': 500, 'offset': offset},
            timeout=10, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('Scanner history HTTP '+str(response.status_code))
        page = response.json()
        if not isinstance(page, list):
            raise ValueError('Unexpected scanner observation response')
        records.extend(row['record'] for row in page)
        if len(page) < 500:
            break
    cycle_response = session.get(url+'/rest/v1/ict_scanner_cycles', headers=headers,
        params={'select': 'started_at,finished_at,observation_count,errors,alerts_enabled',
                'order': 'started_at.desc', 'limit': 1}, timeout=10, allow_redirects=False)
    if cycle_response.status_code != 200:
        raise ValueError('Scanner cycle HTTP '+str(cycle_response.status_code))
    cycles = cycle_response.json()
    if not isinstance(cycles, list):
        raise ValueError('Unexpected scanner cycle response')
    return {'scans': records, 'latest_cycle': cycles[0] if cycles else None}, 'Railway scanner / Supabase (latest 4,000 observations)'
