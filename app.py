import asyncio
import contextlib
import csv
import html
import io
import json
import logging
import os
from pathlib import Path
import secrets
import hmac
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from db import Store, DAY
from domain import ADMIN_DEFAULT, DAILY_LIMIT, STAGES, classify, org_id, spreadsheet_cell
from parser import YandexParser, MapUnavailable

LOG=logging.getLogger('cxz')
ROOT=Path(__file__).parent
MSK=ZoneInfo('Europe/Moscow')
NICHES=['Салоны красоты','Парикмахерские','Барбершопы','Автосервисы','Шиномонтаж','Автомойки','Цветочные магазины','Кафе','Пекарни','Ателье','Ремонт обуви','Ремонт телефонов','Мебельные магазины','Магазины одежды','Зоомагазины','Ветеринарные клиники','Стоматологии','Массажные кабинеты','Фитнес-клубы','Танцевальные студии','Детские центры','Репетиторские центры','Клининг','Химчистки','Строительные магазины','Магазины автозапчастей','Фотостудии','Груминг','Пункты проката','Ремонт бытовой техники']
SECTIONS={'home':'Главное меню','search':'Найти бизнесы','activation':'Активация','results':'Мои результаты','favorites':'Избранное','profile':'Мой доступ','help':'Как работает','admin':'Админ-панель'}

def esc(s): return html.escape(str(s or ''))
def status_name(s): return {'queued':'В очереди','running':'Проверяется','done':'Завершён','cancelled':'Остановлен','blocked':'Карта недоступна','partial':'Частичный результат','error':'Ошибка'}.get(s,s)
def date(v): return v.astimezone(MSK).strftime('%d.%m.%Y %H:%M') if v else '—'
def button(text,data): return {'text':text,'callback_data':data}
def markup(rows): return {'inline_keyboard':rows}
def home_rows(admin=False):
    rows=[[button('🔎 Найти бизнесы','search')],[button('📂 Мои результаты','results:0'),button('⭐ Избранное','favorites:0')],
          [button('🔑 Активировать','activate'),button('👤 Мой доступ','profile')],[button('ℹ️ Как работает','help')]]
    if admin: rows.append([button('⚙️ Админ-панель','admin')])
    return rows
BACK=[[button('← Главное меню','home')]]

class Telegram:
    def __init__(self,session,token): self.session,self.base=session,'https://api.telegram.org/bot'+token+'/'
    async def call(self,method,data=None,form=None):
        # No URL/exception logging: Bot API URLs contain the secret token.
        for attempt in range(3):
            async with self.session.post(self.base+method,json=data if form is None else None,data=form) as r:
                result=await r.json()
            if result.get('ok'): return result.get('result')
            if result.get('error_code')==429 and form is None:
                await asyncio.sleep(min(result.get('parameters',{}).get('retry_after',2),15));continue
            raise RuntimeError('Telegram API request failed: '+str(result.get('error_code','unknown')))
        raise RuntimeError('Telegram rate limit')
    async def send(self,uid,text,rows=None):
        return await self.call('sendMessage',{'chat_id':uid,'text':text[:4000],'parse_mode':'HTML',
            'reply_markup':markup(rows or BACK),'link_preview_options':{'is_disabled':True}})
    async def file(self,uid,content,name):
        form=aiohttp.FormData();form.add_field('chat_id',str(uid));form.add_field('document',content,filename=name,content_type='text/csv')
        return await self.call('sendDocument',form=form)

