"""Standalone training and mini-game transactions, separate from room matches."""
import datetime
import json
import logging
from wire import response


class PracticeError(ValueError):
    pass


class Practice:
    def __init__(self, lobby):
        self.lobby = lobby
        self.db = lobby.db
        self.active = {}
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS practice_scores(
            usn INTEGER NOT NULL, map INTEGER NOT NULL, mode INTEGER NOT NULL,
            score INTEGER NOT NULL, playing_time INTEGER NOT NULL, recorded TEXT NOT NULL,
            PRIMARY KEY(usn,map,mode));
          CREATE TABLE IF NOT EXISTS firing_range_scores(
            usn INTEGER NOT NULL, weapon_type TEXT NOT NULL, score INTEGER NOT NULL,
            initial TEXT NOT NULL DEFAULT '', PRIMARY KEY(usn,weapon_type));
        ''')

    def handle(self, command, reader, usn, profile, encrypted):
        if command not in (145, 146, 147, 168, 169, 170, 180):
            return None
        if command == 180 and usn not in self.active:
            return None
        if command == 180:
            number = self.lobby.membership(usn)
            row = self.db.execute('SELECT data FROM rooms WHERE number=?', (number,)).fetchone()
            if row and json.loads(row['data']).get('playing'):
                # A finished tutorial may return directly to the lobby without
                # 180. Never let that leftover practice intercept a room exit.
                self.active.pop(usn, None)
                return None
        def reply(fields=(), result=1):
            return response(command, fields, result, encrypted)
        if command == 145:
            reader.require_end()
            rows = self.db.execute('SELECT * FROM practice_scores WHERE usn=? ORDER BY mode,map', (usn,)).fetchall()
            return [reply([('short', len(rows))], 2)] + [reply([
                ('short', row['map']), ('short', row['mode']), ('int', row['playing_time']),
                ('int', row['score']), ('string', row['recorded'])]) for row in rows]
        if command == 146:
            map_id, mode_id = reader.get('short'), reader.get('short')
            reader.require_end()
            if mode_id not in (11, 17, 18) or map_id not in self.lobby.modes[mode_id]:
                raise PracticeError('unsupported training map/mode pair')
            number = self.lobby.membership(usn)
            if number is not None:
                room = self.db.execute('SELECT data FROM rooms WHERE number=?', (number,)).fetchone()
                if room and json.loads(room['data']).get('playing'):
                    raise PracticeError('leave the active room match before entering training')
            previous = self.active.get(usn)
            # Tutorial ClientReset sends 146 again after the lobby has started
            # travel. A duplicate start must not erase the active run.
            if not previous or previous['pair'] != (map_id, mode_id) or previous['submitted']:
                self.active[usn] = {'pair': (map_id, mode_id), 'submitted': False, 'reward': (0, 0)}
            logging.info('local practice started usn=%d map=%d mode=%d', usn, map_id, mode_id)
            return [reply()]
        if command == 147:
            pair = (reader.get('short'), reader.get('short'))
            score, exp, playing_time = [reader.get('int') for _ in range(3)]
            reader.require_end()
            run = self.active.get(usn)
            if not run or run['pair'] != pair or min(score, exp, playing_time) < 0:
                raise PracticeError('practice score does not match an active local run')
            if not run['submitted']:
                recorded = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
                with self.db:
                    self.db.execute('''INSERT INTO practice_scores VALUES(?,?,?,?,?,?)
                        ON CONFLICT(usn,map,mode) DO UPDATE SET score=excluded.score,
                        playing_time=excluded.playing_time,recorded=excluded.recorded
                        WHERE excluded.score > practice_scores.score''',
                        (usn, *pair, score, playing_time, recorded))
                    profile = self.lobby.profile(usn)
                    profile['Exp'] = min(2147483647, profile['Exp'] + exp)
                    profile['AllPlayTime'] = min(2147483647, profile['AllPlayTime'] + playing_time)
                    self.lobby.inventory.save_profile(usn, profile)
                run.update(submitted=True, reward=(score, exp))
                logging.info('local practice settled usn=%d map=%d mode=%d score=%d exp=%d', usn, *pair, score, exp)
            return [reply([('int', value) for value in run['reward']])]
        if command == 180:
            reader.require_end()
            self.active.pop(usn, None)
            # Standalone training has no room and must not send a room-ended
            # notification. The native exit ACK still reads nine durabilities.
            durability = [value for armory in range(1, 4) for value in self.lobby.battle.durability(usn, armory)]
            return [reply([('int', value) for value in durability])]
        if command == 169:
            reader.require_end()
            rows = self.db.execute('SELECT * FROM firing_range_scores WHERE usn=? ORDER BY weapon_type', (usn,)).fetchall()
            return [reply([('short', len(rows))], 2)] + [reply([
                ('string', row['weapon_type']), ('int', 1 if row['score'] else 0),
                ('int', row['score']), ('string', row['initial'])]) for row in rows]
        weapon_type = reader.get('string')
        if weapon_type not in ('1', '2', '3'):
            raise PracticeError('unknown firing range weapon category')
        if command == 168:
            score = reader.get('int'); reader.require_end()
            if score < 0 or self.active.get(usn, {}).get('pair') != (177, 11):
                raise PracticeError('firing range score requires the firing range practice mode')
            with self.db:
                self.db.execute('''INSERT INTO firing_range_scores(usn,weapon_type,score) VALUES(?,?,?)
                    ON CONFLICT(usn,weapon_type) DO UPDATE SET score=MAX(score,excluded.score)''',
                    (usn, weapon_type, score))
            return [reply()]
        initial = reader.get('string'); reader.require_end()
        if not 1 <= len(initial) <= 8 or any(ord(char) < 32 for char in initial):
            raise PracticeError('invalid firing range initials')
        with self.db:
            self.db.execute('''INSERT INTO firing_range_scores(usn,weapon_type,score,initial) VALUES(?,?,0,?)
                ON CONFLICT(usn,weapon_type) DO UPDATE SET initial=excluded.initial''', (usn, weapon_type, initial))
        return [reply([('int', profile['SP'])])]
