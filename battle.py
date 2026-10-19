"""Recovered listen-server battle packets for the PH 205267 client."""
import datetime
import json
import logging
import secrets
from wire import Writer, response


class BattleError(ValueError):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# Field order is the serialized order, not the UnrealScript argument order.
PLAYER_FIELDS = (
    [('string', 'force_code')] + [('int', name) for name in (
        'play_time', 'kills_a', 'assists_a', 'deaths_a', 'kills_b', 'assists_b',
        'deaths_b', 'kills_c', 'assists_c', 'deaths_c', 'head_shots', 'chest_hits',
        'alive_count', 'mission_1', 'mission_2', 'mission_3', 'attack_rounds',
        'defense_rounds', 'total_wins', 'total_losses')] +
    [('short', 'mode'), ('short', 'map')] + [('int', name) for name in (
        'clan', 'player_count', 'earn_score', 'lucky_point', 'attack_wins',
        'defense_wins', 'enemy_hits', 'end_state', 'head_hits')] +
    [('string', 'weapon_code'), ('int', 'total_hits'), ('int', 'stomach_hits'),
     ('int', 'limb_hits'), ('byte', 'winner'), ('int', 'deaths_by_ai')])
WEAPON_FIELDS = (
    [('string', 'weapon_set'), ('string', 'weapon_code')] +
    [('int', name) for name in (
        'playTime', 'earnScore', 'kills', 'assists', 'deaths', 'headShots',
        'headHits', 'chestHits', 'hitEnemy', 'aimKills', 'totalHits', 'stomachHits',
        'limbHits')] + [('short', 'map'), ('int', 'set_uses'), ('short', 'mode')] +
    [('int', name) for name in (
        'headPointHits', 'secondary_kills', 'special_kills', 'grenade_kills')])
GAME_FIELDS = (
    [('string', 'rule'), ('string', 'winner'), ('int', 'play_time'),
     ('short', 'mode'), ('short', 'map')] +
    [('byte', name) for name in (
        'rounds', 'attack_wins_a', 'defense_wins_a', 'attack_wins_b', 'defense_wins_b')] +
    [('int', name) for name in (
        'clan_points_a', 'clan_points_b', 'clan_a', 'clan_b', 'winner_clan', 'target_score')] +
    [('byte', 'difficulty'), ('int', 'player_count')])
HERO_PLAYER_FIELDS = (
    [('string','force_code'),('int','play_time'),('short','mode'),('short','map'),
     ('int','earn_score'),('byte','winner')] + [('int',name) for name in (
        'kills','deaths','assists','default_weapon_kills','weapon_kills','special_kills',
        'max_kill_streak','monster_kills','monster_weak_kills','dead_time','mode_money',
        'destroyed_turrets','destroyed_towers','damage_given','damage_received',
        'back_attacks_given','back_attacks_received','attack_points','defense_points',
        'move_points','mode_level','mode_exp','healing_spent','stats_spent','summons_spent',
        'weapons_spent','other_spent','neutral_monster_kills','tower_damage','lucky_point')])
HERO_GAME_FIELDS = GAME_FIELDS[:15] + [('int',team+'_'+name) for team in ('red','blue') for name in (
    'total_level','total_kills','tower_1_health','tower_2_health','alive_towers','mode_money','desertions','intrusions')]


def read_fields(reader, fields):
    return {name: reader.get(kind) for kind, name in fields}


def now():
    return datetime.datetime.now().isoformat(timespec='seconds')