class Bot:
    def __init__(self,db,tg,admin): self.db,self.tg,self.admin=db,tg,admin
    async def screen(self,uid,section,text,rows=None):
        rows=rows or BACK
        fid=await self.db.setting('image_'+section)
        path=ROOT/'assets'/(section+'.png')
        if fid:
            try:
                return await self.tg.call('sendPhoto',{'chat_id':uid,'photo':fid,'caption':text[:1000],'parse_mode':'HTML','reply_markup':markup(rows)})
            except RuntimeError: pass
        if path.exists():
            form=aiohttp.FormData();form.add_field('chat_id',str(uid));form.add_field('caption',text[:1000]);form.add_field('parse_mode','HTML')
            form.add_field('reply_markup',json.dumps(markup(rows),ensure_ascii=False))
            form.add_field('photo',path.read_bytes(),filename=path.name,content_type='image/png')
            result=await self.tg.call('sendPhoto',form=form)
            if result.get('photo'):
                await self.db.run('insert into cxz_settings(name,value) values($1,$2) on conflict(name) do update set value=excluded.value','image_'+section,result['photo'][-1]['file_id'])
            return result
        return await self.tg.send(uid,text,rows)

    async def gate(self,uid):
        if await self.db.access(uid): return True
        await self.db.state(uid,{'step':'key'})
        await self.screen(uid,'activation','<b>Сначала активируйте доступ</b>\nОтправьте ключ одним сообщением. Получить ключ можно у администратора.')
        return False

    async def handle(self,update):
        callback=update.get('callback_query');msg=update.get('message') or (callback or {}).get('message',{})
        actor=(callback or msg).get('from',{})
        if not actor or msg.get('chat',{}).get('type')!='private': return
        uid=actor['id'];await self.db.touch(actor)
        user=await self.db.user(uid)
        if callback:
            with contextlib.suppress(RuntimeError): await self.tg.call('answerCallbackQuery',{'callback_query_id':callback['id']})
        if user['blocked']:
            await self.tg.send(uid,'Доступ заблокирован администратором.');return
        data=callback.get('data','') if callback else ''
        text=msg.get('text','').strip()
        request_id=update['update_id']
        if text in ('/start','/menu','/cancel') or data=='home':
            await self.db.state(uid,{})
            await self.screen(uid,'home','<b>CXZ · Поиск бизнесов</b>\nКомпании, у которых в Яндекс Картах не обнаружены сайт и онлайн-запись.\n\nВыберите действие.',home_rows(uid==self.admin));return
        if data=='help' or text=='/help':
            await self.screen(uid,'help','<b>Как работает</b>\nНиша → город или регион → 5 или 10 компаний.\n\nПроверяем публичные данные карточки Яндекса. Наличие сайта или записи исключает компанию. Это не доказательство отсутствия сайта во всём интернете.\n\nДо 50 новых компаний в сутки, сброс в 00:00 МСК. Если подходящих меньше, вернём найденное.\n\nАдминистратор видит профиль, поиски и результаты.');return
        if data=='activate':
            await self.db.state(uid,{'step':'key'})
            await self.screen(uid,'activation','<b>Активация доступа</b>\nОтправьте ваш ключ одним сообщением. Ключ привяжется к вашему Telegram-аккаунту.');return
        if data=='profile':
            used=await self.db.used(uid);access=await self.db.access(uid)
            await self.screen(uid,'profile',f'<b>{esc(user["name"])}</b>\nID: <code>{uid}</code>\nДоступ: {"активен" if access else "не активирован"}\nСегодня: {used}/50\nОсталось: {max(0,50-used)}\nПервый запуск: {date(user["created_at"])}');return
        if text.startswith('/admin') or data=='admin' or data.startswith('a:'):
            if uid!=self.admin: await self.tg.send(uid,'Раздел доступен только администратору.');return
            await self.admin_action(uid,data or 'admin',request_id);return
        if data=='search':
            if not await self.gate(uid): return
            await self.db.state(uid,{'step':'niche'})
            await self.niches(uid,0);return
        if data.startswith('npage:'):
            if await self.gate(uid): await self.niches(uid,int(data.split(':')[1]))
            return
        if data.startswith('niche:'):
            if not await self.gate(uid):return
            n=int(data.split(':')[1]);niche=NICHES[n] if 0<=n<len(NICHES) else ''
            if not niche:return
            await self.db.state(uid,{'step':'region','niche':niche})
            await self.tg.send(uid,f'Ниша: <b>{esc(niche)}</b>\nНапишите город или регион. Например: «Ростов-на-Дону» или «Ростовская область».');return
        if data=='custom':
            if await self.gate(uid):
                await self.db.state(uid,{'step':'niche'});await self.tg.send(uid,'Напишите свою нишу (2–80 символов).')
            return
        if data.startswith('count:'):
            if not await self.gate(uid):return
            state=(await self.db.user(uid))['state']
            if state.get('step')!='count': await self.tg.send(uid,'Эта кнопка устарела. Начните новый поиск.');return
            target=int(data.split(':')[1])
            jid,error=await self.db.create_job(uid,state['niche'],state['region'],target,request_id)
            await self.tg.send(uid,error or f'Поиск №{jid} добавлен в очередь.\n{esc(state["niche"])} · {esc(state["region"])}\nЦель: {target}. Результаты сохраняются по мере проверки.',
                               BACK if error else [[button('📊 Прогресс',f'job:{jid}'),button('⏹ Остановить',f'stop:{jid}')]]+BACK);return
        if data.startswith(('job:','stop:')):
            jid=int(data.split(':')[1]);j=await self.db.one('select * from cxz_jobs where id=$1 and user_id=$2',jid,uid)
            if not j: await self.tg.send(uid,'Поиск не найден.');return
            if data.startswith('stop:'):
                await self.db.run("update cxz_jobs set cancelled=true,status=case when status='queued' then 'cancelled' else status end where id=$1 and user_id=$2",jid,uid)
                await self.tg.send(uid,'Остановка запрошена. Уже найденные компании доступны в «Мои результаты».');return
            n=await self.db.one('select count(*) n from cxz_results where job_id=$1',jid)
            await self.tg.send(uid,f'<b>Поиск №{jid}</b>\n{esc(j["niche"])} · {esc(j["region"])}\nСтатус: {esc(status_name(j["status"]))}\nПроверено: {j["checked"]}\nПодходит: {n["n"]}/{j["target"]}\nНеполная проверка: {j["uncertain"]}\n{esc(j["detail"])}',[[button('Обновить',data),button('Остановить',f'stop:{jid}')]]+BACK);return
        if data.startswith(('results:','favorites:')):
            kind,offset=data.split(':');await self.list_results(uid,uid,int(offset),kind=='favorites');return
        if data=='history':
            jobs=await self.db.all('select * from cxz_jobs where user_id=$1 order by id desc limit 20',uid)
            await self.tg.send(uid,'<b>Последние поиски</b>',[[button(f'№{j["id"]} · {j["region"][:20]} · {status_name(j["status"])}',f'job:{j["id"]}')] for j in jobs]+BACK);return
        if data.startswith('card:'):
            await self.show_card(uid,int(data.split(':')[1]));return
        if data.startswith(('fav:','note:','stage:')):
            parts=data.split(':');rid=int(parts[1]);r=await self.db.result(rid,uid)
            if not r:await self.tg.send(uid,'Карточка недоступна.');return
            if parts[0]=='fav':
                # Explicit set, not toggle: redelivery is idempotent.
                await self.db.run('update cxz_results set favorite=$3 where id=$1 and user_id=$2',rid,uid,parts[2]=='1')
                await self.show_card(uid,rid)
            elif parts[0]=='note':
                await self.db.state(uid,{'step':'note','rid':rid});await self.tg.send(uid,'Напишите заметку до 1000 символов. «-» удалит заметку.')
            elif len(parts)==3:
                idx=int(parts[2])
                if 0<=idx<len(STAGES):await self.db.run('update cxz_results set stage=$3 where id=$1 and user_id=$2',rid,uid,STAGES[idx]);await self.show_card(uid,rid)
            else:await self.tg.send(uid,'Выберите статус:',[[button(s,f'stage:{rid}:{i}')] for i,s in enumerate(STAGES)]+BACK)
            return
        if data=='export': await self.export(uid,uid);return
        if not callback:
            state=user['state'];step=state.get('step')
            if step=='key':
                if not text or len(text)>100:await self.tg.send(uid,'Неверный формат ключа.');return
                await self.tg.send(uid,await self.db.activate(uid,text),home_rows(uid==self.admin));return
            if step=='niche':
                if not await self.gate(uid):return
                if not 2<=len(text)<=80:await self.tg.send(uid,'Ниша должна содержать от 2 до 80 символов.');return
                await self.db.state(uid,{'step':'region','niche':text});await self.tg.send(uid,'Теперь напишите город или регион.');return
            if step=='region':
                if not await self.gate(uid):return
                if not 2<=len(text)<=100:await self.tg.send(uid,'Введите город или регион (2–100 символов).');return
                await self.db.state(uid,{'step':'count','niche':state['niche'],'region':text})
                used=await self.db.used(uid);buttons=[button(str(n),f'count:{n}') for n in (5,10) if n<=50-used]
                await self.tg.send(uid,f'<b>{esc(state["niche"])}</b> · {esc(text)}\nСколько компаний найти?\nОстаток на сегодня: {50-used}.\nТерриторию ищем по введённому названию; адрес каждой компании будет в результате.',([buttons] if buttons else [])+BACK);return
            if step=='note':
                if len(text)>1000:await self.tg.send(uid,'Максимум 1000 символов.');return
                await self.db.run('update cxz_results set note=$3 where id=$1 and user_id=$2',state['rid'],uid,'' if text=='-' else text)
                await self.db.state(uid,{});await self.show_card(uid,state['rid']);return
            if uid==self.admin and step=='image':
                photo=msg.get('photo',[])
                if not photo:await self.tg.send(uid,'Отправьте картинку как фото.');return
                await self.db.run('insert into cxz_settings(name,value) values($1,$2) on conflict(name) do update set value=excluded.value','image_'+state['section'],photo[-1]['file_id'])
                await self.db.state(uid,{});await self.tg.send(uid,'Картинка обновлена.');return
            if uid==self.admin and step=='lookup':
                if not text.isdigit():await self.tg.send(uid,'Нужен числовой Telegram ID.');return
                await self.db.state(uid,{});await self.admin_user(uid,int(text));return
        await self.tg.send(uid,'Выберите действие в меню.',home_rows(uid==self.admin))

    async def niches(self,uid,page):
        page=max(0,min(4,page));start=page*6
        rows=[[button(n,f'niche:{i}')] for i,n in enumerate(NICHES[start:start+6],start)]
        rows.append([button('Своя ниша','custom')]);rows.append([button('←',f'npage:{max(0,page-1)}'),button('→',f'npage:{min(4,page+1)}')])
        await self.screen(uid,'search',f'<b>Выберите нишу</b>\nСтраница {page+1}/5',rows+BACK)

    async def list_results(self,recipient,owner,offset,favorites=False):
        offset=max(0,offset)
        rows=await self.db.all('select * from cxz_results where user_id=$1 and (not $2::boolean or favorite) order by id desc limit 9 offset $3',owner,favorites,offset)
        prefix='favorites' if favorites else 'results'
        admin_view=recipient!=owner
        buttons=[]
        for r in rows[:8]:
            buttons.append([button(('⭐ ' if r['favorite'] else '')+r['name'][:45],f'a:card:{r["id"]}' if admin_view else f'card:{r["id"]}')])
        nav=[];tag=f'a:results:{owner}' if admin_view else prefix
        if offset:nav.append(button('←',f'{tag}:{max(0,offset-8)}'))
        if len(rows)>8:nav.append(button('→',f'{tag}:{offset+8}'))
        if nav:buttons.append(nav)
        if not admin_view:buttons.append([button('Скачать CSV','export'),button('История поисков','history')])
        else:buttons.append([button('Скачать CSV',f'a:export:{owner}')])
        await self.screen(recipient,'favorites' if favorites else 'results',('<b>Избранное</b>' if favorites else '<b>Результаты</b>')+(' пользователя '+str(owner) if admin_view else '')+(' · пока пусто' if not rows else ''),buttons+BACK)

    async def show_card(self,uid,rid,admin=False):
        r=await self.db.one('select * from cxz_results where id=$1',rid) if admin and uid==self.admin else await self.db.result(rid,uid)
        if not r:await self.tg.send(uid,'Карточка недоступна.');return
        rows=[[{'text':'Открыть Яндекс Карты','url':r['url']}]]
        if not admin:rows += [[button('Убрать из избранного' if r['favorite'] else '⭐ В избранное',f'fav:{rid}:{0 if r["favorite"] else 1}')],
                             [button('✏️ Заметка',f'note:{rid}'),button('Статус',f'stage:{rid}')]]
        await self.tg.send(uid,f'<b>{esc(r["name"])}</b>\n{esc(r["address"])}\nТелефон: {esc(r["phone"]) or "не указан"}\n\nСайт и онлайн-запись не обнаружены в данных карточки Яндекса.\nПроверено: {date(r["checked_at"])}\nСтатус: {esc(r["stage"])}\nЗаметка: {esc(r["note"]) or "—"}',rows+BACK)

    async def export(self,recipient,owner):
        rows=await self.db.all('select * from cxz_results where user_id=$1 order by id',owner)
        buf=io.StringIO();writer=csv.writer(buf,delimiter=';')
        writer.writerow(['Название','Адрес','Телефон','Яндекс Карты','Проверено МСК','Избранное','Статус','Заметка'])
        for r in rows:writer.writerow([spreadsheet_cell(v) for v in [r['name'],r['address'],r['phone'],r['url'],date(r['checked_at']),r['favorite'],r['stage'],r['note']]])
        await self.tg.file(recipient,buf.getvalue().encode('utf-8-sig'),f'cxz-results-{owner}.csv')

    async def admin_action(self,uid,data,request_id):
        if data=='admin':
            await self.screen(uid,'admin','<b>Управление CXZ</b>',[[button('📊 Статистика','a:stats')],[button('👥 Пользователи','a:users:0'),button('Найти по ID','a:lookup')],
                [button('🔑 Создать ключ','a:newkey'),button('Список ключей','a:keys:0')],[button('🖼 Картинки','a:images')]]+BACK);return
        p=data.split(':');action=p[1]
        if action=='stats':
            s=await self.db.one(f'''select count(*) total,count(*) filter(where created_at>={DAY}) fresh,
                count(*) filter(where last_seen>={DAY}) today,count(*) filter(where last_seen>now()-interval '7 days') week,
                count(*) filter(where activated_at is not null) activated,count(*) filter(where activated_at is null) guests from cxz_users''')
            j=await self.db.one("select count(*) total,count(*) filter(where status in ('running','queued')) active from cxz_jobs")
            r=await self.db.one('select count(*) n from cxz_results')
            await self.tg.send(uid,f'<b>Статистика · МСК</b>\nПользователей: {s["total"]}\nНовых сегодня: {s["fresh"]}\nАктивны сегодня: {s["today"]}\nЗа 7 дней: {s["week"]}\nКогда-либо активировали ключ: {s["activated"]}\nБез активации: {s["guests"]}\nПоисков: {j["total"]} (в работе {j["active"]})\nСохранено компаний: {r["n"]}\nАдминистратор также учитывается в посетителях.',[[button('← Админка','admin')]]);return
        if action=='users':
            offset=max(0,int(p[2]));users=await self.db.all('select * from cxz_users order by created_at desc limit 11 offset $1',offset)
            rows=[[button(f'{u["id"]} · {u["name"][:20]}',f'a:user:{u["id"]}')] for u in users[:10]]
            nav=[]
            if offset:nav.append(button('←',f'a:users:{max(0,offset-10)}'))
            if len(users)>10:nav.append(button('→',f'a:users:{offset+10}'))
            await self.tg.send(uid,'<b>Пользователи</b>',rows+([nav] if nav else [])+[[button('Админка','admin')]]);return
        if action=='lookup':await self.db.state(uid,{'step':'lookup'});await self.tg.send(uid,'Введите Telegram ID пользователя.');return
        if action=='user':await self.admin_user(uid,int(p[2]));return
        if action=='results':await self.list_results(uid,int(p[2]),int(p[3]));return
        if action=='card':await self.show_card(uid,int(p[2]),True);return
        if action=='export':await self.export(uid,int(p[2]));return
        if action=='block':
            target=int(p[2])
            if target==self.admin:await self.tg.send(uid,'Администратора блокировать нельзя.');return
            await self.db.run('update cxz_users set blocked=$2 where id=$1',target,p[3]=='1')
            if p[3]=='1':await self.db.run("update cxz_jobs set cancelled=true where user_id=$1 and status in ('queued','running')",target)
            await self.admin_user(uid,target);return
        if action=='newkey':
            key=await self.db.create_key(request_id)
            await self.tg.send(uid,f'<b>Новый одноразовый ключ</b>\n<code>{key}</code>\nДоступ без срока, пока не отозван. Скопируйте ключ: полный текст повторно не показывается.',[[button('Список ключей','a:keys:0'),button('Админка','admin')]]);return
        if action=='keys':
            offset=max(0,int(p[2]));keys=await self.db.all('select * from cxz_keys order by id desc limit 11 offset $1',offset)
            lines=[];rows=[]
            for k in keys[:10]:
                lines.append(f'#{k["id"]} {esc(k["label"])} · {"отозван" if k["revoked"] else k["used_by"] or "свободен"}')
                if not k['revoked']:rows.append([button(f'Отозвать #{k["id"]}',f'a:revoke:{k["id"]}')])
            nav=[]
            if offset:nav.append(button('←',f'a:keys:{max(0,offset-10)}'))
            if len(keys)>10:nav.append(button('→',f'a:keys:{offset+10}'))
            await self.tg.send(uid,'<b>Ключи</b>\n'+'\n'.join(lines),rows+([nav] if nav else [])+[[button('Админка','admin')]]);return
        if action=='revoke':
            await self.db.run('update cxz_keys set revoked=true where id=$1',int(p[2]));await self.tg.send(uid,'Ключ отозван.',[[button('Админка','admin')]]);return
        if action=='images':
            await self.tg.send(uid,'Выберите картинку для замены:',[[button(label,f'a:image:{key}')] for key,label in SECTIONS.items()]+BACK);return
        if action=='image' and p[2] in SECTIONS:
            await self.db.state(uid,{'step':'image','section':p[2]});await self.tg.send(uid,'Пришлите новую картинку как фото.');return

    async def admin_user(self,uid,target):
        u=await self.db.user(target)
        if not u:await self.tg.send(uid,'Такой пользователь ещё не запускал бота.');return
        used=await self.db.used(target)
        jobs=await self.db.all('select * from cxz_jobs where user_id=$1 order by id desc limit 5',target)
        text=f'<b>{esc(u["name"])}</b> @{esc(u["username"])}\nID: {target}\nПервый вход: {date(u["created_at"])}\nПоследнее действие: {date(u["last_seen"])}\nАктивация: {date(u["activated_at"])}\nДоступ сейчас: {await self.db.access(target)}\nСегодня: {used}/50\n\nПоследние поиски:\n'
        text+='\n'.join(f'#{j["id"]} {esc(j["niche"])} / {esc(j["region"])} · {date(j["created_at"])} · {status_name(j["status"])}' for j in jobs)
        await self.tg.send(uid,text,[[button('Результаты',f'a:results:{target}:0')],[button('Разблокировать' if u['blocked'] else 'Заблокировать',f'a:block:{target}:{0 if u["blocked"] else 1}')],[button('Админка','admin')]])

