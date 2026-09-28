import json,urllib.request,urllib.parse,urllib.error,re,datetime,os,sys,time,random,email.utils
PROJECT='vereinskalender-tsv-aw'
API=f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents'
USER_AGENT='TSV-Aue-Wingeshausen-Vereinskalender/1.3 (+https://vereinskalender-tsv-aw.web.app)'


def val(x):
    for k in ('stringValue','booleanValue','integerValue','doubleValue','timestampValue'):
        if k in x:return x[k]
    if 'arrayValue' in x:return [val(y) for y in x['arrayValue'].get('values',[])]
    if 'mapValue' in x:return {k:val(v) for k,v in x['mapValue'].get('fields',{}).items()}
    return None


def firebase_wait(headers,attempt):
    raw=headers.get('Retry-After') if headers else None
    if raw:
        try:return max(1,min(int(raw),180))
        except ValueError:
            try:
                target=email.utils.parsedate_to_datetime(raw)
                now=datetime.datetime.now(datetime.timezone.utc)
                return max(1,min(int((target-now).total_seconds()),180))
            except Exception:pass
    return [20,45,90][min(attempt,2)]

def firebase_json(req,label):
    last=None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req,timeout=45) as r:return json.load(r)
        except urllib.error.HTTPError as e:
            last=e
            if e.code!=429:
                raise RuntimeError(f'{label}: HTTP {e.code} {e.reason}')
            if attempt>=3:break
            wait=firebase_wait(e.headers,attempt)
            print(f'FIREBASE RATE-LIMIT {label}: HTTP 429 – warte {wait}s, Versuch {attempt+2}/4 …',flush=True)
            time.sleep(wait)
        except Exception as e:
            last=e;break
    if isinstance(last,urllib.error.HTTPError) and last.code==429:
        raise RuntimeError(f'{label}: HTTP 429 Too Many Requests – Firebase begrenzt den Abruf vorübergehend')
    raise RuntimeError(f'{label}: {last}')

def docs(collection):
    url=API+'/'+collection+'?pageSize=1000'; out=[]
    while url:
        req=urllib.request.Request(url,headers={'User-Agent':USER_AGENT,'Accept':'application/json'})
        data=firebase_json(req,'Firebase '+collection)
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

