#!/usr/bin/env python3
r"""
sf2_control.py — SF2 Private-Server CONTROL CENTER (everything in one UI).

One command, one web page, all tabs:
  伺服器   start/stop SFOffline server-mode + the 0.0.0.0 forwarder (rewrites 127.0.0.1->your IP),
           shows status, ports, the exact client launch args to hand players, and the live log.
  資料庫   accounts / sp / cash / inventory  (save.db)
  商店      item search, price edit, ONE-CLICK unlock of the 3210 locked items
  封包/扭蛋 package contents + gacha weights
  階級      rank exp table
  Config    decrypt / edit / re-encrypt the client configs (weapon stats, PVE waves, …) in place
  測試      connection self-test (greeting + login) against the running server
  Build     repack a patched SFOffline.exe with the shop/package/rank edits

Run:  py sf2_control.py           → opens http://127.0.0.1:8777
Reuses sf2_admin.py (DB + content), sf2_config.py (Blowfish config), sf2_testclient logic.
"""
import argparse, json, os, sys, socket, struct, subprocess, threading, time, re, webbrowser, glob
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sf2_admin as A                      # reuse all DB + content logic
# Blowfish config tool needs pycryptodome; make it OPTIONAL so the control center still
# starts if it's missing (only the Config tab is affected, with a clear message).
CFGCRYPT = None
for _p in (HERE, os.path.join(HERE, "..", "config_tool")):
    try:
        if _p not in sys.path: sys.path.insert(0, _p)
        import sf2_config as CFGCRYPT
        break
    except Exception:
        CFGCRYPT = None

CTRL_PATH = os.path.join(HERE, "control_config.json")
def load_ctrl():
    d = {"sfoffline": r"D:\sf\SFOffline.exe", "data": r"D:\sf\serverdata",
         "config_dir": r"D:\sf\SFGame\Config", "lobby": 28000, "messenger": 28001,
         "clan": 28002, "advertise": "",
         "cooked_dir": r"D:\sf\SFGame\CookedPCConsole", "umodel": r"D:\sf2_project\admin\umodel.exe",
         "reskin_dir": r"D:\sf2_project\reskin",
         "twn_dir": r"D:\sf2_project\twn_server\server", "twn_python": "python",
         "twn_port": 28080, "twn_ext": 28080,
         "client_exe": r"D:\sf\Binaries\Win32\sf2_unpacked.exe", "client_lang": "TWN",
         "client_resx": 1280, "client_resy": 720,
         "player_dir": r"D:\sf2_project\player_package"}
    try: d.update(json.load(open(CTRL_PATH, encoding="utf-8")))
    except Exception: pass
    return d
def save_ctrl(d):
    try: json.dump(d, open(CTRL_PATH, "w", encoding="utf-8"), indent=2)
    except Exception: pass

# ---------------- server manager (SFOffline --server-only + threaded forwarder) ----------------
SRV = {"proc": None, "forwarders": [], "ports": None, "stop": False, "advertise": ""}

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"

def _port_open(port, host="127.0.0.1", timeout=0.4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

def _rewrite_frames(buf, ip):
    old = struct.pack("<H", len("127.0.0.1")+1) + b"127.0.0.1\x00"
    new = struct.pack("<H", len(ip)+1) + ip.encode() + b"\x00"
    out = bytearray(); off = 0; n = len(buf)
    while n - off >= 2:
        ln = struct.unpack_from("<H", buf, off)[0]
        if ln < 2 or n - off < ln: break
        frame = buf[off:off+ln]; off += ln
        if old in frame:
            body = frame[2:].replace(old, new); frame = struct.pack("<H", 2+len(body)) + body
        out += frame
    return bytes(out), buf[off:]

def _pipe(src, dst, ip=None, st=None):
    st = st or SRV
    buf = bytearray()
    try:
        while not st["stop"]:
            data = src.recv(65536)
            if not data: break
            if ip:
                buf += data; emit, left = _rewrite_frames(bytes(buf), ip); buf = bytearray(left)
                if emit: dst.sendall(emit)
            else:
                dst.sendall(data)
    except Exception: pass
    finally:
        for s in (src, dst):
            try: s.close()
            except Exception: pass

def _forwarder(listen_port, dst_port, ip, st=None):
    st = st or SRV
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("0.0.0.0", listen_port)); ls.listen(16); ls.settimeout(1.0)
    st["forwarders"].append(ls)
    while not st["stop"]:
        try: cs, _ = ls.accept()
        except socket.timeout: continue
        except Exception: break
        try: us = socket.create_connection(("127.0.0.1", dst_port))
        except Exception:
            cs.close(); continue
        # server->client rewritten (server advertises 127.0.0.1); client->server passthrough
        threading.Thread(target=_pipe, args=(us, cs, ip, st), daemon=True).start()
        threading.Thread(target=_pipe, args=(cs, us, None, st), daemon=True).start()

def _read_ports(logpath, timeout=40):
    pat = re.compile(r"-ServerPort=(\d+).*?-MessengerServerPort=(\d+).*?-ClanServerPort=(\d+)", re.S)
    t0 = time.time()
    while time.time()-t0 < timeout:
        if os.path.exists(logpath):
            try: m = pat.search(open(logpath, encoding="utf-8", errors="replace").read())
            except Exception: m = None
            if m: return int(m.group(1)), int(m.group(2)), int(m.group(3))
        time.sleep(0.4)
    return None

def server_start(ctrl):
    if SRV["proc"] and SRV["proc"].poll() is None:
        return {"error": "already running"}
    SRV["stop"] = False; SRV["forwarders"] = []
    adv = ctrl.get("advertise") or local_ip()
    SRV["advertise"] = adv
    os.makedirs(ctrl["data"], exist_ok=True)
    logpath = os.path.join(ctrl["data"], "SFOffline.log")
    try: os.remove(logpath)
    except Exception: pass
    if not os.path.exists(ctrl["sfoffline"]):
        return {"error": f"SFOffline.exe not found: {ctrl['sfoffline']}"}
    SRV["proc"] = subprocess.Popen([ctrl["sfoffline"], "--server-only", "--data", ctrl["data"]],
                                   cwd=os.path.dirname(ctrl["sfoffline"]))
    ports = _read_ports(logpath)
    if not ports:
        try: SRV["proc"].terminate()
        except Exception: pass
        return {"error": "could not read server ports from SFOffline.log (did it launch?)"}
    SRV["ports"] = ports
    for ext, internal in [(ctrl["lobby"], ports[0]), (ctrl["messenger"], ports[1]), (ctrl["clan"], ports[2])]:
        threading.Thread(target=_forwarder, args=(ext, internal, adv), daemon=True).start()
    return server_status(ctrl)

def server_stop(ctrl):
    SRV["stop"] = True
    for ls in SRV["forwarders"]:
        try: ls.close()
        except Exception: pass
    SRV["forwarders"] = []
    if SRV["proc"]:
        try: SRV["proc"].terminate()
        except Exception: pass
    SRV["proc"] = None; SRV["ports"] = None
    return {"running": False}

