#!/usr/bin/env python3
r"""
sf2_content.py - full content editor for SFOffline: shop / items / guns / packages
(capsules) / starter kit, then repack into a new SFOffline.exe.

Reads the 4 embedded resources straight out of your SFOffline.exe, lets you edit them in a
browser, and writes a new SFOffline_custom.exe via repack_sfoffline.py. Edits are applied as
byte-safe field-level patches, so untouched items stay exactly as they were.

What you can change (all server-side -> players need NO download):
  * Shop/items/guns: price (SP/Cash/TP), IsSelling, resell, name - and add brand-new entries
  * Packages / capsules ("gacha"): what each pkg_itemid gives (sub items + counts); new packages
  * Starter kit (SeedItems): what every new account receives

Note: SFOffline packages are DETERMINISTIC (open -> fixed contents); the server has no
random-odds draw, so true probability gacha isn't configurable here.

    py sf2_content.py                         # http://127.0.0.1:8766
    py sf2_content.py --exe D:\sf\SFOffline.exe
Needs repack_sfoffline.py in the same folder. Pure standard library.
"""
import argparse, json, os, re, sys, threading, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import repack_sfoffline as rp

STATE = {"exe": None, "res": {}}   # res: name -> bytes (working copy)

def load_exe(path):
    b = rp.Bundle(open(path, "rb").read())
    dll = b.file_bytes(b.find("SFOffline.dll"))
    res = {rp.sniff(d): d for _, d in rp.read_resources(dll) if rp.sniff(d)}
    STATE["exe"] = path
    STATE["res"] = {k: v for k, v in res.items()}
    return sorted(res)

# ---------- catalogue (SFGameItem.eur) ----------
def _txt(name): return STATE["res"][name].decode("utf-8", "replace")
def _set(name, text): STATE["res"][name] = text.encode("utf-8")

def parse_catalogue():
    out = []
    for line in _txt("SFGameItem.eur").splitlines():
        s = line.strip()
        if not (s.startswith("GameItemInfo=(") and s.endswith(")")): continue
        inner = s[len("GameItemInfo=("):-1]
        f = {}
        for m in re.finditer(r'(\w+)=("(?:[^"\\]|\\.)*"|[^,]*)', inner):
            v = m.group(2)
            f[m.group(1)] = v[1:-1] if v.startswith('"') else v
        out.append(f)
    return out

FIELD_QUOTED = {"ItemName","ItemCode","ItemClassName","ItemImgName","ItemRegDate","ItemUseType",
                "EquipClass","Event","SellType","AddSPType","IsTestItem","SupplyCode","Groups",
                "merge_type","Tag"}
def _patch_line(line, field, value, quoted):
    if quoted:
        pat = re.compile(r'(\b'+re.escape(field)+r'=")(?:[^"\\]|\\.)*(")')
        rep = r'\g<1>'+value.replace('\\','\\\\')+r'\g<2>'
    else:
        pat = re.compile(r'(\b'+re.escape(field)+r'=)([^,\)]*)')
        rep = r'\g<1>'+value
    new, n = pat.subn(rep, line, count=1)
    if n == 0 and value != "":   # field absent -> insert before closing paren
        new = line[:-1].rstrip() + f',{field}=' + (f'"{value}"' if quoted else value) + ')'
    return new

def edit_item(item_id, changes):
    lines = _txt("SFGameItem.eur").splitlines()
    target = f"ItemId={item_id},"
    alt = f"ItemId={item_id})"
    hit = 0
    for i, line in enumerate(lines):
        if ("GameItemInfo=(" in line) and (f"ItemId={item_id}," in line or f"ItemId={item_id})" in line):
            for field, val in changes.items():
                lines[i] = _patch_line(lines[i], field, str(val), field in FIELD_QUOTED)
            hit += 1
    _set("SFGameItem.eur", "\n".join(lines) + "\n")
    return hit

