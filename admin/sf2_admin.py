#!/usr/bin/env python3
r"""
sf2_admin.py  —  full-feature editor for the SFOffline private server.

Two areas:
  * DATABASE (save.db): accounts, cash/sp/tp, inventory — exact SFOffline schema, password
    scheme (PBKDF2-SHA256) and item layout, so accounts log in and items appear. Zero deps.
  * SERVER CONTENT: edit the shop (prices / selling / add items), packages & capsules
    (contents), and starter (seed) items, then BUILD a patched SFOffline.exe so the changes
    take effect. These are SERVER-SIDE — players need no download; they see the new shop on
    connect. (Building needs:  pip install pefile dnfile  — only for the Build step.)

Run:   py sf2_admin.py            → http://127.0.0.1:8765
       py sf2_admin.py --db <save.db path>
"""
import argparse, json, os, sqlite3, hashlib, secrets, webbrowser, threading, subprocess, sys
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import content as C

STATE = {"db": None}
CFG_PATH = os.path.join(HERE, "admin_config.json")
def load_cfg():
    d = {"exe": r"D:\sf\SFOffline.exe", "resdir": os.path.join(HERE, "resources"),
         "out": r"D:\sf\SFOffline.patched.exe", "repacker": os.path.join(HERE, "repack_sfoffline.py")}
    try: d.update(json.load(open(CFG_PATH, encoding="utf-8")))
    except Exception: pass
    return d
def save_cfg(d):
    try: json.dump(d, open(CFG_PATH, "w", encoding="utf-8"), indent=2)
    except Exception: pass

# ---------------- database ----------------
SCHEMA_V1 = r"""
CREATE TABLE account (
 id INTEGER PRIMARY KEY AUTOINCREMENT, login TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password TEXT NOT NULL, codename TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
 creation_level INTEGER NOT NULL DEFAULT 1, sp INTEGER NOT NULL DEFAULT 0,
 cash INTEGER NOT NULL DEFAULT 0, tp INTEGER NOT NULL DEFAULT 0,
 lucky_points INTEGER NOT NULL DEFAULT 0, selected_unit INTEGER NOT NULL DEFAULT 0,
 selected_set INTEGER NOT NULL DEFAULT 1, pmc_code TEXT NOT NULL DEFAULT '       ',
 key_set TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 experience INTEGER NOT NULL DEFAULT 0, kills INTEGER NOT NULL DEFAULT 0,
 deaths INTEGER NOT NULL DEFAULT 0, assists INTEGER NOT NULL DEFAULT 0,
 headshots INTEGER NOT NULL DEFAULT 0, games_played INTEGER NOT NULL DEFAULT 0,
 games_won INTEGER NOT NULL DEFAULT 0, playtime INTEGER NOT NULL DEFAULT 0);
CREATE TABLE item (sn INTEGER PRIMARY KEY AUTOINCREMENT,
 account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
 item_id INTEGER NOT NULL, active INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 1,
 expires_at TEXT, properties TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX item_stack ON item(account_id, item_id) WHERE active = 0;
CREATE TABLE unit (unit_sn INTEGER PRIMARY KEY REFERENCES item(sn) ON DELETE CASCADE,
 account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
 gear TEXT NOT NULL DEFAULT '0,0,0,0,0,0,0,0,0,0,0');
CREATE TABLE weapon_set (account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
 armory_sn INTEGER NOT NULL, main INTEGER NOT NULL DEFAULT 0, sub INTEGER NOT NULL DEFAULT 0,
 grenade INTEGER NOT NULL DEFAULT 0, flash INTEGER NOT NULL DEFAULT 0, smoke INTEGER NOT NULL DEFAULT 0,
 special INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (account_id, armory_sn));
CREATE TABLE item_part (part_sn INTEGER PRIMARY KEY REFERENCES item(sn) ON DELETE CASCADE,
 parent_sn INTEGER NOT NULL REFERENCES item(sn) ON DELETE CASCADE);
CREATE TABLE mode_arms_store (account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
 mode_id INTEGER NOT NULL, slots TEXT NOT NULL, PRIMARY KEY (account_id, mode_id));
"""
MIGRATIONS = [(1, SCHEMA_V1),
 (2, "ALTER TABLE account ADD COLUMN seed_version TEXT NOT NULL DEFAULT '';"),
 (3, "ALTER TABLE account ADD COLUMN vip_map_rotation TEXT NOT NULL DEFAULT '';"),
 (4, "ALTER TABLE account ADD COLUMN sp_gauge INTEGER NOT NULL DEFAULT 0;")]
