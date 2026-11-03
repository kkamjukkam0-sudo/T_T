"""
우리 집 냉장고 — 단일 파일 Streamlit 앱

[실행]
    pip install -r requirements.txt
    streamlit run app.py

[GitHub / Streamlit Community Cloud]
1. app.py와 requirements.txt를 저장소 최상단에 업로드합니다.
2. Community Cloud에서 저장소를 연결하고 main file을 app.py로 지정합니다.
3. 실제 가족 공유에는 Supabase가 필요합니다. 미연결 시 SQLite 로컬 체험입니다.
   Cloud의 로컬 파일은 재시작/재배포 시 없어질 수 있습니다.
4. Supabase SQL Editor에서 아래 SQL을 실행합니다.

create table if not exists public.fridge_records (
  id uuid primary key,
  kind text not null check (kind in ('item', 'shopping')),
  payload jsonb not null
);
create index if not exists fridge_records_kind_idx
  on public.fridge_records(kind);
alter table public.fridge_records enable row level security;
revoke all on public.fridge_records from anon, authenticated;
grant all on public.fridge_records to service_role;

5. Streamlit 배포 설정의 Secrets에 아래 값을 실제 값으로 입력합니다.
   키와 비밀번호를 이 코드나 GitHub에 직접 적지 마세요.

APP_PASSWORD = "10자-이상의-추측하기-어려운-가족-비밀번호"
SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "본인의 서버용 service_role 키"
APP_URL = "실제 HTTPS 앱 주소 (배포 후 입력)"

6. 배포 후 QR / 설정 화면에 실제 앱 주소를 넣고 QR을 다운로드합니다.
   GitHub 저장소 주소가 아니라 배포된 앱 주소를 사용합니다.
7. 다른 휴대폰에서 QR → 비밀번호 → 등록 → 공유/재시작 후 유지 확인을 합니다.

[주의]
- 한 가구용입니다. 공용 비밀번호 방식이며 개인별 권한/로그인 시도 제한은 없습니다.
- 사진은 축소 JPEG의 base64로 DB에 저장합니다. 소규모 사용을 위한 구성입니다.
- 사진/날짜로 부패나 섭취 안전성을 판정하지 않습니다. 상태는 직접 기록합니다.
- 기한 미입력 항목은 마지막에 정렬하며 날짜를 추정하지 않습니다.
- 문 열기는 화면 전환 방식이며 3D 회전 애니메이션은 아닙니다.
- 기한과 기본 구매일의 오늘 날짜는 서버 날짜 기준입니다.
- 같은 항목 동시 수정은 마지막 저장이 반영됩니다.
- 소진/장보기는 두 단계 저장이며 부분 실패 시 재시도 가능합니다.
- 카메라가 안 되면 HTTPS/카메라 권한을 확인하거나 사진 업로드를 사용합니다.
- 업로드와 촬영 중 하나만 사진을 넣고 저장하세요.
- data/fridge.db와 실제 secrets.toml은 GitHub에 올리지 마세요.
"""

import base64
import io
import json
import sqlite3
import uuid
from datetime import date
from pathlib import Path

import requests
from PIL import Image, ImageOps

ZONES = {'냉장실': ['상단 선반', '중단 선반', '채소칸', '문쪽 수납칸'], '냉동실': ['상단 냉동칸', '하단 냉동칸'], '실온': ['식품 보관함']}
STATES = ['괜찮아요', '시들었어요', '곰팡이/이상 있어요', '확인 필요']


def remaining(item, today=None):
    return (date.fromisoformat(item['expiry']) - (today or date.today())).days if item.get('expiry') else None


def urgency(item, today=None):
    days = remaining(item, today)
    return (days is None, days if days is not None else 999999, item.get('name', ''))


def expiry_label(item, today=None):
    n = remaining(item, today)
    if n is None:
        return '기한 미입력 · 포장지 확인'
    if n < 0:
        return f'표시 기한 {abs(n)}일 지남 · 상태 확인'
    if n == 0:
        return '표시 기한 오늘 · 상태 확인'
    return f'표시 기한까지 {n}일'


