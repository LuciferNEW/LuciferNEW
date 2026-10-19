#!/usr/bin/env python3
r"""
reskin_inject.py — same-size texture reskin for SF2's UE3 texture packages (.upk).

SF2 weapon textures live INLINE in uncompressed packages like rif_<weapon>_texture.upk
(verified: no .tfc streaming, PKG_StoreCompressed off). A texture mip is stored as an
FByteBulkData block:
    uint32 BulkDataFlags | int32 ElementCount | int32 BulkDataSizeOnDisk |
    int32  BulkDataOffsetInFile | <data...>
For an INLINE mip, BulkDataOffsetInFile == (position of the data), i.e. header_pos+16.
That is a strong, verifiable signature, so we can find and overwrite a mip in place
WITHOUT a full UE3 parser — as long as the replacement is the EXACT same byte size
(same dimensions + same DXT format). Same size ⇒ no package tables shift ⇒ safe.

Workflow:
  1) umodel exports the base weapon's textures to PNG (you see them).
  2) You edit a texture and SAVE AS .DDS with the SAME dimensions + SAME format
     (DXT1 or DXT5 — same as the original; GIMP/Photoshop/Paint.NET DDS plugins do this).
  3) reskin_inject.py drops that mip0 into the .upk (backs up to .upk.bak first).

  py reskin_inject.py list  rif_m4a1_texture.upk
  py reskin_inject.py check rif_m4a1_texture.upk newskin.dds
  py reskin_inject.py apply rif_m4a1_texture.upk newskin.dds        # writes (after backup)
  py reskin_inject.py apply rif_m4a1_texture.upk newskin.dds --at 0x1234   # force a hit offset
"""
import sys, os, struct

def u32(d, o): return struct.unpack_from("<I", d, o)[0]
def i32(d, o): return struct.unpack_from("<i", d, o)[0]

