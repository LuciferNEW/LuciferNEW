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
DATA=Path(__file__).with_name('economy-data.json')


class Economy:
    def __init__(self,lobby):
        self.lobby=lobby;self.db=lobby.db;self.inventory=lobby.inventory
        data=json.loads(DATA.read_text('utf-8'))
        self.inventory.configure_reward_items(data.get('character_reward_items',{}))
        self.meta={int(k):v for k,v in data['items'].items()}
        self.pools=data['pools'];self.recipes={r['index']:r for r in data['recipes']}
        self.coupon_prices={item_id:item['capsules'] for item_id,item in self.meta.items()
                            if item.get('capsules') and item.get('supply') in self.pools}
        for supply in self.pools:
            quantities={self.meta[item_id]['purchase_quantity'] for item_id in self.coupon_prices
                        if self.meta[item_id]['supply']==supply and self.meta[item_id].get('purchase_quantity')}
            if quantities!={1,6,12}:raise ValueError('Incomplete offline capsule purchase mapping: '+supply)
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

    def on_purchase(self,usn,row,encrypted):
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
            # TWN client sends a 7-byte binary id, not the PMS string form.
            return [reply([('short',0)],2)]
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
            recipe=self.recipes.get(design)
            if recipe is None or hidden!=recipe['hidden_item'] or serial!=0 or payment not in (1,3) or quote not in (0,1):
                raise InventoryError('Invalid local synthesis request')
            with self.db:
                profile=self.lobby.profile(usn);parts=list(profile.get('CombineParts',[0]*4))
                if len(parts)!=4 or any(have<need for have,need in zip(parts,recipe['parts'])):raise InventoryError('Insufficient synthesis parts')
                if profile['TP']<recipe['price_tp']:raise InventoryError('Insufficient TP')
                profile['TP']-=recipe['price_tp'];profile['CombineParts']=[have-need for have,need in zip(parts,recipe['parts'])]
                self.inventory.save_profile(usn,profile);created=self.inventory.add_reward(usn,recipe['item'])
                row=self.inventory.owned(usn,created)
            return [reply([('int',1)]+self.inventory.new_item_fields(row,profile,True))]
        return None
