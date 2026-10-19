"""Editing of SFOffline's embedded content resources (text), preserving unknown fields.
Shop  = SFGameItem.eur   (GameItemInfo=(...) lines; edit only named fields in place)
Pkg   = SFPackageItem.eur (PackageItemArray=(pkg_itemid=,sub_itemid=,counts=) ; full rewrite)
Seed  = SeedItems.tsv     (id<TAB>count<TAB>class<TAB>name ; full rewrite, keep # header)
"""
import re

INT_FIELDS  = ["SP_Price","Cash_Price","TP_Price","ResellPrice","buy_limit","UseLimitRank","ItemUseTime"]
BOOL_FIELDS = ["IsSelling","CanResell","IsCooking"]
STR_FIELDS  = ["SellType","ItemName","ItemCode"]

def _field_re(name): return re.compile(r'(\b'+re.escape(name)+r'=)("(?:[^"\\]|\\.)*"|[^,)]*)')

def parse_shop(text):
    items=[]
    for idx,line in enumerate(text.split("\n")):
        s=line.strip()
        if not (s.startswith("GameItemInfo=(") and s.endswith(")")): continue
        f={}
        for m in re.finditer(r'(\w+)=("(?:[^"\\]|\\.)*"|[^,]*)', s[len("GameItemInfo=("):-1]):
            v=m.group(2)
            if v.startswith('"') and v.endswith('"'): v=v[1:-1]
            f[m.group(1)]=v
        def i(k):
            try: return int(f.get(k,"0") or 0)
            except: return 0
        items.append({"line":idx,"id":i("ItemId"),"code":f.get("ItemCode",""),"name":f.get("ItemName",""),
                      "cls":f.get("ItemClassName",""),"sp":i("SP_Price"),"cash":i("Cash_Price"),"tp":i("TP_Price"),
                      "resell":i("ResellPrice"),"selling":f.get("IsSelling","False")=="True",
                      "canResell":f.get("CanResell","False")=="True","sellType":f.get("SellType","")})
    return items

def set_shop_field(text, item_id, field, value):
    """Replace one field on the GameItemInfo line whose ItemId==item_id, preserving all else."""
    lines=text.split("\n")
    target=None
    for i,l in enumerate(lines):
        if l.strip().startswith("GameItemInfo=(") and re.search(r'\bItemId='+str(item_id)+r'\b', l):
            target=i; break
    if target is None: raise KeyError(f"ItemId {item_id} not found")
    if field in BOOL_FIELDS: rep = "True" if (value in (True,"True","true",1,"1")) else "False"
    elif field in INT_FIELDS: rep = str(int(value))
    elif field in STR_FIELDS: rep = '"'+str(value)+'"'
    else: raise ValueError("unsupported field "+field)
    new, n = _field_re(field).subn(lambda m: m.group(1)+rep, lines[target], count=1)
    if n==0:  # field absent -> insert before closing paren
        new = lines[target][:-1] + f",{field}={rep})"
    lines[target]=new
    return "\n".join(lines)

def add_shop_item(text, template_id, overrides):
    """Clone the GameItemInfo line of template_id, apply overrides (dict field->value), append."""
    lines=text.split("\n"); tmpl=None
    for l in lines:
        if l.strip().startswith("GameItemInfo=(") and re.search(r'\bItemId='+str(template_id)+r'\b', l):
            tmpl=l.strip(); break
    if tmpl is None: raise KeyError(f"template ItemId {template_id} not found")
    newline=tmpl
    for field,value in overrides.items():
        if field in BOOL_FIELDS: rep="True" if value in (True,"True","true",1,"1") else "False"
        elif field in INT_FIELDS or field=="ItemId": rep=str(int(value))
        else: rep='"'+str(value)+'"'
        nl,n=_field_re(field).subn(lambda m:m.group(1)+rep, newline, count=1)
        newline = nl if n else (newline[:-1]+f",{field}={rep})")
    # insert after the last GameItemInfo line
    last=max(i for i,l in enumerate(lines) if l.strip().startswith("GameItemInfo=("))
    lines.insert(last+1, newline)
    return "\n".join(lines)

