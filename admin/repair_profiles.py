r"""One-shot repair for local-lobby.sqlite3 account profiles.

Two problems it fixes, both of which make the client's profile packet (cmd 122)
fail to pack and break the shop/inventory ("buying doesn't go into the backpack"
/ "拿到的東西不對") or spam the server log with struct.error:

  1. a blank/string value sitting in a numeric field, and
  2. a numeric value that is TOO BIG for its field type, e.g. an int above
     2,147,483,647 (the 32-bit limit) -> struct.error "'i' format requires ...".

Run it with the server STOPPED.

Usage (from the pack's server\ folder):
    ..\python\python.exe repair_profiles.py ..\save\local-lobby.sqlite3

It looks for profile-schema.json next to the DB and in the current folder so it
can clamp each field to its exact type. If it can't find the schema it still
fixes everything in the NUMERIC list below, clamping to the 32-bit int range.
"""
import json
import os
import sqlite3
import sys

# Every int/byte field in profile-schema.json (everything that is not a string).
# Used as a fallback when profile-schema.json cannot be located.
NUMERIC = {
    "USN", "Rank", "SPGage", "Op", "SP", "TP", "armoryIdx", "AllPlayTime", "Exp",
    "LadderPoint", "Kills", "Assists", "Deaths", "AttackRound", "AttackRoundWin",
    "DefendRound", "DefendRoundWin", "RunAwayCount", "AllHitCount", "HeadShots",
    "HeadHitCount", "CurrCount", "AllHitCharacter", "AllPlayGameCount",
    "AllWinGameCount", "NametagImage", "CSN", "ClanLevel", "CSNToRequestClanJoin",
    "Age", "Sex", "Coin", "CoinGauge", "CoinState", "CP",
    "TagID[0]", "TagID[1]", "TagID[2]", "LuckyPoint", "Cash",
}

# Safe ranges per schema type. Kept inside both signed and unsigned limits so
# the clamp never produces a value the wire layer rejects, whatever its exact
# struct format. Real profile data never reaches these edges, so clamping only
# ever touches corrupted values.
RANGE = {
    "byte": (0, 255),
    "short": (0, 32767),
    "int": (-2147483647, 2147483647),
}


def load_types(db_path):
    """Return {field_name: type} from profile-schema.json, or {} if not found."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(db_path)), "profile-schema.json"),
        os.path.join(here, "profile-schema.json"),
        "profile-schema.json",
    ]
    for path in candidates:
        try:
            schema = json.loads(open(path, encoding="utf-8").read())
            return {f["name"]: f["type"] for f in schema if f.get("type") != "string"}
        except Exception:
            continue
    return {}


def coerce(value):
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0


def clamp(value, kind):
    lo, hi = RANGE.get(kind, RANGE["int"])
    return lo if value < lo else hi if value > hi else value


def main(db_path):
    types = load_types(db_path)
    if types:
        print(f"loaded profile-schema.json ({len(types)} numeric fields)")
    else:
        print("profile-schema.json not found; using built-in list, clamping to int32")
        types = {name: "int" for name in NUMERIC}
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT usn, profile FROM accounts").fetchall()
    fixed = 0
    for row in rows:
        try:
            pr = json.loads(row["profile"]) if row["profile"] else {}
        except Exception:
            continue
        changed = False
        notes = []
        for key, kind in types.items():
            if key not in pr:
                continue
            original = pr[key]
            # isinstance(bool, int) is True, which is fine for struct.pack.
            value = original if isinstance(original, int) else coerce(original)
            value = clamp(value, kind)
            if value != original:
                pr[key] = value
                changed = True
                notes.append(f"{key}:{original!r}->{value}")
        if changed:
            con.execute("UPDATE accounts SET profile=? WHERE usn=?",
                        (json.dumps(pr, ensure_ascii=False), row["usn"]))
            fixed += 1
            print(f"  usn {row['usn']}: " + ", ".join(notes))
    con.commit()
    con.close()
    print(f"checked {len(rows)} account(s); repaired {fixed}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(r"usage: python repair_profiles.py <path to local-lobby.sqlite3>")
        sys.exit(1)
    main(sys.argv[1])