def add_item(f):
    order = ["ItemName","ItemId","ItemCode","ItemClassName","ItemUseType","ItemUseTime",
             "CanResell","EquipClass","SP_Price","Cash_Price","IsSelling","SellType","TP_Price","ResellPrice"]
    parts = []
    for k in order:
        if k not in f: continue
        v = f[k]
        parts.append(f'{k}="{v}"' if k in FIELD_QUOTED else f'{k}={v}')
    line = "GameItemInfo=(" + ",".join(parts) + ")"
    _set("SFGameItem.eur", _txt("SFGameItem.eur").rstrip("\n") + "\n" + line + "\n")

# ---------- packages / gacha (SFPackageItem.eur) ----------
def parse_packages():
    pkgs = {}
    for line in _txt("SFPackageItem.eur").splitlines():
        s = line.strip()
        if not s.startswith("PackageItemArray=("): continue
        f = dict(re.findall(r'(\w+)\s*=\s*(\d+)', s))
        p = int(f.get("pkg_itemid", 0)); sub = int(f.get("sub_itemid", 0)); c = int(f.get("counts", 1))
        pkgs.setdefault(p, []).append({"sub_itemid": sub, "counts": c})
    return pkgs

def write_packages(pkgs):
    lines = ["[SFPackageItem]", ""]
    for p in sorted(pkgs):
        for it in pkgs[p]:
            lines.append(f"PackageItemArray=(pkg_itemid={p}, sub_itemid={it['sub_itemid']},counts={it['counts']})")
    _set("SFPackageItem.eur", "\n".join(lines) + "\n")

# ---------- seed ----------
def parse_seed():
    out = []
    for line in _txt("SeedItems.tsv").splitlines():
        if not line or line.startswith("#"): continue
        p = line.split("\t")
        if p and p[0].isdigit():
            out.append({"id": int(p[0]), "count": int(p[1]) if len(p)>1 and p[1].isdigit() else 1,
                        "cls": p[2] if len(p)>2 else "", "name": p[3] if len(p)>3 else ""})
    return out

def write_seed(items):
    head = [l for l in _txt("SeedItems.tsv").splitlines() if l.startswith("#")]
    lines = head + [f"{i['id']}\t{i['count']}\t{i.get('cls','')}\t{i.get('name','')}" for i in items]
    _set("SeedItems.tsv", "\n".join(lines) + "\n")

# ---------- build ----------
def build(out_path):
    b = rp.Bundle(open(STATE["exe"], "rb").read())
    dll = b.file_bytes(b.find("SFOffline.dll"))
    newdll = rp.edit_dll_resources(dll, STATE["res"])
    newexe = rp.rebuild_bundle(b, "SFOffline.dll", newdll)
    open(out_path, "wb").write(newexe)
    return {"path": out_path, "size": len(newexe)}

