import json,urllib.request,urllib.parse,re,datetime,os,sys
PROJECT='vereinskalender-tsv-aw'
API=f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents'

def val(x):
    for k in ('stringValue','booleanValue','integerValue','doubleValue','timestampValue'):
        if k in x:return x[k]
    if 'arrayValue' in x:return [val(y) for y in x['arrayValue'].get('values',[])]
    if 'mapValue' in x:return {k:val(v) for k,v in x['mapValue'].get('fields',{}).items()}
    return None

def docs(collection):
    # Nur die bewusst öffentliche technische Mirror-Collection lesen.
    url=API+'/'+collection+'?pageSize=1000'; out=[]
    while url:
        req=urllib.request.Request(url,headers={'User-Agent':'TSV-Vereinskalender-Sync/1.1'})
        try:
            with urllib.request.urlopen(req,timeout=30) as r:data=json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'Firebase {collection}: HTTP {e.code}. Bitte Vereinskalender 0.6.3 inkl. Firestore-Regeln deployen.')
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

sources=docs('publicSyncSources'); events=[]; status=[]
print(f'Gefundene aktive Quellen: {len(sources)}')
for src in sources:
    url=str(src.get('url','')).strip(); url='https://'+url[9:] if url.lower().startswith('webcal://') else url
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 TSV-Aue-Wingeshausen-Vereinskalender/1.1','Accept':'text/calendar,text/plain,*/*'})
        with urllib.request.urlopen(req,timeout=45) as r: body=r.read().decode('utf-8','replace')
        items=parse(body,src)
        if not items: raise RuntimeError('Keine VEVENT-Termine gefunden')
        events.extend(items); status.append({'id':src['id'],'name':src.get('name',''),'count':len(items),'ok':True})
        print(f"OK  {src.get('name','Quelle')}: {len(items)} Termine")
    except Exception as e:
        status.append({'id':src['id'],'name':src.get('name',''),'count':0,'ok':False,'error':str(e)})
        print(f"FEHLER {src.get('name','Quelle')}: {e}")
os.makedirs('generated',exist_ok=True)
now=datetime.datetime.now(datetime.timezone.utc).isoformat()
with open('generated/external-events.json','w',encoding='utf-8') as f:json.dump({'generatedAt':now,'events':events,'sources':status},f,ensure_ascii=False,indent=2)
with open('generated/status.json','w',encoding='utf-8') as f:json.dump({'generatedAt':now,'eventCount':len(events),'sourceCount':len(sources),'sources':status},f,ensure_ascii=False,indent=2)
failed=sum(1 for x in status if not x['ok'])
print(f'Fertig: {len(sources)-failed}/{len(sources)} Quellen erfolgreich, {len(events)} Termine.')
if sources and failed==len(sources):sys.exit(2)