def photo_encode(file):
    if not file:
        return None
    if file.size > 10 * 1024 * 1024:
        raise ValueError('사진은 10MB 이하로 올려 주세요.')
    with Image.open(file) as im:
        if im.width * im.height > 30_000_000:
            raise ValueError('사진 해상도가 너무 큽니다. 작은 사진을 올려 주세요.')
        im = ImageOps.exif_transpose(im).convert('RGB')
        im.thumbnail((1000, 1000))
        out = io.BytesIO()
        im.save(out, 'JPEG', quality=78)
    return base64.b64encode(out.getvalue()).decode('ascii')


class Store:
    def __init__(self, config):
        self.url = str(config.get('SUPABASE_URL', '')).rstrip('/')
        self.key = str(config.get('SUPABASE_SERVICE_ROLE_KEY', ''))
        if bool(self.url) != bool(self.key):
            raise ValueError('Supabase URL과 서버용 키를 모두 설정해 주세요.')
        self.cloud = bool(self.url and self.key)
        if self.cloud:
            if not self.url.startswith('https://'):
                raise ValueError('Supabase URL은 HTTPS여야 합니다.')
            self.headers = {'apikey': self.key, 'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json'}
        else:
            Path('data').mkdir(exist_ok=True)
            with sqlite3.connect('data/fridge.db') as con:
                con.execute('CREATE TABLE IF NOT EXISTS fridge_records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)')

    def _request(self, method, **kwargs):
        r = requests.request(method, self.url + '/rest/v1/fridge_records', headers=self.headers, timeout=25, **kwargs)
        r.raise_for_status()
        return r

    def all(self, kind):
        if self.cloud:
            rows, offset = [], 0
            while True:
                batch = self._request('GET', params={'kind': f'eq.{kind}', 'select': 'id,kind,payload', 'order': 'id.asc', 'limit': '100', 'offset': str(offset)}).json()
                rows.extend(batch)
                if len(batch) < 100:
                    break
                offset += len(batch)
            return [dict(row['payload'], id=row['id']) for row in rows]
        with sqlite3.connect('data/fridge.db') as con:
            return [dict(json.loads(row[1]), id=row[0]) for row in con.execute('SELECT id,payload FROM fridge_records WHERE kind=?', (kind,))]

    def put(self, kind, payload, record_id=None):
        rid = record_id or str(uuid.uuid4())
        clean = {k: v for k, v in payload.items() if k != 'id'}
        if self.cloud:
            headers = dict(self.headers, Prefer='resolution=merge-duplicates,return=minimal')
            r = requests.post(self.url + '/rest/v1/fridge_records', headers=headers, params={'on_conflict': 'id'}, json={'id': rid, 'kind': kind, 'payload': clean}, timeout=25)
            r.raise_for_status()
        else:
            with sqlite3.connect('data/fridge.db') as con:
                con.execute('INSERT INTO fridge_records VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,payload=excluded.payload', (rid, kind, json.dumps(clean, ensure_ascii=False)))
        return rid

    def delete(self, record_id):
        if self.cloud:
            self._request('DELETE', params={'id': f'eq.{record_id}'})
        else:
            with sqlite3.connect('data/fridge.db') as con:
                con.execute('DELETE FROM fridge_records WHERE id=?', (record_id,))

    def consume(self, item, shopping=False):
        # Stable shopping ID makes retries idempotent.
        if shopping:
            sid = str(uuid.uuid5(uuid.NAMESPACE_URL, 'fridge-shopping:' + item['id']))
            self.put('shopping', {'name': item['name'], 'quantity': item.get('quantity', ''), 'checked': False}, sid)
        self.put('item', dict(item, active=False, consumed=date.today().isoformat()), item['id'])


# ================= Streamlit 화면 =================
import base64
import hmac
import io
from datetime import date
from urllib.parse import urlparse

import qrcode
import streamlit as st