async def inbox_worker(bot):
    while True:
        try:
            row=await bot.db.one('''update cxz_inbox set status='running',attempts=attempts+1,lease_until=now()+interval '90 seconds'
                where id=(select id from cxz_inbox where status='queued' or (status='running' and lease_until<now())
                    order by id for update skip locked limit 1) returning *''')
            if not row:await asyncio.sleep(1);continue
            try:
                await asyncio.wait_for(bot.handle(row['payload']),timeout=75)
                await bot.db.run("update cxz_inbox set status='done',payload=null where id=$1",row['id'])
            except Exception as e:
                LOG.warning('Update handling failed (%s), attempt %s',type(e).__name__,row['attempts'])
                if row['attempts']>=3:
                    await bot.db.run("update cxz_inbox set status='failed',payload=null where id=$1",row['id'])
                    payload=row['payload'];msg=payload.get('message') or payload.get('callback_query',{}).get('message',{})
                    uid=msg.get('chat',{}).get('id')
                    if uid:
                        with contextlib.suppress(Exception):await bot.tg.send(uid,'Не удалось выполнить действие. Попробуйте снова через меню.')
                else:
                    await bot.db.run("update cxz_inbox set status='queued' where id=$1",row['id']);await asyncio.sleep(3)
        except asyncio.CancelledError: raise
        except Exception as e:
            LOG.warning('Inbox unavailable (%s)',type(e).__name__);await asyncio.sleep(5)