def server_status(ctrl):
    running = bool(SRV["proc"] and SRV["proc"].poll() is None)
    adv = SRV["advertise"] or ctrl.get("advertise") or local_ip()
    args = (f"-ServerIP={adv} -ServerPort={ctrl['lobby']} "
            f"-MessengerServerIP={adv} -MessengerServerPort={ctrl['messenger']} "
            f"-ClanServerIP={adv} -ClanServerPort={ctrl['clan']}")
    return {"running": running, "ports": SRV["ports"], "advertise": adv, "lan_ip": local_ip(),
            "ext_ports": [ctrl["lobby"], ctrl["messenger"], ctrl["clan"]], "player_args": args}

def server_log(ctrl, n=120):
    # Show whichever log has content: SFOffline (備用路線) or the TWN server.
    for p in (os.path.join(ctrl["data"], "SFOffline.log"),
              os.path.join(ctrl["twn_dir"], "twn_server.log")):
        try:
            lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
            if lines:
                return {"log": os.path.basename(p) + ":\n" + "\n".join(lines[-n:])}
        except Exception: pass
    return {"log": "(no log yet)"}

# ---------------- config editor (Blowfish) ----------------
def cfg_list(ctrl):
    d = ctrl["config_dir"]; out = []
    for p in sorted(glob.glob(os.path.join(d, "*"))):
        if not p.lower().endswith((".ini", ".xml", ".pms")): continue
        try:
            if open(p, "rb").read(2) == b"\xfd\x00":
                out.append({"name": os.path.basename(p), "size": os.path.getsize(p)})
        except Exception: pass
    return out
def cfg_read(ctrl, name):
    if CFGCRYPT is None: return {"error": "config 工具需要 pycryptodome:py -m pip install pycryptodome 後重開"}
    p = os.path.join(ctrl["config_dir"], os.path.basename(name))
    return {"name": name, "text": CFGCRYPT.to_text(open(p, "rb").read())}
def cfg_write(ctrl, name, text):
    if CFGCRYPT is None: return {"error": "config 工具需要 pycryptodome:py -m pip install pycryptodome 後重開"}
    p = os.path.join(ctrl["config_dir"], os.path.basename(name))
    try:  # keep a .bak the first time
        if not os.path.exists(p+".bak"): open(p+".bak","wb").write(open(p,"rb").read())
    except Exception: pass
    open(p, "wb").write(CFGCRYPT.from_text(text))
    return {"ok": True, "bytes": os.path.getsize(p)}

# ---------------- connection self-test ----------------
def test_conn(ctrl, port=None, login="tester", password="test"):
    port = int(port or ctrl["lobby"]); res = {"port": port}
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
    except Exception as e:
        return {**res, "ok": False, "stage": "connect", "msg": f"cannot connect: {e}"}
    try:
        s.settimeout(3); fr = s.recv(64)
        if len(fr) >= 4 and struct.unpack_from("<H", fr, 2)[0] == 2000:
            res["greeting"] = "ok (proto 2000 — SF2 lobby)"
        else:
            return {**res, "ok": False, "stage": "greeting", "msg": f"unexpected first bytes {fr[:8].hex()}"}
        # try AES login via sf2crypto if available (optional)
        try:
            sys.path.insert(0, os.path.join(HERE, "..", "server2"))
            import sfcrypto
            body = sfcrypto.login_body(login, password, hostport=40000)
            pkt = struct.pack("<H", 2+len(body)) + struct.pack("<H", 101) + body
            s.sendall(pkt); s.settimeout(3)
            fr = s.recv(256)
            res["login"] = "response received" if fr else "no response"
        except Exception as e:
            res["login"] = f"skipped ({e})"
        return {**res, "ok": True, "msg": "server reachable and speaking SF2 protocol"}
    finally:
        s.close()

# ---------------- weapon creator ----------------
_GUN = {"步槍":["ar","ak","m4","famas","scar","aug","g36","sig","tar","xm8","galil","hk417","qbz","type89","stg","m16","acr","sar21","an94","arx","fnf2000","mrr","k2"],
        "狙擊":["sni","awp","cheytac","barrett","m200","dsr","trg","svd","dragunov","intervention","vss","m24","ssg"],
        "衝鋒":["smg","mp5","mp7","p90","ump","bizon","mac10","scorpion","kriss","vector","pp2000","ksv","k1"],
        "手槍":["pistol","colt","glock","deagle","cz75","p226","m92","jericho","luger","sw60","elephant","infi"],
        "霰彈":["shotgun","aa12","benelli","ksg","m870","saiga","usas","spas"],
        "機槍":["mg","hk23","k3","l86","mg4","rpk","spw","ultimax","m60","m249"]}
def _gun_type(c):
    s=(c.get("cls","")+" "+c.get("code","")+" "+c.get("name","")).lower()
    for t,keys in _GUN.items():
        if any(k in s for k in keys): return t
    return None
def _active_catalogue(ctrl):
    try:
        if twn_available(ctrl): return twn_catalog_adapted(ctrl)
    except Exception: pass
    return json.load(open(os.path.join(HERE,"catalogue.json"),encoding="utf-8"))
def weapon_bases():
    cat=_active_catalogue(load_ctrl())
    seen={}; out=[]
    for c in cat:
        t=_gun_type(c)
        if not t: continue
        key=c.get("cls","")+c.get("name","")
        if key in seen: continue
        seen[key]=1
        out.append({"id":c["id"],"code":c.get("code",""),"name":c.get("name",""),
                    "cls":c.get("cls",""),"img":c.get("img") or c.get("ItemImgName",""),"type":t})
    out.sort(key=lambda x:(x["type"],x["name"]))
    return out
def _next_item_id():
    cat=_active_catalogue(load_ctrl())
    return max(c["id"] for c in cat)+1
def weapon_specs_path(cfg): return os.path.join(cfg["resdir"],"weapon_specs.json")
def weapon_create(cfg, spec):
    base=int(spec["base_id"]); nid=int(spec.get("item_id") or _next_item_id())
    ov={"ItemId":nid,"ItemName":spec["name"],"IsSelling":True,
        "SP_Price":int(spec.get("sp",0) or 0),"Cash_Price":int(spec.get("cash",0) or 0)}
    if spec.get("code"): ov["ItemCode"]=spec["code"]
    # clone the base weapon's shop line -> new item (same mesh, new identity/price)
    A.shop_save(cfg, [], [{"template":base,"overrides":ov}])
    # persist the design/stats so the creator & preview remember it
    try: specs=json.load(open(weapon_specs_path(cfg),encoding="utf-8"))
    except Exception: specs=[]
    rec={"item_id":nid,"base_id":base,"name":spec["name"],"code":spec.get("code",""),
         "type":spec.get("type",""),"sp":ov["SP_Price"],"cash":ov["Cash_Price"],
         "img":spec.get("img",""),"stats":spec.get("stats",{})}
    specs=[s for s in specs if s["item_id"]!=nid]+[rec]
    json.dump(specs,open(weapon_specs_path(cfg),"w",encoding="utf-8"),ensure_ascii=False,indent=1)
    return {"ok":True,"item_id":nid,"name":spec["name"],"base_id":base,
            "note":"已加入商店(複製底槍模型+新身份)。到 Build 重打包生效。槍的細部數值需在武器類別檔套用(進階)。"}