st.set_page_config(page_title='우리 집 냉장고', page_icon='🥬', layout='centered')
st.markdown('''<style>
.stApp {background:#f6f7f2;color:#213c33}
.block-container {max-width:880px;padding-top:2rem}
h1,h2,h3 {color:#213c33}
.stButton>button {border-radius:14px;min-height:48px;border-color:#b4c8bb}
.stButton>button[kind="primary"] {background:#2f6951;border-color:#2f6951;color:white}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius:20px}
.fridge-brand {letter-spacing:3px;font-size:12px;color:#4f655b;text-align:center;margin-bottom:10px}
.fridge-door {position:relative;box-sizing:border-box;border:2px solid #b6c3c7;background:linear-gradient(115deg,#f5f9f9,#d7e1e5 60%,#edf3f5);border-radius:22px 22px 8px 8px;min-height:140px;padding:28px 45px;box-shadow:inset 0 0 15px #ffffff,0 8px 18px #213c3310}
.fridge-door.large {border-radius:8px 8px 22px 22px;min-height:225px;margin-top:8px}
.handle {position:absolute;left:18px;top:30px;width:9px;height:65%;border-radius:8px;background:linear-gradient(90deg,#81969d,#f8ffff,#879ca3);box-shadow:2px 2px 4px #66777755}
.door-title {font-size:24px;font-weight:750;margin-top:12px}.door-sub {font-size:14px;color:#445a61;margin-top:10px}
.led {display:inline-block;background:#213c33;color:#c3f3c4;padding:5px 12px;border-radius:7px;font-size:12px;letter-spacing:1px}
@media(max-width:600px){.block-container{padding:1rem}.door-title{font-size:21px}}
</style>''', unsafe_allow_html=True)

try:
    config = dict(st.secrets)
except FileNotFoundError:
    config = {}

password = str(config.get('APP_PASSWORD', ''))
cloud_requested = bool(config.get('SUPABASE_URL') or config.get('SUPABASE_SERVICE_ROLE_KEY'))
if cloud_requested and len(password) < 10:
    st.error('공개 배포 전 APP_PASSWORD를 10자 이상으로 설정해 주세요. 데이터 보호를 위해 앱을 잠갔습니다.')
    st.stop()
if password and not st.session_state.get('authenticated'):
    st.title('🥬 우리 집 냉장고')
    st.caption('가족만 함께 보는 작은 식재료 보관함')
    with st.form('login'):
        supplied = st.text_input('가족 공용 비밀번호', type='password')
        submit = st.form_submit_button('냉장고 열기', type='primary')
    if submit:
        if hmac.compare_digest(supplied.encode(), password.encode()):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error('비밀번호를 확인해 주세요.')
    st.stop()

try:
    store = Store(config)
    items = store.all('item')
except Exception:
    st.error('저장소에 연결하지 못했습니다. Supabase 연결 정보와 SQL 설정을 확인한 후 새로고침해 주세요. 보안을 위해 연결 오류 상세는 화면에 표시하지 않습니다.')
    st.stop()


def save(kind, data, rid=None):
    try:
        store.put(kind, data, rid)
        return True
    except Exception:
        st.error('저장하지 못했습니다. 연결을 확인하고 다시 시도해 주세요.')
        return False


def remove(rid):
    try:
        store.delete(rid)
        st.rerun()
    except Exception:
        st.error('삭제하지 못했습니다. 다시 시도해 주세요.')


active = [x for x in items if x.get('active', True)]
if not store.cloud:
    st.warning('로컬 체험 모드입니다. Streamlit Cloud에서는 파일이 초기화될 수 있습니다. 실제 가족 공유에는 안내서대로 Supabase를 연결하세요.')
if st.session_state.pop('saved', False):
    st.success('저장했어요!')