async def heartbeat(db,jid):
    while True:
        await asyncio.sleep(25)
        await db.run("update cxz_jobs set lease_until=now()+interval '3 minutes',updated_at=now() where id=$1 and status='running'",jid)

async def run_search(bot,j):
    db=bot.db;jid=j['id'];uid=j['user_id']
    if not await db.access(uid):
        await db.run("update cxz_jobs set status='cancelled',detail='Доступ отключён',updated_at=now() where id=$1",jid);return
    async with YandexParser() as parser:
        candidates=j['candidates']
        if candidates is None:
            candidates=await parser.discover(j['niche'],j['region'])
            await db.run('update cxz_jobs set candidates=$2,updated_at=now() where id=$1',jid,candidates)
        for idx in range(j['cursor'],len(candidates)):
            current=await db.one('select * from cxz_jobs where id=$1',jid)
            if current['cancelled'] or not await db.access(uid):
                await db.run("update cxz_jobs set status='cancelled',detail='Поиск остановлен',updated_at=now() where id=$1",jid);return
            n=await db.one('select count(*) n from cxz_results where job_id=$1',jid)
            if n['n']>=j['target'] or await db.used(uid)>=DAILY_LIMIT:break
            url=candidates[idx];oid=org_id(url)
            existing=await db.one('select id from cxz_results where user_id=$1 and org_id=$2',uid,oid)
            result='duplicate'
            if not existing:
                card=await parser.inspect(url);result=classify(card)
                if result=='blocked':raise MapUnavailable('Карта ограничила доступ.')
                if result=='accept':await db.accept(j,card,url,oid)
            await db.run('''update cxz_jobs set cursor=$2,checked=checked+1,rejected=rejected+$3,uncertain=uncertain+$4,
                           updated_at=now() where id=$1''',jid,idx+1,int(result=='reject'),int(result=='unknown'))
            await asyncio.sleep(3)
        n=await db.one('select count(*) n from cxz_results where job_id=$1',jid)
        detail='Нужное количество найдено.' if n['n']>=j['target'] else 'Доступные кандидаты проверены или достигнут лимит. Выдано столько, сколько удалось подтвердить.'
        await db.run("update cxz_jobs set status='done',detail=$2,updated_at=now() where id=$1",jid,detail)

