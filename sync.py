import json,urllib.request,urllib.parse,re,datetime,os
PROJECT='vereinskalender-tsv-aw'
API=f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents'
def v(x):
 for k in ('stringValue','booleanValue','integerValue','doubleValue','timestampValue'):
  if k in x:return x[k]
 if 'arrayValue' in x:return [v(y) for y in x['arrayValue'].get('values',[])]
def docs(n):
 u=API+'/'+n+'?pageSize=1000';a=[]
 while u:
  with urllib.request.urlopen(u,timeout=30) as r:d=json.load(r)
  for q in d.get('documents',[]):
   z={k:v(x) for k,x in q.get('fields',{}).items()};z['id']=q['name'].rsplit('/',1)[-1];a.append(z)
  t=d.get('nextPageToken');u=API+'/'+n+'?pageSize=1000&pageToken='+urllib.parse.quote(t) if t else None
 return a
def parse(body,s):
 body=re.sub(r'\r?\n[ \t]','',body);out=[];c=None
 def dt(x):
  return (f'{x[:4]}-{x[4:6]}-{x[6:8]}',f'{x[9:11]}:{x[11:13]}' if 'T' in x else '') if len(x)>=8 else None
 for l in body.splitlines():
  if l=='BEGIN:VEVENT':c={};continue
  if l=='END:VEVENT':
   if c and c.get('DTSTART'):
    st=dt(c['DTSTART']);en=dt(c.get('DTEND',''))
    out.append({'id':'ext-'+s['id']+'-'+(c.get('UID') or str(len(out))),'externalUid':c.get('UID',''),'title':(s.get('prefix') or '')+c.get('SUMMARY','Termin')+(s.get('suffix') or ''),'description':c.get('DESCRIPTION','').replace('\\n','\n'),'location':c.get('LOCATION',''),'date':st[0],'time':st[1],'endTime':en[1] if en and en[0]==st[0] else '','visibility':s.get('visibility','public'),'calendarId':s.get('calendarId',''),'externalSourceId':s['id'],'external':True})
   c=None;continue
  if c is not None and ':' in l:
   k,x=l.split(':',1);k=k.split(';',1)[0].upper()
   if k in ('UID','SUMMARY','DESCRIPTION','LOCATION','DTSTART','DTEND'):c[k]=x
 return out
src=[x for x in docs('icsSources') if x.get('active',True)];events=[];status=[]
for s in src:
 url=str(s.get('url',''));url='https://'+url[9:] if url.lower().startswith('webcal://') else url
 try:
  with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'TSV-Vereinskalender/1.0'}),timeout=30) as r:b=r.read().decode('utf-8','replace')
  q=parse(b,s);events+=q;status.append({'name':s.get('name',''),'count':len(q),'ok':True})
 except Exception as e:status.append({'name':s.get('name',''),'count':0,'ok':False,'error':str(e)})
os.makedirs('generated',exist_ok=True)
json.dump({'generatedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'events':events,'sources':status},open('generated/external-events.json','w',encoding='utf-8'),ensure_ascii=False,indent=2)
json.dump({'generatedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'eventCount':len(events),'sources':status},open('generated/status.json','w',encoding='utf-8'),ensure_ascii=False,indent=2)
print('Synchronisiert:',len(src),'Quellen,',len(events),'Termine')