# ---------- http ----------
PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>SF2 Content</title><style>
:root{--bg:#0f1115;--fg:#e6e6e6;--mut:#8a94a6;--acc:#ffc15c;--line:#262b33;--card:#161a21}
body{font:14px system-ui,'Segoe UI',sans-serif;background:var(--bg);color:var(--fg);margin:0}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input,select,button{font:inherit;background:#1c2129;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px}
button{cursor:pointer}button.pri{background:var(--acc);color:#2a1e04;border:none;font-weight:700}
.tabs{display:flex;gap:6px;padding:8px 16px}.tab{padding:6px 10px;border-radius:6px;cursor:pointer;color:var(--mut)}
.tab.on{background:var(--card);color:var(--fg);border:1px solid var(--line)}
.pane{padding:12px 16px;display:none}.pane.on{display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:10px}
table{width:100%;border-collapse:collapse}td,th{padding:3px 6px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--mut);font-size:12px}.mut{color:var(--mut)}.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:4px 0}
input.n{width:90px}input.s{width:150px}#catb tr:hover{background:#13202a}
</style></head><body>
<header><strong>SF2 Content</strong>
 <input id=exe class=s style="width:360px" placeholder="SFOffline.exe path">
 <button class=pri onclick=openExe()>Load</button><span id=stat class=mut></span>
 <span style="flex:1"></span>
 <input id=out class=s style="width:300px" placeholder="output .exe (blank=SFOffline_custom.exe)">
 <button class=pri onclick=build()>Build custom SFOffline.exe</button>
</header>
<div class=tabs>
 <div class="tab on" data-t=shop onclick=tab('shop')>Shop / items / guns</div>
 <div class=tab data-t=pkg onclick=tab('pkg')>Packages / 扭蛋</div>
 <div class=tab data-t=seed onclick=tab('seed')>Starter kit</div></div>
<div id=p_shop class="pane on">
 <div class=card><div class=row><input id=q class=s placeholder="search id/code/name" oninput=renderShop()>
  <label class=mut><input type=checkbox id=sell onchange=renderShop()> selling only</label><span id=sn class=mut></span></div></div>
 <div class=card><b>Add new item</b><div class=row>
  <input id=aid class=n placeholder=ItemId><input id=acode class=n placeholder=Code><input id=aname class=s placeholder=Name>
  <input id=acls class=s placeholder=ClassName><input id=asp class=n placeholder=SP><input id=acash class=n placeholder=Cash>
  <label class=mut>sell<input type=checkbox id=asell checked></label><button class=pri onclick=addItem()>Add</button></div></div>
 <div class=card style="max-height:52vh;overflow:auto"><table><thead><tr><th>ItemId</th><th>Code</th><th>Name</th>
  <th>SP</th><th>Cash</th><th>TP</th><th>Sell</th><th>Resell</th><th></th></tr></thead><tbody id=catb></tbody></table></div></div>
<div id=p_pkg class=pane>
 <div class=card><div class=row><label class=mut>package pkg_itemid <select id=pksel onchange=renderPkg()></select></label>
  <input id=npkg class=n placeholder="new pkg id"><button onclick=newPkg()>Create package</button></div></div>
 <div class=card><b>Contents</b><div class=row><input id=psub class=n placeholder=sub_itemid><input id=pcnt class=n value=1 placeholder=count>
  <button class=pri onclick=addSub()>Add item to package</button></div><table><thead><tr><th>sub_itemid</th><th>count</th><th>name</th><th></th></tr></thead><tbody id=pkgb></tbody></table></div></div>
<div id=p_seed class=pane>
 <div class=card><b>Add starter item</b><div class=row><input id=sid class=n placeholder=ItemId><input id=scnt class=n value=1 placeholder=count>
  <button class=pri onclick=addSeed()>Add</button><span class=mut id=seedn></span></div></div>
 <div class=card style="max-height:56vh;overflow:auto"><table><thead><tr><th>ItemId</th><th>count</th><th>name</th><th></th></tr></thead><tbody id=seedb></tbody></table></div></div>
<script>
let CAT=[],PKG={},SEED=[],NAMES={};
async function api(p,b){const r=await fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):null});return r.json();}
function tab(t){document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('on',e.dataset.t==t));
 document.querySelectorAll('.pane').forEach(e=>e.classList.remove('on'));document.getElementById('p_'+t).classList.add('on');}
async function openExe(){const r=await api('/api/open',{exe:document.getElementById('exe').value.trim()});
 if(r.error){document.getElementById('stat').textContent='⚠ '+r.error;return;}
 document.getElementById('stat').textContent='loaded: '+r.names.join(', ');await loadAll();}
async function loadAll(){CAT=await api('/api/catalogue');NAMES={};CAT.forEach(c=>NAMES[c.ItemId]=c.ItemName);
 PKG=await api('/api/packages');SEED=await api('/api/seed');renderShop();fillPkgSel();renderSeed();}