async def search_worker(bot):
    while True:
        try:
            # Durable rows make partial results immediately visible; no repeated quota debit on notification.
            await bot.db.run("update cxz_jobs set status='cancelled',detail='Поиск остановлен' where status='queued' and cancelled")
            j=await bot.db.claim_job()
            if j:
                beat=asyncio.create_task(heartbeat(bot.db,j['id']))
                work=asyncio.create_task(asyncio.wait_for(run_search(bot,j),timeout=900))
                try:
                    finished,_=await asyncio.wait([beat,work],return_when=asyncio.FIRST_COMPLETED)
                    if beat in finished:
                        work.cancel();await beat
                    else:await work
                except MapUnavailable as e:
                    await bot.db.run("update cxz_jobs set status='blocked',detail=$2,updated_at=now() where id=$1",j['id'],str(e))
                except asyncio.TimeoutError:
                    await bot.db.run("update cxz_jobs set status='partial',detail='Достигнут лимит времени 15 минут. Найденное сохранено.',updated_at=now() where id=$1",j['id'])
                except asyncio.CancelledError: raise
                except Exception as e:
                    LOG.warning('Search failed (%s)',type(e).__name__)
                    # If DB is unavailable leave lease for a restart; never mark a failed read as a lead.
                    await bot.db.run("update cxz_jobs set status='error',detail='Поиск прерван технической ошибкой. Найденное сохранено.',updated_at=now() where id=$1",j['id'])
                finally:
                    beat.cancel();work.cancel()
                    for task in (beat,work):
                        with contextlib.suppress(asyncio.CancelledError,Exception):await task
            notices=await bot.db.all("select * from cxz_jobs where status not in ('queued','running') and not notified order by id limit 5")
            for notice in notices:
                n=await bot.db.one('select count(*) n from cxz_results where job_id=$1',notice['id'])
                try:
                    await bot.tg.send(notice['user_id'],f'<b>Поиск №{notice["id"]} завершён</b>\nНайдено: {n["n"]}/{notice["target"]}\nПроверено: {notice["checked"]}\n{esc(notice["detail"])}',[[button('📂 Результаты','results:0')]]+BACK)
                    await bot.db.run('update cxz_jobs set notified=true where id=$1',notice['id'])
                except RuntimeError:
                    # User may have blocked the bot: do not flood Telegram with retries.
                    await bot.db.run('update cxz_jobs set notified=true where id=$1',notice['id'])
            await asyncio.sleep(2)
        except asyncio.CancelledError: raise
        except Exception as e:LOG.warning('Search worker unavailable (%s)',type(e).__name__);await asyncio.sleep(5)