# ---- DDS ----
def dds_info(path):
    d = open(path, "rb").read()
    if d[:4] != b"DDS ": raise ValueError("not a DDS file")
    h = d[4:128]
    height = u32(h, 8); width = u32(h, 12)
    fourcc = h[80:84]
    data_off = 128
    if fourcc == b"DX10": data_off = 148  # skip DX10 header
    def blocks(x): return max(1, (x + 3) // 4)
    if fourcc in (b"DXT1",): fmt, mip0 = "DXT1", blocks(width)*blocks(height)*8
    elif fourcc in (b"DXT3", b"DXT5"): fmt, mip0 = fourcc.decode(), blocks(width)*blocks(height)*16
    else: fmt, mip0 = "RAW", width*height*4   # assume 32bpp
    return {"width": width, "height": height, "format": fmt,
            "mip0": mip0, "data_off": data_off, "data": d[data_off:data_off+mip0], "raw": d}

# ---- UE3 name table (for nearby-name hints) ----
def _names(d):
    try:
        off = 8; ths = i32(d, off); off += 4
        sl = i32(d, off); off += 4; off += sl if sl > 0 else 0
        off += 4                                   # PackageFlags
        nc = i32(d, off); no = i32(d, off+4)
        names = []; p = no
        for _ in range(nc):
            l = i32(d, p); p += 4
            if l >= 0: s = d[p:p+l-1].decode("latin1", "replace"); p += l
            else: s = d[p:p+(-l)*2-2].decode("utf-16-le", "replace"); p += (-l)*2
            p += 8; names.append(s)
        return names
    except Exception:
        return []

def _nearby_name(d, pos, names):
    """Find the texture-ish name that appears closest before pos (crude but useful)."""
    window = d[max(0, pos-4000):pos]
    best = None
    for n in names:
        if len(n) < 3: continue
        i = window.rfind(n.encode("latin1", "replace"))
        if i >= 0 and (best is None or i > best[1]): best = (n, i)
    return best[0] if best else "?"

# ---- find inline bulk mips ----
def find_bulk_mips(d, want_size=None):
    """Scan for FByteBulkData blocks whose data is inline. Returns list of hits."""
    hits = []; n = len(d); p = 0
    while p + 16 < n:
        size_on_disk = u32(d, p+8)
        off_in_file  = u32(d, p+12)
        # inline mip: offset points right after the 16-byte header, size is plausible
        if 4096 <= size_on_disk <= 64*1024*1024 and off_in_file == p+16 and p+16+size_on_disk <= n:
            if (want_size is None) or (size_on_disk == want_size):
                hits.append({"hdr": p, "data": p+16, "size": size_on_disk,
                             "elem": u32(d, p+4), "flags": u32(d, p)})
            p += 16 + size_on_disk; continue
        p += 1
    return hits

def cmd_list(upk):
    d = open(upk, "rb").read(); names = _names(d)
    hits = find_bulk_mips(d)
    print(f"{upk}: {len(d)} bytes, {len(hits)} inline texture mip block(s)")
    print(f"{'#':>3} {'offset':>10} {'size(B)':>10}  guess        near-name")
    for i, h in enumerate(hits):
        # guess dims assuming square DXT1/DXT5
        import math
        b1 = h["size"]//8; b5 = h["size"]//16
        g = ""
        for blk, tag in [(b1,"DXT1"), (b5,"DXT5")]:
            s = int(math.isqrt(blk))
            if s*s == blk: g = f"{s*4}x{s*4} {tag}"; break
        print(f"{i:>3} {h['data']:>10} {h['size']:>10}  {g:<12} {_nearby_name(d, h['hdr'], names)}")
    return hits

def cmd_check(upk, dds):
    d = open(upk, "rb").read(); info = dds_info(dds)
    print(f"DDS: {info['width']}x{info['height']} {info['format']}  mip0={info['mip0']} bytes")
    hits = find_bulk_mips(d, want_size=info["mip0"])
    names = _names(d)
    if not hits:
        print("NO matching-size mip found — your DDS must match an original texture's exact "
              "dimensions AND format. Run `list` to see sizes."); return None
    print(f"{len(hits)} slot(s) match this size:")
    for i, h in enumerate(hits):
        print(f"  [{i}] offset {h['data']} (0x{h['data']:x})  near-name: {_nearby_name(d, h['hdr'], names)}")
    if len(hits) > 1:
        print("Multiple slots match — pick one with --at <offset> when applying.")
    return hits

def _blocks(x): return max(1, (x + 3) // 4)
def png_to_mip(png, want_size):
    """Encode a PNG to the DXT format whose mip0 size == want_size (same dims)."""
    from PIL import Image
    import io, struct as _s
    im = Image.open(png).convert("RGBA"); w, h = im.size
    s1 = _blocks(w)*_blocks(h)*8; s5 = _blocks(w)*_blocks(h)*16
    fmt = "DXT1" if want_size == s1 else ("DXT5" if want_size == s5 else None)
    if fmt is None:
        raise ValueError(f"PNG {w}x{h} → DXT1={s1} / DXT5={s5}, neither matches the target slot {want_size}. "
                         f"Your canvas must be the original texture's exact size.")
    b = io.BytesIO(); im.save(b, format="DDS", pixel_format=fmt); raw = b.getvalue()
    off = 148 if raw[84:88] == b"DX10" else 128
    data = raw[off:off+want_size]
    if len(data) != want_size:
        raise ValueError(f"encoded {len(data)}B != target {want_size}B")
    return {"format": fmt, "width": w, "height": h, "mip0": want_size, "data": data}

def _make_info(src, want_size=None):
    """Load a replacement texture from .dds (exact) or .png (auto-encode to matching DXT)."""
    if src.lower().endswith(".png"):
        if want_size is None: raise ValueError("PNG input needs a target slot size (use via check/apply)")
        return png_to_mip(src, want_size)
    return dds_info(src)

def cmd_template(w, h, out, grid=0):
    """Write a blank transparent PNG canvas (optional grid) at the texture's size, to design on."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
    if grid:
        dr = ImageDraw.Draw(im); step = int(grid)
        for x in range(0, int(w), step): dr.line([(x, 0), (x, h)], fill=(255, 255, 255, 40))
        for y in range(0, int(h), step): dr.line([(0, y), (w, y)], fill=(255, 255, 255, 40))
    im.save(out); print(f"template {int(w)}x{int(h)} -> {out}")

def cmd_apply(upk, dds, at=None):
    d = bytearray(open(upk, "rb").read())
    if dds.lower().endswith(".png"):
        # find the slot by matching either DXT1 or DXT5 size for this PNG's dimensions
        from PIL import Image
        w, h = Image.open(dds).size
        s1 = _blocks(w)*_blocks(h)*8; s5 = _blocks(w)*_blocks(h)*16
        hits = find_bulk_mips(bytes(d), want_size=s1) + find_bulk_mips(bytes(d), want_size=s5)
        if at is not None: hits = [x for x in hits if x["data"] == at]
        if not hits: print(f"No slot matches {w}x{h} (DXT1={s1}/DXT5={s5}). Aborting."); return 2
        if len(hits) > 1: print(f"{len(hits)} slots match; use --at <offset> (see `check`). Aborting."); return 3
        info = png_to_mip(dds, hits[0]["size"]); h0 = hits[0]
        bak = upk + ".bak"
        if not os.path.exists(bak): open(bak, "wb").write(bytes(d)); print("backup ->", bak)
        d[h0["data"]:h0["data"]+h0["size"]] = info["data"]; open(upk, "wb").write(bytes(d))
        print(f"OK: {info['format']} {w}x{h} ({info['mip0']}B) -> offset {h0['data']} of {os.path.basename(upk)}")
        return 0
    info = dds_info(dds)
    hits = find_bulk_mips(bytes(d), want_size=info["mip0"])
    if at is not None:
        hits = [h for h in hits if h["data"] == at]
    if not hits:
        print("No matching slot (size mismatch or bad --at). Aborting, nothing written."); return 2
    if len(hits) > 1:
        print(f"{len(hits)} slots match; disambiguate with --at <offset> (see `check`). Aborting."); return 3
    h = hits[0]
    bak = upk + ".bak"
    if not os.path.exists(bak): open(bak, "wb").write(bytes(d)); print("backup ->", bak)
    d[h["data"]:h["data"]+h["size"]] = info["data"]
    open(upk, "wb").write(bytes(d))
    print(f"OK: wrote {info['mip0']} bytes at offset {h['data']} of {os.path.basename(upk)}")
    print("Hand the edited .upk to players (or keep it in your client). No repack needed.")
    return 0

if __name__ == "__main__":
    a = sys.argv
    if len(a) < 3: print(__doc__); sys.exit(1)
    cmd, upk = a[1], a[2]
    at = None
    if "--at" in a: at = int(a[a.index("--at")+1], 0)
    if cmd == "list": cmd_list(upk)
    elif cmd == "check": cmd_check(upk, a[3])
    elif cmd == "apply": sys.exit(cmd_apply(upk, a[3], at))
    elif cmd == "template":
        # template <w> <h> <out.png> [--grid N]   (upk arg position holds width here)
        grid = int(a[a.index("--grid")+1]) if "--grid" in a else 0
        cmd_template(a[2], a[3], a[4], grid)
    else: print("unknown cmd"); sys.exit(1)