def weapon_specs(cfg):
    try: return json.load(open(weapon_specs_path(cfg),encoding="utf-8"))
    except Exception: return []

# ---------------- reskin (umodel export + same-size DDS inject) ----------------
def _base_name(ctrl, base_id):
    cat = _active_catalogue(ctrl)
    for c in cat:
        if c["id"] == int(base_id): return c
    return None
def reskin_find(ctrl, base_id):
    """Guess which texture package(s) hold a weapon's skin, by name tokens."""
    c = _base_name(ctrl, base_id)
    if not c: return {"error": "base not found"}
    import re as _re
    toks = [t for t in _re.findall(r"[a-z0-9]+", (c.get("code","")+" "+c.get("cls","")+" "+c.get("name","")).lower()) if len(t) >= 3]
    cd = ctrl["cooked_dir"]; cand = []
    for p in glob.glob(os.path.join(cd, "*.upk")):
        n = os.path.basename(p).lower()
        if ("texture" in n or n.startswith("rif_") or n.startswith("wp_")) and any(t in n for t in toks):
            cand.append({"pkg": os.path.basename(p), "size": os.path.getsize(p)})
    cand.sort(key=lambda x: -x["size"])
    return {"base": c.get("name",""), "tokens": toks, "candidates": cand[:12]}
def _run(cmd, timeout=600):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)
    except Exception as e:
        return 1, f"failed: {e}"
def reskin_export(ctrl, pkg):
    um = ctrl["umodel"]
    if not os.path.exists(um): return {"error": f"umodel.exe 找不到：{um}（去 gildor.org 下載放到 admin 夾，或在設定改路徑）"}
    out = os.path.join(ctrl["reskin_dir"], os.path.splitext(pkg)[0])
    os.makedirs(out, exist_ok=True)
    rc, log = _run([um, "-export", "-png", "-nooverwrite", "-game=ue3",
                    f"-path={ctrl['cooked_dir']}", f"-out={out}", pkg])
    pngs = []
    for dp, _, fs in os.walk(out):
        for f in fs:
            if f.lower().endswith(".png"): pngs.append(os.path.relpath(os.path.join(dp, f), out))
    return {"ok": rc == 0, "out_dir": out, "pngs": sorted(pngs)[:60], "count": len(pngs), "log": log[-1500:]}
def _reskin_cmd(ctrl, pkg, *rest):
    inj = os.path.join(HERE, "reskin_inject.py")
    upk = os.path.join(ctrl["cooked_dir"], pkg)
    return _run([sys.executable, inj, rest[0], upk, *rest[1:]])
def reskin_list(ctrl, pkg):
    rc, out = _reskin_cmd(ctrl, pkg, "list"); return {"ok": rc == 0, "out": out}
def reskin_apply(ctrl, pkg, dds, at=None):
    if not os.path.exists(dds): return {"error": f"檔案找不到：{dds}（.png 或 .dds）"}
    args = ["apply", dds] + (["--at", str(at)] if at else [])
    rc, out = _reskin_cmd(ctrl, pkg, *args); return {"ok": rc == 0, "out": out}
def reskin_template(ctrl, ref_png, grid=64):
    """Make a blank, same-size grid canvas next to an exported PNG, to design on."""
    if not os.path.exists(ref_png): return {"error": f"參考 PNG 找不到：{ref_png}"}
    try:
        from PIL import Image
        w, h = Image.open(ref_png).size
    except Exception as e:
        return {"error": f"讀不到 PNG 尺寸：{e}"}
    out = os.path.splitext(ref_png)[0] + "_template.png"
    inj = os.path.join(HERE, "reskin_inject.py")
    rc, log = _run([sys.executable, inj, "template", str(w), str(h), out, "--grid", str(grid)])
    return {"ok": rc == 0, "out_png": out, "size": f"{w}x{h}", "log": log}

# ---------------- TWN full server (the recovered Python emulator) ----------------
TWN = {"proc": None, "forwarders": [], "stop": False, "ready": None, "advertise": ""}
def twn_catalog_path(ctrl): return os.path.join(ctrl["twn_dir"], "catalog.json")
# Cache the 6.4MB catalog in memory so clicking 商店 / saving doesn't re-parse it
# every time (that was the source of the lag). Invalidated by file mtime.
_TWN_CAT = {"path": None, "mtime": 0, "data": None}
def twn_load_catalog(ctrl):
    p = twn_catalog_path(ctrl)
    try: mt = os.path.getmtime(p)
    except OSError: return []
    if _TWN_CAT["path"] == p and _TWN_CAT["mtime"] == mt and _TWN_CAT["data"] is not None:
        return _TWN_CAT["data"]
    data = json.load(open(p, encoding="utf-8"))
    _TWN_CAT.update(path=p, mtime=mt, data=data)
    return data
