"""Local balances, owned lottery capsules, synthesis and consumable effects."""
import datetime
import json
import logging
import secrets
from pathlib import Path
from inventory import InventoryError,END_DATE
from wire import response,Writer

RECHARGE_PROTOCOL=430
MAX_BALANCE=2_147_483_647
# Reply layouts tried for 262, in the order the next synthesis uses them.
# 4 (item record only, no trailing 123/124/258 refreshes) is the only one that
# raises the client's success dialog: the refresh frames arrived in the same
# batch and made the craft scene close before the window could open. The client
# re-requests 123/124/258 itself when the inventory or craft screen is reopened.
CRAFT_VARIANTS=(4,)
DATA=Path(__file__).with_name('economy-data.json')


class Economy:
    def __init__(self,lobby):
        self.lobby=lobby;self.db=lobby.db;self.inventory=lobby.inventory
        data=json.loads(DATA.read_text('utf-8'))
        self.inventory.configure_reward_items(data.get('character_reward_items',{}))
        self.meta={int(k):v for k,v in data['items'].items()}
        # The TWN client GashaList data store boots with these supply codes.
        # Put them first so the lottery screen always has its native entries
        # even if the client ignores unknown supply codes.
        boot=('PVE0001','PVE0002','PVE0003','PVE0004','SPY0001','GAAA188')
        pools=data['pools']
        self.pools={supply:pools[supply] for supply in boot if supply in pools}
        self.pools.update({supply:pool for supply,pool in pools.items() if supply not in self.pools})
        self.recipes={r['index']:r for r in data['recipes']}
        self.coupon_prices={item_id:item['capsules'] for item_id,item in self.meta.items()
                            if item.get('capsules') and item.get('supply') in self.pools}
        for supply in self.pools:
            quantities={self.meta[item_id]['purchase_quantity'] for item_id in self.coupon_prices
                        if self.meta[item_id]['supply']==supply and self.meta[item_id].get('purchase_quantity')}
            # The TWN client lottery UI boots with PVE0001..4 / SPY0001 /
            # GAAA188 even when a pool has no coupon in the local shop.  Only
            # validate packs that actually have coupons; an empty set is valid.
            if quantities and quantities!={1,6,12}:raise ValueError('Incomplete offline capsule purchase mapping: '+supply)
        for pool in self.pools.values():
            if sum(r['weight'] for r in pool)!=10000 or any(r['id'] not in lobby.catalog or r['weight']<1 for r in pool):
                raise ValueError('Invalid offline prize pool')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS lottery_capsules(id INTEGER PRIMARY KEY AUTOINCREMENT,usn INTEGER NOT NULL,supply TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS lottery_log(id INTEGER PRIMARY KEY AUTOINCREMENT,usn INTEGER NOT NULL,supply TEXT NOT NULL,item_id INTEGER NOT NULL,rare INTEGER NOT NULL,date TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS currency_grants(id INTEGER PRIMARY KEY AUTOINCREMENT,usn INTEGER NOT NULL,amount INTEGER NOT NULL,date TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS client_diagnostics(id INTEGER PRIMARY KEY AUTOINCREMENT,usn INTEGER NOT NULL,message TEXT NOT NULL,date TEXT NOT NULL);
        ''')
        self.inventory.special=self

    def purchase_price(self,item_id):
        return self.coupon_prices.get(item_id,1)

    def refresh(self,profile,encrypted):
        return [response(command,[('int',profile.get(field,0))],encrypted=encrypted)
                for command,field in [(138,'SP'),(401,'TP'),(194,'Cash')]]

    def add_balance(self,profile,field,amount):
        current=profile.get(field,0)
        if type(current) is not int or current<0 or amount<0 or current+amount>MAX_BALANCE:
            raise InventoryError('Local balance would overflow')
        profile[field]=current+amount

    def convert(self,usn,row):
        item=self.meta.get(row['item_id'],{});supply=item.get('supply')
        if not item.get('capsules') or supply not in self.pools:raise InventoryError('No local capsule pool for this item')
        if row['used']:raise InventoryError('Capsule coupon has already been used')
        capsules=[]
        for _ in range(item['capsules']):
            capsule=self.db.execute('INSERT INTO lottery_capsules(usn,supply) VALUES(?,?)',(usn,supply)).lastrowid
            if capsule>MAX_BALANCE:raise InventoryError('Capsule ID exceeds client range')
            capsules.append((capsule,supply,row['item_id']))
        self.db.execute('DELETE FROM inventory WHERE serial=? AND usn=?',(row['serial'],usn))
        self.inventory.release_empty_stack(usn,row['item_id'])
        return capsules

    def capsule_notifications(self,capsules,encrypted):
        packets=[]
        for start in range(0,len(capsules),70):
            batch=capsules[start:start+70];writer=Writer().add('short',len(batch))
            for capsule,supply,item_id in batch:
                writer.add('int',capsule).add('string',supply).add('int',item_id)
            packets.append(writer.frame(1013,encrypted))
        return packets

    def chat_draw(self,usn,count=1):
        """Chat-command lottery, used because the TWN Gashapon screen hangs."""
        supply='GAAA188' if 'GAAA188' in self.pools else next(iter(self.pools))
        pool=self.pools[supply];names=[]
        with self.db:
            for _ in range(max(1,min(50,count))):
                roll=secrets.randbelow(10000)
                for prize in pool:
                    roll-=prize['weight']
                    if roll<0:break
                self.inventory.add_reward(usn,prize['id'])
                names.append(self.lobby.catalog[prize['id']]['name'])
        logging.info('local chat draw usn=%s supply=%s count=%s',usn,supply,len(names))
        return names

    def use_coupon(self,usn,row):
        """Consume a capsule coupon and return the awarded inventory rows."""
        item=self.meta.get(row['item_id'],{});supply=item.get('supply')
        if not item.get('capsules') or supply not in self.pools:
            raise InventoryError('No local capsule pool for this item')
        pool=self.pools[supply];awarded=[]
        with self.db:
            self.db.execute('DELETE FROM inventory WHERE serial=? AND usn=?',(row['serial'],usn))
            for _ in range(item['capsules']):
                roll=secrets.randbelow(10000)
                for prize in pool:
                    roll-=prize['weight']
                    if roll<0:break
                serial=self.inventory.add_reward(usn,prize['id'])
                awarded.append(self.inventory.owned(usn,serial))
        self.inventory.release_empty_stack(usn,row['item_id'])
        logging.info('local capsule use usn=%s supply=%s prizes=%s',usn,supply,[r['item_id'] for r in awarded])
        return awarded

    def use_package_draw(self,usn,row):
        """Open a client-side '轉蛋包' package that has no contents table."""
        supply='GAAA180' if 'GAAA180' in self.pools else next(iter(self.pools))
        pool=self.pools[supply]
        roll=secrets.randbelow(10000)
        for prize in pool:
            roll-=prize['weight']
            if roll<0:break
        with self.db:
            self.db.execute('DELETE FROM inventory WHERE serial=? AND usn=?',(row['serial'],usn))
            serial=self.inventory.add_reward(usn,prize['id'])
            awarded=self.inventory.owned(usn,serial)
        self.inventory.release_empty_stack(usn,row['item_id'])
        logging.info('local package draw usn=%s item=%s supply=%s prize=%s',
                     usn,row['item_id'],supply,prize['id'])
        return [awarded]

    def use_capsule(self,usn,capsule_id):
        """Open one owned lottery capsule (protocol 196 on the capsule row)."""
        capsule_id=int(capsule_id)&0xffffffff
        row=self.db.execute('SELECT supply FROM lottery_capsules WHERE id=? AND usn=?',
                            (capsule_id,usn)).fetchone()
        if row is None:raise InventoryError('Capsule is not owned or has already been opened')
        supply=row['supply'];pool=self.pools.get(supply)
        if not pool:raise InventoryError('Unknown capsule supply')
        roll=secrets.randbelow(10000)
        for prize in pool:
            roll-=prize['weight']
            if roll<0:break
        with self.db:
            self.db.execute('DELETE FROM lottery_capsules WHERE id=? AND usn=?',(capsule_id,usn))
            serial=self.inventory.add_reward(usn,prize['id'])
            awarded=self.inventory.owned(usn,serial)
            self.db.execute('INSERT INTO lottery_log(usn,supply,item_id,rare,date) VALUES(?,?,?,?,?)',
                            (usn,supply,prize['id'],int(prize['rare']),datetime.datetime.now().isoformat()))
        logging.info('local capsule open usn=%s supply=%s capsule=%s prize=%s',
                     usn,supply,capsule_id,prize['id'])
        return [awarded]

    def on_purchase(self,usn,row,encrypted):
        # Restore the V2 flow: a bought coupon is converted into lottery
        # capsules and the client receives the 1013 capsule notification.
        if self.meta.get(row['item_id'],{}).get('capsules'):
            return self.capsule_notifications(self.convert(usn,row),encrypted)
        return []

    def activate(self,usn,row,option,profile):
        item=self.lobby.catalog[row['item_id']];data=self.meta.get(row['item_id'],{})
        if 'combine_part' in data:
            parts=list(profile.get('CombineParts',[0]*4));index=data['combine_part']
            if len(parts)!=4 or any(type(x) is not int or x<0 for x in parts):raise InventoryError('Invalid synthesis ledger')
            if parts[index]+data['combine_count']>MAX_BALANCE:raise InventoryError('Synthesis parts would overflow')
            parts[index]+=data['combine_count'];profile['CombineParts']=parts
        self.add_balance(profile,'SP',data.get('add_sp',0))
        self.add_balance(profile,'TP',data.get('exchange_tp',0))
        cls=item['class']
        reset={
            'SFCItem_Func_InitKillDeath':('Kills','Deaths','Assists','HeadShots','HeadHitCount','AllHitCount'),
            'SFCItem_Func_InitWinLose':('AllWinGameCount','AllPlayGameCount','AttackRound','AttackRoundWin','DefendRound','DefendRoundWin'),
            'SFCItem_Func_InitDesertion':('RunAwayCount',),
        }
        for key in reset.get(cls,()):profile[key]=0
        self.inventory.save_profile(usn,profile)

    def handle(self,command,reader,usn,profile,encrypted):
        def reply(fields=(),result=1):return response(command,fields,result,encrypted)
        if command==415:
            # Weapon-crafting material purchase: int itemId, byte count,
            # int totalPrice, short flag.
            raw=reader.payload
            if len(raw)!=11:raise InventoryError('Invalid craft purchase')
            item_id=int.from_bytes(raw[0:4],'little',signed=True)
            count=raw[4]
            price=int.from_bytes(raw[5:9],'little',signed=True)
            item=self.lobby.catalog.get(item_id)
            if item is None or not 1<=count<=99 or price<0:raise InventoryError('Invalid craft purchase')
            currency=item.get('currency')
            field={'SP':'SP','CASH':'Cash','TP':'TP'}.get(currency)
            if field is None:raise InventoryError('Unsupported craft currency')
            with self.db:
                profile=self.lobby.profile(usn)
                if profile.get(field,0)<price:raise InventoryError('Insufficient '+field)
                profile[field]-=price;self.inventory.save_profile(usn,profile)
                for _ in range(count):self.inventory.add(usn,item_id)
            logging.info('local craft purchase usn=%s item=%s count=%s cost=%s %s',
                         usn,item_id,count,price,field)
            return [reply()]+self.refresh(profile,encrypted)
        if command==RECHARGE_PROTOCOL:
            reader.require_end()
            with self.db:
                profile=self.lobby.profile(usn)
                for field in ('TP','Cash','SP'):self.add_balance(profile,field,10000)
                self.inventory.save_profile(usn,profile)
                self.db.execute('INSERT INTO currency_grants(usn,amount,date) VALUES(?,?,?)',
                                (usn,10000,datetime.datetime.now().isoformat()))
            logging.info('local recharge usn=%s TP=%s CASH=%s SP=%s',usn,profile['TP'],profile['Cash'],profile['SP'])
            return [reply()]+self.refresh(profile,encrypted)
        if command==228:
            message=reader.get('string');reader.require_end()
            if len(message)>256:raise InventoryError('Diagnostic is too long')
            with self.db:self.db.execute('INSERT INTO client_diagnostics(usn,message,date) VALUES(?,?,?)',
                                       (usn,message,datetime.datetime.now().isoformat()))
            logging.warning('local client diagnostic usn=%s reason=%s',usn,message)
            return [reply()]
        if command==246:
            reader.require_end();rows=[(supply,r,100 if r['rare'] else index+1)
                                     for supply,pool in self.pools.items() for index,r in enumerate(pool)]
            return [reply([('short',len(rows))],2)]+[reply([
                ('string',supply),('string','I'),('int',r['id']),('int',rank),
                ('string','Y' if r['rare'] else 'N'),('int',r['weight']),('int',0),('string','N'),('string','Y')]) for supply,r,rank in rows]
        if command==211:
            # The TWN client sends the 7-byte supply code without an FString
            # length prefix; other builds send the PMS FString form.  Answer
            # with that pool's prize list instead of an empty table so the
            # lottery detail view has data to draw.
            raw=reader.payload[reader.offset:]
            reader.offset=len(reader.payload)
            supply=None
            if len(raw)==7:
                try:supply=raw.decode('ascii')
                except UnicodeDecodeError:supply=None
            elif len(raw)>=12:
                try:
                    length=int.from_bytes(raw[:4],'little',signed=True)
                    if 1<=length<=64:
                        text=raw[4:4+length-1].decode('utf-8')
                        if text in self.pools:supply=text
                except (UnicodeDecodeError,ValueError):pass
            logging.debug('gasha 211 payload=%s supply=%s',raw.hex(),supply)
            if len(raw)==3:
                # The Gashapon selection screen polls this 3-byte request.
                # Its exact row layout is still being recovered, so the reply
                # is driven by an editable JSON file for live testing.
                cfg={'mode':'silent'}
                cfg_path=DATA.with_name('gasha211.json')
                if cfg_path.exists():
                    try:cfg=json.loads(cfg_path.read_text('utf-8'))
                    except ValueError:pass
                mode=cfg.get('mode','silent')
                if mode=='echo':
                    # Some PCPMS requests echo the request command instead of
                    # the generic result code.
                    return [response(211,[('short',0)],211)]
                if mode=='fail':
                    # Tell the Gashapon screen the request failed so it can
                    # leave the loading state instead of hanging forever.
                    return [response(211,[('byte',1)],0)]
                if mode=='silent':
                    return []
                if mode=='empty':
                    return [reply([('short',0)],2)]
                if mode=='ack':
                    return [reply([('byte',1)])]
                supplies=[value for value in cfg.get('supplies',[]) if value in self.pools]
                if mode=='rows':
                    packets=[reply([('short',len(supplies))],2)]
                    for value in supplies:
                        packets.append(reply([('string',value),('int',0),('int',0),('int',0),
                                              ('int',0),('int',0),('int',10000),('string','')]))
                    return packets
                if mode=='prizes':
                    packets=[reply([('short',sum(len(self.pools[value]) for value in supplies))],2)]
                    for value in supplies:
                        for index,prize in enumerate(self.pools[value]):
                            packets.append(reply([('string',value),('string','I'),('int',prize['id']),
                                                  ('int',index+1),('string','Y' if prize['rare'] else 'N'),
                                                  ('int',prize['weight']),('int',0),('string','N'),('string','Y')]))
                    return packets
                return [reply([('short',0)],2)]
            if supply not in self.pools:
                return [reply([('short',0)],2)]
            pool=self.pools[supply]
            return [reply([('short',len(pool))],2)]+[reply([
                ('string',supply),('string','I'),('int',r['id']),('int',index+1),
                ('string','Y' if r['rare'] else 'N'),('int',r['weight']),('int',0),
                ('string','N'),('string','Y')]) for index,r in enumerate(pool)]
        if command in (268,297):
            reader.require_end()
            return [reply([('short',0)],2)]
        if command==296:
            supply=reader.get('string');reader.require_end()
            if supply not in self.pools:raise InventoryError('Unknown capsule supply')
            return [reply([('string',supply),('int',0)])]
        if command==234:
            reader.require_end();rows=self.db.execute('SELECT id,supply FROM lottery_capsules WHERE usn=? ORDER BY id',(usn,)).fetchall()
            return [reply([('short',len(rows))],2)]+[reply([('int',r['id']),('string',r['supply'])]) for r in rows]
        if command==235:
            reader.require_end();capsules=[]
            with self.db:
                rows=self.db.execute('SELECT * FROM inventory WHERE usn=? AND used=0 ORDER BY serial',(usn,)).fetchall()
                for row in rows:
                    if self.meta.get(row['item_id'],{}).get('capsules'):capsules.extend(self.convert(usn,row))
            return [reply()]+self.capsule_notifications(capsules,encrypted)
        if command==233:
            reader.require_end();packets=[reply([('short',len(self.pools))],2)]
            for supply,pool in self.pools.items():
                count=self.db.execute('SELECT COUNT(*),COALESCE(SUM(rare),0) FROM lottery_log WHERE usn=? AND supply=?',(usn,supply)).fetchone()
                pending=self.db.execute('SELECT COUNT(*) FROM lottery_capsules WHERE usn=? AND supply=?',(usn,supply)).fetchone()[0]
                packets.append(reply([('string',supply),('int',count[0]),('int',0),('int',count[1]),
                                      ('int',pending),('int',count[0]),('int',10000),('string','')]))
            return packets
        if command==210:
            kind=reader.get('byte');supply=reader.get('string')
            params=[reader.get('int') for _ in range(4)];reader.require_end()
            if kind!=1 or params[:3]!=[0,0,0]:raise InventoryError('This local lottery requires an owned capsule')
            capsule=params[3]
            with self.db:
                row=self.db.execute('SELECT supply FROM lottery_capsules WHERE id=? AND usn=?',(capsule,usn)).fetchone()
                if row is None or row['supply']!=supply:raise InventoryError('Capsule is not owned or has already been opened')
                pool=self.pools[supply];roll=secrets.randbelow(10000)
                for prize in pool:
                    roll-=prize['weight']
                    if roll<0:break
                self.db.execute('DELETE FROM lottery_capsules WHERE id=? AND usn=?',(capsule,usn))
                serial=self.inventory.add_reward(usn,prize['id'])
                awarded=self.inventory.owned(usn,serial)
                self.db.execute('INSERT INTO lottery_log(usn,supply,item_id,rare,date) VALUES(?,?,?,?,?)',
                                (usn,supply,awarded['item_id'],int(prize['rare']),datetime.datetime.now().isoformat()))
            return [reply([('short',1)],2),reply([('int',awarded['item_id']),('string','I'),('long',serial),
                                                ('string',END_DATE),('int',100),('string','Y' if awarded['used'] else 'N'),('byte',0)])]
        if command==258:
            reader.require_end();return [reply([('int',n) for n in profile.get('CombineParts',[0]*4)])]
        if command==259:
            reader.require_end();return [reply([('short',len(self.recipes))],2)]+[reply([
                ('int',r['index']),('int',r['item'])]+[('int',n) for n in r['parts']]+[
                ('string',r['group']),('int',r['hidden_item']),('int',r['probability'])]) for r in self.recipes.values()]
        if command==262:
            design=reader.get('int');hidden=reader.get('int');serial=reader.get('long')
            payment=reader.get('byte');quote=reader.get('int');reader.require_end()
            logging.info('craft 262 design=%s hidden=%s serial=%s payment=%s quote=%s',
                         design,hidden,serial,payment,quote)
            recipe=self.recipes.get(design)
            if recipe is None:
                # Older captures showed the client passing the recipe item id
                # instead of the design index; accept both.
                recipe=next((r for r in self.recipes.values() if r['item']==design),None)
            if recipe is None:
                logging.warning('craft 262 unknown design=%s known=%s',design,sorted(self.recipes))
                raise InventoryError('Invalid local synthesis request')
            if hidden!=recipe['hidden_item'] or serial!=0 or payment not in (1,3) or quote not in (0,1):
                logging.info('craft 262 slots hidden=%s(want %s) serial=%s payment=%s quote=%s',
                             hidden,recipe['hidden_item'],serial,payment,quote)
            with self.db:
                profile=self.lobby.profile(usn);parts=list(profile.get('CombineParts',[0]*4))
                if len(parts)!=4 or any(have<need for have,need in zip(parts,recipe['parts'])):raise InventoryError('Insufficient synthesis parts')
                if profile['TP']<recipe['price_tp']:raise InventoryError('Insufficient TP')
                profile['TP']-=recipe['price_tp'];profile['CombineParts']=[have-need for have,need in zip(parts,recipe['parts'])]
                self.inventory.save_profile(usn,profile);created=self.inventory.add_reward(usn,recipe['item'])
                row=self.inventory.owned(usn,created)
            logging.info('craft 262 made item=%s serial=%s parts=%s TP=%s',
                         row['item_id'],created,profile['CombineParts'],profile['TP'])
            # The client never raises Lobby_CombinePopup_Success for the
            # historical layouts. Probe every reply shape automatically: each
            # synthesis uses the next variant in CRAFT_VARIANTS and the variant
            # is logged, so one run of clicks identifies the working layout
            # even when the in-game chat is unavailable.
            attempt=getattr(self.lobby,'craft_attempt',0)
            self.lobby.craft_attempt=attempt+1
            variant=CRAFT_VARIANTS[attempt%len(CRAFT_VARIANTS)]
            logging.info('craft 262 attempt=%s variant=%s',attempt+1,variant)
            item_fields=self.inventory.new_item_fields(row,profile,True)
            reward_row=[('int',row['item_id']),('string','Y'),('long',created),
                        ('string',END_DATE),('int',100),('int',1)]
            if variant in (2,4):
                # Mirror the shop purchase reply (127): a bare item record.
                packets=[reply(item_fields)]
            elif variant in (3,5):
                # Mirror the package-open reply (196): count header + item row.
                packets=[reply([('short',1)],2),reply(reward_row)]
            else:
                packets=[reply([('int',1)]+item_fields)]
            if variant not in (4,5):
                packets+=self.inventory.list_packets(usn,encrypted)
                packets.append(reply([('int',n) for n in profile['CombineParts']]))
            if variant==6:
                # Last-resort feedback: the local chat channel (500) is known to
                # render, so announce the crafted item when no native dialog
                # can be reached.
                event=Writer()
                for kind,value in [('byte',0),('string','系統'),
                                   ('string','製作完成：%s' % self.lobby.catalog[row['item_id']].get('name','')),
                                   ('byte',0),('byte',0),('string','')]:event.add(kind,value)
                packets.append(event.frame(500,encrypted))
            return packets
        return None