async def webhook(request):
    if not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token',''),request.app['secret']):
        raise web.HTTPForbidden()
    try:
        payload=await request.json()
        update_id=payload['update_id']
        if not isinstance(update_id,int):raise ValueError()
    except (ValueError,KeyError,TypeError):raise web.HTTPBadRequest()
    try:
        await request.app['bot'].db.run('insert into cxz_inbox(id,payload) values($1,$2) on conflict(id) do nothing',update_id,payload)
    except Exception:raise web.HTTPServiceUnavailable(text='Storage unavailable')
    return web.json_response({'ok':True})

async def health(request): return web.json_response({'ok':True})

async def lifecycle(app):
    required=['BOT_TOKEN','DATABASE_URL','WEBHOOK_SECRET','KEY_PEPPER']
    missing=[n for n in required if not os.environ.get(n)]
    if missing:raise RuntimeError('Set environment variables: '+', '.join(missing))
    if min(len(os.environ['WEBHOOK_SECRET']),len(os.environ['KEY_PEPPER']))<32:
        raise RuntimeError('WEBHOOK_SECRET and KEY_PEPPER must contain at least 32 characters')
    admin=int(os.getenv('ADMIN_ID',str(ADMIN_DEFAULT)))
    db=await Store.connect(os.environ['DATABASE_URL'],os.environ['KEY_PEPPER'],admin)
    # Fail clearly at startup if migration was not run.
    await db.one('select id from cxz_users limit 1')
    session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25),trust_env=True)
    tg=Telegram(session,os.environ['BOT_TOKEN']);bot=Bot(db,tg,admin)
    app['bot']=bot;app['secret']=os.environ['WEBHOOK_SECRET']
    public=os.getenv('PUBLIC_URL') or os.getenv('RENDER_EXTERNAL_URL')
    if not public or not public.startswith('https://'):raise RuntimeError('PUBLIC_URL must be the HTTPS Render service address')
    await tg.call('setWebhook',{'url':public.rstrip('/')+'/telegram','secret_token':app['secret'],
        'allowed_updates':['message','callback_query'],'max_connections':2,'drop_pending_updates':False})
    await tg.call('setMyCommands',{'commands':[{'command':'start','description':'Главное меню'},{'command':'help','description':'Как работает'},{'command':'cancel','description':'Отменить ввод и открыть меню'}]})
    tasks=[asyncio.create_task(inbox_worker(bot)),asyncio.create_task(search_worker(bot))]
    LOG.info('CXZ ready')
    try:yield
    finally:
        for t in tasks:t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):await t
        await session.close();await db.pool.close()

def create_app():
    app=web.Application(client_max_size=1024*1024)
    app.router.add_get('/health',health);app.router.add_post('/telegram',webhook)
    app.cleanup_ctx.append(lifecycle)
    return app

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    # Access logs disabled to avoid accidental sensitive request logging.
    web.run_app(create_app(),host='0.0.0.0',port=int(os.getenv('PORT','10000')),access_log=None)