def twn_save_catalog(ctrl, cat):
    p = twn_catalog_path(ctrl)
    if not os.path.exists(p + ".bak") and os.path.exists(p):
        try: open(p + ".bak", "wb").write(open(p, "rb").read())   # true original
        except Exception: pass
    json.dump(cat, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    try: _TWN_CAT.update(path=p, data=cat, mtime=os.path.getmtime(p))
    except OSError: pass
def twn_available(ctrl): return os.path.exists(os.path.join(ctrl["twn_dir"], "server.py")) and os.path.exists(twn_catalog_path(ctrl))
def twn_catalog_adapted(ctrl):
    """Map the TWN server's catalog.json into the UI's shop/weapon shape."""
    cat = twn_load_catalog(ctrl)
    out = []
    for c in cat:
        cur = (c.get("currency") or "").upper(); price = c.get("price", 0) or 0
        out.append({"id": c.get("id"), "code": c.get("code", ""), "name": c.get("name", ""),
                    "cls": c.get("class", ""), "img": c.get("icon", ""),
                    "sp": price if cur == "SP" else 0, "cash": price if cur == "CASH" else 0,
                    "tp": price if cur == "TP" else 0, "selling": bool(c.get("shop_selling"))})
    return out
def twn_status(ctrl):
    running = bool(TWN["proc"] and TWN["proc"].poll() is None)
    adv = TWN["advertise"] or ctrl.get("advertise") or local_ip()
    return {"available": twn_available(ctrl), "running": running, "advertise": adv, "lan_ip": local_ip(),
            "ext_port": ctrl["twn_ext"], "ready": TWN["ready"],
            "note": "台服完整版 server。只在 28080 開大廳;對戰由主機客端在 28082 自己 host(P2P)。開給別人=轉發 28080(改IP)+28082(對戰)。clan 28081 不提供。"}
def twn_start(ctrl, remote=False):
    if TWN["proc"] and TWN["proc"].poll() is None: return {"error": "已在運行"}
    if not twn_available(ctrl): return {"error": f"找不到 server.py / catalog.json（把 zip 解到 {ctrl['twn_dir']}）"}
    TWN["stop"] = False; TWN["forwarders"] = []
    internal = 29080 if remote else ctrl["twn_port"]          # loopback port the emulator binds
    adv = ctrl.get("advertise") or local_ip(); TWN["advertise"] = adv
    ready = os.path.join(ctrl["twn_dir"], "_ready.json")
    try: os.remove(ready)
    except Exception: pass
    # Capture the emulator's stdout/stderr so import errors / tracebacks are
    # visible instead of vanishing (and so the panel can show them).
    log_path = os.path.join(ctrl["twn_dir"], "twn_server.log")
    try: logf = open(log_path, "w", encoding="utf-8", errors="replace")
    except Exception: logf = subprocess.DEVNULL
    def _twn_log_tail(n=25):
        try: return "\n".join(open(log_path, encoding="utf-8", errors="replace").read().splitlines()[-n:])
        except Exception: return ""
    # Launch the server exactly like the V4 pack's own launcher: same database
    # (the pack's save\ DB, so the panel edits and the running server share one
    # DB), same catalog, HTTP stats on 28081, plaintext responses. This makes
    # the panel a drop-in single entry point — no need for the pack's launcher.
    cmd = [ctrl["twn_python"], "server.py", "--port", str(internal),
           "--http-port", "28081", "--response-encryption", "none",
           "--database", twn_db_path(ctrl),
           "--catalog", os.path.join(ctrl["twn_dir"], "catalog.json"),
           "--ready-file", ready]
    try: os.makedirs(os.path.dirname(twn_db_path(ctrl)), exist_ok=True)
    except Exception: pass
    TWN["proc"] = subprocess.Popen(cmd, cwd=ctrl["twn_dir"],
                                   stdout=logf, stderr=subprocess.STDOUT)
    t0 = time.time(); info = None
    while time.time() - t0 < 25:
        if os.path.exists(ready):
            try: info = json.load(open(ready, encoding="utf-8")); break
            except Exception: pass
        if TWN["proc"].poll() is not None:
            return {"error": "server.py 啟動後就結束了（多半是缺檔或 import 失敗）。\n"
                             f"log：{log_path}\n\n{_twn_log_tail()}"}
        time.sleep(0.4)
    if info is None:
        return {"error": f"server.py 25 秒內沒就緒。log：{log_path}\n\n{_twn_log_tail()}"}
    TWN["ready"] = info
    if remote:
        # 28080: lobby/game -> emulator (rewrite advertised 127.0.0.1 -> your IP)
        threading.Thread(target=_forwarder, args=(ctrl["twn_ext"], internal, adv, TWN), daemon=True).start()
        # 28082: in-game MATCH is hosted by the host player's own client on 28082 (P2P) -> relay to local client
        threading.Thread(target=_forwarder, args=(28082, 28082, None, TWN), daemon=True).start()
    return twn_status(ctrl)
def twn_stop(ctrl):
    TWN["stop"] = True
    for ls in TWN["forwarders"]:
        try: ls.close()
        except Exception: pass
    TWN["forwarders"] = []
    if TWN["proc"]:
        try: TWN["proc"].terminate()
        except Exception: pass
    TWN["proc"] = None; TWN["ready"] = None
    return {"running": False}
def twn_unlock(ctrl, which="content"):
    """Open the TWN shop by flipping shop_selling=True on locked items in catalog.json (server reads it on start)."""
    p = twn_catalog_path(ctrl)
    if not os.path.exists(p): return {"error": "找不到 twn catalog.json"}
    cat = twn_load_catalog(ctrl); n = 0
    for c in cat:
        if not c.get("shop_selling"):
            cls = (c.get("class", "") + c.get("code", "")).lower()
            is_gun = any(k in cls for k in ["weap", "rif", "sni", "smg", "pistol", "shotgun", "mg", "ar_", "sig", "ak", "m4", "awp", "famas", "scar"])
            if which == "all" or is_gun or c.get("code", "").startswith(("RA", "EC", "DO")):
                c["shop_selling"] = True
                if not c.get("price"): c["price"] = 1000
                n += 1
    twn_save_catalog(ctrl, cat)
    return {"ok": True, "opened": n, "note": "重啟台服 server 生效（它啟動時讀 catalog.json）"}

def twn_shop_save(ctrl, changes):
    """Per-item shop control for the TWN catalog: toggle on-sale and set price.
    changes = [{id, selling?(bool), sp?/cash?/tp?(price in that currency)}]."""
    p = twn_catalog_path(ctrl)
    if not os.path.exists(p): return {"error": "找不到 catalog.json"}
    cat = twn_load_catalog(ctrl)
    byid = {}
    for c in cat:
        try: byid[int(c.get("id"))] = c
        except (TypeError, ValueError): pass
    n = 0
    for ch in (changes or []):
        try: c = byid[int(ch.get("id"))]
        except (TypeError, ValueError, KeyError): continue
        if "selling" in ch:
            c["shop_selling"] = bool(ch["selling"])
        # One currency+price per item; first provided wins.
        for cur, key in (("SP", "sp"), ("CASH", "cash"), ("TP", "tp")):
            if ch.get(key) not in (None, ""):
                try: c["price"] = int(ch[key]); c["currency"] = cur
                except (TypeError, ValueError): pass
                break
        n += 1
    twn_save_catalog(ctrl, cat)
    return {"ok": True, "saved": n, "note": "已存 catalog.json；重啟台服 server 生效"}

def _twn_weapon_pool(cat, scope):
    """Weapons the client can render (cooked), excluding functional/common items,
    characters and units. scope: 'icon' = distinct by icon (all have art),
    'class' = distinct by weapon class, 'all' = every cooked weapon."""
    def is_unit(c): return str(c.get("code", ""))[:3] in ("ECU", "PCU", "RCU")
    def is_func(c): return str(c.get("class", "")).startswith(("SFCItem_Func", "SFCItem_Common"))
    weapons = [c for c in cat if c.get("cooked") and not is_func(c)
               and not is_unit(c) and not c.get("is_pmc")]
    if scope == "all":
        return weapons
    key = "icon" if scope == "icon" else "class"
    seen, out = set(), []
    for c in weapons:
        k = c.get(key)
        if scope == "icon" and not k:   # need an icon so it shows in the bag
            continue
        if k and k not in seen:
            seen.add(k); out.append(c)
    return out

def twn_gm_grant(ctrl, usn, scope="icon", maxmoney=True):
    """GM helper: drop every weapon in the chosen pool into an account's bag
    (used=0), using inventory.py's own insert shape, and max the wallet."""
    import sqlite3
    p = twn_catalog_path(ctrl)
    if not os.path.exists(p): return {"error": "找不到 catalog.json"}
    if TWN["proc"] and TWN["proc"].poll() is None:
        return {"error": "請先在面板「關伺服器」再執行（避免與執行中的存檔衝突）"}
    dbp = twn_db_path(ctrl)
    if not os.path.exists(dbp): return {"error": "台服資料庫還沒建立（先進遊戲登入一次建立帳號）"}
    cat = twn_load_catalog(ctrl)
    pool = _twn_weapon_pool(cat, scope)
    con = sqlite3.connect(dbp, timeout=10)
    try:
        row = con.execute("SELECT profile FROM accounts WHERE usn=?", (usn,)).fetchone()
        if not row: return {"error": f"找不到帳號 usn={usn}（先在遊戲登入建立）"}
        owned = {r[0] for r in con.execute("SELECT DISTINCT item_id FROM inventory WHERE usn=?", (usn,))}
        added = 0
        for c in pool:
            try: iid = int(c.get("id"))
            except (TypeError, ValueError): continue
            if iid in owned: continue
            con.execute("INSERT INTO inventory(usn,item_id,used) VALUES(?,?,0)", (usn, iid))
            owned.add(iid); added += 1
        if maxmoney:
            pr = json.loads(row[0]) if row[0] else {}
            pr["SP"] = 2000000000; pr["Cash"] = 2000000000; pr["TP"] = 99999
            for k in list(pr.keys()):
                if k in _TWN_NUMERIC and not isinstance(pr[k], int):
                    try: pr[k] = int(str(pr[k]).strip() or 0)
                    except (TypeError, ValueError): pr[k] = 0
            con.execute("UPDATE accounts SET profile=? WHERE usn=?",
                        (json.dumps(pr, ensure_ascii=False), usn))
        con.commit()
    finally: con.close()
    return {"ok": True, "added": added, "pool": len(pool),
            "note": f"已加 {added} 把槍到背包（範圍 {scope}，池 {len(pool)} 項）。重啟台服 server 後登入生效。"
                    + ("" if added <= 2500 else " ⚠ 數量很多，登入可能變慢，若卡住請改用較小範圍或刪帳號重來。")}

def twn_item_give(ctrl, usn, item_id, count=1):
    """Give one specific catalog item (gun / character / anything) to an account's
    bag. Units (ECU/PCU/RCU) are inserted activated with a unit_equipment row,
    like inventory.py's add()."""
    import sqlite3
    if TWN["proc"] and TWN["proc"].poll() is None:
        return {"error": "請先在面板「關伺服器」再執行"}
    dbp = twn_db_path(ctrl)
    if not os.path.exists(dbp): return {"error": "台服資料庫還沒建立（先進遊戲登入一次）"}
    cat = twn_load_catalog(ctrl)
    byid = {}
    for c in cat:
        try: byid[int(c.get("id"))] = c
        except (TypeError, ValueError): pass
    try: iid = int(item_id)
    except (TypeError, ValueError): return {"error": "道具 id 不是數字"}
    it = byid.get(iid)
    if not it: return {"error": f"catalog 沒有道具 id={iid}"}
    code = str(it.get("code", "")); is_unit = code[:3] in ("ECU", "PCU", "RCU")
    n = max(1, min(int(count or 1), 500))
    if is_unit: n = 1
    con = sqlite3.connect(dbp, timeout=10)
    try:
        if con.execute("SELECT 1 FROM accounts WHERE usn=?", (usn,)).fetchone() is None:
            return {"error": f"找不到帳號 usn={usn}"}
        for _ in range(n):
            serial = con.execute("INSERT INTO inventory(usn,item_id,used) VALUES(?,?,?)",
                                 (usn, iid, 1 if is_unit else 0)).lastrowid
            if is_unit:
                con.execute("INSERT OR IGNORE INTO unit_equipment VALUES(?,?,?)",
                            (usn, code, json.dumps([serial] + [0] * 15)))
        con.commit()
    finally: con.close()
    return {"ok": True, "note": f"已給 {n} 個「{it.get('name', iid)}」給 #{usn}（重啟 server 登入生效）"
                                + ("" if it.get("cooked") else " ⚠ 此道具客端無資源，可能顯示空白")}

def twn_clear_inventory(ctrl, usn, scope="bag"):
    """Clear an account's items. scope 'bag' = backpack (unused) only;
    'all' = everything incl. equipped loadouts and units."""
    import sqlite3
    if TWN["proc"] and TWN["proc"].poll() is None:
        return {"error": "請先在面板「關伺服器」再執行"}
    dbp = twn_db_path(ctrl)
    if not os.path.exists(dbp): return {"error": "台服資料庫還沒建立"}
    con = sqlite3.connect(dbp, timeout=10)
    try:
        if scope == "all":
            n = con.execute("DELETE FROM inventory WHERE usn=?", (usn,)).rowcount
            con.execute("DELETE FROM weapon_sets WHERE usn=?", (usn,))
            con.execute("DELETE FROM unit_equipment WHERE usn=?", (usn,))
        else:
            n = con.execute("DELETE FROM inventory WHERE usn=? AND used=0", (usn,)).rowcount
        con.commit()
    finally: con.close()
    return {"ok": True, "removed": n, "note": f"已清除 {n} 件（{scope}）。重啟 server 登入生效。"}

# ---------------- TWN player DB (local-lobby.sqlite3: SP/Cash/Exp/Rank live inside accounts.profile JSON) ----------------
def twn_db_path(ctrl):
    # Explicit override wins; else find the active DB. The V4 pack keeps it in
    # <pack>\save\ (sibling of server\), launched via --database save\...;
    # older layouts keep it beside server.py in twn_dir.
    if ctrl.get("twn_db"):
        return ctrl["twn_db"]
    cands = [os.path.join(os.path.dirname(ctrl["twn_dir"]), "save", "local-lobby.sqlite3"),
             os.path.join(ctrl["twn_dir"], "local-lobby.sqlite3")]
    for p in cands:
        if os.path.exists(p): return p
    return cands[0]
def twn_db_ready(ctrl): return os.path.exists(twn_db_path(ctrl))
_TWN_FLD = {"sp": "SP", "cash": "Cash", "experience": "Exp", "exp": "Exp", "tp": "TP", "rank": "Rank"}
# Every int/byte field in the profile (profile-schema.json). A non-integer in any
# of these breaks the client's profile packet (cmd 122) -> shop/inventory break.
_TWN_NUMERIC = {
    "USN", "Rank", "SPGage", "Op", "SP", "TP", "armoryIdx", "AllPlayTime", "Exp",
    "LadderPoint", "Kills", "Assists", "Deaths", "AttackRound", "AttackRoundWin",
    "DefendRound", "DefendRoundWin", "RunAwayCount", "AllHitCount", "HeadShots",
    "HeadHitCount", "CurrCount", "AllHitCharacter", "AllPlayGameCount",
    "AllWinGameCount", "NametagImage", "CSN", "ClanLevel", "CSNToRequestClanJoin",
    "Age", "Sex", "Coin", "CoinGauge", "CoinState", "CP",
    "TagID[0]", "TagID[1]", "TagID[2]", "LuckyPoint", "Cash",
}
def twn_accounts(ctrl):
    import sqlite3
    p = twn_db_path(ctrl)
    if not os.path.exists(p): return []
    c = sqlite3.connect(p, timeout=5); out = []
    try:
        for usn, login, profile in c.execute("SELECT usn,login,profile FROM accounts ORDER BY usn"):
            try: pr = json.loads(profile) if profile else {}
            except Exception: pr = {}
            out.append({"id": usn, "login": login or "", "codename": pr.get("CodeName", ""),
                        "sp": pr.get("SP", 0), "cash": pr.get("Cash", 0), "experience": pr.get("Exp", 0),
                        "tp": pr.get("TP", 0), "rank": pr.get("Rank", 1)})
    finally: c.close()
    return out
def twn_account_update(ctrl, usn, fields):
    import sqlite3
    p = twn_db_path(ctrl)
    if not os.path.exists(p): return {"error": "台服資料庫還沒建立（先讓 server 跑過並登入一次）"}
    running = bool(TWN["proc"] and TWN["proc"].poll() is None)
    c = sqlite3.connect(p, timeout=5)
    try:
        row = c.execute("SELECT profile FROM accounts WHERE usn=?", (usn,)).fetchone()
        if not row: return {"error": f"找不到帳號 usn={usn}"}
        pr = json.loads(row[0]) if row[0] else {}
        for k, v in (fields or {}).items():
            if k in _TWN_FLD:
                # NEVER store a non-integer into a numeric profile field: a blank
                # or garbage input would make the client's profile packet (cmd 122)
                # fail to pack, which breaks the shop/inventory. Skip bad input.
                try: pr[_TWN_FLD[k]] = int(str(v).strip())
                except (TypeError, ValueError): continue
        # Self-heal: coerce every numeric profile field back to a valid int, in
        # case an earlier edit (or another tool) left a string/blank there — a
        # non-int in an int field breaks the client's profile packet (cmd 122).
        for _fld in list(pr.keys()):
            if _fld in _TWN_NUMERIC and not isinstance(pr[_fld], int):
                try: pr[_fld] = int(str(pr[_fld]).strip() or 0)
                except (TypeError, ValueError): pr[_fld] = 0
        c.execute("UPDATE accounts SET profile=? WHERE usn=?", (json.dumps(pr, ensure_ascii=False), usn)); c.commit()
    finally: c.close()
    return {"ok": True, "warn": ("台服 server 運行中：若該玩家正登入，登出會覆蓋你的修改——先關 server 或確定已登出再改" if running else "")}
def twn_weapon_create(ctrl, spec):
    import copy
    cat = twn_load_catalog(ctrl)
    byid = {c["id"]: c for c in cat}
    base = byid.get(int(spec["base_id"]))
    if not base: return {"error": f"找不到底槍 id={spec.get('base_id')}"}
    nid = int(spec.get("item_id") or (max(c["id"] for c in cat) + 1))
    nw = copy.deepcopy(base); nw["id"] = nid
    if spec.get("code"): nw["code"] = spec["code"]
    if spec.get("name"): nw["name"] = spec["name"]
    if spec.get("sp"): nw["currency"] = "SP"; nw["price"] = int(spec["sp"])
    elif spec.get("cash"): nw["currency"] = "CASH"; nw["price"] = int(spec["cash"])
    elif spec.get("tp"): nw["currency"] = "TP"; nw["price"] = int(spec["tp"])
    nw["shop_selling"] = True; nw["original_selling"] = True
    cat = [c for c in cat if c["id"] != nid] + [nw]
    twn_save_catalog(ctrl, cat)
    return {"ok": True, "item_id": nid, "name": nw.get("name", ""), "base_id": base["id"],
            "note": "已複製底槍為新槍加入台服商店（沿用底槍模型/圖示，新 id/名字/價格）。重啟台服 server 生效。"}
def _client_lang(ctrl):
    """Pick a -Language whose Startup_<LANG>.upk actually exists, so the engine can boot.
    Priority: explicit client_lang (if not 'auto') -> client's setting.ini Localization ->
    EUR/TWN/INT -> whatever Startup_*.upk is present. D:\\sf is an EUR build (only
    Startup_EUR.upk), so forcing TWN makes UE3 fail to find package 'Startup'."""
    import glob
    # Search the client's OWN CookedPCConsole (derived from the real exe) first,
    # then the configured cooked_dir. The config may point at a different client.
    cooked_dirs = []
    try:
        exe = _resolve_client_exe(ctrl.get("client_exe")) or ctrl.get("client_exe", "")
        root = os.path.dirname(os.path.dirname(os.path.dirname(exe)))  # ...\Binaries\Win32\x -> client root
        cooked_dirs.append(os.path.join(root, "SFGame", "CookedPCConsole"))
    except Exception:
        root = ""
    if ctrl.get("cooked_dir"):
        cooked_dirs.append(ctrl["cooked_dir"])
    def has_startup(l):
        return bool(l) and any(os.path.exists(os.path.join(c, f"Startup_{l}.upk")) for c in cooked_dirs)
    cands = []
    v = (ctrl.get("client_lang") or "").strip()
    if v and v.lower() != "auto": cands.append(v)
    try:
        with open(os.path.join(root, "setting.ini"), encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.strip().lower().startswith("localization") and "=" in line:
                    cands.append(line.split("=", 1)[1].strip())
    except Exception:
        pass
    cands += ["TWN", "EUR", "INT"]
    for l in cands:
        if has_startup(l): return l
    # nothing matched a requested lang -> use whatever Startup_*.upk actually exists
    for c in cooked_dirs:
        try:
            hits = glob.glob(os.path.join(c, "Startup_*.upk"))
            if hits:
                b = os.path.basename(hits[0])
                return b[len("Startup_"):-len(".upk")]
        except Exception:
            pass
    return cands[0] if cands else "EUR"

def _resolve_client_exe(raw):
    """Find the client exe from whatever client_exe points at: the exe itself,
    the client root folder, or a folder holding Binaries\\Win32. Prefers the
    emulator-compatible sf2_unpacked.exe when present, else sf2.exe."""
    if not raw:
        return None
    # a direct, existing exe path -> use it
    if os.path.isfile(raw):
        return raw
    # a folder, or a path whose exe is missing -> look for a client exe nearby,
    # preferring sf2_unpacked.exe (the build this server emulator speaks).
    d = os.path.dirname(raw) if os.path.splitext(raw)[1] else raw
    for cand in (os.path.join(d, "sf2_unpacked.exe"),
                 os.path.join(d, "Binaries", "Win32", "sf2_unpacked.exe"),
                 os.path.join(d, "sf2.exe"),
                 os.path.join(d, "Binaries", "Win32", "sf2.exe")):
        if os.path.isfile(cand):
            return cand
    return None

def client_launch(ctrl, ip="127.0.0.1"):
    """Our own launcher: start the player's real client pointed at the server.
    Language is auto-detected from which Startup_<LANG>.upk exists (this client is EUR)."""
    exe = _resolve_client_exe(ctrl.get("client_exe"))
    if not exe:
        return {"error": f"客端找不到 sf2.exe：{ctrl.get('client_exe')}（在設定把 client_exe 指到 ...\\Binaries\\Win32\\sf2.exe）"}
    # The client hangs ("not responding") if the lobby isn't answering on 28080.
    # For local play, make sure the TWN server is up first (auto-start it).
    if ip in ("127.0.0.1", "localhost") and not _port_open(28080):
        if not twn_available(ctrl):
            return {"error": f"大廳 28080 沒開，且找不到 TWN server。請先按「開(本機測試)」，或把 server 放到 {ctrl['twn_dir']}。"}
        started = twn_start(ctrl, remote=False)
        if isinstance(started, dict) and started.get("error"):
            return {"error": "自動開 server 失敗：\n" + started["error"]}
        for _ in range(30):  # wait up to ~6s for the lobby to accept connections
            if _port_open(28080): break
            time.sleep(0.2)
        if not _port_open(28080):
            return {"error": "TWN server 起來了但大廳 28080 還沒開，請看 twn_server.log。"}
    lang = _client_lang(ctrl)
    args = [exe, "SFFrontEnd_new",
            f"-ServerIP={ip}", "-ServerPort=28080",
            f"-ClanServerIP={ip}", "-ClanServerPort=28081",
            f"-MessengerServerIP={ip}", "-MessengerServerPort=28082",
            "-GameStartFrom=0", f"-Language={lang}",
            "-NoSplash", "-nomoviestartup", "-windowed",
            f"-ResX={ctrl.get('client_resx',1280)}", f"-ResY={ctrl.get('client_resy',720)}"]
    try:
        # DETACHED_PROCESS so the client keeps running after the panel exits,
        # and so it does not inherit this process' console (matches the proven
        # standalone launcher; avoids the XIGNCODE init issues seen otherwise).
        flags = 0x00000008 if os.name == "nt" else 0
        proc = subprocess.Popen(args, cwd=os.path.dirname(exe), creationflags=flags, close_fds=True)
        # If the client dies within a couple of seconds it never really booted
        # (usually a -Language without a matching Startup_<LANG>.upk). Report it
        # instead of claiming success.
        time.sleep(2.0)
        if proc.poll() is not None:
            return {"error": f"客端開了就退（lang={lang}）。多半是語系對不上：確認 "
                             f"{os.path.dirname(os.path.dirname(os.path.dirname(exe)))}\\SFGame\\CookedPCConsole "
                             f"裡有 Startup_{lang}.upk；沒有的話在設定把 client_lang 改成實際存在的那個。",
                    "exit_code": proc.returncode, "cmd": " ".join(args)}
        return {"ok": True, "cmd": " ".join(args), "exe": exe, "lang": lang}
    except Exception as e:
        return {"error": f"啟動失敗：{e}"}
PLAYER_BAT = r"""@echo off
chcp 65001 >nul
setlocal
title SF2 連線啟動器
set "SERVER_IP=__IP__"
if "%SERVER_IP%"=="" set /p SERVER_IP=請輸入伺服器 IP (主機給你的):
if "%SERVER_IP%"=="" set "SERVER_IP=127.0.0.1"
set "GAME=%~dp0Binaries\Win32\sf2_unpacked.exe"
if not exist "%GAME%" set "GAME=%~dp0Binaries\Win32\sf2.exe"
if not exist "%GAME%" ( echo [ERROR] 找不到遊戲主程式，請把本檔放到客端根目錄^(有 Binaries\Win32^)。& pause & exit /b 1 )
echo 連線到 %SERVER_IP% ...
set "SFLANG=EUR"
if exist "%~dp0SFGame\CookedPCConsole\Startup_TWN.upk" set "SFLANG=TWN"
cd /d "%~dp0Binaries\Win32"
start "" "%GAME%" SFFrontEnd_new -ServerIP=%SERVER_IP% -ServerPort=28080 -ClanServerIP=%SERVER_IP% -ClanServerPort=28081 -MessengerServerIP=%SERVER_IP% -MessengerServerPort=28082 -GameStartFrom=0 -Language=%SFLANG% -NoSplash -nomoviestartup -windowed -ResX=1280 -ResY=720
exit /b 0
"""
PLAYER_TXT = """SF2 連線方式（玩家看這份就好）

1. 把這整個資料夾（客端）放到電腦任意位置。
2. 若主機要你裝 VPN（ZeroTier / Radmin），先裝好、加入他給的網路。
3. 雙擊「玩家啟動.bat」；要你輸入 IP 就貼主機給的 IP。
4. 進遊戲 → 登入（第一次自動建帳號）→ 大廳 → 加入房間。

* 你不需要任何伺服器檔案，只要這個客端資料夾 + 啟動器。
"""
def make_player_pack(ctrl, ip=""):
    """Write a PLAYER-ONLY launcher (no server files) with the IP baked in."""
    d = ctrl.get("player_dir") or os.path.join(HERE, "..", "player_package")
    os.makedirs(d, exist_ok=True)
    bat = PLAYER_BAT.replace("__IP__", ip or "")
    open(os.path.join(d, "玩家啟動.bat"), "w", encoding="utf-8").write(bat)
    open(os.path.join(d, "玩家說明.txt"), "w", encoding="utf-8").write(PLAYER_TXT)
    return {"ok": True, "dir": d, "ip": ip or "(玩家自行輸入)",
            "note": "只含啟動器+說明,無任何伺服器檔。把它和客端資料夾一起給玩家(或放進客端根目錄一起壓縮)。"}
def make_player_zip(ctrl, ip=""):
    """Bundle client + launcher into ONE zip (server files excluded) via the PS1, in a visible window."""
    make_player_pack(ctrl, ip)                                   # refresh bat/txt with IP first
    exe = ctrl["client_exe"]
    client_root = os.path.dirname(os.path.dirname(os.path.dirname(exe)))   # D:\sf\Binaries\Win32\x -> D:\sf
    pdir = ctrl.get("player_dir") or os.path.join(HERE, "..", "player_package")
    ps1 = os.path.join(HERE, "打包玩家包.ps1")
    if not os.path.exists(ps1): return {"error": f"打包腳本找不到：{ps1}"}
    if not os.path.isdir(client_root): return {"error": f"客端根目錄推不出來：{client_root}（檢查設定 client_exe）"}
    out = os.path.join(pdir, "SF2_玩家包.zip")
    try:
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)     # visible PowerShell window on Windows
        subprocess.Popen(["powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", ps1,
                          "-ClientRoot", client_root, "-PlayerPkg", pdir, "-Out", out], creationflags=flags)
        return {"ok": True, "out": out, "client_root": client_root,
                "note": "已開 PowerShell 視窗打包(客端幾 GB,要幾分鐘)。完成後 zip 在 player_package\\,只含客端+啟動器。"}
    except Exception as e:
        return {"error": f"啟動打包失敗：{e}"}

# ---------------- unlock locked items (one-click) ----------------
def unlock_apply(ctrl, which="content"):
    fn = "unlock_content.changeset.json" if which == "content" else "unlock_all.changeset.json"
    p = os.path.join(HERE, fn)
    if not os.path.exists(p): return {"error": f"{fn} not found in admin folder"}
    cs = json.load(open(p, encoding="utf-8"))
    return A.apply_changeset(A.load_cfg(), cs)

# ---------------- HTTP ----------------
def _page(): return open(os.path.join(HERE, "control_ui.html"), encoding="utf-8").read()
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _s(self, obj, ct="application/json"):
        b = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def _b(self):
        n = int(self.headers.get("Content-Length", 0)); return json.loads(self.rfile.read(n) or b"{}")
    def _q(self, k): return parse_qs(urlparse(self.path).query).get(k, [None])[0]
    def do_GET(self):
        p = self.path.split("?")[0]; cfg = A.load_cfg(); ctrl = load_ctrl()
        try:
            if p == "/": return self._s(_page().encode("utf-8"), "text/html; charset=utf-8")
            # control
            if p == "/api/ctrl": return self._s(ctrl)
            if p == "/api/server/status": return self._s(server_status(ctrl))
            if p == "/api/server/log": return self._s(server_log(ctrl))
            if p == "/api/cfg/list": return self._s(cfg_list(ctrl))
            if p == "/api/cfg/read": return self._s(cfg_read(ctrl, self._q("name")))
            if p == "/api/weapon/bases": return self._s(weapon_bases())
            if p == "/api/weapon/specs": return self._s(weapon_specs(cfg))
            if p == "/api/reskin/find": return self._s(reskin_find(ctrl, int(self._q("base"))))
            # reused admin reads
            if p == "/api/catalogue":
                if twn_available(ctrl): return self._s(twn_catalog_adapted(ctrl))
                return self._s(open(os.path.join(HERE, "catalogue.json"), "rb").read())
            if p == "/api/twn/status": return self._s(twn_status(ctrl))
            if p == "/api/config": return self._s(cfg)
            if p == "/api/accounts": return self._s(twn_accounts(ctrl) if twn_available(ctrl) else A.accounts())
            if p == "/api/items": return self._s(A.items(int(self._q("account"))))
            if p == "/api/content/shop": return self._s(A.shop_get(cfg))
            if p == "/api/content/packages": return self._s(A.pkg_get(cfg))
            if p == "/api/content/seed": return self._s(A.seed_get(cfg))
            if p == "/api/content/ranks": return self._s(A.ranks_get(cfg))
            if p == "/api/content/gacha": return self._s(A.gacha_get(cfg))
            if p == "/api/units": return self._s(A.units(int(self._q("account"))))
            if p == "/api/wsets": return self._s(A.wsets(int(self._q("account"))))
        except Exception as e: return self._s({"error": str(e)})
        self._s({"error": "not found"})
    def do_POST(self):
        cfg = A.load_cfg(); ctrl = load_ctrl()
        try:
            b = self._b()
            # control
            if self.path == "/api/ctrl": save_ctrl(b); return self._s({"ok": True})
            if self.path == "/api/server/start": return self._s(server_start(load_ctrl()))
            if self.path == "/api/server/stop": return self._s(server_stop(ctrl))
            if self.path == "/api/cfg/write": return self._s(cfg_write(ctrl, b["name"], b["text"]))
            if self.path == "/api/test": return self._s(test_conn(ctrl, b.get("port"), b.get("login", "tester"), b.get("password", "test")))
            if self.path == "/api/unlock": return self._s(unlock_apply(ctrl, b.get("which", "content")))
            if self.path == "/api/twn/start": return self._s(twn_start(load_ctrl(), b.get("remote", False)))
            if self.path == "/api/twn/stop": return self._s(twn_stop(ctrl))
            if self.path == "/api/twn/unlock": return self._s(twn_unlock(ctrl, b.get("which", "content")))
            if self.path == "/api/shop/save" and twn_available(ctrl): return self._s(twn_shop_save(ctrl, b.get("changes", [])))
            if self.path == "/api/twn/gm" and twn_available(ctrl): return self._s(twn_gm_grant(ctrl, int(b["usn"]), b.get("scope", "icon"), b.get("maxmoney", True)))
            if self.path == "/api/twn/give" and twn_available(ctrl): return self._s(twn_item_give(ctrl, int(b["usn"]), b["item_id"], b.get("count", 1)))
            if self.path == "/api/twn/clearbag" and twn_available(ctrl): return self._s(twn_clear_inventory(ctrl, int(b["usn"]), b.get("scope", "bag")))
            if self.path == "/api/client/launch": return self._s(client_launch(ctrl, b.get("ip", "127.0.0.1")))
            if self.path == "/api/player/pack": return self._s(make_player_pack(ctrl, b.get("ip", "")))
            if self.path == "/api/player/zip": return self._s(make_player_zip(ctrl, b.get("ip", "")))
            if self.path == "/api/weapon/create": return self._s(twn_weapon_create(ctrl, b) if twn_available(ctrl) else weapon_create(cfg, b))
            if self.path == "/api/reskin/export": return self._s(reskin_export(ctrl, b["pkg"]))
            if self.path == "/api/reskin/list": return self._s(reskin_list(ctrl, b["pkg"]))
            if self.path == "/api/reskin/apply": return self._s(reskin_apply(ctrl, b["pkg"], b["dds"], b.get("at")))
            if self.path == "/api/reskin/template": return self._s(reskin_template(ctrl, b["ref"], b.get("grid", 64)))
            # reused admin writes
            if self.path == "/api/open": return self._s(A.open_db(b["path"] or A.default_db()))
            if self.path == "/api/config": A.save_cfg(b); return self._s({"ok": True})
            if self.path == "/api/content/shop": A.shop_save(cfg, b.get("changes", []), b.get("adds", [])); return self._s({"ok": True})
            if self.path == "/api/content/packages": A.pkg_save(cfg, b["model"]); return self._s({"ok": True})
            if self.path == "/api/content/seed": A.seed_save(cfg, b["rows"]); return self._s({"ok": True})
            if self.path == "/api/content/ranks": A.ranks_save(cfg, b.get("ranks", []), b.get("sp", []), b.get("scale")); return self._s({"ok": True})
            if self.path == "/api/content/gacha": A.gacha_save(cfg, b["model"]); return self._s({"ok": True})
            if self.path == "/api/content/apply": return self._s(A.apply_changeset(cfg, b["changeset"]))
            if self.path == "/api/content/build": return self._s(A.build(cfg))
            if twn_available(ctrl) and self.path == "/api/account/update":
                return self._s(twn_account_update(ctrl, int(b["id"]), b.get("fields", {})))
            if twn_available(ctrl) and self.path.startswith("/api/account"):
                return self._s({"error": "台服模式：帳號會在遊戲登入時自動建立；這裡用「存」改 SP/Cash/經驗即可"})
            if A.STATE["db"] is None and self.path.startswith("/api/account") or (A.STATE["db"] is None and self.path.startswith("/api/item")):
                return self._s({"error": "open a database first (資料庫 分頁)"})
            if self.path == "/api/account/create": return self._s({"id": A.create_account(b["login"], b["password"], b.get("seed", False))})
            if self.path == "/api/account/update": A.update_account(b["id"], b["fields"]); return self._s({"ok": True})
            if self.path == "/api/account/delete": A.delete_account(b["id"]); return self._s({"ok": True})
            if self.path == "/api/item/add": A.add_item(b["account"], b["item_id"], b["active"], b.get("count", 1), b.get("expire_days", 0)); return self._s({"ok": True})
            if self.path == "/api/item/remove": A.remove_item(b["sn"]); return self._s({"ok": True})
        except Exception as e: return self._s({"error": str(e)})
        self._s({"error": "not found"})

def main():
    try: sys.stdout.reconfigure(line_buffering=True)   # show the banner without needing -u
    except Exception: pass
    ap = argparse.ArgumentParser(); ap.add_argument("--db"); ap.add_argument("--port", type=int, default=8777); a = ap.parse_args()
    if a.db: A.open_db(a.db)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H); url = f"http://127.0.0.1:{a.port}"
    print(f"SF2 Control Center on {url}\nCtrl+C to stop (also stops the game server if running).", flush=True)
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt:
        server_stop(load_ctrl())
if __name__ == "__main__": main()
