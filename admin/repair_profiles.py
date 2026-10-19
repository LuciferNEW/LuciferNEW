"""One-shot repair: coerce every numeric profile field in local-lobby.sqlite3
back to a valid integer. A blank/string value in an int/byte field makes the
client's profile packet (cmd 122) fail to pack, which breaks the shop/inventory
("buying doesn't go into the backpack"). Run it with the server STOPPED.

Usage (from the pack's server\ folder):
    ..\python\python.exe repair_profiles.py ..\save\local-lobby.sqlite3
"""
import json
import sqlite3
import sys

# Every int/byte field in profile-schema.json (everything that is not a string).
NUMERIC = {
    "USN", "Rank", "SPGage", "Op", "SP", "TP", "armoryIdx", "AllPlayTime", "Exp",
    "LadderPoint", "Kills", "Assists", "Deaths", "AttackRound", "AttackRoundWin",
    "DefendRound", "DefendRoundWin", "RunAwayCount", "AllHitCount", "HeadShots",
    "HeadHitCount", "CurrCount", "AllHitCharacter", "AllPlayGameCount",
    "AllWinGameCount", "NametagImage", "CSN", "ClanLevel", "CSNToRequestClanJoin",
    "Age", "Sex", "Coin", "CoinGauge", "CoinState", "CP",
    "TagID[0]", "TagID[1]", "TagID[2]", "LuckyPoint", "Cash",
}


def coerce(value):
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0


def main(db_path):
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
        for key in list(pr.keys()):
            # isinstance(bool, int) is True, which is fine for struct.pack.
            if key in NUMERIC and not isinstance(pr[key], int):
                pr[key] = coerce(pr[key])
                changed = True
        if changed:
            con.execute("UPDATE accounts SET profile=? WHERE usn=?",
                        (json.dumps(pr, ensure_ascii=False), row["usn"]))
            fixed += 1
    con.commit()
    con.close()
    print(f"checked {len(rows)} account(s); repaired {fixed}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(r"usage: python repair_profiles.py <path to local-lobby.sqlite3>")
        sys.exit(1)
    main(sys.argv[1])