function renderShop(){const q=(document.getElementById('q').value||'').toLowerCase(),so=document.getElementById('sell').checked;
 let r=CAT.filter(c=>(!so||c.IsSelling=='True')&&(!q||(''+c.ItemId).includes(q)||(c.ItemCode||'').toLowerCase().includes(q)||(c.ItemName||'').toLowerCase().includes(q)));
 document.getElementById('sn').textContent=r.length+' items';const b=document.getElementById('catb');b.innerHTML='';
 r.slice(0,400).forEach(c=>{const tr=document.createElement('tr');tr.innerHTML=
  `<td>${c.ItemId}</td><td>${c.ItemCode||''}</td><td>${c.ItemName||''}</td>
   <td><input class=n value="${c.SP_Price||0}" id=sp_${c.ItemId}></td>
   <td><input class=n value="${c.Cash_Price||0}" id=ca_${c.ItemId}></td>
   <td><input class=n value="${c.TP_Price||0}" id=tp_${c.ItemId}></td>
   <td><input type=checkbox ${c.IsSelling=='True'?'checked':''} id=se_${c.ItemId}></td>
   <td><input class=n value="${c.ResellPrice||0}" id=rs_${c.ItemId}></td>
   <td><button onclick=saveItem(${c.ItemId})>save</button></td>`;b.appendChild(tr);});}
async function saveItem(id){const ch={SP_Price:document.getElementById('sp_'+id).value,Cash_Price:document.getElementById('ca_'+id).value,
  TP_Price:document.getElementById('tp_'+id).value,ResellPrice:document.getElementById('rs_'+id).value,
  IsSelling:document.getElementById('se_'+id).checked?'True':'False'};
 const r=await api('/api/item/edit',{id:id,changes:ch});await reloadCat();flash('saved item '+id+' ('+r.hit+')');}
async function addItem(){const f={ItemId:document.getElementById('aid').value,ItemCode:document.getElementById('acode').value,
  ItemName:document.getElementById('aname').value,ItemClassName:document.getElementById('acls').value,ItemUseType:'E',ItemUseTime:'1',
  SP_Price:document.getElementById('asp').value||'0',Cash_Price:document.getElementById('acash').value||'0',TP_Price:'0',ResellPrice:'0',
  CanResell:'False',SellType:'S',IsSelling:document.getElementById('asell').checked?'True':'False'};
 if(!f.ItemId){alert('ItemId');return;}await api('/api/item/add',{fields:f});await reloadCat();flash('added item '+f.ItemId);}
async function reloadCat(){CAT=await api('/api/catalogue');NAMES={};CAT.forEach(c=>NAMES[c.ItemId]=c.ItemName);renderShop();}
function fillPkgSel(){const s=document.getElementById('pksel');s.innerHTML='';Object.keys(PKG).sort((a,b)=>a-b).forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=p+' ('+NAMES[p]||''+')';s.appendChild(o);});renderPkg();}
function renderPkg(){const p=document.getElementById('pksel').value;const items=PKG[p]||[];const b=document.getElementById('pkgb');b.innerHTML='';
 items.forEach((it,idx)=>{const tr=document.createElement('tr');tr.innerHTML=`<td>${it.sub_itemid}</td><td>${it.counts}</td><td>${NAMES[it.sub_itemid]||''}</td><td><button onclick=delSub(${idx})>x</button></td>`;b.appendChild(tr);});}
async function addSub(){const p=document.getElementById('pksel').value;const sub=+document.getElementById('psub').value,c=+document.getElementById('pcnt').value||1;
 if(!sub)return;(PKG[p]=PKG[p]||[]).push({sub_itemid:sub,counts:c});await savePkg();renderPkg();}
