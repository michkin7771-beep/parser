"""Public HTML only: embedded page data, no hidden API, proxy or CAPTCHA bypass.
The adapter fails closed if the public-page schema changes.
"""
import asyncio
import json
import re
import urllib.request
from urllib.parse import quote, urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup
from domain import org_id, classify

class MapUnavailable(Exception): pass

def decode(html):
    soup = BeautifulSoup(html, 'html.parser')
    tag = soup.select_one('script.state-view[type="application/json"]')
    if not tag:
        text = soup.get_text(' ',strip=True).lower()
        if any(x in text for x in ('не робот','not a robot','captcha','доступ ограничен')):
            raise MapUnavailable('Яндекс запросил проверку или ограничил доступ. Поиск остановлен без обхода защиты.')
        raise MapUnavailable('Страница Яндекса не распознана. Нужна проверка адаптера.')
    try: state=json.loads(tag.string or tag.get_text())
    except (ValueError,TypeError): raise MapUnavailable('Данные карточки не удалось прочитать.')
    return soup,state

def items(state):
    for panel in state.get('stack',[]):
        results=panel.get('results',{})
        for item in results.get('items',[]):
            if item.get('type')=='business':
                yield item

BOOKING=re.compile(r'booking|reservation|yclients|dikidi|altegio|rubitime|reservi|запис|брони',re.I)

def parse_card(state, expected, soup=None):
    target=next((i for i in items(state) if str(i.get('id'))==expected),None)
    if not target: return {'loaded':False}
    required=('title','address','urls','businessLinks','status','modularCard','phones')
    dom_contacts=bool(soup is not None and soup.select_one('.business-contacts-view__address')
                      and len(soup.select('.business-contacts-view__block'))>=2 and soup.select_one('h1'))
    # Observed public schema omits urls completely for a site-less organization.
    # Accept this only when the same fetched HTML also contains the rendered contacts.
    missing_urls_confirmed='urls' not in target and dom_contacts
    complete=all(k in target or (k=='urls' and missing_urls_confirmed) for k in required)
    complete=complete and (isinstance(target.get('urls'),list) or missing_urls_confirmed) and isinstance(target.get('businessLinks'),list)
    dom_site=bool(soup is not None and soup.select_one('.business-urls-view'))
    links=target.get('businessLinks') or []
    booking=bool(target.get('booking')) or bool(BOOKING.search(json.dumps(links,ensure_ascii=False)))
    # Social chat alone isn't booking; booking service links on social fields are.
    social=json.dumps(target.get('socialLinks',[]),ensure_ascii=False)
    booking=booking or bool(re.search(r'yclients|dikidi|altegio|rubitime',social,re.I))
    for feature in target.get('features',[]):
        if feature.get('value') and re.search(r'online.*(book|appoint|reserv)|онлайн.*запис', str(feature.get('id',''))+' '+str(feature.get('name','')),re.I):
            booking=True
    phone=next((p.get('formatted') or p.get('number','') for p in target.get('phones',[]) if isinstance(p,dict)), '')
    contact=bool(target.get('modularCard',{}).get('showContacts'))
    return dict(loaded=complete,name=target.get('title',''),address=target.get('address',''),phone=phone,
                website=bool(target.get('urls')) or dom_site,booking=booking,
                contacts_loaded=contact,overview_loaded=complete,
                closed=target.get('status')!='open',
                evidence={'source':'Яндекс Карты','schema':'public HTML state-view',
                          'urls':target.get('urls'), 'businessLinks':links,
                          'booking_present':bool(target.get('booking')), 'status':target.get('status'),
                          'showContacts':contact, 'fields_complete':complete,'rendered_contacts':dom_contacts,
                          'scope':'Проверены данные этой карточки; другие карты и интернет не проверялись'})

class YandexParser:
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass
    async def fetch(self,url):
        def get():
            try:
                with urllib.request.urlopen(url,timeout=35) as response:
                    end=urlparse(response.url)
                    if end.hostname not in ('yandex.ru','yandex.com') or 'captcha' in end.path.lower():
                        raise MapUnavailable('Яндекс перенаправил на проверку доступа. Поиск остановлен.')
                    data=response.read(6_000_001)
                    if len(data)>6_000_000: raise MapUnavailable('Страница слишком велика для безопасной обработки.')
                    return data.decode('utf-8'),response.url
            except MapUnavailable: raise
            except Exception as e:
                raise MapUnavailable('Яндекс Карты недоступны или вернули ошибку. Повторите позже.') from e
        html,final=await asyncio.to_thread(get)
        soup,state=await asyncio.to_thread(decode,html)
        return soup,state,final

    async def discover(self,niche,region,limit=60):
        url='https://yandex.ru/maps/?text='+quote(niche+' '+region)+'&lang=ru_RU'
        links=[]; seen=set()
        # Public pagination links only. Hard bound protects the free server.
        for _ in range(12):
            if url in seen: break
            seen.add(url)
            soup,state,final=await self.fetch(url)
            panels=[p for p in state.get('stack',[]) if 'results' in p]
            if not panels: raise MapUnavailable('Список организаций не распознан.')
            found=list(items(state))
            for item in found:
                oid=str(item.get('id',''))
                if oid.isdigit():
                    link='https://yandex.ru/maps/org/'+oid+'/'
                    if link not in links: links.append(link)
            if len(links)>=limit or not found: break
            current=int(parse_qs(urlparse(final).query).get('page',['1'])[0])
            next_url=None
            for a in soup.select('a[href]'):
                href=urljoin(final,a['href']);p=urlparse(href)
                if p.hostname in ('yandex.ru','yandex.com') and '/search/' in p.path and parse_qs(p.query).get('page')==[str(current+1)]:
                    next_url=href;break
            if not next_url: break
            url=next_url
            await asyncio.sleep(3)
        return links[:limit]

    async def inspect(self,url):
        oid=org_id(url)
        if not oid: raise MapUnavailable('Некорректная ссылка на организацию.')
        soup,state,_=await self.fetch(url+'?lang=ru_RU')
        return parse_card(state,oid,soup)

async def smoke(niche,region):
    async with YandexParser() as parser:
        urls=await parser.discover(niche,region,5)
        print('Кандидатов:',len(urls))
        for url in urls[:3]:
            card=await parser.inspect(url)
            print(url,classify(card),json.dumps(card,ensure_ascii=False))
            await asyncio.sleep(3)

if __name__=='__main__':
    import sys
    asyncio.run(smoke(*(sys.argv[1:3] or ['салоны красоты','Казань'])))