os.makedirs('generated',exist_ok=True)
SOURCE_CACHE='generated/sync-sources.json'
source_mode='firebase'
try:
    sources=docs('publicSyncSources')
    with open(SOURCE_CACHE,'w',encoding='utf-8') as f:json.dump({'savedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'sources':sources},f,ensure_ascii=False,indent=2)
except Exception as e:
    print(f'HINWEIS Firebase-Quellenliste: {e}',flush=True)
    try:
        with open(SOURCE_CACHE,'r',encoding='utf-8') as f:sources=json.load(f).get('sources',[])
        source_mode='cache'
        print(f'Verwende letzte gültige Quellenkonfiguration aus Cache: {len(sources)} Quellen.',flush=True)
    except Exception:
        print('FEHLER: Weder Firebase noch eine gespeicherte Quellenkonfiguration sind verfügbar.',flush=True)
        sys.exit(3)
new_events=[]; status=[]
print(f'Gefundene aktive Quellen: {len(sources)} (Konfiguration: {source_mode})',flush=True)
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
with open('generated/status.json','w',encoding='utf-8') as f:json.dump({'generatedAt':now,'eventCount':len(new_events),'sourceCount':len(sources),'sourceConfigMode':source_mode,'sources':status},f,ensure_ascii=False,indent=2)
failed=sum(1 for x in status if not x['ok'])
print(f'Fertig: {len(sources)-failed}/{len(sources)} Quellen aktuell erfolgreich, {len(new_events)} Termine verfügbar.',flush=True)
# ICS-Ausgabe für alle veröffentlichten Kalender erzeugen.
def icsesc(s):
    b=chr(92)
    return str(s or '').replace(b,b+b).replace(chr(10),b+'n').replace(',',b+',').replace(';',b+';')
def icstamp(d,t=''):
    d=str(d or '').replace('-','');tt=str(t or '').replace(':','');return d+'T'+(tt+'000000')[:6] if t else d
def truthy(v):return v is True or str(v).lower() in ('true','1','yes','ja')
def is_ics_published(c):
    return any(truthy(c.get(k)) for k in ('publishIcs','publishICS','icsPublished','icsEnabled','publish_ics'))
def write_calendar(cal,items):
    L=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//TSV Aue-Wingeshausen//Vereinskalender//DE','CALSCALE:GREGORIAN','METHOD:PUBLISH','X-WR-CALNAME:'+icsesc(cal.get('name','Kalender'))];seen=set()
    for e in sorted(items,key=lambda x:(x.get('date',''),x.get('time',''),x.get('title',''))):
        key=(e.get('externalUid') or e.get('id'),e.get('date'),e.get('time'))
        if key in seen:continue
        seen.add(key);L+=['BEGIN:VEVENT','UID:'+icsesc(str(key[0])+'@vereinskalender-tsv-aw'),'DTSTAMP:'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')]
        if e.get('allDay') or not e.get('time'):L+=['DTSTART;VALUE=DATE:'+icstamp(e.get('date'))]
        else:L+=['DTSTART:'+icstamp(e.get('date'),e.get('time')),'DTEND:'+icstamp(e.get('date'),e.get('endTime') or e.get('time'))]
        L+=['SUMMARY:'+icsesc(e.get('title','Termin'))]
        if e.get('description'):L+=['DESCRIPTION:'+icsesc(e.get('description'))]
        if e.get('location'):L+=['LOCATION:'+icsesc(e.get('location'))]
        L+=['END:VEVENT']
    L+=['END:VCALENDAR'];os.makedirs('generated/calendars',exist_ok=True)
    path='generated/calendars/'+cal['id']+'.ics'
    with open(path,'w',encoding='utf-8',newline='') as f:f.write(chr(13)+chr(10).join([]) if False else ('\r\n'.join(L)+'\r\n'))
    return path,len(seen)
try:
    all_cals=docs('calendars');all_events=docs('events')
    cals=[c for c in all_cals if is_ics_published(c)]
    print(f'ICS-freigegebene Kalender: {len(cals)}',flush=True);manifest=[]
    for c in cals:
        ctype=str(c.get('type','')).lower()
        ids=(c.get('sources') or c.get('sourceCalendarIds') or []) if ctype in ('aggregate','collection','sammelkalender') else [c['id']]
        ids=[str(x) for x in ids if x]
        items=[e for e in new_events if e.get('calendarId') in ids and e.get('visibility','public')=='public']
        items += [e for e in all_events if e.get('calendarId') in ids and e.get('visibility','public')=='public']
        path,count=write_calendar(c,items)
        if not os.path.isfile(path):raise RuntimeError('Datei wurde nicht erzeugt: '+path)
        manifest.append({'id':c['id'],'name':c.get('name',c['id']),'path':path,'eventCount':count})
        print(f'ICS OK {c.get("name",c["id"])}: {count} Termine -> {path}',flush=True)
    os.makedirs('generated',exist_ok=True)
    with open('generated/calendars.json','w',encoding='utf-8') as f:json.dump({'generatedAt':now,'calendars':manifest},f,ensure_ascii=False,indent=2)
    if not cals:print('HINWEIS: Kein Kalender ist für ICS freigegeben.',flush=True)
except Exception as e:
    print('HINWEIS ICS-Ausgabe konnte nicht aktualisiert werden:',e,flush=True)
    existing=os.path.isdir('generated/calendars') and any(x.endswith('.ics') for x in os.listdir('generated/calendars'))
    if existing:print('Vorhandene ICS-Ausgabedateien bleiben erhalten.',flush=True)
    else:sys.exit(4)
if sources and failed==len(sources):print('Hinweis: externe Quellen aktuell nicht erreichbar; vorhandene Daten/ICS-Ausgaben wurden trotzdem erzeugt.',flush=True)
