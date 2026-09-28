import json,urllib.request,urllib.parse,urllib.error,re,datetime,os,sys,time,random,email.utils
PROJECT='vereinskalender-tsv-aw'
API=f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents'
USER_AGENT='TSV-Aue-Wingeshausen-Vereinskalender/1.2 (+https://vereinskalender-tsv-aw.web.app)'


def val(x):
    for k in ('stringValue','booleanValue','integerValue','doubleValue','timestampValue'):
        if k in x:return x[k]
    if 'arrayValue' in x:return [val(y) for y in x['arrayValue'].get('values',[])]
    if 'mapValue' in x:return {k:val(v) for k,v in x['mapValue'].get('fields',{}).items()}
    return None


def docs(collection):
    url=API+'/'+collection+'?pageSize=1000'; out=[]
    while url:
        req=urllib.request.Request(url,headers={'User-Agent':USER_AGENT})
        try:
            with urllib.request.urlopen(req,timeout=30) as r:data=json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'Firebase {collection}: HTTP {e.code}. Bitte Vereinskalender 0.6.4 inkl. Firestore-Regeln deployen.')
        for q in data.get('documents',[]):
            z={k:val(v) for k,v in q.get('fields',{}).items()}; z['id']=q['name'].rsplit('/',1)[-1]; out.append(z)
        token=data.get('nextPageToken'); url=API+'/'+collection+'?pageSize=1000&pageToken='+urllib.parse.quote(token) if token else None
    return out


def unfold(body): return re.sub(r'\r?\n[ \t]','',body)
def text(v): return (v or '').replace('\\n','\n').replace('\\,',',').replace('\\;',';').replace('\\\\','\\')
def dt(x):
    if not x:return None
    z=x.rstrip('Z')
    if re.match(r'^\d{8}$',z):return (f'{z[:4]}-{z[4:6]}-{z[6:8]}','',True)
    m=re.match(r'^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})',z)
    return (f'{m.group(1)}-{m.group(2)}-{m.group(3)}',f'{m.group(4)}:{m.group(5)}',False) if m else None


def parse(body,src):
    out=[]; cur=None
    for line in unfold(body).splitlines():
        if line=='BEGIN:VEVENT':cur={};continue
        if line=='END:VEVENT':
            if cur and cur.get('DTSTART'):
                st=dt(cur['DTSTART']); en=dt(cur.get('DTEND'))
                if st:
                    out.append({'id':'ext-'+src['id']+'-'+(cur.get('UID') or str(len(out))), 'externalUid':cur.get('UID',''), 'title':(src.get('prefix') or '')+text(cur.get('SUMMARY','Termin'))+(src.get('suffix') or ''), 'description':text(cur.get('DESCRIPTION','')), 'location':text(cur.get('LOCATION','')), 'date':st[0], 'time':st[1], 'endTime':en[1] if en and en[0]==st[0] else '', 'allDay':st[2], 'visibility':src.get('visibility','public'), 'calendarId':src.get('calendarId',''), 'externalSourceId':src['id'], 'external':True})
            cur=None;continue
        if cur is not None and ':' in line:
            k,v=line.split(':',1); k=k.split(';',1)[0].upper()
            if k in ('UID','SUMMARY','DESCRIPTION','LOCATION','DTSTART','DTEND'):cur[k]=v
    return out


def retry_after_seconds(headers,attempt):
    raw=headers.get('Retry-After') if headers else None
    if raw:
        try:return max(1,min(int(raw),180))
        except ValueError:
            try:
                target=email.utils.parsedate_to_datetime(raw)
                now=datetime.datetime.now(datetime.timezone.utc)
                return max(1,min(int((target-now).total_seconds()),180))
            except Exception:pass
    return [30,60][min(attempt,1)]


def fetch_ics(url,name):
    headers={'User-Agent':USER_AGENT,'Accept':'text/calendar,text/plain;q=0.9,*/*;q=0.1','Accept-Language':'de-DE,de;q=0.9,en;q=0.5','Cache-Control':'no-cache'}
    last=None
    for attempt in range(3):
        try:
            req=urllib.request.Request(url,headers=headers)
            with urllib.request.urlopen(req,timeout=60) as r:
                return r.read().decode('utf-8','replace')
        except urllib.error.HTTPError as e:
            last=e
            if e.code!=429: raise
            if attempt>=2: break
            wait=retry_after_seconds(e.headers,attempt)
            print(f'RATE-LIMIT {name}: HTTP 429 – warte {wait}s, Versuch {attempt+2}/3 …',flush=True)
            time.sleep(wait)
        except Exception as e:
            last=e; break
    raise RuntimeError(f'HTTP 429: Too Many Requests – myTischtennis begrenzt den Abruf nach 3 Versuchen') if isinstance(last,urllib.error.HTTPError) and last.code==429 else last

sources=docs('publicSyncSources'); new_events=[]; status=[]
print(f'Gefundene aktive Quellen: {len(sources)}',flush=True)
# Vorherige erfolgreiche Daten laden, damit ein Rate-Limit nichts löscht.
old_events=[]
try:
    with open('generated/external-events.json','r',encoding='utf-8') as f:old_events=json.load(f).get('events',[])
except Exception:pass
old_by_source={}
for e in old_events: old_by_source.setdefault(e.get('externalSourceId',''),[]).append(e)

for idx,src in enumerate(sources):
    if idx:
        delay=15+random.randint(0,10)
        print(f'Pause zwischen Quellen: {delay}s …',flush=True); time.sleep(delay)
    url=str(src.get('url','')).strip(); url='https://'+url[9:] if url.lower().startswith('webcal://') else url
    name=src.get('name','Quelle')
    try:
        body=fetch_ics(url,name)
        items=parse(body,src)
        if not items: raise RuntimeError('Keine VEVENT-Termine gefunden')
        new_events.extend(items); status.append({'id':src['id'],'name':name,'count':len(items),'ok':True,'stale':False})
        print(f'OK  {name}: {len(items)} Termine',flush=True)
    except Exception as e:
        kept=old_by_source.get(src['id'],[])
        new_events.extend(kept)
        status.append({'id':src['id'],'name':name,'count':len(kept),'ok':False,'stale':bool(kept),'error':str(e)})
        suffix=f' – {len(kept)} zuletzt erfolgreiche Termine bleiben erhalten' if kept else ''
        print(f'FEHLER {name}: {e}{suffix}',flush=True)

os.makedirs('generated',exist_ok=True)
now=datetime.datetime.now(datetime.timezone.utc).isoformat()
with open('generated/external-events.json','w',encoding='utf-8') as f:json.dump({'generatedAt':now,'events':new_events,'sources':status},f,ensure_ascii=False,indent=2)
with open('generated/status.json','w',encoding='utf-8') as f:json.dump({'generatedAt':now,'eventCount':len(new_events),'sourceCount':len(sources),'sources':status},f,ensure_ascii=False,indent=2)
failed=sum(1 for x in status if not x['ok'])
print(f'Fertig: {len(sources)-failed}/{len(sources)} Quellen aktuell erfolgreich, {len(new_events)} Termine verfügbar.',flush=True)
if sources and failed==len(sources):sys.exit(2)