def parse_packages(text):
    pkgs={}
    for m in re.finditer(r'PackageItemArray=\(([^)]*)\)', text):
        f=dict(re.findall(r'(\w+)\s*=\s*([0-9]+)', m.group(1)))
        p=int(f.get("pkg_itemid",0)); s=int(f.get("sub_itemid",0)); c=int(f.get("counts",1))
        pkgs.setdefault(p,[]).append([s,c])
    return pkgs

def write_packages(pkgs):
    out=["[SFPackageItem]",""]
    for p in sorted(pkgs, key=lambda x:int(x)):
        for s,c in pkgs[p]:
            out.append(f"PackageItemArray=(pkg_itemid={p}, sub_itemid={s},counts={c})")
    return "\n".join(out)+"\n"

def parse_seed(text):
    header=[]; rows=[]
    for line in text.split("\n"):
        if line.startswith("#"): header.append(line); continue
        if not line.strip(): continue
        p=line.split("\t")
        if p and p[0].strip().isdigit():
            rows.append({"id":int(p[0]),"count":int(p[1]) if len(p)>1 and p[1].strip().isdigit() else 1,
                         "cls":p[2] if len(p)>2 else "","name":p[3] if len(p)>3 else ""})
    return header, rows

def write_seed(header, rows):
    out=list(header)
    for r in rows:
        out.append(f'{r["id"]}\t{r.get("count",1)}\t{r.get("cls","")}\t{r.get("name","")}')
    return "\n".join(out)+"\n"

# ---- ranks (DefaultRankPoint.ini) ----
import re as _re
def parse_ranks(text):
    ranks=[]; sp=[]
    for m in _re.finditer(r'RankPointArray=\(Rank=(\d+),RealRank=(\d+),RankExp=(\d+)\)', text):
        ranks.append({"rank":int(m.group(1)),"real":int(m.group(2)),"exp":int(m.group(3))})
    for m in _re.finditer(r'SPGageArray=\(Rank=(\d+),SPGage=(\d+),AddSP=(\d+)\)', text):
        sp.append({"rank":int(m.group(1)),"gage":int(m.group(2)),"addsp":int(m.group(3))})
    return {"ranks":ranks,"sp":sp}
def set_rank_exp(text, rank, exp):
    pat=_re.compile(r'(RankPointArray=\(Rank='+str(rank)+r',RealRank=\d+,RankExp=)\d+(\))')
    new,n=pat.subn(lambda m:m.group(1)+str(int(exp))+m.group(2), text, count=1)
    if n==0: raise KeyError(f"rank {rank} not found")
    return new
def set_sp_gage(text, rank, gage, addsp):
    pat=_re.compile(r'(SPGageArray=\(Rank='+str(rank)+r',SPGage=)\d+(,AddSP=)\d+(\))')
    new,n=pat.subn(lambda m:m.group(1)+str(int(gage))+m.group(2)+str(int(addsp))+m.group(3), text, count=1)
    if n==0: raise KeyError(f"sp rank {rank} not found")
    return new
def scale_rank_exp(text, factor):
    def rep(m): return m.group(1)+str(max(0,int(int(m.group(2))*factor)))+m.group(3)
    return _re.sub(r'(RankPointArray=\(Rank=\d+,RealRank=\d+,RankExp=)(\d+)(\))', rep, text)

# ---- gacha weights (stored as a sidecar JSON next to resources; used by a patched server
#      or our python server; format: {pkg_itemid: {"mode":"one","weights":{sub_itemid:weight}}}) ----
def draw(weights:dict):
    """weighted random pick -> sub_itemid (int). weights: {sub_itemid:weight}. Pure-stdlib."""
    import random
    items=list(weights.items()); total=sum(w for _,w in items)
    if total<=0: return int(items[0][0]) if items else None
    r=random.uniform(0,total); acc=0
    for sid,w in items:
        acc+=w
        if r<=acc: return int(sid)
    return int(items[-1][0])