EDITABLE = ["codename","creation_level","sp","cash","tp","lucky_points","selected_unit",
            "selected_set","experience","kills","deaths","assists","headshots",
            "games_played","games_won","playtime","sp_gauge"]
def fmt(dt): return dt.strftime("%Y%m%d%H%M%S")
def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 100000, 32)
    return salt.hex().upper()+":"+dk.hex().upper()
def connect(p):
    c=sqlite3.connect(p,timeout=5); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c
from contextlib import contextmanager
@contextmanager
def _db():
    c=connect(STATE["db"])
    try:
        yield c; c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
def migrate(con):
    v=con.execute("PRAGMA user_version").fetchone()[0]
    for t,sql in MIGRATIONS:
        if v<t: con.executescript(sql); con.execute(f"PRAGMA user_version={t}"); v=t
    con.commit()
def open_db(p):
    newf=not os.path.exists(p); os.makedirs(os.path.dirname(os.path.abspath(p)),exist_ok=True)
    c=connect(p); migrate(c); c.close(); STATE["db"]=p; return {"path":p,"created":newf}
def accounts():
    c=connect(STATE["db"]); r=[dict(x) for x in c.execute("SELECT * FROM account ORDER BY id")]; c.close(); return r
def create_account(login,pw,seed=False):
    with _db() as c:
        aid=c.execute("INSERT INTO account(login,password,created_at) VALUES(?,?,?) RETURNING id",
                      (login,hash_password(pw),fmt(datetime.now(timezone.utc)))).fetchone()[0]
        if seed:
            for s in json.load(open(os.path.join(HERE,"seed.json"),encoding="utf-8")):
                _add(c,aid,s["id"],True,1,None)
    return aid
def update_account(aid,f):
    s={k:v for k,v in f.items() if k in EDITABLE}
    if not s: return
    with _db() as c: c.execute(f"UPDATE account SET {', '.join(k+'=?' for k in s)} WHERE id=?",(*s.values(),aid))
def delete_account(aid):
    with _db() as c: c.execute("DELETE FROM account WHERE id=?",(aid,))
def items(aid):
    c=connect(STATE["db"])
    try: return [dict(x) for x in c.execute("SELECT * FROM item WHERE account_id=? ORDER BY sn",(aid,))]
    finally: c.close()
def _add(c,aid,iid,active,count,expires):
    if active: c.execute("INSERT INTO item(account_id,item_id,active,expires_at,properties) VALUES(?,?,1,?,'')",(aid,iid,expires))
    else: c.execute("INSERT INTO item(account_id,item_id,active,count) VALUES(?,?,0,?) ON CONFLICT(account_id,item_id) WHERE active=0 DO UPDATE SET count=count+excluded.count",(aid,iid,count))
def add_item(aid,iid,active,count,days):
    exp=fmt(datetime.now(timezone.utc)+timedelta(days=int(days))) if days and int(days)>0 else None
    with _db() as c: _add(c,aid,int(iid),bool(active),int(count or 1),exp)
def remove_item(sn):
    with _db() as c: c.execute("DELETE FROM item WHERE sn=?",(sn,))

# ---------------- content (resources) ----------------
def rp(cfg,name): return os.path.join(cfg["resdir"], name)
def shop_get(cfg):
    return C.parse_shop(open(rp(cfg,"SFGameItem.eur"),encoding="utf-8").read())
def shop_save(cfg, changes, adds):
    p=rp(cfg,"SFGameItem.eur"); txt=open(p,encoding="utf-8").read()
    for ch in changes: txt=C.set_shop_field(txt,int(ch["id"]),ch["field"],ch["value"])
    for a in adds: txt=C.add_shop_item(txt,int(a["template"]),a["overrides"])
    open(p,"w",encoding="utf-8").write(txt)
def pkg_get(cfg):
    pk=C.parse_packages(open(rp(cfg,"SFPackageItem.eur"),encoding="utf-8").read())
    return [{"pkg":k,"items":[{"sub":s,"count":c} for s,c in v]} for k,v in sorted(pk.items())]
def pkg_save(cfg, model):
    pk={int(e["pkg"]):[[int(i["sub"]),int(i["count"])] for i in e["items"]] for e in model if e.get("items")}
    open(rp(cfg,"SFPackageItem.eur"),"w",encoding="utf-8").write(C.write_packages(pk))
def seed_get(cfg):
    h,r=C.parse_seed(open(rp(cfg,"SeedItems.tsv"),encoding="utf-8").read()); return r
def seed_save(cfg, rows):
    h,_=C.parse_seed(open(rp(cfg,"SeedItems.tsv"),encoding="utf-8").read())
    open(rp(cfg,"SeedItems.tsv"),"w",encoding="utf-8").write(C.write_seed(h,rows))
