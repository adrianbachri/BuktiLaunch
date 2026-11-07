#!/usr/bin/env python3
"""Export a read-only review desk from actual run files; never generates verdicts."""
from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
campaign = sys.argv[1] if len(sys.argv) > 1 else 'BL-DEMO-001'
if not campaign.startswith('BL-') or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-' for c in campaign):
    raise SystemExit('Invalid campaign ID')
base = ROOT / 'runs' / campaign
state = json.loads((base / 'state.json').read_text())
def read(rel):
    path = (base / rel).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError('Run path escapes campaign')
    return path.read_text(encoding='utf-8')

reviews = []
for path in sorted((base / 'reviews').glob('*.json')):
    record = json.loads(path.read_text())
    reviews.append({'file':str(path.relative_to(base)), 'payload':record})
outputs = []
for path in sorted((base / 'copy').glob('*.txt')):
    raw = path.read_bytes()
    outputs.append({'file':str(path.relative_to(base)), 'text':raw.decode(),
                    'sha256':hashlib.sha256(raw).hexdigest()})
sources = [{'file':str(p.relative_to(base)), 'text':p.read_text(),
            'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
           for p in sorted((base / 'inputs').glob('*.md')) if 'legacy' not in p.name]
payload = {
    'campaign':campaign, 'status':state['status'], 'round':state['round'],
    'max_rounds':state['max_rounds'], 'approval_count':len(state.get('approvals',[])),
    'reviews':reviews, 'outputs':outputs, 'sources':sources,
    'history':[{'at':e['at'],'event':e['event'],'round':e['round'],'status':e['status']}
               for e in state.get('history',[])],
    'historical_issue':campaign == 'BL-DEMO-001',
    'label':'MOCK / FIKTIF. Bukti tersimpan; tidak menjalankan model.',
}
assert len(outputs) >= 2, 'Review desk requires two existing outputs'
assert all(x['text'].strip() for x in outputs), 'Empty output'
out = ROOT / 'ui' / 'data.js'
out.parent.mkdir(exist_ok=True)
out.write_text('window.BUKTI_DATA = '+json.dumps(payload,ensure_ascii=False).replace('</','<\\/')+';\n')
print(f'{out.name}: {len(outputs)} outputs, {len(reviews)} reviews, {len(sources)} sources; {campaign}')