st.title('우리 집 냉장고 🥬')
st.caption('문을 열고, 오늘 먹을 재료를 찾아보세요.')
c1, c2, c3 = st.columns(3)
c1.metric('보관 중', f'{len(active)}개')
c2.metric('기한 오늘~3일', f'{sum(remaining(x) is not None and 0 <= remaining(x) <= 3 for x in active)}개')
c3.metric('이상 상태 기록', f'{sum(x.get("state") == "곰팡이/이상 있어요" for x in active)}개')
st.caption('날짜는 포장지의 표시 기한입니다. 사진·날짜만으로 부패 여부나 섭취 안전성을 판정하지 않습니다.')
page = st.radio('메뉴', ['🧊 냉장고', '➕ 식재료 등록', '🛒 장보기', '🕓 소진 기록', '📱 QR / 설정'], horizontal=True, label_visibility='collapsed')


def editor(item=None):
    item = item or {}
    rid = item.get('id', 'new')
    with st.form('edit_' + rid):
        name = st.text_input('식재료 이름 *', value=item.get('name', ''), max_chars=60)
        quantity = st.text_input('수량 / 용량', value=item.get('quantity', ''), placeholder='예: 2개, 500g', max_chars=60)
        purchase = st.date_input('구매일', value=date.fromisoformat(item['purchase']) if item.get('purchase') else date.today(), max_value=date.today())
        known = st.checkbox('포장지에 표시된 기한이 있어요', value=bool(item.get('expiry')))
        dtype = st.selectbox('기한 종류', ['소비기한', '유통기한', '품질유지기한'], index=['소비기한', '유통기한', '품질유지기한'].index(item.get('date_type', '소비기한')))
        expiry = st.date_input('포장지의 날짜 (위 체크를 해제하면 저장하지 않아요)', value=date.fromisoformat(item['expiry']) if item.get('expiry') else date.today())
        st.caption('기한 미입력 시 날짜를 추정하지 않습니다. 유통기한을 소비기한으로 자동 변환하지 않습니다.')
        # Form widgets submit together; independent zone list avoids stale dependent selects.
        locations = [f'{room} / {zone}' for room, zones in ZONES.items() for zone in zones]
        current = f'{item.get("room", "냉장실")} / {item.get("zone", "중단 선반")}'
        location = st.selectbox('보관 위치', locations, index=locations.index(current) if current in locations else 0)
        state = st.selectbox('직접 확인한 상태', STATES, index=STATES.index(item.get('state', '확인 필요')))
        note = st.text_area('메모', value=item.get('note', ''), max_chars=500, placeholder='예: 개봉일, 보관 방법')
        st.caption('사진을 바꿀 때는 카메라 또는 업로드 중 하나만 선택하세요. 사진은 자동 축소되며 위치 등 EXIF 정보는 제거됩니다.')
        upload = st.file_uploader('사진 업로드 (JPG / PNG / WEBP, 최대 10MB)', type=['jpg', 'jpeg', 'png', 'webp'], key='upload_' + rid)
        camera = st.camera_input('카메라 촬영', key='camera_' + rid)
        clear = st.checkbox('기존 사진 삭제', key='clear_' + rid) if item.get('photo') else False
        submitted = st.form_submit_button('식재료 저장', type='primary')
    if submitted:
        if not name.strip():
            st.error('식재료 이름을 입력해 주세요.')
            return
        try:
            photo = None if clear else item.get('photo')
            if camera and upload:
                st.error('촬영 사진과 업로드 사진 중 하나만 남겨 주세요.')
                return
            chosen = camera or upload
            if chosen:
                photo = photo_encode(chosen)
        except Exception:
            st.error('사진을 읽지 못했습니다. 10MB 이하의 JPG·PNG·WEBP 사진으로 다시 시도해 주세요.')
            return
        room, zone = location.split(' / ')
        payload = dict(item, name=name.strip(), quantity=quantity.strip(), purchase=purchase.isoformat(), expiry=expiry.isoformat() if known else None, date_type=dtype, room=room, zone=zone, state=state, note=note, photo=photo, active=True, checked_at=date.today().isoformat())
        if save('item', payload, item.get('id')):
            st.session_state.saved = True
            st.rerun()


