"""Loopback-only SF2 PH lobby with persistent profiles, shop and equipment."""
from pathlib import Path
import argparse
import asyncio
import datetime
import hashlib
import json
import logging
import os
import sqlite3
import struct
from wire import Reader, Writer, ProtocolError, make_frame, response, decrypt_payload, MAX_PAYLOAD
from inventory import Inventory, InventoryError, STARTING_CASH, STARTING_SP
from http_stats import serve_stats
from battle import Battle, BattleError
from client_modes import load_modes, load_other_ai_settings,load_relaxed_ai_settings,load_native_large_ai_settings,load_hero_mode_settings
from practice import Practice, PracticeError
from economy import Economy

BASE = Path(__file__).resolve().parent
BUILD_FILES = ('server.py', 'wire.py', 'inventory.py', 'battle.py', 'http_stats.py', 'practice.py', 'economy.py', 'economy-data.json',
               'client_modes.py', 'client-modes.json', 'catalog.json', 'profile-schema.json','relaxed-mode-definitions.json')
HOLD_RESPONSES = set()
SCHEMA = json.loads((BASE / 'profile-schema.json').read_text(encoding='utf-8'))
CREATE_TYPES = ['string','byte','short','short','byte','byte','byte','string','byte','byte','byte',
                'byte','byte','byte','short','byte','byte','byte','byte','byte','byte','byte','byte']
CREATE_NAMES = ['name','capacity','mode','map','intrude','difficulty','locked','password','switch_side',
                'rounds','score','killcam','third_person','balance','time','vip','red_players','blue_players',
                'red_ai','blue_ai','last_stand','custom','pickup']
SETTING_NAMES=['mode','map','rounds','switch_side','intrude','name','difficulty','password','capacity',
               'killcam','score','third_person','balance','number','time','red_players','blue_players',
               'red_ai','blue_ai','last_stand','custom','pickup']
SETTING_TYPES=['short','short','byte','byte','byte','string','byte','string','byte','byte','byte','byte',
               'byte','int','short','byte','byte','byte','byte','byte','byte','byte']

class BusinessError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code

def values(*fields):
    return list(fields)

