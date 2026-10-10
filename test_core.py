import copy
import unittest
from unittest.mock import AsyncMock
from domain import classify, key_digest, allowed_count, canonical_url, spreadsheet_cell
from parser import parse_card, decode, MapUnavailable
from bs4 import BeautifulSoup
from app import Bot


def fixture(**changes):
    card={'id':'123','type':'business','title':'Мастерская','address':'Казань, ул. Примерная, 1',
          'urls':[],'businessLinks':[],'phones':[], 'status':'open','modularCard':{'showContacts':True}}
    card.update(changes)
    return {'stack':[{'results':{'items':[card]}}]}

class Rules(unittest.TestCase):
    def test_incomplete_is_never_a_lead(self):
        s=fixture();del s['stack'][0]['results']['items'][0]['urls']
        self.assertEqual(classify(parse_card(s,'123')),'unknown')
    def test_omitted_urls_needs_rendered_contacts(self):
        state=fixture();del state['stack'][0]['results']['items'][0]['urls']
        soup=BeautifulSoup('<h1>Мастерская</h1><div class="business-contacts-view__address">Казань</div><div class="business-contacts-view__block">Адрес</div><div class="business-contacts-view__block">Контакты</div>','html.parser')
        self.assertEqual(classify(parse_card(state,'123',soup)),'accept')
    def test_dom_site_overrides_empty_urls(self):
        soup=BeautifulSoup('<div class="business-urls-view">example.com</div>','html.parser')
        self.assertEqual(classify(parse_card(fixture(),'123',soup)),'reject')
    def test_website_rejected(self):
        self.assertEqual(classify(parse_card(fixture(urls=['https://example.com']),'123')),'reject')
    def test_booking_rejected(self):
        self.assertEqual(classify(parse_card(fixture(businessLinks=[{'type':'booking','href':'https://example.com'}]),'123')),'reject')
    def test_closed_rejected(self):
        self.assertEqual(classify(parse_card(fixture(status='closed'),'123')),'reject')
    def test_matching_id_only(self):
        self.assertEqual(classify(parse_card(fixture(),'999')),'unknown')
    def test_known_empty_contact_fields(self):
        self.assertEqual(classify(parse_card(fixture(),'123')),'accept')
    def test_hidden_contacts_unknown(self):
        self.assertEqual(classify(parse_card(fixture(modularCard={'showContacts':False}),'123')),'unknown')
    def test_captcha_not_empty_results(self):
        with self.assertRaises(MapUnavailable):decode('<html>Подтвердите, что вы не робот</html>')
    def test_quota_boundary(self):
        self.assertTrue(allowed_count(5,45));self.assertFalse(allowed_count(10,45));self.assertFalse(allowed_count(5,50));self.assertFalse(allowed_count(7,0))
    def test_url_validation(self):
        self.assertIsNone(canonical_url('https://evil.com/maps/org/123/'))
        self.assertIsNone(canonical_url('http://127.0.0.1/maps/org/123/'))
        self.assertEqual(canonical_url('https://yandex.ru/maps/org/name/123/reviews/'),'https://yandex.ru/maps/org/123/')
    def test_key_hash(self):
        self.assertEqual(key_digest(' cxz-123 ','a'),key_digest('CXZ-123','a'))
        self.assertNotEqual(key_digest('CXZ-123','a'),key_digest('CXZ-123','b'))
    def test_excel_injection(self):
        self.assertTrue(spreadsheet_cell('=HYPERLINK("bad")').startswith("'"))

class Authorization(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db=AsyncMock();self.tg=AsyncMock();self.bot=Bot(self.db,self.tg,8051325598)
        self.db.user.return_value={'blocked':False,'state':{},'name':'Test'}
    def update(self,data):
        return {'update_id':1,'callback_query':{'id':'q','from':{'id':9},'data':data,'message':{'chat':{'id':9,'type':'private'}}}}
    async def test_admin_action_denied(self):
        await self.bot.handle(self.update('a:newkey'))
        self.db.create_key.assert_not_called()
        self.assertIn('только администратору',self.tg.send.call_args.args[1])
    async def test_search_requires_key_before_niche_selection(self):
        self.db.access.return_value=False;self.bot.screen=AsyncMock()
        await self.bot.handle(self.update('search'))
        self.db.state.assert_awaited_with(9,{'step':'key'})
        self.db.create_job.assert_not_called()
    async def test_foreign_result_cannot_be_changed(self):
        self.db.result.return_value=None
        await self.bot.handle(self.update('fav:100:1'))
        self.db.result.assert_awaited_with(100,9)
        self.db.run.assert_not_called()
    async def test_stale_count_cannot_create_job(self):
        self.db.access.return_value=True
        await self.bot.handle(self.update('count:10'))
        self.db.create_job.assert_not_called()
    async def test_non_private_chat_ignored(self):
        upd=self.update('admin');upd['callback_query']['message']['chat']['type']='group'
        await self.bot.handle(upd)
        self.db.touch.assert_not_called()

if __name__=='__main__':unittest.main()
