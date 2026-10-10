"""Optional integration tests; only use a separate EMPTY PostgreSQL test database.
TEST_DATABASE_URL=... python -m unittest discover -s tests -v
"""
import asyncio
import os
from pathlib import Path
import unittest
from db import Store

@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'),'No isolated test PostgreSQL configured')
class Database(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db=await Store.connect(os.environ['TEST_DATABASE_URL'],'test-pepper',8051325598)
        schema=(Path(__file__).parents[1]/'schema.sql').read_text()
        # Vanilla PostgreSQL may not have the Supabase API roles.
        schema='\n'.join(s for s in schema.splitlines() if not s.startswith('revoke '))
        await self.db.run(schema)
        await self.db.run('truncate cxz_inbox,cxz_results,cxz_jobs,cxz_keys,cxz_users,cxz_settings restart identity cascade')
        for uid in (1,2,8051325598):await self.db.touch({'id':uid})
    async def asyncTearDown(self):await self.db.pool.close()
    async def test_one_key_cannot_activate_two_users(self):
        key=await self.db.create_key(1)
        await asyncio.gather(self.db.activate(1,key),self.db.activate(2,key))
        self.assertEqual(sum([await self.db.access(1),await self.db.access(2)]),1)
    async def test_job_idempotency_and_one_active_job(self):
        uid=8051325598
        a,b=await asyncio.gather(self.db.create_job(uid,'кафе','Казань',5,100),self.db.create_job(uid,'кафе','Казань',5,100))
        self.assertEqual(a,b)
        c=await self.db.create_job(uid,'кафе','Казань',5,101)
        self.assertIsNone(c[0])
    async def test_unique_results_and_quota_under_race(self):
        uid=8051325598;jid,_=await self.db.create_job(uid,'кафе','Казань',10,1)
        job=await self.db.one('select * from cxz_jobs where id=$1',jid)
        card={'name':'Example','address':'Казань','evidence':{}}
        await asyncio.gather(*(self.db.accept(job,card,'https://yandex.ru/maps/org/123/','123') for _ in range(5)))
        self.assertEqual(await self.db.used(uid),1)
        await asyncio.gather(*(self.db.accept(job,card,f'https://yandex.ru/maps/org/{i}/',str(i)) for i in range(1000,1020)))
        self.assertEqual(await self.db.used(uid),10)
    async def test_revoked_access_prevents_commit(self):
        key=await self.db.create_key(42);await self.db.activate(1,key)
        jid,_=await self.db.create_job(1,'кафе','Казань',5,1)
        j=await self.db.one('select * from cxz_jobs where id=$1',jid)
        await self.db.run('update cxz_keys set revoked=true')
        ok=await self.db.accept(j,{'name':'Test','address':'Казань'},'https://yandex.ru/maps/org/123/','123')
        self.assertFalse(ok)