def apply_changeset(cfg, cs):
    """Apply a changeset copied from the online SF2 Server Console."""
    shop=cs.get("shop",{}) or {}; changes=[]
    for iid,fields in shop.items():
        for f,v in (fields or {}).items(): changes.append({"id":int(iid),"field":f,"value":v})
    adds=cs.get("shopAdds",[]) or []
    if changes or adds: shop_save(cfg,changes,adds)
    if "packages" in cs: pkg_save(cfg,cs["packages"])
    if "gacha" in cs: gacha_save(cfg,cs["gacha"])
    if "seed" in cs: seed_save(cfg,cs["seed"])
    if "ranks" in cs:
        rk=cs["ranks"] or {}
        rc=[{"rank":int(k),"exp":v} for k,v in (rk.get("exp") or {}).items()]
        sc=[{"rank":int(k),"gage":v[0],"addsp":v[1]} for k,v in (rk.get("sp") or {}).items()]
        ranks_save(cfg,rc,sc,0)
    return {"shop_fields":len(changes),"added_items":len(adds),"packages":"packages" in cs,
            "seed":"seed" in cs,"ranks":"ranks" in cs,"gacha":"gacha" in cs}
def build(cfg):
    cmd=[sys.executable, cfg["repacker"], "--exe",cfg["exe"], "--res-dir",cfg["resdir"], "--out",cfg["out"], "--verify"]
    try:
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=600)
        return {"ok": r.returncode==0, "out": r.stdout+r.stderr, "cmd":" ".join(cmd)}
    except Exception as e:
        return {"ok": False, "out": f"failed to run repacker: {e}\n(need: pip install pefile dnfile)", "cmd":" ".join(cmd)}

def ranks_get(cfg):
    return C.parse_ranks(open(rp(cfg,"DefaultRankPoint.ini"),encoding="utf-8").read())
def ranks_save(cfg, rank_changes, sp_changes, scale):
    p=rp(cfg,"DefaultRankPoint.ini"); txt=open(p,encoding="utf-8").read()
    if scale and float(scale)>0: txt=C.scale_rank_exp(txt,float(scale))
    for c in rank_changes: txt=C.set_rank_exp(txt,int(c["rank"]),int(c["exp"]))
    for c in sp_changes: txt=C.set_sp_gage(txt,int(c["rank"]),int(c["gage"]),int(c["addsp"]))
    open(p,"w",encoding="utf-8").write(txt)
def gacha_path(cfg): return os.path.join(cfg["resdir"],"gacha.json")
def gacha_get(cfg):
    try: return json.load(open(gacha_path(cfg),encoding="utf-8"))
    except Exception: return {}
def gacha_save(cfg, model): json.dump(model, open(gacha_path(cfg),"w",encoding="utf-8"), ensure_ascii=False, indent=1)

# db: units / weapon_sets for an account
def units(aid):
    c=connect(STATE["db"])
    try: return [dict(x) for x in c.execute("SELECT * FROM unit WHERE account_id=? ORDER BY unit_sn",(aid,))]
    finally: c.close()
def wsets(aid):
    c=connect(STATE["db"])
    try: return [dict(x) for x in c.execute("SELECT * FROM weapon_set WHERE account_id=? ORDER BY armory_sn",(aid,))]
    finally: c.close()
def unit_save(aid, unit_sn, gear):
    with _db() as c:
        c.execute("INSERT INTO unit(unit_sn,account_id,gear) VALUES(?,?,?) ON CONFLICT(unit_sn) DO UPDATE SET gear=excluded.gear",(int(unit_sn),aid,gear))
def unit_del(unit_sn):
    with _db() as c: c.execute("DELETE FROM unit WHERE unit_sn=?",(int(unit_sn),))
def wset_save(aid, w):
    with _db() as c:
        c.execute("""INSERT INTO weapon_set(account_id,armory_sn,main,sub,grenade,flash,smoke,special)
                     VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_id,armory_sn) DO UPDATE SET
                     main=excluded.main,sub=excluded.sub,grenade=excluded.grenade,flash=excluded.flash,
                     smoke=excluded.smoke,special=excluded.special""",
                  (aid,int(w["armory_sn"]),int(w.get("main",0)),int(w.get("sub",0)),int(w.get("grenade",0)),
                   int(w.get("flash",0)),int(w.get("smoke",0)),int(w.get("special",0))))
def wset_del(aid, armory_sn):
    with _db() as c: c.execute("DELETE FROM weapon_set WHERE account_id=? AND armory_sn=?",(aid,int(armory_sn)))

