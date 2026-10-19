"""Persistent local loadouts and a single TP ledger for every shop purchase."""
import json
import logging
import sqlite3
from wire import response

INITIAL_TOKENS = 99999
# A brand-new local character starts with full CASH and SP so the capsule shop
# and lottery are immediately usable.  2e9 is inside the client's signed int32
# range while still showing as "full" in the wallet UI.
STARTING_CASH = 2_000_000_000
STARTING_SP = 2_000_000_000
STARTER_IDS = [1730010,1030040,1010030,1110010,1120010,1130010,1080009]
# One A/B/C/D part per recipe; seed a ledger so 製作 is usable without the
# (unsold) material coupons. Disassembly still tops it up.
STARTING_COMBINE_PARTS = (10, 10, 10, 10)
END_DATE = '20991231235959'

class InventoryError(ValueError):pass

class Inventory:
    def __init__(self,db,catalog):
        self.db=db;self.catalog=catalog
        self.special=None
        self.reward_items={}
        columns={x[1] for x in db.execute('PRAGMA table_info(inventory)')}
        for name,definition in [('used','INTEGER NOT NULL DEFAULT 0'),('expires',"TEXT NOT NULL DEFAULT '20991231235959'"),
                                ('durability','INTEGER NOT NULL DEFAULT 100'),('properties',"TEXT NOT NULL DEFAULT ''")]:
            if name not in columns:db.execute(f'ALTER TABLE inventory ADD COLUMN {name} {definition}')
        db.executescript('''
          CREATE TABLE IF NOT EXISTS weapon_sets(usn INTEGER NOT NULL,armory INTEGER NOT NULL,name TEXT NOT NULL,slots TEXT NOT NULL,PRIMARY KEY(usn,armory));
          CREATE TABLE IF NOT EXISTS unit_equipment(usn INTEGER NOT NULL,code TEXT NOT NULL,slots TEXT NOT NULL,PRIMARY KEY(usn,code));
        ''')
        # Direct character UNITs are equipment, not the visible character
        # consumable coupons. Keeping them in the unused-item list makes
        # lottery UNIT rewards inaccessible in this client's inventory UI.
        # Preserve their serials and any existing loadout when upgrading saves.
        with db:
            for row in db.execute('SELECT * FROM inventory WHERE used=0').fetchall():
                if self.is_unit(row['item_id']):self.activate_unit(row['usn'],row['serial'],row['item_id'])
        # The client retains one unused-item handle per item type, including
        # while individual copies are consumed. Handles are session-local and
        # may only resolve to that account's remaining copies of the same item.
        self.unused_handles={(r['usn'],r['serial']):r['item_id'] for r in
                             db.execute('SELECT usn,serial,item_id FROM inventory WHERE used=0')}

    def ensure_account(self,usn):
        profile=json.loads(self.db.execute('SELECT profile FROM accounts WHERE usn=?',(usn,)).fetchone()[0])
        if profile.get('LocalSchema')==2:
            changed=False
            if 'Cash' not in profile:
                profile['Cash']=0;changed=True
            if len(profile.get('CombineParts') or [])!=4:
                # 259 recipes ask for one A/B/C/D part each, but the material
                # coupons are not sold in the local shop, so a save without a
                # parts ledger leaves 製作 permanently disabled (0/1, red X).
                profile['CombineParts']=list(STARTING_COMBINE_PARTS);changed=True
            if changed:
                with self.db:self.save_profile(usn,profile)
            return
        for item_id in STARTER_IDS:
            if item_id not in self.catalog:raise InventoryError(f'Recovered catalog is missing starter {item_id}')
        with self.db:
            profile.update(LocalSchema=2,CodeName='LocalPlayer',TP=INITIAL_TOKENS,SP=STARTING_SP,Cash=STARTING_CASH,Age=20,Sex=0,
                UnitItemCode=self.catalog[STARTER_IDS[0]]['code'],armoryIdx=1,
                CombineParts=list(STARTING_COMBINE_PARTS),
                LongTimeMainWeaponItemCode=self.catalog[STARTER_IDS[1]]['code'])
            self.save_profile(usn,profile)
            serials=[]
            for item_id in STARTER_IDS:serials.append(self.add(usn,item_id,used=True))
            for armory,name in enumerate(['A','B','C'],1):
                slots=[serials[1],serials[2],serials[3],serials[4],serials[5],serials[6],0]
                self.db.execute('INSERT OR REPLACE INTO weapon_sets VALUES(?,?,?,?)',(usn,armory,name,json.dumps(slots)))
            self.db.execute('INSERT OR REPLACE INTO unit_equipment VALUES(?,?,?)',
                (usn,profile['UnitItemCode'],json.dumps([serials[0]]+[0]*15)))

    def save_profile(self,usn,profile):
        self.db.execute('UPDATE accounts SET profile=? WHERE usn=?',(json.dumps(profile),usn))

    def add(self,usn,item_id,used=False):
        if item_id not in self.catalog:raise InventoryError('Unknown item ID')
        unit=self.is_unit(item_id)
        serial=self.db.execute('INSERT INTO inventory(usn,item_id,used) VALUES(?,?,?)',(usn,item_id,int(used or unit))).lastrowid
        if unit:self.activate_unit(usn,serial,item_id)
        elif not used:self.unused_handles[(usn,serial)]=item_id
        return serial

    def is_unit(self,item_id):
        return self.catalog.get(item_id,{}).get('code','')[:3] in ('ECU','PCU','RCU')

    def activate_unit(self,usn,serial,item_id):
        self.db.execute('UPDATE inventory SET used=1 WHERE serial=? AND usn=?',(serial,usn))
        self.db.execute('INSERT OR IGNORE INTO unit_equipment VALUES(?,?,?)',
                        (usn,self.catalog[item_id]['code'],json.dumps([serial]+[0]*15)))

    def configure_reward_items(self,mapping):
        self.reward_items={int(old):new for old,new in mapping.items()}
        if any(not self.is_unit(old) or new not in self.catalog or
               not self.catalog[new].get('is_pmc') or not self.catalog[new]['cooked']
               for old,new in self.reward_items.items()):raise InventoryError('Invalid character reward mapping')
        self.db.execute('''CREATE TABLE IF NOT EXISTS reward_item_repairs(
            serial INTEGER PRIMARY KEY,usn INTEGER NOT NULL,original_id INTEGER NOT NULL,
            replacement_id INTEGER NOT NULL,date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        changed={}
        with self.db:
            for row in self.db.execute('SELECT * FROM inventory ORDER BY serial').fetchall():
                target=self.reward_items.get(row['item_id'])
                if target is None:continue
                self.db.execute('INSERT OR IGNORE INTO reward_item_repairs(serial,usn,original_id,replacement_id) VALUES(?,?,?,?)',
                                (row['serial'],row['usn'],row['item_id'],target))
                self.db.execute('UPDATE inventory SET item_id=? WHERE serial=?',(target,row['serial']))
                changed.setdefault(row['usn'],{})[self.catalog[row['item_id']]['code']]=self.catalog[target]['code']
            for usn,codes in changed.items():
                # Earlier builds created a UNIT equipment row for these hidden
                # model IDs. PMC coupons live on the normal base unit instead.
                owned_codes={self.catalog[r['item_id']]['code'] for r in self.db.execute(
                    'SELECT item_id FROM inventory WHERE usn=? AND used=1',(usn,)) if self.is_unit(r['item_id'])}
                profile=json.loads(self.db.execute('SELECT profile FROM accounts WHERE usn=?',(usn,)).fetchone()[0])
                for code,pmc in codes.items():
                    if code in owned_codes:continue
                    self.db.execute('DELETE FROM unit_equipment WHERE usn=? AND code=?',(usn,code))
                    if profile.get('UnitItemCode')==code:
                        profile['UnitItemCode']='ECU0001' if 'ECU0001' in owned_codes else next(iter(sorted(owned_codes)),'ECU0001')
                        profile['PmcCode']=pmc;self.save_profile(usn,profile)
        self.unused_handles={(r['usn'],r['serial']):r['item_id'] for r in
                             self.db.execute('SELECT usn,serial,item_id FROM inventory WHERE used=0')}

    def add_reward(self,usn,item_id):
        return self.add(usn,self.reward_items.get(item_id,item_id))

    def owned(self,usn,serial,used=None):
        row=self.db.execute('SELECT * FROM inventory WHERE serial=? AND usn=?',(serial,usn)).fetchone()
        if row is None or (used is not None and bool(row['used'])!=used):raise InventoryError('Item is not owned in the required state')
        return row

    def unused(self,usn,handle):
        item_id=self.unused_handles.get((usn,handle))
        if item_id is None:
            row=self.owned(usn,handle,False);item_id=row['item_id']
            self.unused_handles[(usn,handle)]=item_id
        row=self.db.execute('SELECT * FROM inventory WHERE usn=? AND item_id=? AND used=0 ORDER BY serial LIMIT 1',
                            (usn,item_id)).fetchone()
        if row is None:raise InventoryError('No unused copies remain')
        return row

    def release_empty_stack(self,usn,item_id):
        if self.db.execute('SELECT 1 FROM inventory WHERE usn=? AND item_id=? AND used=0 LIMIT 1',(usn,item_id)).fetchone() is None:
            self.unused_handles={k:v for k,v in self.unused_handles.items() if not(k[0]==usn and v==item_id)}

    def validate_slots(self,usn,slots):
        for serial in slots:
            if serial:self.owned(usn,serial,True)

    def new_item_fields(self,row,profile,bought=False):
        fields=[('int',row['item_id']),('string','Y' if row['used'] else 'N'),('long',row['serial']),
                ('string',row['expires']),('int',profile['SP']),('int',profile['TP']),('int',row['durability'])]
        if bought:fields += [('int',1),('int',profile.get('Cash',0)),('int',0),('int',profile['LuckyPoint'])]
        return fields

    def list_packets(self,usn,encrypted):
        """Push 123/124 inventory lists so the client refreshes after a use."""
        packets=[]
        rows=self.db.execute('SELECT item_id,MIN(serial) AS serial,COUNT(*) AS quantity FROM inventory '
                             'WHERE usn=? AND used=0 GROUP BY item_id ORDER BY serial',(usn,)).fetchall()
        packets.append(response(123,[('short',len(rows))],2,encrypted))
        for row in rows:
            packets.append(response(123,[('int',row['item_id']),('int',row['quantity']),
                                         ('long',row['serial'])],1,encrypted))
        rows=self.db.execute('SELECT * FROM inventory WHERE usn=? AND used=1 ORDER BY serial',(usn,)).fetchall()
        packets.append(response(124,[('short',len(rows))],2,encrypted))
        for row in rows:
            packets.append(response(124,[('long',row['serial']),('string',self.catalog[row['item_id']]['code']),
                ('string',row['expires']),('int',row['durability']),('int',100),('string',row['properties']),
                ('int',row['item_id'])],1,encrypted))
        return packets

    def handle(self,command,reader,usn,profile,encrypted):
        def reply(fields=(),result=1):return response(command,fields,result,encrypted)
        if command==405:
            # Weapon disassembly: consume one owned copy of the requested
            # weapon and add crafting parts to the character ledger.
            item_id=reader.get('int')
            reader.offset=len(reader.payload)
            row=self.db.execute('SELECT * FROM inventory WHERE usn=? AND item_id=? ORDER BY used,serial LIMIT 1',
                                (usn,item_id)).fetchone()
            parts=list(profile.get('CombineParts',[0]*4))
            if len(parts)!=4:parts=[0]*4
            if row is None:
                # The client still lists an already-disassembled weapon until
                # its equipment page refreshes. Acknowledge instead of raising
                # so the UI does not pop "Error 405".
                logging.info('local disassemble usn=%s item=%s already consumed',usn,item_id)
                return [reply([('int',n) for n in parts])]
            with self.db:
                self.db.execute('DELETE FROM inventory WHERE serial=? AND usn=?',(row['serial'],usn))
                parts=[min(999,p+10) for p in parts]
                profile['CombineParts']=parts;self.save_profile(usn,profile)
            self.release_empty_stack(usn,item_id)
            logging.info('local disassemble usn=%s item=%s parts=%s',usn,item_id,parts)
            packets=[reply([('int',n) for n in parts]),
                     response(258,[('int',n) for n in parts],1,encrypted)]
            packets+=self.list_packets(usn,encrypted)
            return packets
        if command==124:
            rows=self.db.execute('SELECT * FROM inventory WHERE usn=? AND used=1 ORDER BY serial',(usn,)).fetchall()
            return [reply([('short',len(rows))],2)]+[reply([
                ('long',row['serial']),('string',self.catalog[row['item_id']]['code']),('string',row['expires']),
                ('int',row['durability']),('int',100),('string',row['properties']),('int',row['item_id'])]) for row in rows]
        if command==123:
            # InsertNotUsedItem merges packets by ItemId. Counting physical
            # copies makes CheckRecivedNotUsedItemList wait forever at login.
            rows=self.db.execute('SELECT item_id,MIN(serial) AS serial,COUNT(*) AS quantity FROM inventory '
                                 'WHERE usn=? AND used=0 GROUP BY item_id ORDER BY serial',(usn,)).fetchall()
            for row in rows:self.unused_handles[(usn,row['serial'])]=row['item_id']
            return [reply([('short',len(rows))],2)]+[reply([('int',row['item_id']),('int',row['quantity']),('long',row['serial'])]) for row in rows]
        if command==126:
            rows=self.db.execute('SELECT * FROM weapon_sets WHERE usn=? ORDER BY armory',(usn,)).fetchall()
            return [reply([('short',len(rows))],2)]+[reply([('string',row['name'])]+
                [('long',x) for x in json.loads(row['slots'])]+[('int',row['armory'])]) for row in rows]
        if command==128:
            rows=self.db.execute('SELECT * FROM unit_equipment WHERE usn=? ORDER BY code',(usn,)).fetchall()
            return [reply([('short',len(rows))],2)]+[reply([('string',row['code'])]+
                [('long',x) for x in json.loads(row['slots'])[:13]]) for row in rows]
        if command==127:
            item_id=reader.get('int');payment=reader.get('byte');reader.get('string');reader.get('byte');quoted=reader.get('int')
            reader.require_end()
            if item_id not in self.catalog:
                self.catalog[item_id]={'id':item_id,'code':f'UNK{item_id}','name':f'TWN {item_id}','shop_selling':True}
            price=self.special.purchase_price(item_id) if self.special else 1
            with self.db:
                # The database balance is authoritative, including multiple sessions.
                fresh=json.loads(self.db.execute('SELECT profile FROM accounts WHERE usn=?',(usn,)).fetchone()[0])
                if fresh['TP']<price:raise InventoryError('Insufficient TP')
                fresh['TP']-=price;self.save_profile(usn,fresh)
                serial=self.add(usn,item_id)
                row=self.owned(usn,serial)
                extras=self.special.on_purchase(usn,row,encrypted) if self.special else []
            return [reply(self.new_item_fields(row,fresh,True))]+extras
        if command==130:
            handle=reader.get('long');option=reader.get('string')
            row=self.db.execute('SELECT * FROM inventory WHERE usn=? AND (serial=? OR item_id=?) ORDER BY used,serial',(usn,handle,handle & 0xffffffff)).fetchone()
            if row is None:
                return [reply()],usn
            serial=row['serial']
            effects=self.special.meta.get(row['item_id'],{}) if self.special else {}
            consume=bool(effects.get('add_sp') or effects.get('exchange_tp'))
            with self.db:
                if self.special:self.special.activate(usn,row,option,profile)
                if consume:
                    # Consumables (SP/TP cards, crafting parts) are consumed
                    # instead of being marked equipped; leaving them in the
                    # equipped list broke the client's inventory parsing.
                    self.db.execute('DELETE FROM inventory WHERE serial=?',(serial,))
                else:
                    self.db.execute('UPDATE inventory SET used=1 WHERE serial=?',(serial,))
                item=self.catalog[row['item_id']]
                if item['code'][:3] in ['ECU','PCU','RCU']:
                    self.db.execute('INSERT OR IGNORE INTO unit_equipment VALUES(?,?,?)',
                        (usn,item['code'],json.dumps([serial]+[0]*15)))
            self.release_empty_stack(usn,row['item_id'])
            if consume:
                fresh=self.lobby.profile(usn)
                return [reply()]+self.special.refresh(fresh,encrypted)
            row=self.owned(usn,serial,True)
            packets=[reply([('int',row['item_id']),('long',serial),('string',row['expires']),('int',row['durability'])])]
            # Push 123/124 so the used weapon leaves the warehouse's "not used"
            # list immediately (consumables are deleted and never reach here).
            packets+=self.list_packets(usn,encrypted)
            return packets
        if command in (196,265):
            handle=reader.get('long')
            try:row=self.unused(usn,handle)
            except InventoryError:
                if command==196 and self.special:
                    try:awarded=self.special.use_capsule(usn,handle)
                    except InventoryError:return [reply([('short',0)],2)]
                    packets=[reply([('short',len(awarded))],2)]
                    for item_row in awarded:
                        packets.append(reply([('int',item_row['item_id']),('string','Y' if item_row['used'] else 'N'),
                            ('long',item_row['serial']),('string',END_DATE),('int',100),('int',1)]))
                    return packets
                # Stale/unknown unused handle: acknowledge an empty package
                # instead of surfacing ErrorID 1 in the client UI.
                return [reply([('short',0)],2)]
            serial=row['serial']
            if command==196 and self.special:
                meta=self.special.meta.get(row['item_id'],{})
                if meta.get('capsules'):
                    # Capsule coupon: draw the prizes and report them as the
                    # package contents so the client's "use item" flow works.
                    reader.require_end()
                    awarded=self.special.use_coupon(usn,row)
                    packets=[reply([('short',len(awarded))],2)]
                    for item_row in awarded:
                        packets.append(reply([('int',item_row['item_id']),('string','Y' if item_row['used'] else 'N'),
                            ('long',item_row['serial']),('string',END_DATE),('int',100),('int',1)]))
                    return packets
            contents=self.catalog[row['item_id']].get('contents',[])
            if command==265:
                selected=reader.get('int')
                contents=[item for item in contents if item['id']==selected]
            reader.require_end()
            if not contents:
                cls=str(self.catalog[row['item_id']].get('class',''))
                if command==196 and self.special and cls.startswith('SFCItem_Func_PKG_'):
                    # Client-side lottery package without a contents table
                    # (e.g. 足球王者轉蛋包): open one default pool prize.
                    awarded=self.special.use_package_draw(usn,row)
                    packets=[reply([('short',len(awarded))],2)]
                    for item_row in awarded:
                        packets.append(reply([('int',item_row['item_id']),('string','Y' if item_row['used'] else 'N'),
                            ('long',item_row['serial']),('string',END_DATE),('int',100),('int',1)]))
                    return packets
                # Client-only boxes/capsules have no contents table here;
                # return an empty success so the UI can close normally.
                return [reply([('short',0)],2)]
            if any(item['id'] not in self.catalog or not 1<=item['count']<=1000 for item in contents):
                raise InventoryError('Invalid package contents')
            count=sum(item['count'] for item in contents)
            if count>32767 or (command==265 and count!=1):raise InventoryError('Unsupported package size')
            packets=[reply([('short',count)],2)] if command==196 else []
            with self.db:
                self.db.execute('DELETE FROM inventory WHERE serial=?',(serial,))
                for item in contents:
                    for _ in range(item['count']):
                        created=self.add_reward(usn,item['id'])
                        awarded=self.owned(usn,created)
                        packets.append(reply([('int',awarded['item_id']),('string','Y' if awarded['used'] else 'N'),('long',created),
                            ('string',END_DATE),('int',100),('int',1)]))
            self.release_empty_stack(usn,row['item_id'])
            return packets
        if command in (164,192):
            serials=[reader.get('long') for _ in range(1 if command==164 else 3)];reader.require_end()
            for serial in serials:
                if serial:self.owned(usn,serial,True)
            with self.db:
                for serial in serials:
                    if serial:self.db.execute('UPDATE inventory SET durability=100 WHERE serial=?',(serial,))
            if command==164:return [reply([('int',0),('int',100),('int',profile['SP']),('long',serials[0])])]
            return [reply([('int',profile['SP'])]+[('long',x) for x in serials]+[('int',100 if x else 0) for x in serials])]
        if command==131:
            serial=reader.get('long')
            extended=reader.offset<len(reader.payload)
            count=reader.get('int') if extended else 1
            item_type=reader.get('string') if reader.offset<len(reader.payload) else None
            row=self.unused(usn,serial) if item_type=='N' else self.owned(usn,serial)
            if item_type is None:item_type='U' if row['used'] else 'N'
            reader.require_end()
            if count<1 or item_type not in ('U','N') or (item_type=='U')!=bool(row['used']):
                raise InventoryError('Invalid resale item state')
            rows=[row] if row['used'] else self.db.execute(
                'SELECT * FROM inventory WHERE usn=? AND item_id=? AND used=0 ORDER BY serial LIMIT ?',
                (usn,row['item_id'],count)).fetchall()
            if len(rows)!=count:raise InventoryError('Invalid resale count')
            serials={item['serial'] for item in rows}
            for table in ('weapon_sets','unit_equipment'):
                for equipment in self.db.execute(f'SELECT slots FROM {table} WHERE usn=?',(usn,)):
                    if serials.intersection(json.loads(equipment['slots'])):raise InventoryError('Unequip the item before resale')
            with self.db:self.db.executemany('DELETE FROM inventory WHERE serial=?',[(x,) for x in serials])
            self.release_empty_stack(usn,row['item_id'])
            fields=[('long',serial)]
            if extended:fields+=[('int',count)]
            fields+=[('int',0),('int',profile['SP'])]
            if extended:fields+=[('int',profile['TP']),('string',item_type)]
            return [reply(fields)]
        if command==125:
            armory=reader.get('int');slots=[reader.get('long') for _ in range(7)];reader.require_end()
            self.validate_slots(usn,slots)
            with self.db:
                cursor=self.db.execute('UPDATE weapon_sets SET slots=? WHERE usn=? AND armory=?',(json.dumps(slots),usn,armory))
                if cursor.rowcount!=1:raise InventoryError('Unknown weapon set')
            return [reply()]
        if command==129:
            code=reader.get('string');slots=[reader.get('long') for _ in range(16)];reader.require_end()
            self.validate_slots(usn,slots)
            if not any(x['code']==code for x in self.catalog.values()):raise InventoryError('Unknown unit')
            with self.db:self.db.execute('INSERT OR REPLACE INTO unit_equipment VALUES(?,?,?)',(usn,code,json.dumps(slots)))
            return [reply()]
        if command==140:
            code=reader.get('string');reader.require_end()
            if self.db.execute('SELECT 1 FROM unit_equipment WHERE usn=? AND code=?',(usn,code)).fetchone() is None:
                raise InventoryError('Unit has not been activated')
            profile['UnitItemCode']=code
            with self.db:self.save_profile(usn,profile)
            return [reply()]
        if command==256:
            code=reader.get('string');reader.require_end()
            logging.info('pmc cmd=256 code=%r',code)
            if code:
                activated=self.db.execute('SELECT item_id FROM inventory WHERE usn=? AND used=1',(usn,)).fetchall()
                match=next((row['item_id'] for row in activated
                            if self.catalog[row['item_id']]['code']==code),None)
                coupon=match is not None and str(self.catalog[match].get('class','')).startswith('SFCItem_Func_Provide')
                if match is None or not (self.catalog[match].get('is_pmc') or coupon):
                    logging.warning('pmc cmd=256 rejected code=%r owned=%r',code,
                        [(row['item_id'],self.catalog[row['item_id']]['code'],
                          bool(self.catalog[row['item_id']].get('is_pmc'))) for row in activated])
                    raise InventoryError('PMC character must be owned and activated before equipping')
                if not self.catalog[match].get('is_pmc'):
                    # Some character coupons are only flagged is_pmc on one of
                    # their catalog rows; the copy the shop/pool hands out can
                    # miss the flag (PWF0030 紅蓮: 5090122 flagged, 5778701 not).
                    # A Provide* coupon with a matching owned code is a PMC.
                    logging.warning('pmc cmd=256 unflagged coupon item=%s code=%r class=%r',
                                    match,code,self.catalog[match].get('class'))
            # PMC overlays use their own profile field; empty code unequips them.
            # The success handler commits the pending client selection without reading fields.
            profile['PmcCode']=code
            with self.db:self.save_profile(usn,profile)
            return [reply()]
        if command==141:
            armory=reader.get('int');reader.require_end()
            if self.db.execute('SELECT 1 FROM weapon_sets WHERE usn=? AND armory=?',(usn,armory)).fetchone() is None:
                raise InventoryError('Unknown weapon set')
            profile['armoryIdx']=armory
            with self.db:self.save_profile(usn,profile)
            return [reply()]
        return None
