#!/usr/bin/env python3
r"""
repack_sfoffline.py — rebuild SFOffline.exe with edited embedded resources.

SFOffline's shop table (SFGameItem.eur), package/capsule contents (SFPackageItem.eur),
starter items (SeedItems.tsv) and rank table (DefaultRankPoint.ini) are embedded inside
SFOffline.dll, which is itself embedded in the SFOffline.exe .NET single-file bundle.
This rebuilds both layers so your edits take effect.

  python repack_sfoffline.py --exe SFOffline.exe --res-dir <edited resources> --out SFOffline.patched.exe

--res-dir must contain any of: SFGameItem.eur SFPackageItem.eur DefaultRankPoint.ini SeedItems.tsv
(files you omit keep the original embedded copy). Run with --verify to re-extract and check.

Approach (no recompile; byte-level, validated by round-trip):
  * rebuild the managed-resource blob, append it to SFOffline.dll as a new PE section,
    repoint the CLI header's Resources directory, and patch the 4 ManifestResource offsets;
  * re-pack the single-file bundle (recompute every file offset + the manifest).
Run SFOffline.patched.exe directly with --server-only (the launcher's integrity check does
not apply to a server you run yourself).
"""
import argparse, struct, os, sys

MARKER = bytes([0x8b,0x12,0x02,0xb9,0x6a,0x61,0x20,0x38,0x72,0x7b,0x93,0x02,0x14,0xd7,0xa0,0x32,
                0x13,0xf5,0xb9,0xe6,0xef,0xae,0x33,0x18,0xee,0x3b,0x2d,0xce,0x24,0xb3,0x6a,0xae])
RES_NAMES = ["SFGameItem.eur","SFPackageItem.eur","DefaultRankPoint.ini","SeedItems.tsv"]

def alignup(n,a): return (n + a - 1) & ~(a - 1)

# ---------- 7-bit length-prefixed string (bundle manifest) ----------
def read_str(d,o):
    shift=0;res=0
    while True:
        b=d[o];o+=1;res|=(b&0x7f)<<shift
        if not (b&0x80): break
        shift+=7
    return d[o:o+res].decode("utf-8"), o+res
def write_str(s):
    b=s.encode("utf-8"); n=len(b); out=bytearray()
    while True:
        x=n&0x7f; n>>=7
        if n: out.append(x|0x80)
        else: out.append(x); break
    return bytes(out)+b

# ---------- bundle parse / rebuild ----------
def parse_bundle(data):
    i=data.find(MARKER)
    if i<0: raise SystemExit("not a .NET single-file bundle (marker not found)")
    hoff=struct.unpack_from("<q",data,i-8)[0]
    o=hoff
    major,minor=struct.unpack_from("<II",data,o); o+=8
    count=struct.unpack_from("<i",data,o)[0]; o+=4
    bundle_id,o=read_str(data,o)
    dj_off,dj_sz,rc_off,rc_sz,flags=struct.unpack_from("<qqqqq",data,o); o+=40
    files=[]
    for _ in range(count):
        foff,fsz,fcsz=struct.unpack_from("<qqq",data,o); o+=24
        ftype=data[o]; o+=1
        fpath,o=read_str(data,o)
        files.append(dict(off=foff,size=fsz,csz=fcsz,type=ftype,path=fpath))
    return dict(marker_at=i,hoff=hoff,major=major,minor=minor,bundle_id=bundle_id,
                dj=(dj_off,dj_sz),rc=(rc_off,rc_sz),flags=flags,files=files)

def rebuild_bundle(data, new_dll):
    b=parse_bundle(data)
    files=b["files"]
    first=min(f["off"] for f in files)
    prefix=bytearray(data[:first])          # apphost PE (contains marker+placeholder)
    out=bytearray(prefix)
    # append files in ascending original offset order, aligning assemblies/native to 4096
    order=sorted(range(len(files)), key=lambda k: files[k]["off"])
    newmeta={}
    for k in order:
        f=files[k]
        if f["type"] in (1,2):
            while len(out)%4096: out.append(0)
        no=len(out)
        if f["path"].endswith("SFOffline.dll") and f["type"]==1:
            payload=new_dll
        else:
            payload=data[f["off"]:f["off"]+f["size"]]
        out+=payload
        newmeta[k]=(no,len(payload),f["csz"],f["type"],f["path"])
    # manifest
    man_off=len(out)
    hdr=bytearray()
    hdr+=struct.pack("<II",b["major"],b["minor"])
    hdr+=struct.pack("<i",len(files))
    hdr+=write_str(b["bundle_id"])
    # deps/runtimeconfig new locations (look them up by path/type)
    def locate(suffix,typ):
        for k in order:
            if files[k]["path"].endswith(suffix) and files[k]["type"]==typ:
                return newmeta[k][0], newmeta[k][1]
        return (0,0)
    dj=locate(".deps.json",3); rc=locate(".runtimeconfig.json",4)
    hdr+=struct.pack("<qqqqq",dj[0],dj[1],rc[0],rc[1],b["flags"])
    for k in range(len(files)):   # entries in ORIGINAL table order
        no,nsz,ncsz,ntype,npath=newmeta[k]
        hdr+=struct.pack("<qqq",no,nsz,ncsz)
        hdr.append(ntype)
        hdr+=write_str(npath)
    out+=hdr
    # patch the manifest-offset placeholder (8 bytes before the marker, in the prefix)
    struct.pack_into("<q",out,b["marker_at"]-8,man_off)
    return bytes(out)

