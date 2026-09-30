#!/usr/bin/env python3
"""Read-only local HTTP checks for the bounded project-history experiment."""
import argparse
import html
import json
import re
import sqlite3
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from project_history import COHORT_PATH, validate_history_cohort

p=argparse.ArgumentParser();p.add_argument('--base-url',required=True);p.add_argument('--baseline-url');p.add_argument('--db',default='cache/pkg.sqlite');p.add_argument('--output');a=p.parse_args()
m=json.loads(COHORT_PATH.read_text());validate_history_cohort(cohort=m)

def get(path,base=None,method='GET',headers=None):
 req=urllib.request.Request((base or a.base_url).rstrip('/')+path,method=method,headers=headers or {})
 try:
  with urllib.request.urlopen(req,timeout=20) as r:return r.status,dict(r.headers),r.read().decode()
 except urllib.error.HTTPError as e:return e.code,dict(e.headers),e.read().decode()

def ok(condition,message):
 if not condition:raise AssertionError(message)

projects=m['projects'];ns={'s':'http://www.sitemaps.org/schemas/sitemap/0.9'}
status,_,sitemap=get('/sitemap-history.xml');ok(status==200,'history sitemap status')
urls=[n.text for n in ET.fromstring(sitemap).findall('s:url/s:loc',ns)]
expected=['https://pkg.so'+p['path'] for p in projects];ok(sorted(urls)==sorted(expected),'exactly frozen25 sitemap URLs')
ok('lastmod' not in sitemap and 'hreflang' not in sitemap,'no invented dates/localizations')
status,_,index=get('/sitemap.xml');ok(status==200 and 'https://pkg.so/sitemap-history.xml' in index,'sitemap index discoverability')
status,_,home=get('/');ok(status==200,'catalog status')
checks=0
for project in projects:
 path=project['path'];status,headers,text=get(path);ok(status==200,path+' status')
 ok(text.count('<h1 ')==1,path+' single H1');ok('The history of '+html.escape(project['name']) in text,path+' H1')
 ok('<title>'+html.escape(project['title'],quote=True)+'</title>' in text,path+' title')
 ok('rel="canonical" href="https://pkg.so'+path+'"' in text,path+' canonical')
 ok('hreflang=' not in text,path+' no nonexistent translations')
 schemas=re.findall(r'<script type="application/ld\+json">(.*?)</script>',text,re.S);ok(len(schemas)==1,path+' JSON-LD count')
 schema=json.loads(schemas[0]);graph=schema['@graph'];ok(graph[0]['@type']=='SoftwareSourceCode' and graph[1]['@type']=='Article',path+' types')
 ok(graph[1]['citation']==project['citations'],path+' citation schema')
 for field in ['author','datePublished','dateModified','aggregateRating','offers']:ok(field not in graph[1],path+' no fabricated '+field)
 for url in project['citations']:ok('href="'+html.escape(url,quote=True)+'"' in text,path+' linked citation '+url)
 history=project['history_snapshot'];paragraphs=history if isinstance(history,list) else sum([v for k,v in history.items() if k!='sources'],[])
 for paragraph in paragraphs:ok(paragraph in html.unescape(text),path+' unchanged history text')
 ok('href="'+path+'"' in home,path+' ordinary catalog link')
 head,_,body=get(path,method='HEAD');ok(head==200 and body=='',path+' HEAD')
 etag=headers.get('ETag') or headers.get('etag');ok(bool(etag),path+' ETag');ok(get(path,headers={'If-None-Match':etag})[0]==304,path+' conditional GET')
 for prefix in ['de','fr','ja','zh-hans','brew','npm','pip','cargo','cask']:ok(get('/'+prefix+path)[0]==404,path+' no fan-out '+prefix)
 checks+=1
for path in ['/curl/history/','/jq/history/','/not-a-project/history/','/ffmpeg/history/index.html']:
 ok(get(path)[0]==404,path+' bounded unknown route')
con=sqlite3.connect(a.db);package_count=0;providers=set();baseline_count=0
for project in projects:
 for key in project['package_keys']:
  r=con.execute('SELECT path,provider FROM packages WHERE package_key=?',(key,)).fetchone()
  if not r:continue
  path,provider=r;status,_,text=get(path);ok(status==200,path+' package status');ok('href="'+project['path']+'"' in text,path+' prominent project link')
  ok('href="#install"' in text and 'id="install"' in text,path+' install UI')
  if a.baseline_url:
   bs,_,original=get(path,base=a.baseline_url);ok(bs==200,path+' baseline status')
   cleaned=re.sub(r'<a class="button secondary" href="/[^"]+/history/">The history of .*?</a>','',text)
   ok(cleaned==original,path+' exact baseline package HTML except history link')
   for suffix in ['index.md','index.json']:
    new=get(path+suffix);old=get(path+suffix,base=a.baseline_url);ok(new[0]==old[0] and new[2]==old[2],path+suffix+' unchanged')
   baseline_count+=1
  package_count+=1;providers.add(provider)
result={'history_routes_checked':checks,'sitemap_urls':len(urls),'package_routes_checked':package_count,'providers':sorted(providers),'baseline_package_comparisons':baseline_count,'unknown_and_fanout_routes':'404','history_text_and_citations':'preserved','schema':'SoftwareSourceCode + Article, no invented author/date/rating','status':'passed'}
if a.output:Path(a.output).write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