# ---------------- HTTP ----------------
def _page():
    return open(os.path.join(HERE,"admin_ui.html"),encoding="utf-8").read()
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _s(self,obj,ct="application/json"):
        b=obj if isinstance(obj,(bytes,bytearray)) else json.dumps(obj,ensure_ascii=False).encode("utf-8")
        self.send_response(200); self.send_header("Content-Type",ct); self.send_header("Content-Length",str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def _b(self):
        n=int(self.headers.get("Content-Length",0)); return json.loads(self.rfile.read(n) or b"{}")
    def do_GET(self):
        p=self.path.split("?")[0]; cfg=load_cfg()
        try:
            if p=="/": return self._s(_page().encode("utf-8"),"text/html; charset=utf-8")
            if p=="/api/catalogue": return self._s(open(os.path.join(HERE,"catalogue.json"),"rb").read())
            if p=="/api/config": return self._s(cfg)
            if p=="/api/accounts": return self._s(accounts())
            if p=="/api/items": return self._s(items(int(parse_qs(urlparse(self.path).query)["account"][0])))
            if p=="/api/content/shop": return self._s(shop_get(cfg))
            if p=="/api/content/packages": return self._s(pkg_get(cfg))
            if p=="/api/content/seed": return self._s(seed_get(cfg))
            if p=="/api/content/ranks": return self._s(ranks_get(cfg))
            if p=="/api/content/gacha": return self._s(gacha_get(cfg))
            if p=="/api/units": return self._s(units(int(parse_qs(urlparse(self.path).query)["account"][0])))
            if p=="/api/wsets": return self._s(wsets(int(parse_qs(urlparse(self.path).query)["account"][0])))
        except Exception as e: return self._s({"error":str(e)})
        self._s({"error":"not found"})
    def do_POST(self):
        cfg=load_cfg()
        try:
            b=self._b()
            if self.path=="/api/open": return self._s(open_db(b["path"] or default_db()))
            if self.path=="/api/config": save_cfg(b); return self._s({"ok":True})
            if self.path=="/api/content/shop": shop_save(cfg,b.get("changes",[]),b.get("adds",[])); return self._s({"ok":True})
            if self.path=="/api/content/packages": pkg_save(cfg,b["model"]); return self._s({"ok":True})
            if self.path=="/api/content/seed": seed_save(cfg,b["rows"]); return self._s({"ok":True})
            if self.path=="/api/content/ranks": ranks_save(cfg,b.get("ranks",[]),b.get("sp",[]),b.get("scale")); return self._s({"ok":True})
            if self.path=="/api/content/gacha": gacha_save(cfg,b["model"]); return self._s({"ok":True})
            if self.path=="/api/content/apply": return self._s(apply_changeset(cfg,b["changeset"]))
            if self.path=="/api/content/build": return self._s(build(cfg))
            if STATE["db"] is None: return self._s({"error":"open a database first"})
            if self.path=="/api/account/create": return self._s({"id":create_account(b["login"],b["password"],b.get("seed",False))})
            if self.path=="/api/account/update": update_account(b["id"],b["fields"]); return self._s({"ok":True})
            if self.path=="/api/account/delete": delete_account(b["id"]); return self._s({"ok":True})
            if self.path=="/api/item/add": add_item(b["account"],b["item_id"],b["active"],b.get("count",1),b.get("expire_days",0)); return self._s({"ok":True})
            if self.path=="/api/item/remove": remove_item(b["sn"]); return self._s({"ok":True})
            if self.path=="/api/unit/save": unit_save(b["account"],b["unit_sn"],b["gear"]); return self._s({"ok":True})
            if self.path=="/api/unit/delete": unit_del(b["unit_sn"]); return self._s({"ok":True})
            if self.path=="/api/wset/save": wset_save(b["account"],b["w"]); return self._s({"ok":True})
            if self.path=="/api/wset/delete": wset_del(b["account"],b["armory_sn"]); return self._s({"ok":True})
        except Exception as e: return self._s({"error":str(e)})
        self._s({"error":"not found"})
def default_db():
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),"SFOffline","save.db")
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--db"); ap.add_argument("--port",type=int,default=8765); a=ap.parse_args()
    if a.db: open_db(a.db)
    srv=ThreadingHTTPServer(("127.0.0.1",a.port),H); url=f"http://127.0.0.1:{a.port}"
    print(f"SF2 Admin on {url}   (default save.db: {default_db()})\nCtrl+C to stop.")
    threading.Timer(0.6,lambda:webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
if __name__=="__main__": main()