def card(item):
    rid = item['id']
    with st.container(border=True):
        if item.get('photo'):
            st.image(base64.b64decode(item['photo']), width=220)
        st.subheader(item['name'])
        st.write(f'{item.get("quantity", "")} · {item["room"]} / {item["zone"]}')
        st.caption(f'구매일 {item["purchase"]} | {item.get("date_type", "소비기한")} {item.get("expiry") or "미입력"}')
        st.write(expiry_label(item))
        state = item.get('state', '확인 필요')
        if state == '곰팡이/이상 있어요':
            st.error('이상 상태가 기록되어 있어요. 섭취하지 말고 식품별 안전 지침에 따라 처리하세요.')
        else:
            st.info(f'직접 기록한 상태: {state} · 마지막 기록 {item.get("checked_at", "미기록")}')
        if item.get('note'):
            st.write(item['note'])
        shop = st.checkbox('장보기 목록에도 추가', key='shop_' + rid)
        if st.button('✓ 다 먹었어요', key='consume_' + rid, use_container_width=True):
            try:
                store.consume(item, shop)
                st.rerun()
            except Exception:
                st.error('처리를 완료하지 못했습니다. 다시 시도해 주세요. 장보기 항목이 먼저 추가됐을 수 있으나 재시도 시 중복 생성하지 않습니다.')
        with st.expander('수정 / 상태 기록 / 삭제'):
            editor(item)
            confirmed = st.checkbox('이 식재료와 사진을 영구 삭제합니다', key='confirm_' + rid)
            if st.button('삭제', key='delete_' + rid, disabled=not confirmed):
                remove(rid)


if page == '🧊 냉장고':
    opened = st.session_state.get('door_open', False)
    if not opened:
        st.markdown('<div class="fridge-brand">OUR LITTLE FRESH HOME</div><div class="fridge-door"><div class="handle"></div><span class="led">FREEZER</span><div class="door-title">❄️ 냉동실</div></div>', unsafe_allow_html=True)
        if st.button('냉동실 문 열기 ↗', use_container_width=True):
            st.session_state.door_open = True
            st.session_state.room = '냉동실'
            st.rerun()
        st.markdown('<div class="fridge-door large"><div class="handle"></div><span class="led">FRESH HOME</span><div class="door-title">🥬 우리 집 냉장고</div><div class="door-sub">오늘은 어떤 재료가 들어 있을까요?</div></div>', unsafe_allow_html=True)
        if st.button('냉장실 문 열기 ↗', type='primary', use_container_width=True):
            st.session_state.door_open = True
            st.session_state.room = '냉장실'
            st.rerun()
        if st.button('🧺 실온 식품 보관함 열기', use_container_width=True):
            st.session_state.door_open = True
            st.session_state.room = '실온'
            st.rerun()
    else:
        if st.button('← 냉장고 문 닫기'):
            st.session_state.door_open = False
            st.rerun()
        room = st.radio('보관 장소', list(ZONES), key='room', horizontal=True)
        st.caption('칸을 누르면 해당 위치의 식재료가 아래에 표시됩니다. 숫자는 보관 개수예요.')
        picked = st.session_state.get('zone', '전체')
        if picked not in ['전체'] + ZONES[room]:
            picked = '전체'
        if st.button('🔎 이 공간 전체 보기', use_container_width=True):
            picked = '전체'
            st.session_state.zone = picked
        with st.container(border=True):
            # Main cabinet and door rack: native clickable buttons preserve mobile accessibility.
            main, door = st.columns([2, 1])
            for zone in ZONES[room]:
                target = door if zone == '문쪽 수납칸' else main
                n = sum(x['room'] == room and x['zone'] == zone for x in active)
                emoji = '🥬' if zone == '채소칸' else ('🥛' if zone == '문쪽 수납칸' else '🧊' if room == '냉동실' else '🥣')
                with target:
                    if st.button(f'{emoji} {zone}\n({n}개)', key='zone_' + zone, type='primary' if picked == zone else 'secondary', use_container_width=True):
                        st.session_state.zone = zone
                        st.rerun()
                    st.divider()
        query = st.text_input('이름으로 검색', placeholder='예: 우유, 당근', max_chars=60)
        st.subheader(f'{room} · {picked}')
        shown = sorted([x for x in active if x['room'] == room and (picked == '전체' or x['zone'] == picked) and query.casefold() in x['name'].casefold()], key=urgency)
        if not shown:
            st.info('아직 식재료가 없어요. 상단의 식재료 등록 메뉴에서 추가해 주세요.')
        for item in shown:
            card(item)
        st.divider()
        with st.expander('⏳ 모든 보관 장소 · 기한 가까운 순서'):
            for item in sorted(active, key=urgency):
                st.write(f'{item["name"]} · {item["room"]}/{item["zone"]} · {expiry_label(item)}')