class Lobby:
    def __init__(self, database, catalog):
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS accounts(usn INTEGER PRIMARY KEY, login TEXT UNIQUE NOT NULL, profile TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS inventory(serial INTEGER PRIMARY KEY AUTOINCREMENT, usn INTEGER NOT NULL, item_id INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS rooms(number INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS members(usn INTEGER PRIMARY KEY, room INTEGER NOT NULL);
        ''')
        # Room state is transient; profile, balances and inventory survive restarts.
        with self.db:
            self.db.execute('DELETE FROM members')
            self.db.execute('DELETE FROM rooms')
        self.catalog = {int(item['id']):item for item in catalog}
        if any(not isinstance(item.get('price'),int) or item['price']<0 for item in self.catalog.values()):
            raise ValueError('catalog prices must be nonnegative integers')
        self.inventory = Inventory(self.db,self.catalog)
        self.member_status={}
        self.member_slots={}
        self.channel_members={}
        # PATCH(multiplayer): remember which live connection belongs to which
        # usn so the lobby can PUSH room updates to players who are already in
        # a room when someone else joins/leaves/changes slot. Maps usn ->
        # (writer, encrypted). Populated by serve_client after each request.
        self.connections={}
        # PATCH(reconnect): usn -> scheduled "leave the room" timer handle.
        # When a socket drops we defer the room-leave by a grace period so a
        # brief XIGNCODE/VPN blip (or a reconnect racing the old socket's
        # teardown) does not hand the host's room away and lock them out.
        self.pending_leave={}
        # Which layout the 262 (synthesis) reply uses; switchable in-game with
        # the chat command "!resp 1|2|3" so the success popup can be probed
        # without restarting the server. 3 mirrors the 196 package-open reply,
        # the only reward layout this client is known to render as a window.
        self.craft_response_variant=3
        self.modes=load_modes()
        self.other_ai_settings=load_other_ai_settings()
        self.native_large_ai_settings=load_native_large_ai_settings()
        self.hero_mode_settings=load_hero_mode_settings()
        self.relaxed_ai_settings=load_relaxed_ai_settings()
        self.battle=Battle(self)
        self.practice=Practice(self)
        self.economy=Economy(self)

    def login(self, name):
        if not name or len(name)>64 or any(ord(c)<32 for c in name):
            raise BusinessError('invalid local account name')
        existing = self.db.execute('SELECT usn FROM accounts WHERE login=?',(name,)).fetchone()
        if existing:
            self.inventory.ensure_account(existing['usn'])
            return existing['usn']
        usn = self.db.execute('SELECT COALESCE(MAX(usn),100000)+1 FROM accounts').fetchone()[0]
        profile = {f['name']:('' if f['type']=='string' else 0) for f in SCHEMA}
        profile.update(USN=usn,CodeName=name,Rank=1,SP=STARTING_SP,TP=99999,Cash=STARTING_CASH,Age=20)
        with self.db:self.db.execute('INSERT INTO accounts VALUES(?,?,?)',(usn,name,json.dumps(profile)))
        self.inventory.ensure_account(usn)
        return usn

    def profile(self, usn):
        row = self.db.execute('SELECT profile FROM accounts WHERE usn=?',(usn,)).fetchone()
        if not row:raise BusinessError('unknown local player')
        return json.loads(row['profile'])

    def membership(self, usn):
        row=self.db.execute('SELECT room FROM members WHERE usn=?',(usn,)).fetchone()
        return row['room'] if row else None

    def configure_local_bots(self, room, usn, number=None):
        if room['mode']==33 and self.hero_mode_settings.get('enabled'):
            from local_hero_mode import counts
            room['capacity']=max(2,min(room['capacity'],10))//2*2
            red,blue=counts(room['capacity'],self.member_slots.get(usn,0)%2==1)
            room.update(red_ai=red,blue_ai=blue,red_players=room['capacity']//2,
                        blue_players=room['capacity']-room['capacity']//2)
            return
        if room['mode']==110 and self.native_large_ai_settings.get('enabled'):
            size=self.native_large_ai_settings['team_size']
            own_blue=self.member_slots.get(usn,0)%2==1
            room.update(capacity=size*2,red_players=size,blue_players=size,
                        red_ai=size-int(not own_blue),blue_ai=size-int(own_blue))
            return
        if self.relaxed_ai_settings.get('enabled') and room['mode'] not in (0,60,109,110):
            from local_relaxed_modes import RULES
            rule=RULES.get(room['mode'])
            if rule is None:return
            cfg=self.relaxed_ai_settings;family=rule['family']
            if cfg.get('count_from_room_capacity', True):
                from local_relaxed_modes import room_bot_counts
                red,blue=room_bot_counts(room['mode'],room['capacity'],self.member_slots.get(usn,0)%2==1)
            elif family=='ffa':red,blue=cfg['ffa_bots'],0
            elif family=='friendly':red,blue=cfg['friendly_bots'],0
            elif family=='duel':
                own_blue=self.member_slots.get(usn,0)%2==1
                red,blue=int(own_blue),int(not own_blue)
            else:
                own_blue=self.member_slots.get(usn,0)%2==1
                red,blue=cfg['team_size']-int(not own_blue),cfg['team_size']-int(own_blue)
            room.update(red_ai=red,blue_ai=blue,difficulty=cfg['difficulty'])
            return
        if room['mode'] not in (0,60) or not self.other_ai_settings.get('enabled'):
            return
        size=self.other_ai_settings['large_team_size' if room['mode']==60 else 'team_size']
        # This local adapter hosts one real player and fills the remaining slots
        # with native AI; it does not invent peers or change account ownership.
        if number is not None:
            count=self.db.execute('SELECT COUNT(*) FROM members WHERE room=?',(number,)).fetchone()[0]
            # PATCH(multiplayer): more than one real human in the room -> this is
            # a genuine PvP room, not a solo-vs-AI room. Do not raise and do not
            # fill the empty slots with bots; let the humans take the slots.
            if count>1:
                room.update(red_ai=0,blue_ai=0)
                return
        half=max(1,room['capacity']//2)
        # Ordinary balanced-room slots alternate red/blue. This mirrors the
        # native GetModeTeamIndex(SlotIndex) modulo-two path, not screen columns.
        blue=self.member_slots.get(usn,0)%2==1
        if self.other_ai_settings.get('count_from_room_capacity',True):
            from local_relaxed_modes import room_bot_counts
            red_ai,blue_ai=room_bot_counts(room['mode'],room['capacity'],blue)
        else:red_ai,blue_ai=size-int(not blue),size-int(blue)
        room.update(red_ai=red_ai,blue_ai=blue_ai,
                    difficulty=self.other_ai_settings['difficulty'],
                    red_players=half,blue_players=room['capacity']-half)

    def join(self, usn, number, password):
        row=self.db.execute('SELECT data FROM rooms WHERE number=?',(number,)).fetchone()
        if not row:raise BusinessError('room does not exist')
        room=json.loads(row['data'])
        if room['locked'] and password!=room['password']:raise BusinessError('wrong local room password')
        count=self.db.execute('SELECT COUNT(*) FROM members WHERE room=?',(number,)).fetchone()[0]
        if self.membership(usn)!=number and count>=room['capacity']:raise BusinessError('room is full')
        with self.db:self.db.execute('INSERT OR REPLACE INTO members VALUES(?,?)',(usn,number))
        return room

    def leave(self, usn):
        self.member_status.pop(usn,None)
        self.member_slots.pop(usn,None)
        number=self.membership(usn)
        if number is None:return
        with self.db:
            self.db.execute('DELETE FROM members WHERE usn=?',(usn,))
            other=self.db.execute('SELECT usn FROM members WHERE room=? ORDER BY usn',(number,)).fetchone()
            if other:self.db.execute('UPDATE rooms SET owner=? WHERE number=?',(other['usn'],number))
            else:self.db.execute('DELETE FROM rooms WHERE number=?',(number,))
        # PATCH(multiplayer): if anyone is still in the room, refresh their list
        # so the leaver disappears and the new owner is reflected.
        if other:self.broadcast_roster(number,exclude=usn)

    def roster_packets(self, number, encrypted):
        # PATCH(multiplayer): build the protocol-109 room member list for a
        # given room. Factored out of the 109 handler so the same bytes can be
        # pushed unsolicited to players already in the room.
        row=self.db.execute('SELECT owner FROM rooms WHERE number=?',(number,)).fetchone()
        if row is None:return []
        members=self.db.execute('SELECT usn FROM members WHERE room=? ORDER BY usn',(number,)).fetchall()
        def reply(fields=(),result=1):return response(109,fields,result,encrypted)
        packets=[reply(values(('short',len(members)),('int',number)),2)]
        for slot,member in enumerate(members):
            info=self.profile(member['usn'])
            slot=self.member_slots.setdefault(member['usn'],slot)
            fields=values(('int',info['USN']),('string',info['CodeName']),('byte',slot),
                ('byte',int(row['owner']==member['usn'])),('byte',self.member_status.get(member['usn'],0)),('int',info['Exp']),
                ('int',info['LadderPoint']),('int',info['Kills']),('int',info['Deaths']),('int',0),
                ('int',info['NametagImage']),('string',info['NametagShowType']),('string',info['NametagText']),
                ('string','20261013000000'),('int',info['HeadShots']),('int',info['AllWinGameCount']),
                ('int',info['AllPlayGameCount']),('string',info['LongTimeMainWeaponItemCode']),
                ('int',info['CSN']),('string',info['ClanName']),('int',0),('string',info['UnitItemCode']),
                ('string','0'),('byte',info['Sex']),('int',info['RunAwayCount']),('int',info['AllPlayTime']),
                ('int',info['Rank']),('int',0),('byte',0),('string',''),('byte',0),('int',number),
                ('string',info['ClanMark']),('byte',0),('byte',0),('string',''),('string',''),
                ('string',''),('int',info['Assists']),('int',0),('byte',0))
            packets.append(reply(fields))
        return packets

    def broadcast_roster(self, number, exclude=None):
        # PATCH(multiplayer): push the current room roster (protocol 109) to
        # every member who has a live connection, so a host sees a newcomer the
        # instant they join instead of being stuck on a stale list. Best effort:
        # a slow or dead peer must never take the server down, so every write is
        # guarded. If the client ignores an unsolicited 109 this is simply a
        # no-op; it cannot crash the client.
        members=[r['usn'] for r in self.db.execute('SELECT usn FROM members WHERE room=?',(number,)).fetchall()]
        for member in members:
            if member==exclude:continue
            entry=self.connections.get(member)
            if not entry:continue
            writer,enc=entry
            try:
                for packet in self.roster_packets(number,enc):writer.write(packet)
            except Exception:
                logging.exception('roster push failed for usn=%s',member)

    def cancel_pending_leave(self, usn):
        # PATCH(reconnect): a live connection for this player is back, so drop
        # any grace-period removal scheduled when an earlier socket closed.
        handle=self.pending_leave.pop(usn,None)
        if handle is not None:handle.cancel()

    def schedule_leave(self, usn, delay=30):
        # PATCH(reconnect): do not remove a player from their room the instant
        # a socket closes. Wait a short grace period; if they are back by then
        # (same account reconnects) keep them and their room ownership. Only a
        # genuine, lasting disconnect migrates the room to whoever remains.
        self.cancel_pending_leave(usn)
        try:
            loop=asyncio.get_running_loop()
        except RuntimeError:
            self.leave(usn);return
        self.pending_leave[usn]=loop.call_later(delay,self._grace_leave,usn)

    def _grace_leave(self, usn):
        self.pending_leave.pop(usn,None)
        # Reconnected in time -> a live connection exists, so keep the player.
        if usn in self.connections:return
        try:self.leave(usn)
        except Exception:logging.exception('grace leave failed for usn=%s',usn)

    def handle(self, command, reader, usn, encrypted):
        def reply(fields=(), result=1):return response(command,fields,result,encrypted)
        if command==101:
            # PH login order comes directly from SFOnlineSubsystemPCPMS.ServerToSendLogin.
            name=reader.get('string')
            for kind in ['string','string','string','int','int','int','int','string','string']:
                reader.get(kind)  # Do not persist password, ticket, IP or MAC fields.
            reader.require_end()
            usn=self.login(name)
            now=datetime.datetime.now().strftime('%Y%m%d%H%M%S')
            fields=values(('int',usn),('int',3),('byte',1),('string',now),('string',f'{usn:010d}'),
                          ('byte',0),('string','127.0.0.1'),('int',0),('string',name))
            return [reply(fields)],usn
        if usn is None:raise BusinessError('login required')
        profile=self.profile(usn)
        economy_packets=self.economy.handle(command,reader,usn,profile,encrypted)
        if economy_packets is not None:return economy_packets,usn
        inventory_packets=self.inventory.handle(command,reader,usn,profile,encrypted)
        if inventory_packets is not None:return inventory_packets,usn
        practice_packets=self.practice.handle(command,reader,usn,profile,encrypted)
        if practice_packets is not None:return practice_packets,usn
        battle_packets=self.battle.handle(command,reader,usn,profile,encrypted)
        if battle_packets is not None:
            if command==114:self.practice.active.pop(usn,None)
            return battle_packets,usn
        if command==156:
            # End-of-match challenge updates expect a reward transaction header,
            # even when no local challenge reward service is offered.
            count=reader.get('int')
            if not 0<=count<=MAX_PAYLOAD//11:raise ProtocolError('invalid challenge update count')
            for _ in range(count):
                reader.get('string');reader.get('int');reader.get('int')
            reader.require_end()
            return [reply([('short',0)],2)],usn
        if command==195:
            # Client performance report. Consume the actual serialized schema;
            # acknowledge locally without storing or forwarding diagnostics.
            kinds=(['string','byte','string']+['float']*3+['int']*9+
                   ['string','float','int','int','float','float']+['string']*6+
                   ['int','int','short','short'])
            for kind in kinds:reader.get(kind)
            reader.require_end()
            return [reply()],usn
        if command==118:return [reply()],usn
        if command==105:
            scope=reader.get('byte');reader.get('int');message=reader.get('string');reader.get('int');reader.require_end()
            if len(message)>100:raise BusinessError('chat message too long')
            raw=message.strip()
            command_word=raw.split()[0] if raw else ''
            if command_word in ('!抽奖','!抽獎','抽奖','抽獎'):
                count=1
                parts=raw.split()
                if len(parts)>1 and parts[1].isdigit():count=max(1,min(50,int(parts[1])))
                names=self.economy.chat_draw(usn,count)
                shown='、'.join(names[:6])+(' 等 %d 件'%len(names) if len(names)>6 else '')
                message=f'抽獎獲得：{shown}'
                scope=0;name='系統'
            elif command_word in ('!resp','!制作弹窗','!弹窗'):
                parts=raw.split()
                if len(parts)>1 and parts[1].isdigit():
                    self.craft_response_variant=max(1,min(5,int(parts[1])))
                message=f'craft response variant = {self.craft_response_variant}'
                scope=0;name='系統'
            else:
                name=profile['CodeName']
            # Single-player local echo; no external messenger or publisher service.
            event=Writer()
            for kind,value in [('byte',scope),('string',name),('string',message),
                               ('byte',0),('byte',0),('string','')]:event.add(kind,value)
            return [event.frame(500,encrypted),reply()],usn
        if command==122:
            return [reply([(field['type'],profile[field['name']]) for field in SCHEMA])],usn
        if command==158:
            reader.require_end()
            # PMS groups recognize 90 (Beginner) and 10 (Open). Type 0 is
            # absent from SFChannelType and disappears on the first tab filter.
            rows=[(90,1),(10,1)]
            return [reply([('short',len(rows))],2)]+[reply(values(
                ('int',1),('int',number),('int',kind),
                ('int',sum(joined==(kind,number) for joined in self.channel_members.values())),
                ('int',100),('byte',1),('int',0))) for kind,number in rows],usn
        if command==159:
            kind=reader.get('int');number=reader.get('int');reader.get('byte');reader.get('int');reader.require_end()
            if (kind,number) not in ((90,1),(10,1)):
                raise BusinessError('local channel does not exist',3)
            self.channel_members[usn]=(kind,number)
            return [reply()],usn
        if command==161:return [reply(values(('string','127.0.0.1'),('int',28080),('string','LOCAL')))],usn
        if command==160:
            reader.require_end();self.channel_members.pop(usn,None)
            return [reply()],usn
        if command==197:
            # The stock OK handlers request protocol 109 and read no fields.
            reader.require_end()
            number=self.membership(usn)
            row=self.db.execute('SELECT owner,data FROM rooms WHERE number=?',(number,)).fetchone()
            if row is None or row['owner']!=usn:raise BusinessError('only the room owner can arrange teams')
            room=json.loads(row['data'])
            if room.get('playing'):raise BusinessError('team arrangement requires a waiting room')
            users=[r['usn'] for r in self.db.execute('SELECT usn FROM members WHERE room=? ORDER BY usn',(number,))]
            for index,member in enumerate(users):self.member_slots.setdefault(member,index)
            # A solo local room has no human imbalance to correct. The native
            # game fills its own AI teams from the stored red/blue counts.
            # Keep the real slot and do not fabricate additional lobby users.
            return [reply()],usn
        if command in (113,111):
            value=reader.get('byte');number=reader.get('int');reader.require_end()
            if self.membership(usn)!=number:raise BusinessError('player is not in this room')
            if command==113:
                self.member_status[usn]=value
                # PATCH(multiplayer): push ready-state change to other members.
                self.broadcast_roster(number,exclude=usn)
                return [reply([('byte',value),('int',number)])],usn
            if value>=32 or any(k!=usn and self.membership(k)==number and v==value for k,v in self.member_slots.items()):
                raise BusinessError('room slot is unavailable')
            old=self.member_slots.get(usn,0);self.member_slots[usn]=value
            owner=self.db.execute('SELECT owner FROM rooms WHERE number=?',(number,)).fetchone()[0]
            # PATCH(multiplayer): push slot change to other members.
            self.broadcast_roster(number,exclude=usn)
            return [reply([('int',usn),('string',profile['CodeName']),('byte',old),('byte',value),
                           ('byte',int(owner==usn)),('int',number)])],usn
        if command==116:
            fields=[(kind,reader.get(kind)) for kind in SETTING_TYPES];reader.require_end()
            setting=dict(zip(SETTING_NAMES,(value for _,value in fields)));number=setting.pop('number')
            row=self.db.execute('SELECT owner,data FROM rooms WHERE number=?',(number,)).fetchone()
            if row is None or row['owner']!=usn:raise BusinessError('only the owner can change room settings')
            if not 1<=setting['capacity']<=32 or len(setting['name'])>128:raise BusinessError('invalid room settings')
            room=json.loads(row['data']);room.update(setting);room['locked']=int(bool(room['password']))
            self.configure_local_bots(room,usn,number)
            fields=[(kind,room[name] if name!='number' else number) for kind,name in zip(SETTING_TYPES,SETTING_NAMES)]
            with self.db:self.db.execute('UPDATE rooms SET data=? WHERE number=?',(json.dumps(room),number))
            return [reply(fields)],usn
        if command==108:
            room=dict(zip(CREATE_NAMES,(reader.get(kind) for kind in CREATE_TYPES)))
            reader.require_end()
            if not 1<=room['capacity']<=32 or len(room['name'])>128:raise BusinessError('invalid room settings')
            self.configure_local_bots(room,usn)
            self.leave(usn)
            with self.db:
                number=self.db.execute('INSERT INTO rooms(owner,data) VALUES(?,?)',(usn,json.dumps(room))).lastrowid
                self.db.execute('INSERT INTO members VALUES(?,?)',(usn,number))
            return [reply([('int',number)])],usn
        if command==110:
            rows=self.db.execute('SELECT * FROM rooms ORDER BY number').fetchall()
            packets=[reply([('short',len(rows))],2)]
            for row in rows:
                room=json.loads(row['data'])
                count=self.db.execute('SELECT COUNT(*) FROM members WHERE room=?',(row['number'],)).fetchone()[0]
                fields=values(('byte',room['locked']),('int',row['number']),('string',room['name']),
                    ('short',room['mode']),('short',room['map']),('byte',count),('byte',room['capacity']),
                    ('byte',0),('byte',room['intrude']),('byte',0),('byte',room['rounds']),('byte',0),
                    ('byte',room['difficulty']),('short',room['time']),('byte',room['vip']),
                    ('byte',room['custom']),('byte',room['pickup']),('byte',0))
                packets.append(reply(fields))
            return packets,usn
        if command==106:
            requested_usn,number,password=reader.get('int'),reader.get('int'),reader.get('string')
            reader.require_end()
            if requested_usn!=usn:raise BusinessError('player identity mismatch')
            room=self.join(usn,number,password)
            # PATCH(multiplayer): tell everyone already in the room that someone
            # joined, so the host's member list refreshes instead of going stale.
            self.broadcast_roster(number,exclude=usn)
            owner=self.db.execute('SELECT owner FROM rooms WHERE number=?',(number,)).fetchone()[0]
            fields=values(('int',number),('string',room['name']),('short',room['mode']),('short',room['map']),
                ('byte',room['rounds']),('byte',room['switch_side']),('byte',room['capacity']),
                ('byte',room['intrude']),('byte',room['difficulty']),('int',owner),('byte',0),
                ('string',room['password']),('byte',room['score']),('byte',room['killcam']),
                ('byte',room['third_person']),('byte',room['balance']),('byte',0),('int',0),
                ('short',room['time']),('byte',room['red_players']),('byte',room['blue_players']),
                ('byte',room['red_ai']),('byte',room['blue_ai']),('byte',room['last_stand']),
                ('byte',room['custom']),('byte',room['pickup']))
            return [reply(fields)],usn
        if command==107:
            number=reader.get('int');reader.require_end()
            if self.membership(usn)!=number:raise BusinessError('player is not in this room')
            self.leave(usn)
            return [reply([('int',number)])],usn
        if command==109:
            number=self.membership(usn)
            if number is None:raise BusinessError('player is not in a room')
            return self.roster_packets(number,encrypted),usn
        if command==278:
            # A nonempty table completion triggers SetItemSellingTag in this client.
            # Keep the static SP=0 / TP=1 prices; identify a few local featured items.
            featured=[1010010, 1010020, 1010030, 1010040, 1010050, 1010070, 1010078, 1020010, 1020020, 1020030, 1020040, 1020042, 1020050, 1030010, 1030020, 1030027, 1030030, 1030040, 1030048, 1030050, 1030060, 1030070, 1030072, 1030080, 1030090, 1030100, 1030102, 1030110, 1030120, 1040010, 1040020, 1040030, 1040040, 1040041, 1050010, 1050012, 1050020, 1050030, 1050040, 1050050, 1050060, 1050070, 1060010, 1060020, 1060030, 1060037, 1060040, 1060050, 1070010, 1080008, 1110010, 1120010, 1130010, 1410010, 1500015, 1500016, 1500017, 1500018, 1500515, 1500516, 1500517, 1500518, 1501015, 1501016, 1501017, 1501018, 1502015, 1502016, 1502017, 1502018, 1502515, 1502516, 1502517, 1502518, 1503025, 1503026, 1503027, 1503028, 1510013, 1510014, 1510021, 1510023, 1510024, 1510025, 1510030, 1510032, 1510033, 1510034, 1510036, 1510037, 1510038, 1510040, 1510041, 1510042, 1510044, 1510045, 1510046, 1510048, 1510049, 1510050, 1510052, 1510053, 1510054, 1510056, 1510057, 1510058, 1510060, 1510061, 1510062, 1510063, 1510064, 1510065, 1510066, 1510067, 1510068, 1510069, 1510513, 1510514, 1510521, 1510523, 1510524, 1510525, 1510531, 1510533, 1510534, 1510535, 1510537, 1510538, 1510539, 1510541, 1510542, 1510543, 1510545, 1510546, 1510547, 1510549, 1510550, 1510551, 1510553, 1510554, 1510555, 1510557, 1510558, 1510559, 1510561, 1510562, 1510563, 1510564, 1510565, 1510566, 1510567, 1510568, 1510569, 1510570, 1511011, 1511013, 1511014, 1511015, 1511017, 1511018, 1511019, 1511021, 1511022, 1511023, 1511025, 1511026, 1511027, 1511029, 1511030, 1511031, 1511033, 1511034, 1511035, 1511037, 1511038, 1511039, 1511041, 1511042, 1511045, 1511046, 1511047, 1511048, 1511049, 1511050, 1511051, 1511052, 1511053, 1511056, 1511057, 1512011, 1512013, 1512014, 1512015, 1512021, 1512023, 1512024, 1512025, 1512027, 1512028, 1512029, 1512031, 1512032, 1512033, 1512035, 1512036, 1512037, 1512039, 1512040, 1512041, 1512043, 1512044, 1512045, 1512047, 1512048, 1512049, 1512051, 1512052, 1512053, 1512054, 1512055, 1512056, 1512057, 1512058, 1512059, 1512060, 1512063, 1512064, 1512509, 1512511, 1512513, 1512514, 1512515, 1512517, 1512518, 1512519, 1512521, 1512522, 1512523, 1512525, 1512526, 1512527, 1512529, 1512530, 1512531, 1512533, 1512534, 1512535, 1512537, 1512538, 1512539, 1512541, 1512542, 1512543, 1512544, 1512545, 1512546, 1512547, 1512548, 1512549, 1512550, 1512553, 1512554, 1513021, 1513023, 1513024, 1513025, 1513031, 1513033, 1513034, 1513035, 1513037, 1513038, 1513039, 1513041, 1513042, 1513043, 1513045, 1513046, 1513047, 1513049, 1513050, 1513051, 1513053, 1513054, 1513055, 1513057, 1513058, 1513059, 1513061, 1513062, 1513063, 1513064, 1513065, 1513066, 1513067, 1513068, 1513069, 1513070, 1513071, 1513073, 1513074, 1513075, 1513077, 1513078, 1513079, 1513081, 1513082, 1513083, 1513085, 1513086, 1513087, 1513088, 1513089, 1513090, 1513093, 1513094, 1519010, 1519011, 1520011, 1520013, 1520014, 1520015, 1520017, 1520018, 1520019, 1520020, 1520021, 1520023, 1520024, 1520025, 1520027, 1520028, 1520031, 1520033, 1520034, 1520035, 1520037, 1520038, 1520041, 1520043, 1520044, 1520045, 1520047, 1520048, 1520049, 1520050, 1520051, 1520053, 1520054, 1520055, 1520057, 1520058, 1520059, 1520061, 1520062, 1520063, 1520064, 1520079, 1520081, 1520082, 1520083, 1520085, 1520086, 1520087, 1520089, 1520090, 1520091, 1520093, 1520094, 1520095, 1520097, 1520098, 1520100, 1520102, 1520103, 1520104, 1520106, 1520107, 1520108, 1520110, 1520111, 1520112, 1520114, 1520115, 1520116, 1520117, 1520118, 1520119, 1520120, 1520121, 1520122, 1520123, 1520124, 1520125, 1520511, 1520513, 1520514, 1520515, 1520541, 1520543, 1520544, 1520545, 1521015, 1521016, 1521017, 1521018, 1521115, 1522011, 1522013, 1522014, 1522015, 1522515, 1522525, 1530009, 1530010, 1530011, 1530013, 1530014, 1530015, 1530024, 1530025, 1530031, 1530033, 1530034, 1530035, 1530044, 1530045, 1530051, 1530053, 1530054, 1530521, 1530523, 1530524, 1531015, 1531025, 1531031, 1531033, 1531034, 1532011, 1532013, 1532014, 1532015, 1532024, 1532025, 1532031, 1532033, 1532034, 1532515, 1532525, 1532531, 1532533, 1532534, 1533021, 1533023, 1533024, 1533028, 1533029, 1533031, 1533032, 1533033, 1533034, 1533035, 1533041, 1533043, 1533044, 1550031, 1550033, 1550034, 1550035, 1550061, 1550063, 1550064, 1550065, 1550531, 1550533, 1550534, 1550535, 1550561, 1550563, 1550564, 1550565, 1551015, 1551025, 1552011, 1552013, 1552014, 1552015, 1552021, 1552023, 1552024, 1552025, 1552515, 1552525, 1553021, 1553023, 1553024, 1553025, 1553031, 1553033, 1553034, 1553035, 1560021, 1560023, 1560024, 1560025, 1560031, 1560033, 1560034, 1560035, 1560041, 1560043, 1560044, 1560045, 1560051, 1560053, 1560054, 1560055, 1561015, 1561025, 1562011, 1562013, 1562014, 1562015, 1562021, 1562023, 1562024, 1562025, 1562515, 1562525, 1563021, 1563023, 1563024, 1563025, 1563027, 1563028, 1580011, 1580013, 1580014, 1580015, 1580021, 1580023, 1580024, 1580025, 1580031, 1580033, 1580034, 1580035, 1580041, 1580043, 1580044, 1580045, 1580046, 1580047, 1581015, 1582515, 1590021, 1590023, 1590024, 1590025, 1590031, 1590033, 1590034, 1590035, 1590521, 1590523, 1590524, 1590525, 1590531, 1590533, 1590534, 1590535, 1591015, 1591025, 1592011, 1592013, 1592014, 1592015, 1592021, 1592023, 1592024, 1592025, 1592515, 1592525, 1593021, 1593023, 1593024, 1593025, 1593031, 1593033, 1593034, 1593035, 1600021, 1600023, 1600024, 1600025, 1600031, 1600033, 1600034, 1600035, 1600051, 1600053, 1600054, 1600055, 1600061, 1600063, 1600064, 1600065, 1601015, 1601025, 1602515, 1603021, 1603023, 1603024, 1603025, 1603027, 1603028, 1605011, 1605013, 1605014, 1605021, 1605023, 1605024, 1605025, 1605031, 1605033, 1605034, 1605035, 1605041, 1605043, 1605044, 1605045, 1605051, 1605053, 1605054, 1605055, 1605061, 1605063, 1605064, 1605065, 1609010, 1609011, 1609017, 1610011, 1610013, 1610014, 1610021, 1610023, 1610024, 1610025, 1610511, 1610513, 1610514, 1611025, 1612011, 1612013, 1612014, 1612015, 1613021, 1613023, 1613024, 1613025, 1613027, 1613028, 1613036, 1613037, 1613039, 1620091, 1620093, 1620094, 1620095, 1620101, 1620103, 1620104, 1620105, 1620511, 1620513, 1620514, 1620515, 1622011, 1622013, 1622014, 1622015, 1622515, 1630011, 1630013, 1630014, 1630015, 1730010, 1730020, 1730030, 1730050, 1730060, 1730080, 1900010, 1900013, 2000028, 2000055, 2000057, 2000061, 2000063, 2000065, 2000067, 2000069, 2000071, 2000073, 2000075, 2000077, 2000079, 2000081, 2000083, 2000088, 2000090, 2000092, 2000094, 2000096, 2000098, 2000100, 2000102, 2000106, 2000108, 2000112, 2000114, 2000116, 2000118, 2000120, 2000121, 2000122, 2000123, 2000124, 2000126, 2000127, 2000128, 2000129, 2000130, 2000131, 2000132, 2000133, 2000134, 2000135, 2000136, 2000137, 2000138, 2000139, 2000140, 2000141, 2000142, 2000143, 2000144, 2000145, 2000146, 2000147, 2000148, 2000149, 2000150, 2000153, 2000154, 2000155, 2000156, 2000158, 2000159, 2000160, 2000161, 2000162, 2000163, 2000164, 2000165, 2000166, 2000167, 2000168, 2000169, 2000170, 2000171, 2000172, 2000173, 2000174, 2000175, 2000176, 2000177, 2000178, 2000179, 2000180, 2000181, 2000182, 2010103, 2010104, 2010118, 2010119, 2010120, 2010203, 2010204, 2020026, 2020027, 2020028, 2030045, 2030046, 2030047, 2030070, 2090002, 2090003, 2090006, 2090007, 2090012, 2090013, 2090018, 2090019, 2090020, 2090035, 2090042, 2090043, 2110010, 2110020, 2110030, 2110040, 2110070, 2110072, 2110073, 2110075, 2110078, 2110079, 2110080, 2110086, 2110087, 2110088, 2110091, 2110094, 2110097, 2110109, 2110111, 2110112, 2110119, 2110121, 2110122, 2110125, 2110126, 2110129, 2110130, 2110146, 2110163, 2110164, 2110167, 2110168, 2110171, 2110172, 2110177, 2110180, 2110181, 2110182, 2110192, 2110193, 2110218, 2111173, 2220004, 2220012, 2220016, 2220058, 2220094, 2220098, 2220109, 2220115, 2220133, 2220137, 2220143, 2220156, 2220159, 2220164, 2220173]
            # Capsule coupons are intentionally omitted: opening the TWN
            # Gashapon screen hangs on the unrecovered protocol 211, so the
            # shop no longer exposes a button that leads into that dead end.
            # Lottery is done through the chat command instead.
            # 278 is also PKT_GetItemTableInfo: the client builds its item
            # mapping from this list. Sending only the featured set left
            # weapons such as the Steampunk SVCh without an ItemClass, which
            # is why the craft detail panel showed all zeros.
            items=sorted(self.catalog)
            return [reply([('short',len(items))],2)]+[reply([
                ('int',item_id),('string',self.catalog[item_id].get('sell_type') or 'S'),
                ('int',self.catalog[item_id].get('price',0)),('string','NEW'),('int',0),
                ('string',''),('string',''),('string','O')]) for item_id in items],usn
        if command==194:return [reply([('int',profile.get('Cash',0))])],usn
        if command==401:return [reply([('int',profile['TP'])])],usn
        if command==138:return [reply([('int',profile['SP'])])],usn
        if command==257:return [reply([('int',profile['LuckyPoint'])])],usn
        if command==222:return [reply([('int',0),('int',0)])],usn
        if command==267:return [reply(values(('string',''),('int',0),('int',0),('byte',0),('float',0.0),('string','20991231235959')))],usn
        if command==416:return [reply([('int',0)])],usn
        if command==201:return [reply([('int',0)]*23)],usn
        if command==205:return [reply([('int',1),('int',-1)])],usn
        if command==148:
            profile['KeySet']=reader.get('string');reader.require_end()
            with self.db:self.inventory.save_profile(usn,profile)
            return [reply()],usn
        if command==258:return [reply([('int',0)]*4)],usn
        if command in [136,155,199,234,249,251,252,268,297,269,270,412,243,246,266,276,281,294,223,224,407,409,165,166,259,247,414,408,248]:
            return [reply([('short',0)],2)],usn
        # GetInt(2) consumes the next scalar; its argument is not a field offset.
        # A date string here is misread as an enormous array length by the client.
        if command==417:return [reply([('int',0)])],usn
        if command in [119,154,200,202,204,239,244,245,271,279,280,283,287,288,289,290,291,292,293,299,400,402,405]:
            if command in (290,291,292,293,405):
                logging.info('craft cmd=%s payload=%s',command,reader.payload.hex())
            return [reply()],usn
        if command==102:
            self.leave(usn)
            self.channel_members.pop(usn,None);self.practice.active.pop(usn,None)
            return [reply()],None
        logging.info('unsupported cmd=%s payload=%s',command,reader.payload.hex())
        raise BusinessError(f'unsupported prototype protocol {command}')

async def serve_client(reader, writer, lobby, mode):
    usn=None; observed_encryption=False
    def encrypted_response():return mode=='always' or (mode=='auto' and observed_encryption)
    logging.info('connection accepted peer=%s mode=%s',writer.get_extra_info('peername'),mode)
    try:
        # The script handles command 2000 by sending the login request.
        writer.write(make_frame(2000));await writer.drain()
        while True:
            header=await asyncio.wait_for(reader.readexactly(4),timeout=180)
            size,command=struct.unpack('<HH',header)
            if not 4<=size<=MAX_PAYLOAD+4:raise ProtocolError('invalid frame size')
            payload=await reader.readexactly(size-4)
            if payload and len(payload)%16==0:
                try:payload=decrypt_payload(payload);observed_encryption=True
                except ProtocolError:pass
            logging.info('request command=%d bytes=%d local_usn=%s',command,size,usn)
            if command in HOLD_RESPONSES:
                logging.info('diagnostic hold protocol=%d',command)
                continue
            try:
                previous_usn=usn
                packets,usn=lobby.handle(command,Reader(payload),usn,encrypted_response())
                # PATCH(multiplayer): keep the usn -> connection map current so
                # the lobby can push room updates to this player.
                if usn is not None:
                    lobby.connections[usn]=(writer,encrypted_response())
                    lobby.cancel_pending_leave(usn)
                elif previous_usn is not None:
                    lobby.connections.pop(previous_usn,None)
            except (BusinessError,InventoryError,BattleError,PracticeError) as error:
                logging.info('request rejected: %s',error)
                packets=[response(command,[('byte',getattr(error,'code',1))],0,encrypted_response())]
            except Exception:
                # Keep the local session alive so a server-side bug is visible
                # in the log instead of silently dropping the client to login.
                logging.exception('unhandled request protocol=%d local_usn=%s',command,usn)
                packets=[response(command,[('byte',1)],0,encrypted_response())]
            logging.info('response protocol=%d frames=%d encrypted=%s',command,len(packets),encrypted_response())
            for packet in packets:writer.write(packet)
            await writer.drain()
    except (asyncio.IncompleteReadError,asyncio.TimeoutError,ConnectionError):pass
    except (ProtocolError,UnicodeDecodeError,struct.error) as error:logging.info('invalid packet: %s',error)
    finally:
        if usn is not None:
            # PATCH(reconnect): only clear the map entry if it still points at
            # THIS socket -- a newer reconnect may already own it, and clobbering
            # that would strand the live connection. Then DEFER the room-leave
            # so a quick reconnect keeps the room instead of losing it.
            if lobby.connections.get(usn) and lobby.connections[usn][0] is writer:
                lobby.connections.pop(usn,None)
            lobby.channel_members.pop(usn,None);lobby.practice.active.pop(usn,None)
            lobby.schedule_leave(usn)
        logging.info('connection closed local_usn=%s',usn)
        writer.close()
        try:await writer.wait_closed()
        except ConnectionError:pass

async def run(args):
    registry=json.loads((BASE/'client-modes.json').read_text('utf-8'))
    route_files={entry['route_file'] for mode in registry['modes'].values() for entry in mode['maps'] if entry.get('local_route_assistance')}
    build_files={name:hashlib.sha256((BASE/name).read_bytes()).hexdigest() for name in (*BUILD_FILES,'../local_routes.py','../local_relaxed_modes.py','../local_hero_mode.py',*sorted(route_files))}
    catalog=json.loads(Path(args.catalog).read_text(encoding='utf-8'))
    lobby=Lobby(args.database,catalog)
    server=await asyncio.start_server(lambda r,w:serve_client(r,w,lobby,args.response_encryption),args.host,args.port)
    http=None
    try:
        if args.http_port is not None:
            http=await asyncio.start_server(lambda r,w:serve_stats(r,w,lobby),args.host,args.http_port,limit=8192)
    except Exception:
        server.close();await server.wait_closed();lobby.db.close();raise
    http_info=f'; HTTP 127.0.0.1:{http.sockets[0].getsockname()[1]}' if http else ''
    if args.port == 28080 and Path(args.database).resolve() == (BASE/'local-lobby.sqlite3').resolve():
        (BASE.parent/'evidence/local-server-runtime.json').write_text(json.dumps(
            {'pid':os.getpid(), 'files':build_files, 'started':datetime.datetime.now().isoformat(timespec='seconds')},
            indent=2),encoding='utf-8')
    print(f'LISTENING {args.host}:{server.sockets[0].getsockname()[1]}{http_info}; local lobby ready; shop 1 TP, capsule packs 1 TP per capsule',flush=True)
    if args.ready_file:
        ready=Path(args.ready_file);temporary=ready.with_suffix('.tmp')
        temporary.write_text(json.dumps({'pid':os.getpid(),'port':server.sockets[0].getsockname()[1],
            'http_port':http.sockets[0].getsockname()[1] if http else None}),'utf-8');temporary.replace(ready)
    try:
        async with server:await server.serve_forever()
    finally:
        if http:http.close();await http.wait_closed()
        lobby.db.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=28080)
    parser.add_argument('--http-port',type=int,help='Enable local weapon statistics HTTP on loopback')
    parser.add_argument('--database',default=str(BASE/'local-lobby.sqlite3'))
    parser.add_argument('--catalog',default=str(BASE/'catalog.json'),help='Recovered local item catalog')
    parser.add_argument('--response-encryption',choices=['always','auto','none'],default='none')
    parser.add_argument('--hold-response',type=int,action='append',default=[],help='Diagnostic: withhold a selected response')
    parser.add_argument('--ready-file',help=argparse.SUPPRESS)
    args=parser.parse_args()
    HOLD_RESPONSES.update(args.hold_response)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    try:asyncio.run(run(args))
    except KeyboardInterrupt:pass