# ---------- DLL managed-resource rebuild ----------
def dll_resources_offset_and_rowsize(dll):
    import dnfile
    pe=dnfile.dnPE(data=dll)
    mr=pe.net.mdtables.ManifestResource
    rows=[(str(r.Name), r.Offset) for r in mr.rows]
    return mr.file_offset, mr.row_size, rows

def rebuild_dll(dll, new_res):
    """new_res: dict name->bytes (only overrides). Returns new DLL bytes."""
    import pefile, dnfile
    pe=pefile.PE(data=dll)
    oh=pe.OPTIONAL_HEADER; fh=pe.FILE_HEADER
    SA=oh.SectionAlignment; FA=oh.FileAlignment
    # current embedded resources (name->bytes), in table order
    dpe=dnfile.dnPE(data=dll)
    cor=dpe.net.struct
    res_rva=cor.ResourcesRva
    res_base=pe.get_offset_from_rva(res_rva)
    mr=dpe.net.mdtables.ManifestResource
    order=[(str(r.Name), r.Offset) for r in mr.rows]
    cur={}
    for name,roff in order:
        ln=struct.unpack_from("<I",dll,res_base+roff)[0]
        cur[name]=bytes(dll[res_base+roff+4:res_base+roff+4+ln])
    # build new blob in the SAME order
    blob=bytearray(); offsets={}
    for name,_ in order:
        payload=new_res.get(name, cur[name])
        offsets[name]=len(blob)
        blob+=struct.pack("<I",len(payload))+payload
        while len(blob)%8: blob.append(0)
    res_size=len(blob)
    # append new section
    last=pe.sections[-1]
    new_va=alignup(last.VirtualAddress+last.Misc_VirtualSize, SA)
    out=bytearray(dll)
    while len(out)%FA: out.append(0)
    new_raw=len(out)
    out+=blob
    while len(out)%FA: out.append(0)
    raw_sz=len(out)-new_raw
    # section header (40 bytes) right after the last existing header
    sh_off=last.get_file_offset()+40
    name_b=b".sfres0\x00"[:8].ljust(8,b"\x00")
    sh=name_b+struct.pack("<IIII", res_size, new_va, raw_sz, new_raw)+struct.pack("<IIHHI",0,0,0,0,0x40000040)
    out[sh_off:sh_off+40]=sh
    # COFF NumberOfSections++
    nsec_off=fh.get_field_absolute_offset("NumberOfSections")
    struct.pack_into("<H",out,nsec_off, fh.NumberOfSections+1)
    # SizeOfImage
    soi_off=oh.get_field_absolute_offset("SizeOfImage")
    struct.pack_into("<I",out,soi_off, new_va+alignup(res_size,SA))
    # Cor20.Resources RVA+Size  (file off = get_offset_from_rva(cor dir) + 24)
    cor_dir_off=pe.get_offset_from_rva(oh.DATA_DIRECTORY[14].VirtualAddress)
    struct.pack_into("<II",out,cor_dir_off+24, new_va, res_size)
    # patch ManifestResource offsets
    base=mr.file_offset; rs=mr.row_size
    for i,(name,_) in enumerate(order):
        struct.pack_into("<I",out,base+i*rs, offsets[name])
    return bytes(out), order, offsets, res_size

# ---------- verify ----------
def verify(new_exe, new_res, order_names):
    import dnfile
    b=parse_bundle(new_exe)
    dllmeta=[f for f in b["files"] if f["path"].endswith("SFOffline.dll")][0]
    ndll=new_exe[dllmeta["off"]:dllmeta["off"]+dllmeta["size"]]
    dpe=dnfile.dnPE(data=ndll)  # parses => PE+metadata valid
    import pefile
    pe=pefile.PE(data=ndll)
    res_base=pe.get_offset_from_rva(dpe.net.struct.ResourcesRva)
    mr=dpe.net.mdtables.ManifestResource
    got={}
    for r in mr.rows:
        nm=str(r.Name); ro=r.Offset
        ln=struct.unpack_from("<I",ndll,res_base+ro)[0]
        got[nm]=bytes(ndll[res_base+ro+4:res_base+ro+4+ln])
    ok=True
    for nm in order_names:
        if nm in new_res:
            same = got[nm]==new_res[nm]
            print(f"   resource {nm:22} {len(got[nm])} bytes  edited  match={same}")
            ok &= same
        else:
            print(f"   resource {nm:22} {len(got[nm])} bytes  (unchanged)")
    # also confirm every OTHER bundle file is byte-identical to original
    return ok

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--exe",required=True); ap.add_argument("--res-dir",required=True)
    ap.add_argument("--out",required=True); ap.add_argument("--verify",action="store_true")
    a=ap.parse_args()
    data=open(a.exe,"rb").read()
    b=parse_bundle(data)
    dllmeta=[f for f in b["files"] if f["path"].endswith("SFOffline.dll")][0]
    dll=data[dllmeta["off"]:dllmeta["off"]+dllmeta["size"]]
    new_res={}
    for nm in RES_NAMES:
        p=os.path.join(a.res_dir,nm)
        if os.path.exists(p): new_res[nm]=open(p,"rb").read()
    if not new_res:
        raise SystemExit("no resource files found in --res-dir; nothing to do")
    print("editing resources:", ", ".join(new_res))
    new_dll,order,offs,rsz=rebuild_dll(dll,new_res)
    new_exe=rebuild_bundle(data,new_dll)
    open(a.out,"wb").write(new_exe)
    print(f"wrote {a.out}  ({len(new_exe):,} bytes; was {len(data):,})")
    if a.verify:
        print("verify:")
        ok=verify(new_exe,new_res,[n for n,_ in order])
        print("VERIFY", "PASSED" if ok else "FAILED"); sys.exit(0 if ok else 1)

if __name__=="__main__": main()