class Battle:
    def __init__(self, lobby):
        self.lobby = lobby
        self.db = lobby.db
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS battle_matches(
            game_key TEXT PRIMARY KEY, room INTEGER NOT NULL, owner INTEGER NOT NULL,
            mode INTEGER NOT NULL, map INTEGER NOT NULL, started TEXT NOT NULL,
            ended TEXT, status TEXT NOT NULL, red_score INTEGER NOT NULL DEFAULT 0,
            blue_score INTEGER NOT NULL DEFAULT 0, game_log TEXT);
          CREATE TABLE IF NOT EXISTS battle_players(
            game_key TEXT NOT NULL, usn INTEGER NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY(game_key,usn));
          CREATE TABLE IF NOT EXISTS battle_weapons(
            game_key TEXT NOT NULL, usn INTEGER NOT NULL, weapon_code TEXT NOT NULL,
            data TEXT NOT NULL, PRIMARY KEY(game_key,usn,weapon_code));
          CREATE TABLE IF NOT EXISTS instant_respawns(
            game_key TEXT NOT NULL, usn INTEGER NOT NULL, sequence INTEGER NOT NULL,
            cost INTEGER NOT NULL, sp_after INTEGER NOT NULL, created TEXT NOT NULL,
            PRIMARY KEY(game_key,usn,sequence));
        ''')
        with self.db:
            self.db.execute("UPDATE battle_matches SET status='interrupted',ended=? WHERE status='playing'", (now(),))

    def room(self, usn, owner=False):
        number = self.lobby.membership(usn)
        row = self.db.execute('SELECT owner,data FROM rooms WHERE number=?', (number,)).fetchone()
        if not row or (owner and row['owner'] != usn):
            raise BattleError('player must be in the room' + (' as owner' if owner else ''))
        return number, json.loads(row['data'])

    def save_room(self, number, room):
        self.db.execute('UPDATE rooms SET data=? WHERE number=?', (json.dumps(room), number))

    def match(self, room):
        row = self.db.execute('SELECT * FROM battle_matches WHERE game_key=?', (room.get('game_key'),)).fetchone()
        if not row:
            raise BattleError('there is no started local match')
        return row

    def check_log(self, room, data):
        self.match(room)
        if data['mode'] != room['mode'] or data['map'] != room['map']:
            raise BattleError('battle log does not match this room')

    def durability(self, usn, armory):
        row = self.db.execute('SELECT slots FROM weapon_sets WHERE usn=? AND armory=?', (usn, armory)).fetchone()
        slots = json.loads(row['slots'])[:3] if row else [0, 0, 0]
        result = []
        for serial in slots:
            item = self.db.execute('SELECT durability FROM inventory WHERE serial=? AND usn=?', (serial, usn)).fetchone()
            result.append(item['durability'] if item else 0)
        return result

    def end(self, number, room, status):
        room['playing'] = False
        self.save_room(number, room)
        self.db.execute("UPDATE battle_matches SET status=?,ended=? WHERE game_key=? AND status='playing'",
                        (status, now(), room.get('game_key')))

    def settle(self, usn, room, data, increments):
        """One transaction/participant key shared by ordinary and Hero logs."""
        with self.db:
            inserted=self.db.execute('INSERT OR IGNORE INTO battle_players VALUES(?,?,?)',
                (room['game_key'],usn,json.dumps(data))).rowcount
            profile=self.lobby.profile(usn)
            if inserted:
                for name,value in increments.items():
                    profile[name]=min(2147483647,profile.get(name,0)+max(0,value))
                if data.get('weapon_code'):profile['LongTimeMainWeaponItemCode']=data['weapon_code']
                self.lobby.inventory.save_profile(usn,profile)
        return bool(inserted),profile

    def handle(self, command, reader, usn, profile, encrypted):
        if command not in (114, 115, 149, 150, 162, 180, 181, 182, 187, 189, 260, 261, 286, 287, 432):
            return None
        def reply(fields=(), result=1):
            return response(command, fields, result, encrypted)

        if command == 432:
            key, sequence = reader.get('string'), reader.get('int'); reader.require_end()
            success = False
            number = self.lobby.membership(usn)
            row = self.db.execute('SELECT data FROM rooms WHERE number=?', (number,)).fetchone()
            room = json.loads(row['data']) if row else {}
            match = self.db.execute('SELECT status FROM battle_matches WHERE game_key=?', (key,)).fetchone()
            if (room.get('playing') and room.get('game_key') == key and match and
                    match['status'] == 'playing' and sequence > 0):
                with self.db:
                    # A repeated request/ACK must never pay for another revive.
                    paid = self.db.execute('SELECT 1 FROM instant_respawns WHERE game_key=? AND usn=? AND sequence=?',
                                           (key, usn, sequence)).fetchone()
                    last = self.db.execute('SELECT COALESCE(MAX(sequence),0) FROM instant_respawns WHERE game_key=? AND usn=?',
                                           (key, usn)).fetchone()[0]
                    if paid:
                        success = True
                    elif sequence == last+1 and profile['SP'] >= 1:
                        profile['SP'] -= 1
                        self.lobby.inventory.save_profile(usn, profile)
                        self.db.execute('INSERT INTO instant_respawns VALUES(?,?,?,?,?,?)',
                                        (key, usn, sequence, 1, profile['SP'], now()))
                        success = True
            logging.info('local instant respawn usn=%d room=%s sequence=%d accepted=%s SP=%d',
                         usn, number, sequence, success, profile['SP'])
            return [reply([('int', profile['SP']), ('string', key), ('int', sequence), ('int', int(success))])]

        if command == 182:
            number = reader.get('int'); reader.require_end()
            row = self.db.execute('SELECT data FROM rooms WHERE number=?', (number,)).fetchone()
            if not row:
                raise BattleError('room does not exist')
            room = json.loads(row['data'])
            return [reply([('byte', int(not room.get('playing', False))),
                           ('int', room.get('red_score', 0)), ('int', room.get('blue_score', 0))])]

        number, room = self.room(usn, owner=command in (114, 115, 181, 260, 286))
        if command == 114:
            reader.require_end()
            relaxed=self.lobby.relaxed_ai_settings.get('enabled',False) or (room['mode']==33 and self.lobby.hero_mode_settings.get('enabled'))
            mode_maps=self.lobby.modes.get(room['mode'])
            if mode_maps is None and 70 <= room['mode'] <= 79:
                mode_maps={}
            if (room['mode'] not in (109, 110, 0, 60) and not relaxed) or mode_maps is None:
                raise BattleError('choose a locally supported AI-capable team mode', 20)
            if mode_maps and room['map'] not in mode_maps:
                logging.warning('local map fallback mode=%s map=%s -> 13', room['mode'], room['map'])
                room['map']=13
            if room.get('playing'):
                raise BattleError('this local match has already started', 13)
            if room['mode'] in (0,60,110) or relaxed:
                if not relaxed and room.get('custom',0) not in (0,1):
                    raise BattleError('custom weapon rules need separate bot equipment support', 20)
                self.lobby.configure_local_bots(room,usn,number)
            if room['red_ai'] + room['blue_ai'] == 0:
                raise BattleError('the local match requires at least one bot', 12)
            room.update(playing=True, game_key=secrets.token_hex(16), red_score=0, blue_score=0)
            room.pop('validation', None)
            with self.db:
                self.save_room(number, room)
                self.db.execute('INSERT INTO battle_matches(game_key,room,owner,mode,map,started,status) VALUES(?,?,?,?,?,?,?)',
                                (room['game_key'], number, usn, room['mode'], room['map'], now(), 'playing'))
            event = Writer()
            for kind, value in [('int', usn), ('short', room['map']), ('string', '127.0.0.1'), ('int', 28082),
                                ('byte', 0), ('string', ''), ('int', 0), ('int', number), ('string', room['game_key']),
                                ('int', 0), ('byte', 1)]:
                event.add(kind, value)
            logging.info('local match started room=%d key=%s map=%d', number, room['game_key'], room['map'])
            return [reply(), event.frame(509, encrypted)]
        if command == 181:
            red, blue = reader.get('int'), reader.get('int'); reader.require_end()
            self.match(room)
            if not room.get('playing') or min(red, blue) < 0:
                raise BattleError('score update requires an active match and nonnegative scores')
            room.update(red_score=red, blue_score=blue)
            with self.db:
                self.save_room(number, room)
                self.db.execute('UPDATE battle_matches SET red_score=?,blue_score=? WHERE game_key=?',
                                (red, blue, room['game_key']))
            logging.info('local match score room=%d red=%d blue=%d', number, red, blue)
            return [reply()]
        if command in (115, 180):
            reader.require_end()
            self.match(room)
            with self.db:
                self.end(number, room, 'ended' if command == 115 else 'aborted')
            event = Writer().add('int', number).frame(510, encrypted)
            if command == 115:
                # SFPlayerController's 115 OK triggers UserGameDataToDatabase.
                return [reply(), event]
            self.lobby.member_status[usn] = 0
            durability = [value for armory in range(1, 4) for value in self.durability(usn, armory)]
            return [event, reply([('int', value) for value in durability])]
        if command == 189:
            reader.require_end(); self.match(room)
            return [reply()]
        if command == 187:
            data = [reader.get('int') for _ in range(8)]; reader.require_end()
            self.match(room)
            room['validation'] = data
            with self.db:
                self.save_room(number, room)
            return [reply()]
        if command == 162:
            data = read_fields(reader, WEAPON_FIELDS); reader.require_end()
            self.check_log(room, data)
            if data['weapon_set'] not in ('A', 'B', 'C') or data['playTime'] < 0:
                raise BattleError('invalid local weapon log')
            # These are cumulative per-match counters. Duplicate sets with the
            # same primary weapon send zero counters; never erase the real log.
            previous = self.db.execute('SELECT data FROM battle_weapons WHERE game_key=? AND usn=? AND weapon_code=?',
                                       (room['game_key'], usn, data['weapon_code'])).fetchone()
            if previous:
                old = json.loads(previous['data'])
                for kind, name in WEAPON_FIELDS:
                    if kind == 'int': data[name] = max(data[name], old[name])
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO battle_weapons VALUES(?,?,?,?)',
                                (room['game_key'], usn, data['weapon_code'], json.dumps(data)))
            durability = self.durability(usn, 'ABC'.index(data['weapon_set']) + 1)
            return [reply([('string', data['weapon_set'])] + [('int', value) for value in durability])]
        if command == 260:
            data=read_fields(reader,HERO_GAME_FIELDS);reader.require_end()
            self.check_log(room,data)
            if data['mode']!=33 or data['play_time']<0:
                raise BattleError('invalid Hero match log')
            with self.db:
                self.db.execute('UPDATE battle_matches SET game_log=? WHERE game_key=?',(json.dumps(data),room['game_key']))
            return [reply()]
        if command == 261:
            data=read_fields(reader,HERO_PLAYER_FIELDS);reader.require_end()
            self.check_log(room,data)
            if data['mode']!=33 or data['winner'] not in (0,1) or any(
                data[name]<0 for kind,name in HERO_PLAYER_FIELDS if kind=='int' and name!='lucky_point'):
                raise BattleError('invalid Hero player log')
            match=self.match(room)
            completed=int(match['status']=='ended')
            inserted,profile=self.settle(usn,room,data,{
                'AllPlayTime':data['play_time'],'Exp':data['earn_score'],
                'Kills':data['kills'],'Deaths':data['deaths'],'Assists':data['assists'],
                'AllPlayGameCount':completed,'AllWinGameCount':completed*data['winner'],
                'RunAwayCount':1-completed})
            logging.info('local Hero settlement room=%d usn=%d new=%s play_time=%d earned_exp=%d',
                number,usn,inserted,data['play_time'],data['earn_score'])
            # Native header has seven balances. No operator-only Hero coin
            # claim is offered; in-match money remains a separate native wallet.
            fields=[('short',0)]+[('int',profile.get(name,0)) for name in
                ('SPGage','SP','Coin','CoinGauge','CoinState','MiniGachaCoin')]+[('int',0)]
            return [reply(fields,2)]
        if command in (149,286):
            data = read_fields(reader, GAME_FIELDS[:15] if command==149 else GAME_FIELDS)
            teams = []
            while reader.offset < len(reader.payload):
                teams.append({name: reader.get('int') for name in ('kills', 'assists', 'deaths', 'ai_kills', 'ai_assists', 'ai_deaths')})
            reader.require_end()
            if not self.lobby.relaxed_ai_settings.get('enabled') and len(teams) != 2:
                raise BattleError('AI Team Deathmatch must log two teams')
            if len(teams)>9:raise BattleError('invalid local match team log')
            self.check_log(room, data); data['teams'] = teams
            with self.db:
                self.db.execute('UPDATE battle_matches SET game_log=? WHERE game_key=?', (json.dumps(data), room['game_key']))
            return [reply()]
        if command in (150,287):
            fields=PLAYER_FIELDS[:-1]+[('int','game_score')] if command==150 else PLAYER_FIELDS
            data = read_fields(reader, fields); reader.require_end()
            self.check_log(room, data)
            if data['play_time'] < 0 or data['earn_score'] < 0 or data['winner'] not in (0, 1):
                raise BattleError('invalid local player log')
            with self.db:
                inserted = self.db.execute('INSERT OR IGNORE INTO battle_players VALUES(?,?,?)',
                                          (room['game_key'], usn, json.dumps(data))).rowcount
                if inserted:
                    profile = self.lobby.profile(usn)
                    increments = {
                        'AllPlayTime': data['play_time'], 'Exp': data['earn_score'],
                        'Kills': sum(data['kills_' + suffix] for suffix in 'abc'),
                        'Assists': sum(data['assists_' + suffix] for suffix in 'abc'),
                        'Deaths': sum(data['deaths_' + suffix] for suffix in 'abc'),
                        'HeadShots': data['head_shots'], 'HeadHitCount': data['head_hits'],
                        'AllHitCount': data['total_hits'], 'AllHitCharacter': data['enemy_hits'],
                        'AttackRound': data['attack_rounds'], 'DefendRound': data['defense_rounds'],
                        'AttackRoundWin': data['attack_wins'], 'DefendRoundWin': data['defense_wins'],
                        'AllPlayGameCount': int(data['end_state'] == 0),
                        'AllWinGameCount': int(data['end_state'] == 0) * data['winner'],
                        'RunAwayCount': int(data['end_state'] != 0)}
                    for name, value in increments.items():
                        profile[name] = min(2147483647, profile.get(name, 0) + max(0, value))
                    if data['weapon_code']: profile['LongTimeMainWeaponItemCode'] = data['weapon_code']
                    self.lobby.inventory.save_profile(usn, profile)
            profile = self.lobby.profile(usn)
            logging.info('local settlement room=%d usn=%d new=%s play_time=%d earned_exp=%d',
                         number, usn, bool(inserted), data['play_time'], data['earn_score'])
            # Result 2 is the transaction header. Result 1 is only used for each
            # reward item, whose full six-field schema is different. Zero rewards
            # still require the header and all six actual balance fields.
            fields = [('short', 0)] + [('int', profile.get(name, 0)) for name in
                                      ('SPGage', 'SP', 'Coin', 'CoinGauge', 'CoinState', 'MiniGachaCoin')]
            return [reply(fields, 2)]