elif page == '➕ 식재료 등록':
    st.subheader('새 식재료를 넣어 주세요')
    editor()
elif page == '🛒 장보기':
    st.subheader('다음 장볼 때 챙겨요')
    with st.form('shopping_add', clear_on_submit=True):
        name = st.text_input('살 물건', max_chars=60)
        qty = st.text_input('수량', max_chars=60)
        if st.form_submit_button('목록에 추가'):
            if name.strip() and save('shopping', {'name': name.strip(), 'quantity': qty.strip(), 'checked': False}):
                st.rerun()
    try:
        shopping = store.all('shopping')
    except Exception:
        st.error('장보기 목록을 불러오지 못했습니다.')
        st.stop()
    if not shopping:
        st.info('장보기 목록이 비어 있어요.')
    for row in shopping:
        with st.container(border=True):
            checked = st.checkbox(f'{row["name"]} {row.get("quantity", "")}', value=row.get('checked', False), key='bought_' + row['id'])
            if checked != row.get('checked', False):
                if save('shopping', dict(row, checked=checked), row['id']):
                    st.rerun()
            if st.button('목록에서 지우기', key='del_shop_' + row['id']):
                remove(row['id'])
elif page == '🕓 소진 기록':
    st.subheader('다 먹은 식재료')
    archived = sorted([x for x in items if not x.get('active', True)], key=lambda x: x.get('consumed', ''), reverse=True)
    if not archived:
        st.info('아직 소진 기록이 없어요.')
    for x in archived:
        with st.container(border=True):
            st.write(f'{x["name"]} · 소진일 {x.get("consumed", "")}')
            if st.button('다시 보관 중으로 복원', key='restore_' + x['id']):
                if save('item', dict(x, active=True, consumed=None), x['id']):
                    st.rerun()
            confirmed = st.checkbox('기록과 사진을 영구 삭제', key='archive_confirm_' + x['id'])
            if st.button('기록 삭제', key='archive_delete_' + x['id'], disabled=not confirmed):
                remove(x['id'])
else:
    st.subheader('냉장고에 붙일 QR')
    url = st.text_input('배포한 앱의 HTTPS 주소', value=str(config.get('APP_URL', '')), placeholder='Streamlit에 배포한 실제 주소 입력')
    parsed = urlparse(url.strip())
    if url:
        if parsed.scheme == 'https' and parsed.netloc and not parsed.username and not parsed.password:
            img = qrcode.make(url.strip())
            out = io.BytesIO()
            img.save(out, format='PNG')
            st.image(out.getvalue(), width=240)
            st.download_button('QR 이미지 다운로드', out.getvalue(), file_name='our-fridge-qr.png', mime='image/png')
            st.caption('QR에는 앱 주소만 담깁니다. 비밀번호는 포함하지 않습니다.')
        else:
            st.warning('올바른 HTTPS 앱 주소를 입력해 주세요.')
    st.info('휴대폰에서 QR 스캔 → 앱 접속 → 가족 비밀번호 입력 → 냉장고 문 열기')
    st.write('저장 방식: ' + ('Supabase · 가족 공유' if store.cloud else 'SQLite · 로컬 체험'))
    st.caption('가족이 동시에 같은 항목을 수정하면 마지막 저장이 반영됩니다. 이 앱은 한 가구용입니다.')
    if password and st.button('로그아웃'):
        st.session_state.clear()
        st.rerun()