async function delSub(i){const p=document.getElementById('pksel').value;PKG[p].splice(i,1);await savePkg();renderPkg();}
async function newPkg(){const id=document.getElementById('npkg').value;if(!id)return;PKG[id]=PKG[id]||[];await savePkg();fillPkgSel();document.getElementById('pksel').value=id;renderPkg();}
async function savePkg(){await api('/api/packages/save',{packages:PKG});}
function renderSeed(){const b=document.getElementById('seedb');b.innerHTML='';document.getElementById('seedn').textContent=SEED.length+' items';
 SEED.forEach((it,idx)=>{const tr=document.createElement('tr');tr.innerHTML=`<td>${it.id}</td><td><input class=n value="${it.count}" id=sc_${idx}></td><td>${NAMES[it.id]||it.name||''}</td><td><button onclick=saveSeedRow(${idx})>save</button> <button onclick=delSeed(${idx})>x</button></td>`;b.appendChild(tr);});}
async function addSeed(){const id=+document.getElementById('sid').value,c=+document.getElementById('scnt').value||1;if(!id)return;SEED.push({id:id,count:c,cls:'',name:''});await saveSeed();renderSeed();}
async function saveSeedRow(i){SEED[i].count=+document.getElementById('sc_'+i).value||1;await saveSeed();flash('saved');}
async function delSeed(i){SEED.splice(i,1);await saveSeed();renderSeed();}
async function saveSeed(){await api('/api/seed/save',{seed:SEED});}
async function build(){const r=await api('/api/build',{out:document.getElementById('out').value.trim()});
 if(r.error){alert(r.error);return;}alert('Built: '+r.path+'\n'+r.size+' bytes\n\nRun it with  --server-only  to serve your custom content.');}
function flash(m){document.getElementById('stat').textContent=m;}
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _s(self,o):
        body=o if isinstance(o,(bytes,bytearray)) else json.dumps(o).encode()
        self.send_response(200);self.send_header("Content-Type","application/json" if not isinstance(o,(bytes,bytearray)) else "text/html; charset=utf-8")
        self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
    def _b(self): n=int(self.headers.get("Content-Length",0));return json.loads(self.rfile.read(n) or b"{}")
    def do_GET(self):
        p=self.path.split("?")[0]
        try:
            if p=="/": return self._s(PAGE.encode())
            if STATE["exe"] is None and p.startswith("/api/"): return self._s({"error":"load an SFOffline.exe first"})
            if p=="/api/catalogue": return self._s(parse_catalogue())
            if p=="/api/packages": return self._s({str(k):v for k,v in parse_packages().items()})
            if p=="/api/seed": return self._s(parse_seed())
        except Exception as e: return self._s({"error":str(e)})
        self._s({"error":"not found"})
    def do_POST(self):
        try:
            b=self._b()
            if self.path=="/api/open": return self._s({"names":load_exe(b["exe"] or r"D:\sf\SFOffline.exe")})
            if STATE["exe"] is None: return self._s({"error":"load an SFOffline.exe first"})
            if self.path=="/api/item/edit": return self._s({"hit":edit_item(b["id"],b["changes"])})
            if self.path=="/api/item/add": add_item(b["fields"]); return self._s({"ok":True})
            if self.path=="/api/packages/save": write_packages({int(k):v for k,v in b["packages"].items()}); return self._s({"ok":True})
            if self.path=="/api/seed/save": write_seed(b["seed"]); return self._s({"ok":True})
            if self.path=="/api/build":
                out=b.get("out") or os.path.join(os.path.dirname(STATE["exe"]),"SFOffline_custom.exe")
                return self._s(build(out))
        except Exception as e:
            import traceback; traceback.print_exc()
            return self._s({"error":str(e)})
        self._s({"error":"not found"})

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--exe",default=None); ap.add_argument("--port",type=int,default=8766)
    a=ap.parse_args()
    if a.exe: load_exe(a.exe)
    srv=ThreadingHTTPServer(("127.0.0.1",a.port),H); url=f"http://127.0.0.1:{a.port}"
    print(f"SF2 Content on {url}")
    threading.Timer(0.6,lambda:webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass

if __name__=="__main__": main()
