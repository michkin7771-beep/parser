import json
import secrets
import asyncpg
from domain import DAILY_LIMIT, allowed_count, key_digest

DAY="date_trunc('day', now() at time zone 'Europe/Moscow') at time zone 'Europe/Moscow'"

class Store:
    def __init__(self,pool,secret,admin): self.pool,self.secret,self.admin=pool,secret,admin
    @classmethod
    async def connect(cls,url,secret,admin):
        async def init(c):
            await c.set_type_codec('jsonb',schema='pg_catalog',encoder=json.dumps,decoder=json.loads,format='text')
        pool=await asyncpg.create_pool(url,min_size=1,max_size=4,statement_cache_size=0,init=init,command_timeout=30)
        return cls(pool,secret,admin)
    async def one(self,q,*a): return await self.pool.fetchrow(q,*a)
    async def all(self,q,*a): return await self.pool.fetch(q,*a)
    async def run(self,q,*a): return await self.pool.execute(q,*a)
    async def user(self,uid): return await self.one('select * from cxz_users where id=$1',uid)
    async def state(self,uid,state): await self.run('update cxz_users set state=$2 where id=$1',uid,state)
    async def touch(self,u):
        await self.run('''insert into cxz_users(id,name,username) values($1,$2,$3)
            on conflict(id) do update set name=excluded.name,username=excluded.username,last_seen=now()''',u['id'],u.get('first_name','')[:200],u.get('username','')[:200])
    async def access(self,uid,c=None):
        c=c or self.pool
        u=await c.fetchrow('select blocked from cxz_users where id=$1',uid)
        if not u or u['blocked']: return False
        if uid==self.admin: return True
        return bool(await c.fetchval('''select exists(select 1 from cxz_keys where used_by=$1 and not revoked
            and (expires_at is null or expires_at>now()))''',uid))
    async def used(self,uid,c=None):
        return await (c or self.pool).fetchval(f'select count(*) from cxz_results where user_id=$1 and checked_at>={DAY}',uid)
    async def activate(self,uid,key):
        async with self.pool.acquire() as c, c.transaction():
            u=await c.fetchrow('select *, key_lock_until>now() as locked from cxz_users where id=$1 for update',uid)
            if u['blocked']: return 'Доступ заблокирован.'
            if u['locked']: return 'Слишком много попыток. Подождите 15 минут.'
            k=await c.fetchrow('''select *, (expires_at is null or expires_at>now()) as valid
                from cxz_keys where digest=$1 for update''',key_digest(key,self.secret))
            if not k or k['revoked'] or not k['valid'] or k['used_by'] not in (None,uid):
                await c.execute('''update cxz_users set key_failures=key_failures+1,
                    key_lock_until=case when (key_failures+1)%5=0 then now()+interval '15 minutes' else key_lock_until end
                    where id=$1''',uid)
                return 'Ключ неверен, уже использован или отключён.'
            await c.execute('update cxz_keys set used_by=$2,used_at=coalesce(used_at,now()) where id=$1',k['id'],uid)
            await c.execute("update cxz_users set activated_at=coalesce(activated_at,now()),state='{}',key_failures=0,key_lock_until=null where id=$1",uid)
            return 'Доступ активирован. Теперь можно искать бизнесы.'
    async def create_key(self,request_id):
        # Deterministic per Telegram update: retries cannot mint a second key.
        key='CXZ-'+key_digest('generate:'+str(request_id),self.secret)[:24].upper()
        await self.run('insert into cxz_keys(digest,label) values($1,$2) on conflict(digest) do nothing',
                       key_digest(key,self.secret),key[:8]+'…'+key[-4:])
        return key
    async def create_job(self,uid,niche,region,target,request_id):
        async with self.pool.acquire() as c, c.transaction():
            await c.fetchrow('select id from cxz_users where id=$1 for update',uid)
            existing=await c.fetchrow('select * from cxz_jobs where request_id=$1',request_id)
            if existing: return existing['id'],None
            if not await self.access(uid,c): return None,'Сначала активируйте доступ.'
            if await c.fetchval("select exists(select 1 from cxz_jobs where user_id=$1 and status in ('queued','running'))",uid):
                return None,'У вас уже есть поиск. Дождитесь результата или остановите его.'
            used=await self.used(uid,c)
            if not allowed_count(target,used): return None,f'Осталось {DAILY_LIMIT-used} из 50. Можно выбрать 5 или 10 в пределах остатка.'
            jid=await c.fetchval('''insert into cxz_jobs(user_id,request_id,niche,region,target)
                values($1,$2,$3,$4,$5) returning id''',uid,request_id,niche,region,target)
            await c.execute("update cxz_users set state='{}' where id=$1",uid)
            return jid,None
    async def accept(self,job,card,url,oid):
        async with self.pool.acquire() as c, c.transaction():
            await c.fetchrow('select id from cxz_users where id=$1 for update',job['user_id'])
            active=await c.fetchrow('select * from cxz_jobs where id=$1 for update',job['id'])
            if active['cancelled'] or not await self.access(job['user_id'],c): return False
            if await self.used(job['user_id'],c)>=DAILY_LIMIT: return False
            n=await c.fetchval('select count(*) from cxz_results where job_id=$1',job['id'])
            if n>=job['target']: return False
            return bool(await c.fetchval('''insert into cxz_results(user_id,job_id,org_id,name,address,phone,url,evidence)
                values($1,$2,$3,$4,$5,$6,$7,$8) on conflict(user_id,org_id) do nothing returning id''',
                job['user_id'],job['id'],oid,card['name'],card['address'],card.get('phone',''),url,card.get('evidence',{})))
    async def claim_job(self):
        return await self.one('''update cxz_jobs set status='running',lease_until=now()+interval '3 minutes',updated_at=now()
            where id=(select id from cxz_jobs where (status='queued' or (status='running' and lease_until<now()))
                and not cancelled order by created_at for update skip locked limit 1) returning *''')
    async def result(self,rid,uid): return await self.one('select * from cxz_results where id=$1 and user_id=$2',rid,uid)
    async def setting(self,key):
        r=await self.one('select value from cxz_settings where name=$1',key)
        return r['value'] if r else None
