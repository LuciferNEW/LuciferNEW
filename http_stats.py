"""Local replacement for the synchronous legacy weapon statistics endpoint."""
import asyncio
import logging
import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

# SFUIDataStore_PlayerInfo.RecvWeaponLogInfo reads code followed by these 14
# values. The native parser at 0xDA9910 flattens list/pojo child text in order.
FIELDS=('playTime','earnScore','kills','assists','deaths','headShots','headHits',
        'chestHits','hitEnemy','aimKills','totalHits','stomachHits','limbHits','headPointHits')

def weapon_statistics(lobby,usn):
    if lobby.db.execute('SELECT 1 FROM accounts WHERE usn=?',(usn,)).fetchone() is None:
        return None
    codes=set()
    for row in lobby.db.execute('SELECT item_id FROM inventory WHERE usn=? AND used=1',(usn,)):
        item=lobby.catalog.get(row['item_id'])
        if item and item['code'].startswith('RA'):codes.add(item['code'])
    statistics={}
    for row in lobby.db.execute('SELECT weapon_code,data FROM battle_weapons WHERE usn=?',(usn,)):
        values=json.loads(row['data'])
        totals=statistics.setdefault(row['weapon_code'],{field:0 for field in FIELDS})
        for field in FIELDS:totals[field]+=max(0,values.get(field,0))
    document=ET.Element('list')
    for code in sorted(codes):
        entry=ET.SubElement(document,'pojo')
        ET.SubElement(entry,'weaponCode').text=code
        for field in FIELDS:ET.SubElement(entry,field).text=str(statistics.get(code,{}).get(field,0))
    return ET.tostring(document,encoding='utf-8',xml_declaration=True)

async def serve_stats(reader,writer,lobby):
    started=time.perf_counter();status=400;route='invalid'
    try:
        header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
        if len(header)>8192:raise ValueError('headers too large')
        method,target,version=header.split(b'\r\n',1)[0].decode('ascii').split(' ')
        path=urlsplit(target).path
        # ClanServerBaseURL defaults to info/rest in this exact PMS client.
        match=re.fullmatch(r'(?:/info/rest)?/getArmsPlayList/(\d{1,12})/?',path)
        body=b'<list />'
        if method!='GET':status=405
        elif match:
            route='getArmsPlayList';body=weapon_statistics(lobby,int(match[1]))
            status=200 if body is not None else 404
            if body is None:body=b'<list />'
        else:status=404
        reason={200:'OK',400:'Bad Request',404:'Not Found',405:'Method Not Allowed'}[status]
        writer.write((f'HTTP/1.1 {status} {reason}\r\nContent-Type: text/xml; charset=utf-8\r\n'
                      f'Content-Length: {len(body)}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n').encode('ascii')+body)
        await writer.drain()
    except (ValueError,UnicodeError,asyncio.TimeoutError,asyncio.IncompleteReadError,asyncio.LimitOverrunError,ConnectionError):
        pass
    finally:
        logging.info('local HTTP route=%s status=%d elapsed_ms=%.3f',route,status,(time.perf_counter()-started)*1000)
        writer.close()
        try:await writer.wait_closed()
        except ConnectionError:pass
