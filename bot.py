import discord
from discord import app_commands, ui, Webhook
from discord.ext import commands
import math
import asyncio
from aiohttp import web, ClientSession
import os
import random
import json
import time
import sys
import io
import shutil
from datetime import datetime
from jinja2 import Environment, FileSystemLoader

# --- การตั้งค่าเบื้องต้น ---
TOKEN = os.getenv("DISCORD_TOKEN")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASS", "admin1234") 
LOG_WEBHOOK_URL = os.getenv("LOG_WEBHOOK_URL", "") 
DEFAULT_RANGE = 10 #ระยะเริ่มต้น
DATA_FILE = "server_data.json"
MOVE_COOLDOWN = 3.0 #คูลดาวน์การย้าย

ALLOWED_GUILD_IDS = {1510209557761359922} #ไอดีเซิฟ discord
OWNER_USER_IDS = {933529869487321161} #ไอดีเจ้าของ

server_data = {}  
game_state = {}   
user_last_move = {}
testing_guilds = set()
DYNAMIC_RANGE = DEFAULT_RANGE
active_call_groups = []
active_call_lookup = {}
zones_state = {}
random_call_opt_in = {}
audio_state = {}
room_sessions = {}

# --- ระบบจัดเก็บข้อมูล ---
def load_data():
    global server_data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                temp_data = {int(k): v for k, v in raw_data.items()}
                
                for gid, gdata in temp_data.items():
                    if 'zones' not in gdata:
                        gdata['zones'] = {}
                    if 'users' in gdata:
                        new_users = {}
                        for uid, udata in gdata['users'].items():
                            if isinstance(udata, str):
                                new_users[int(uid)] = {'gamertag': udata, 'ic_name': udata}
                            elif isinstance(udata, dict):
                                new_users[int(uid)] = udata
                        gdata['users'] = new_users
                    if 'zones' in gdata and isinstance(gdata['zones'], dict):
                        for zname, zdata in gdata['zones'].items():
                            if isinstance(zdata, dict):
                                if 'voice_channel_id' in zdata and 'category_id' not in zdata:
                                    zdata.pop('voice_channel_id', None)
                                normalized_range = normalize_zone_range(zdata.get('range'))
                                if normalized_range is not None:
                                    zdata['range'] = normalized_range
                                if 'parts' not in zdata or not isinstance(zdata.get('parts'), list):
                                    if isinstance(zdata.get('bounds'), dict):
                                        zdata['parts'] = [zdata['bounds']]
                                    else:
                                        zdata['parts'] = []
                                else:
                                    cleaned_parts = []
                                    for part in zdata.get('parts', []):
                                        if isinstance(part, dict) and isinstance(part.get('min'), dict) and isinstance(part.get('max'), dict):
                                            cleaned_parts.append({'min': part['min'], 'max': part['max']})
                                    zdata['parts'] = cleaned_parts
                                if zdata.get('parts'):
                                    zdata['bounds'] = zdata['parts'][0]
                                rooms = zdata.get('rooms')
                                if not isinstance(rooms, list):
                                    rooms = []
                                cleaned_rooms = []
                                for idx, room in enumerate(rooms):
                                    if not isinstance(room, dict):
                                        continue
                                    if isinstance(room.get('bounds'), dict):
                                        bounds = room.get('bounds')
                                        minp = bounds.get('min') if isinstance(bounds.get('min'), dict) else None
                                        maxp = bounds.get('max') if isinstance(bounds.get('max'), dict) else None
                                    else:
                                        minp = room.get('min') if isinstance(room.get('min'), dict) else None
                                        maxp = room.get('max') if isinstance(room.get('max'), dict) else None
                                    if minp and maxp:
                                        cleaned_rooms.append({
                                            'name': str(room.get('name') or f'Room {idx + 1}'),
                                            'min': minp,
                                            'max': maxp
                                        })
                                zdata['rooms'] = cleaned_rooms
                
                server_data = temp_data
            print(f"[ระบบ] โหลดข้อมูลสำเร็จ: {len(server_data)} เซิร์ฟเวอร์")
        except Exception as e:
            print(f"[ข้อผิดพลาด] ไม่สามารถโหลดข้อมูลได้: {e}")
            server_data = {}

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(server_data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"[ข้อผิดพลาด] ไม่สามารถบันทึกข้อมูลได้: {e}")

load_data()

def get_guild_data(guild_id):
    if guild_id not in server_data:
        server_data[guild_id] = {'whitelist': {}, 'config': {}, 'users': {}, 'zones': {}}
    return server_data[guild_id]

def update_whitelist(guild_id, name, active=True):
    data = get_guild_data(guild_id)
    data['whitelist'] = {'active': active, 'name': name}
    save_data()

def remove_whitelist(guild_id):
    if guild_id in server_data:
        if 'whitelist' in server_data[guild_id]:
            del server_data[guild_id]['whitelist']
            save_data()

def toggle_whitelist(guild_id):
    data = get_guild_data(guild_id)
    if data.get('whitelist'):
        data['whitelist']['active'] = not data['whitelist'].get('active', False)
        save_data()

def update_config(guild_id, category_id, start_channel_id, range_val):
    data = get_guild_data(guild_id)
    data['config'] = {'category_id': category_id, 'start_channel_id': start_channel_id, 'range': range_val}
    save_data()

def update_user(guild_id, user_id, gamertag, ic_name):
    data = get_guild_data(guild_id)
    if 'users' not in data: data['users'] = {}
    data['users'][user_id] = {'gamertag': gamertag, 'ic_name': ic_name}
    save_data()


def get_zone_map(guild_id):
    data = get_guild_data(guild_id)
    if 'zones' not in data or not isinstance(data['zones'], dict):
        data['zones'] = {}
    return data['zones']

def upsert_zone(guild_id, zone_name, category_id=None, zone_range=None):
    zones = get_zone_map(guild_id)
    zone = zones.get(zone_name, {})
    if category_id is not None:
        zone['category_id'] = category_id
    normalized_range = normalize_zone_range(zone_range)
    if normalized_range is not None:
        zone['range'] = normalized_range
    zone['name'] = zone_name
    zones[zone_name] = zone
    save_data()
    return zone

def set_zone_bounds(guild_id, zone_name, min_point, max_point, zone_range=None, append_part=False, edit_part_index=None):
    zone = upsert_zone(guild_id, zone_name, zone_range=zone_range)
    new_bounds = {'min': min_point, 'max': max_point}
    parts = zone.get('parts') if isinstance(zone.get('parts'), list) else []
    try:
        edit_part_index = int(edit_part_index) if edit_part_index is not None else None
    except (TypeError, ValueError):
        edit_part_index = None

    if edit_part_index is not None and 0 <= edit_part_index < len(parts):
        parts[edit_part_index] = new_bounds
    elif append_part:
        parts.append(new_bounds)
    else:
        parts = [new_bounds]

    zone['parts'] = parts
    zone['bounds'] = parts[0] if parts else new_bounds
    normalized_range = normalize_zone_range(zone_range)
    if normalized_range is not None:
        zone['range'] = normalized_range
    save_data()
    return zone

def delete_zone_part(guild_id, zone_name, part_index):
    zones = get_zone_map(guild_id)
    zone = zones.get(zone_name)
    if not isinstance(zone, dict):
        return False
    parts = zone.get('parts') if isinstance(zone.get('parts'), list) else []
    try:
        part_index = int(part_index)
    except (TypeError, ValueError):
        return False
    if part_index < 0 or part_index >= len(parts):
        return False
    parts.pop(part_index)
    zone['parts'] = parts
    if parts:
        zone['bounds'] = parts[0]
    else:
        zone.pop('bounds', None)
    save_data()
    return True

def get_zone_rooms(guild_id, zone_name):
    zones = get_zone_map(guild_id)
    zone = zones.get(zone_name)
    if not isinstance(zone, dict):
        return []
    rooms = zone.get('rooms')
    if not isinstance(rooms, list):
        rooms = []
        zone['rooms'] = rooms
    return rooms

def set_zone_room_bounds(guild_id, zone_name, room_name, min_point, max_point, room_index=None):
    zone = upsert_zone(guild_id, zone_name)
    rooms = zone.get('rooms') if isinstance(zone.get('rooms'), list) else []
    try:
        room_index = int(room_index) if room_index is not None else None
    except (TypeError, ValueError):
        room_index = None

    room = {
        'name': str(room_name or '').strip() or f'Room {len(rooms) + 1}',
        'min': min_point,
        'max': max_point
    }

    if room_index is not None and 0 <= room_index < len(rooms):
        old_name = str(rooms[room_index].get('name') or '').strip()
        if old_name and not room_name:
            room['name'] = old_name
        rooms[room_index] = room
    else:
        rooms.append(room)

    zone['rooms'] = rooms
    save_data()
    return room

def delete_zone_room(guild_id, zone_name, room_index):
    rooms = get_zone_rooms(guild_id, zone_name)
    try:
        room_index = int(room_index)
    except (TypeError, ValueError):
        return False
    if room_index < 0 or room_index >= len(rooms):
        return False
    rooms.pop(room_index)
    save_data()
    # clear runtime session for this room index
    guild_sessions = room_sessions.get(guild_id, {})
    for key in list(guild_sessions.keys()):
        try:
            zname, idx = key.rsplit(':', 1)
            if zname == zone_name and int(idx) == room_index:
                del guild_sessions[key]
        except Exception:
            pass
    return True

def delete_zone(guild_id, zone_name):
    zones = get_zone_map(guild_id)
    existed = zone_name in zones
    if existed:
        del zones[zone_name]
        save_data()
    return existed

def point_in_bounds(point, bounds):
    minp = bounds.get('min', {})
    maxp = bounds.get('max', {})
    return (
        minp.get('x', 0) <= point.get('x', 0) <= maxp.get('x', 0) and
        minp.get('y', -9999) <= point.get('y', 0) <= maxp.get('y', 0) and
        minp.get('z', 0) <= point.get('z', 0) <= maxp.get('z', 0)
    )

def find_player_zone(guild_id, point):
    zones = get_zone_map(guild_id)
    for name, zone in zones.items():
        parts = zone.get('parts') if isinstance(zone.get('parts'), list) else []
        if not parts and isinstance(zone.get('bounds'), dict):
            parts = [zone['bounds']]
        for bounds in parts:
            if bounds and point_in_bounds(point, bounds):
                return name, zone
    return None, None

def find_player_room(guild_id, zone_name, point):
    rooms = get_zone_rooms(guild_id, zone_name)
    for idx, room in enumerate(rooms):
        bounds = {'min': room.get('min', {}), 'max': room.get('max', {})}
        if point_in_bounds(point, bounds):
            return idx, room
    return None, None


def normalize_zone_range(zone_range):
    try:
        zone_range = int(zone_range)
    except (TypeError, ValueError):
        zone_range = None
    if zone_range is None or zone_range <= 0:
        return None
    return zone_range

def build_call_groups(call_payload):
    groups = []
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(a, b):
        if not a or not b:
            return
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    explicit_groups = []

    for item in call_payload or []:
        if isinstance(item, dict):
            members = item.get('members') or item.get('participants') or item.get('users')
            if isinstance(members, list):
                cleaned = [str(m).strip() for m in members if str(m).strip()]
                if len(cleaned) >= 2:
                    explicit_groups.append(cleaned)
                continue
            p1 = str(item.get('p1') or '').strip()
            p2 = str(item.get('p2') or '').strip()
            if p1 and p2:
                union(p1, p2)
        elif isinstance(item, (list, tuple, set)):
            cleaned = [str(m).strip() for m in item if str(m).strip()]
            if len(cleaned) >= 2:
                explicit_groups.append(cleaned)

    grouped = {}
    for member in list(parent.keys()):
        root = find(member)
        grouped.setdefault(root, set()).add(member)

    for members in explicit_groups:
        first = members[0]
        for other in members[1:]:
            union(first, other)

    # rebuild after explicit unions
    grouped = {}
    for member in list(parent.keys()):
        root = find(member)
        grouped.setdefault(root, set()).add(member)

    for members in explicit_groups:
        root = find(members[0])
        grouped.setdefault(root, set()).update(members)

    groups = [sorted(list(v)) for v in grouped.values() if len(v) >= 2]
    groups.sort(key=lambda g: (-len(g), g))
    return groups

def rebuild_active_call_lookup():
    global active_call_lookup
    active_call_lookup = {}
    for idx, members in enumerate(active_call_groups):
        for member in members:
            active_call_lookup[member] = idx

def is_owner_or_admin(interaction: discord.Interaction):
    return bool(
        interaction.user.id in OWNER_USER_IDS or
        getattr(interaction.user.guild_permissions, "administrator", False)
    )


intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.web_server = None
        self.is_rate_limited = False

    async def setup_hook(self):
        app = web.Application()
        app.router.add_post('/update_coords', self.handle_coords)
        app.router.add_get('/zones', self.handle_zones)
        app.router.add_get('/zone/parts', self.handle_zone_parts)
        app.router.add_post('/zone/bounds', self.handle_zone_bounds)
        app.router.add_post('/zone/part/delete', self.handle_zone_part_delete)
        app.router.add_get('/zone/rooms', self.handle_zone_rooms)
        app.router.add_post('/zone/room/bounds', self.handle_zone_room_bounds)
        app.router.add_post('/zone/room/delete', self.handle_zone_room_delete)
        app.router.add_post('/random/toggle', self.handle_random_toggle)
        app.router.add_get('/', self.handle_index)
        app.router.add_get('/dashboard', self.handle_dashboard)
        app.router.add_post('/dashboard/add', self.handle_dash_add)
        app.router.add_post('/dashboard/remove', self.handle_dash_remove)
        app.router.add_post('/dashboard/toggle', self.handle_dash_toggle)

        try:
            self.add_view(SetupView())
        except Exception as e:
            print(f"[ระบบ] ไม่สามารถลงทะเบียน Persistent View ได้: {e}")

        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        self.web_server = web.TCPSite(runner, '0.0.0.0', port)
        await self.web_server.start()
        print(f"[ระบบ] เริ่มการทำงานเว็บเซิร์ฟเวอร์บนพอร์ต {port}")
        try: await self.tree.sync()
        except: pass

    async def handle_zones(self, request):
        try:
            guild_id = int(request.query.get('guild_id', '0'))
            if guild_id <= 0:
                return web.json_response({'status': 'error', 'message': 'missing guild_id'}, status=400)
            zone_map = get_zone_map(guild_id)
            zones = []
            for name, zone in zone_map.items():
                parts = zone.get('parts') if isinstance(zone.get('parts'), list) else []
                if not parts and isinstance(zone.get('bounds'), dict):
                    parts = [zone.get('bounds')]
                rooms = zone.get('rooms') if isinstance(zone.get('rooms'), list) else []
                zones.append({
                    'name': name,
                    'has_bounds': bool(parts),
                    'part_count': len(parts),
                    'room_count': len(rooms),
                    'category_id': zone.get('category_id'),
                    'range': zone.get('range')
                })
            zones.sort(key=lambda z: z['name'])
            return web.json_response({'status': 'ok', 'zones': zones})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def handle_zone_bounds(self, request):
        try:
            data = await request.json()
            password = request.headers.get('X-Dashboard-Password', '')
            if password and password != DASHBOARD_PASSWORD:
                return web.json_response({'status': 'error', 'message': 'invalid password'}, status=403)

            guild_id = int(data.get('guild_id') or 0)
            zone_name = str(data.get('zone_name') or '').strip()
            min_point = data.get('min') or {}
            max_point = data.get('max') or {}
            zone_range = data.get('range')
            append_part = bool(data.get('append_part', False))
            edit_part_index = data.get('edit_part_index', None)
            if guild_id <= 0 or not zone_name:
                return web.json_response({'status': 'error', 'message': 'invalid guild_id or zone_name'}, status=400)
            required = ['x', 'y', 'z']
            if not all(k in min_point for k in required) or not all(k in max_point for k in required):
                return web.json_response({'status': 'error', 'message': 'invalid bounds'}, status=400)
            zone = set_zone_bounds(guild_id, zone_name, min_point, max_point, zone_range=zone_range, append_part=append_part, edit_part_index=edit_part_index)
            return web.json_response({'status': 'ok', 'zone': zone})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def handle_zone_parts(self, request):
        try:
            guild_id = int(request.query.get('guild_id', '0'))
            zone_name = str(request.query.get('zone_name') or '').strip()
            if guild_id <= 0 or not zone_name:
                return web.json_response({'status': 'error', 'message': 'missing guild_id or zone_name'}, status=400)

            zone = get_zone_map(guild_id).get(zone_name)
            if not isinstance(zone, dict):
                return web.json_response({'status': 'error', 'message': 'zone not found'}, status=404)

            parts = zone.get('parts') if isinstance(zone.get('parts'), list) else []
            if not parts and isinstance(zone.get('bounds'), dict):
                parts = [zone.get('bounds')]

            return web.json_response({
                'status': 'ok',
                'zone_name': zone_name,
                'parts': [
                    {'index': idx, 'min': part.get('min', {}), 'max': part.get('max', {})}
                    for idx, part in enumerate(parts)
                ]
            })
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def handle_zone_part_delete(self, request):
        try:
            data = await request.json()
            password = request.headers.get('X-Dashboard-Password', '')
            if password and password != DASHBOARD_PASSWORD:
                return web.json_response({'status': 'error', 'message': 'invalid password'}, status=403)

            guild_id = int(data.get('guild_id') or 0)
            zone_name = str(data.get('zone_name') or '').strip()
            part_index = data.get('part_index')
            ok = delete_zone_part(guild_id, zone_name, part_index)
            if not ok:
                return web.json_response({'status': 'error', 'message': 'part not found'}, status=404)
            return web.json_response({'status': 'ok'})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def handle_zone_rooms(self, request):
        try:
            guild_id = int(request.query.get('guild_id', '0'))
            zone_name = str(request.query.get('zone_name') or '').strip()
            if guild_id <= 0 or not zone_name:
                return web.json_response({'status': 'error', 'message': 'missing guild_id or zone_name'}, status=400)

            zone = get_zone_map(guild_id).get(zone_name)
            if not isinstance(zone, dict):
                return web.json_response({'status': 'error', 'message': 'zone not found'}, status=404)

            rooms = get_zone_rooms(guild_id, zone_name)
            return web.json_response({
                'status': 'ok',
                'zone_name': zone_name,
                'rooms': [
                    {
                        'index': idx,
                        'name': room.get('name') or f'Room {idx + 1}',
                        'has_bounds': bool(room.get('min') and room.get('max')),
                        'min': room.get('min', {}),
                        'max': room.get('max', {})
                    }
                    for idx, room in enumerate(rooms)
                ]
            })
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def handle_zone_room_bounds(self, request):
        try:
            data = await request.json()
            password = request.headers.get('X-Dashboard-Password', '')
            if password and password != DASHBOARD_PASSWORD:
                return web.json_response({'status': 'error', 'message': 'invalid password'}, status=403)

            guild_id = int(data.get('guild_id') or 0)
            zone_name = str(data.get('zone_name') or '').strip()
            room_name = str(data.get('room_name') or '').strip()
            room_index = data.get('room_index', None)
            min_point = data.get('min') or {}
            max_point = data.get('max') or {}

            if guild_id <= 0 or not zone_name:
                return web.json_response({'status': 'error', 'message': 'invalid guild_id or zone_name'}, status=400)
            required = ['x', 'y', 'z']
            if not all(k in min_point for k in required) or not all(k in max_point for k in required):
                return web.json_response({'status': 'error', 'message': 'invalid bounds'}, status=400)

            room = set_zone_room_bounds(guild_id, zone_name, room_name, min_point, max_point, room_index=room_index)
            return web.json_response({'status': 'ok', 'room': room})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def handle_zone_room_delete(self, request):
        try:
            data = await request.json()
            password = request.headers.get('X-Dashboard-Password', '')
            if password and password != DASHBOARD_PASSWORD:
                return web.json_response({'status': 'error', 'message': 'invalid password'}, status=403)

            guild_id = int(data.get('guild_id') or 0)
            zone_name = str(data.get('zone_name') or '').strip()
            room_index = data.get('room_index')
            ok = delete_zone_room(guild_id, zone_name, room_index)
            if not ok:
                return web.json_response({'status': 'error', 'message': 'room not found'}, status=404)
            return web.json_response({'status': 'ok'})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def handle_random_toggle(self, request):
        try:
            data = await request.json()
            gamertag = str(data.get('player') or data.get('gamertag') or '').strip()
            enabled = bool(data.get('enabled', True))
            if not gamertag:
                return web.json_response({'status': 'error', 'message': 'missing player'}, status=400)
            random_call_opt_in[gamertag] = enabled
            return web.json_response({'status': 'ok', 'player': gamertag, 'enabled': enabled})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def handle_index(self, request):
        return web.Response(text=f"บอทกำลังทำงาน (Bot Online)")

    async def handle_dashboard(self, request):
        try:
            whitelist_flat = {str(gid): d.get('whitelist') for gid, d in server_data.items() if d.get('whitelist')}
            env = Environment(loader=FileSystemLoader('templates'))
            if not os.path.exists('templates/dashboard.html'):
                 return web.Response(text="ไม่พบไฟล์หน้าเว็บ (Template not found)", status=404)
            template = env.get_template('dashboard.html')
            rendered = template.render(whitelist=whitelist_flat, password=DASHBOARD_PASSWORD)
            return web.Response(text=rendered, content_type='text/html')
        except Exception as e: return web.Response(text=str(e), status=500)

    async def check_pass(self, data): return data.get('password') == DASHBOARD_PASSWORD
    async def handle_dash_toggle(self, request): return web.HTTPFound('/dashboard')
    async def handle_dash_add(self, request): return web.HTTPFound('/dashboard')
    async def handle_dash_remove(self, request): return web.HTTPFound('/dashboard')

    # --- ระบบเชื่อมต่อกับ Addon ---
    async def handle_coords(self, request):
        try:
            data = await request.json()
            global game_state
            global DYNAMIC_RANGE
            global active_call_groups
            global audio_state

            user_list = []
            server_calls = []

            if isinstance(data, dict):
                user_list = data.get('users', [])
                received_range = data.get('range')
                if received_range:
                    try:
                        DYNAMIC_RANGE = int(received_range)
                    except (TypeError, ValueError):
                        pass
                server_calls = data.get('calls', [])
            elif isinstance(data, list):
                user_list = data

            current = {}
            new_audio_state = {}
            for p in user_list:
                name = str(p.get('name') or '').strip()
                if not name:
                    continue
                current[name] = {'x': p['x'], 'y': p['y'], 'z': p['z']}
                new_audio_state[name] = {
                    'mic_disabled': bool(p.get('mic_disabled', False)),
                    'headphone_disabled': bool(p.get('headphone_disabled', False))
                }
            game_state = current
            audio_state = new_audio_state

            active_call_groups = build_call_groups(server_calls)
            rebuild_active_call_lookup()

            all_ic_map = {}
            for gid, gdata in server_data.items():
                for uid, udata in gdata.get('users', {}).items():
                    if isinstance(udata, dict):
                        all_ic_map[udata['gamertag']] = udata['ic_name']
                    elif isinstance(udata, str):
                        all_ic_map[udata] = udata

            if not self.is_rate_limited:
                await process_voice_logic()

            return web.json_response({'status': 'ok', 'ic_map': all_ic_map})

        except Exception as e:
            print(f"เกิดข้อผิดพลาดในการรับข้อมูล: {e}")
            return web.json_response({'status': 'error'}, status=500)

bot = MyBot()

@bot.event
async def on_guild_join(guild):
    data = get_guild_data(guild.id)
    if guild.id in ALLOWED_GUILD_IDS:
        update_whitelist(guild.id, guild.name, True)
        return
    if not data.get('whitelist'):
        try:
            await guild.leave()
        except:
            pass
    else:
        update_whitelist(guild.id, guild.name, data['whitelist'].get('active', True))

class LinkModal(ui.Modal, title='ลงทะเบียนระบบสนทนาด้วยเสียง'):
    xbox_name = ui.TextInput(label='ชื่อในเกม Xbox (Gamertag)', placeholder='ใส่ชื่อที่ใช้ในเกม Minecraft...', min_length=3, max_length=20)
    ic_name = ui.TextInput(label='ชื่อตัวละคร (IC Name)', placeholder='ใส่ชื่อตัวละครที่ใช้สวมบทบาท...', min_length=1, max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gamertag = self.xbox_name.value.strip()
        ic = self.ic_name.value.strip()
        
        update_user(interaction.guild_id, interaction.user.id, gamertag, ic)
        
        data = get_guild_data(interaction.guild_id)
        cfg = data.get('config', {})
        msg = f"บันทึกข้อมูลสำเร็จ!\nชื่อ Xbox: **{gamertag}**\nชื่อตัวละคร: **{ic}**"
        if 'start_channel_id' in cfg:
            chan = interaction.guild.get_channel(cfg['start_channel_id'])
            if chan: msg += f"\n\nกรุณาเข้าไปรอในห้องเสียง: {chan.mention}"
        await interaction.followup.send(msg, ephemeral=True)

class SetupView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.button(label="ลงทะเบียน / แก้ไขข้อมูล", style=discord.ButtonStyle.green, custom_id="mc_link")
    async def link(self, i: discord.Interaction, b: ui.Button):
        await i.response.send_modal(LinkModal())

    @ui.button(label="ตรวจสอบสถานะ", style=discord.ButtonStyle.primary, custom_id="mc_status")
    async def status(self, i: discord.Interaction, b: ui.Button):
        data = get_guild_data(i.guild_id)
        users = data.get('users', {})
        if i.user.id not in users:
            return await i.response.send_message("คุณยังไม่ได้ลงทะเบียนในระบบ", ephemeral=True)
        
        udata = users[i.user.id]
        gamertag = udata if isinstance(udata, str) else udata['gamertag']
        ic = udata if isinstance(udata, str) else udata['ic_name']
        is_online = gamertag in game_state
        
        embed = discord.Embed(title="ข้อมูลผู้ใช้งาน", color=0x3498db)
        embed.add_field(name="ชื่อ Xbox (Gamertag)", value=gamertag, inline=True)
        embed.add_field(name="ชื่อตัวละคร (IC)", value=ic, inline=True)
        embed.add_field(name="สถานะในเกม", value="อยู่ในเกม" if is_online else "ออฟไลน์", inline=False)
        await i.response.send_message(embed=embed, ephemeral=True)


# --- ระบบ Backup / Restore ---
def build_setup_embed():
    return discord.Embed(
        title="ระบบสนทนาด้วยเสียง (Voice Chat)",
        description=(
            "คำแนะนำการใช้งานอย่างละเอียด:\n"
            "1. กดปุ่ม 'ลงทะเบียน / แก้ไขข้อมูล' ด้านล่าง\n"
            "2. กรอกชื่อ Xbox และชื่อตัวละคร (IC) ของคุณ\n"
            "3. เมื่อลงทะเบียนเสร็จสิ้น ให้เข้าไปรอในห้องเสียงล็อบบี้ (Lobby)\n"
            "4. ระบบจะทำการย้ายห้องของคุณโดยอัตโนมัติเมื่อพบคุณเข้าเกม"
        ),
        color=0x2ecc71
    )

async def restore_registered_setup_embeds():
    restored = 0
    recreated = 0

    try:
        bot.add_view(SetupView())
    except Exception:
        pass

    for gid, gdata in list(server_data.items()):
        try:
            guild_id = int(gid)
        except Exception:
            continue

        guild = bot.get_guild(guild_id)
        if not guild:
            continue

        setup_info = gdata.get("setup_embed")
        if not isinstance(setup_info, dict):
            continue

        channel_id = setup_info.get("channel_id")
        if not channel_id:
            continue

        channel = guild.get_channel(int(channel_id))
        if not channel:
            continue

        embed = build_setup_embed()
        message_id = setup_info.get("message_id")

        if message_id:
            try:
                msg = await channel.fetch_message(int(message_id))
                await msg.edit(embed=embed, view=SetupView())
                restored += 1
                continue
            except Exception:
                pass

        try:
            msg = await channel.send(embed=embed, view=SetupView())
            setup_info["channel_id"] = channel.id
            setup_info["message_id"] = msg.id
            gdata["setup_embed"] = setup_info
            recreated += 1
        except Exception:
            pass

    if recreated:
        save_data()

    return restored, recreated

def build_registered_users_snapshot():
    registered = []
    for guild_id, gdata in server_data.items():
        users = gdata.get("users", {}) if isinstance(gdata, dict) else {}
        for user_id, udata in users.items():
            if isinstance(udata, dict):
                gamertag = str(udata.get("gamertag", "")).strip()
                ic_name = str(udata.get("ic_name", gamertag)).strip()
            else:
                gamertag = str(udata or "").strip()
                ic_name = gamertag
            if not gamertag and not ic_name:
                continue
            registered.append({
                "guild_id": int(guild_id),
                "user_id": int(user_id),
                "xbox_user_name": gamertag,
                "gamertag": gamertag,
                "ic_name": ic_name
            })
    registered.sort(key=lambda item: (item["guild_id"], item["user_id"]))
    return registered

def hydrate_registered_users_into_server_data(restored_raw, registered_users):
    if not isinstance(restored_raw, dict) or not isinstance(registered_users, list):
        return restored_raw

    for item in registered_users:
        if not isinstance(item, dict):
            continue
        try:
            guild_id = int(item.get("guild_id"))
            user_id = int(item.get("user_id"))
        except (TypeError, ValueError):
            continue

        gamertag = str(item.get("xbox_user_name") or item.get("gamertag") or "").strip()
        ic_name = str(item.get("ic_name") or gamertag).strip()
        if not gamertag:
            continue

        guild_key = str(guild_id)
        if guild_key not in restored_raw and guild_id not in restored_raw:
            restored_raw[guild_key] = {"whitelist": {}, "config": {}, "users": {}, "zones": {}}

        gdata = restored_raw.get(guild_key, restored_raw.get(guild_id))
        if not isinstance(gdata, dict):
            gdata = {"whitelist": {}, "config": {}, "users": {}, "zones": {}}
            restored_raw[guild_key] = gdata

        users = gdata.setdefault("users", {})
        users[str(user_id)] = {"gamertag": gamertag, "ic_name": ic_name}

    return restored_raw

def make_backup_bytes():
    save_data()
    registered_users = build_registered_users_snapshot()
    payload = {
        "backup_version": 2,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "data_file": DATA_FILE,
        "registered_users_count": len(registered_users),
        "registered_users": registered_users,
        "server_data": server_data
    }
    return json.dumps(payload, ensure_ascii=False, indent=4).encode("utf-8")

def extract_restore_payload(raw_bytes):
    parsed = json.loads(raw_bytes.decode("utf-8-sig"))
    if isinstance(parsed, dict) and "server_data" in parsed and isinstance(parsed["server_data"], dict):
        restored_raw = parsed["server_data"]
        hydrate_registered_users_into_server_data(restored_raw, parsed.get("registered_users", []))
        return restored_raw
    if isinstance(parsed, dict):
        hydrate_registered_users_into_server_data(parsed, parsed.get("registered_users", []))
        return parsed
    raise ValueError("ไฟล์ backup ไม่ถูกต้อง: JSON ต้องเป็น object")

def apply_restored_server_data(restored_raw):
    if os.path.exists(DATA_FILE):
        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        shutil.copyfile(DATA_FILE, f"{DATA_FILE}.before_restore_{stamp}.bak")

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(restored_raw, f, ensure_ascii=False, indent=4)

    load_data()
    save_data()


@bot.tree.command(
    name="login",
    description="ล็อกอินบัญชี Microsoft/Xbox"
)
async def login(interaction: discord.Interaction):
    await interaction.response.send_modal(LinkModal())


from discord import app_commands

@bot.tree.command(
    name="setup",
    description="ตั้งค่าระบบห้องเสียง"
)
@app_commands.describe(
    category="หมวดหมู่ที่จะสร้างห้อง",
    start_channel="ห้องเสียงเริ่มต้น",
    role="ยศที่อนุญาตให้ใช้งาน"
)
async def setup(
    interaction: discord.Interaction,
    category: discord.CategoryChannel,
    start_channel: discord.VoiceChannel,
    role: discord.Role = None
):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    if not is_owner_or_admin(interaction):
        return await interaction.followup.send(
            "คำสั่งนี้สำหรับผู้ดูแลระบบเท่านั้น",
            ephemeral=True
        )

    update_whitelist(interaction.guild_id, interaction.guild.name)
    update_config(interaction.guild_id, category.id, start_channel.id, DEFAULT_RANGE)

    embed = build_setup_embed()
    setup_msg = await interaction.channel.send(embed=embed, view=SetupView())

    data = get_guild_data(interaction.guild_id)
    data['setup_embed'] = {
        'channel_id': interaction.channel.id,
        'message_id': setup_msg.id,
        'category_id': category.id,
        'start_channel_id': start_channel.id,
        'role_id': role.id if role else None
    }

    save_data()
    await interaction.followup.send("ตั้งค่าระบบเสร็จสมบูรณ์", ephemeral=True)

@bot.tree.command(
    name="backup",
    description="สำลองข้อมูล")
async def backup_data(i: discord.Interaction):
    if not is_owner_or_admin(i):
        return await i.response.send_message("คำสั่งนี้สำหรับผู้ดูแลระบบเท่านั้น", ephemeral=True)

    await i.response.defer(ephemeral=True)

    try:
        raw = make_backup_bytes()
        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        file = discord.File(io.BytesIO(raw), filename=f"vc_backup_{stamp}.json")
        await i.followup.send(
            "สำรองข้อมูลสำเร็จ ไฟล์นี้เก็บ whitelist, config, users, รายชื่อผู้ลงทะเบียน User ID/Xbox/IC, zones, parts หลายสี่เหลี่ยม และข้อมูล embed ลงทะเบียนที่บันทึกไว้",
            file=file,
            ephemeral=True
        )
    except Exception as e:
        await i.followup.send(f"สำรองข้อมูลไม่สำเร็จ: {e}", ephemeral=True)

@bot.tree.command(name="restore",
                 description="กู้ข้อมูลจากไฟล์")
async def restore_data(i: discord.Interaction, file: discord.Attachment):
    if not is_owner_or_admin(i):
        return await i.response.send_message("คำสั่งนี้สำหรับผู้ดูแลระบบเท่านั้น", ephemeral=True)

    await i.response.defer(ephemeral=True)

    try:
        if file.size and file.size > 5 * 1024 * 1024:
            return await i.followup.send("ไฟล์ใหญ่เกินไป จำกัดไม่เกิน 5MB", ephemeral=True)

        raw = await file.read()
        restored_raw = extract_restore_payload(raw)
        apply_restored_server_data(restored_raw)

        restored, recreated = await restore_registered_setup_embeds()

        await i.followup.send(
            "กู้คืนข้อมูลสำเร็จ\n"
            f"- โหลดเซิร์ฟเวอร์ทั้งหมด: {len(server_data)}\n"
            f"- ผูกปุ่ม embed เดิมกลับมา: {restored}\n"
            f"- สร้าง embed ใหม่แทนตัวที่หาไม่เจอ: {recreated}\n"
            "ระบบ zone, users, รายชื่อผู้ลงทะเบียน User ID/Xbox/IC, config และ whitelist ถูกกู้คืนจากไฟล์แล้ว",
            ephemeral=True
        )
    except Exception as e:
        await i.followup.send(f"กู้คืนข้อมูลไม่สำเร็จ: {e}", ephemeral=True)


@bot.tree.command(name="whitelist",
                 description="เพิ่มรายชื่อเข้าเซิฟเวอร์")
async def wl(i: discord.Interaction, server_id: str):
    if not is_owner_or_admin(i): return await i.response.send_message("คำสั่งนี้สำหรับผู้ดูแลระบบเท่านั้น", ephemeral=True)
    update_whitelist(int(server_id), "Added via Cmd")
    await i.response.send_message(f"เพิ่มรายชื่อเซิร์ฟเวอร์สำเร็จ: {server_id}", ephemeral=True)

@bot.tree.command(name="range",
                 description="กำหนดระยะบล็อค")
async def set_range(i: discord.Interaction, distance: int):
    data = get_guild_data(i.guild_id)
    cfg = data.get('config', {})
    if 'category_id' in cfg:
        update_config(i.guild_id, cfg['category_id'], cfg['start_channel_id'], distance)
        await i.response.send_message(f"ตั้งค่าระยะเสียงเริ่มต้นเป็น {distance} บล็อกเรียบร้อยแล้ว", ephemeral=True)
    else:
        await i.response.send_message("โปรดพิมพ์คำสั่ง /setup ก่อนใช้งานคำสั่งนี้", ephemeral=True)

@bot.tree.command(name="zone",
                 description="เอาไว้สร้างห้องโทร")
async def zone_create(i: discord.Interaction, name: str, category: discord.CategoryChannel, range_val: int = None):
    if not is_owner_or_admin(i):
        return await i.response.send_message("คำสั่งนี้สำหรับผู้ดูแลระบบเท่านั้น", ephemeral=True)
    zone = upsert_zone(i.guild_id, name.strip(), category.id, range_val)
    has_bounds = 'bounds' in zone
    voice_count = sum(1 for ch in category.channels if isinstance(ch, discord.VoiceChannel))
    await i.response.send_message(
        f"สร้าง/อัปเดตโซน **{name}** เรียบร้อยแล้ว\nหมวดหมู่: **{category.name}**\nจำนวนห้องเสียงในหมวด: **{voice_count}**\nสถานะขอบเขต: {'ตั้งแล้ว' if has_bounds else 'ยังไม่ได้ตั้ง'}\nระยะไมค์โซน: **{zone.get('range', 'ใช้ค่าเริ่มต้น')}**",
        ephemeral=True
    )

@bot.tree.command(name="delzone",
                 description="ลบโซน")
async def zone_delete(i: discord.Interaction, name: str):
    if not is_owner_or_admin(i):
        return await i.response.send_message("คำสั่งนี้สำหรับผู้ดูแลระบบเท่านั้น", ephemeral=True)
    ok = delete_zone(i.guild_id, name.strip())
    await i.response.send_message((f"ลบโซน **{name}** เรียบร้อยแล้ว" if ok else f"ไม่พบโซน **{name}**"), ephemeral=True)

@bot.tree.command(name="zones",
                 description="ดูโซนทั้งหมด")
async def zone_list_cmd(i: discord.Interaction):
    if not is_owner_or_admin(i):
        return await i.response.send_message("คำสั่งนี้สำหรับผู้ดูแลระบบเท่านั้น", ephemeral=True)
    zone_map = get_zone_map(i.guild_id)
    if not zone_map:
        return await i.response.send_message("ยังไม่มีโซนในเซิร์ฟเวอร์นี้", ephemeral=True)
    lines = []
    for name, zone in sorted(zone_map.items()):
        cat_obj = i.guild.get_channel(zone.get('category_id')) if zone.get('category_id') else None
        parts = zone.get('parts') if isinstance(zone.get('parts'), list) else []
        if not parts and zone.get('bounds'):
            parts = [zone.get('bounds')]
        rooms = zone.get('rooms') if isinstance(zone.get('rooms'), list) else []
        lines.append(f"• **{name}** → หมวด: {cat_obj.name if cat_obj else 'ไม่มีหมวด'} | parts: {len(parts)} | rooms: {len(rooms)} | bounds: {'set' if parts else 'unset'} | range: {zone.get('range', 'default')}")
    await i.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="zonerange",
                 description="ตั้งระยะในโซน")
async def zone_range_cmd(i: discord.Interaction, name: str, distance: int):
    if not is_owner_or_admin(i):
        return await i.response.send_message("คำสั่งนี้สำหรับผู้ดูแลระบบเท่านั้น", ephemeral=True)
    zones = get_zone_map(i.guild_id)
    zone = zones.get(name.strip())
    if not zone:
        return await i.response.send_message(f"ไม่พบโซน **{name}**", ephemeral=True)
    zone['range'] = distance
    save_data()
    await i.response.send_message(f"ตั้งค่าระยะไมค์ของโซน **{name}** เป็น **{distance}** บล็อกแล้ว", ephemeral=True)

@bot.tree.command(name="test",
                 description="คำสั่งนี้ใช้ในเกมมีไอเท็มให้")
async def test_mode(interaction: discord.Interaction):
    if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
    if not is_owner_or_admin(interaction): return await interaction.followup.send("คำสั่งนี้สำหรับผู้ดูแลระบบเท่านั้น", ephemeral=True)
    gid = interaction.guild_id
    if gid in testing_guilds:
        testing_guilds.remove(gid)
        if interaction.guild.voice_client: await interaction.guild.voice_client.disconnect()
        await interaction.followup.send("โหมดทดสอบ: ปิดการใช้งาน", ephemeral=True)
    else:
        testing_guilds.add(gid)
        await interaction.followup.send("โหมดทดสอบ: เปิดการใช้งาน (บอทจะตามพิกัดของหุ่น botvc ในเกม)", ephemeral=True)

# --- ระบบจัดการห้องและตำแหน่ง (Center of Mass Clustering) ---
async def assign_groups_in_category(guild, members_with_pos, category, fallback_channel, taken_rooms, curr, active_range=None):
    if not members_with_pos or not category:
        return

    voice_channels = [c for c in category.channels if isinstance(c, discord.VoiceChannel)]
    if not voice_channels:
        return

    config_range = get_guild_data(guild.id).get('config', {}).get('range', DEFAULT_RANGE)
    if active_range is None:
        active_range = DYNAMIC_RANGE if DYNAMIC_RANGE > 0 else config_range
    dist_sq = max(int(active_range), 1) ** 2

    clusters = []
    for m, x, y, z in members_with_pos:
        clusters.append({'members': [m], 'cx': float(x), 'cy': float(y), 'cz': float(z), 'size': 1})

    while True:
        best_pair = None
        min_score = float('inf')
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                c1 = clusters[i]
                c2 = clusters[j]
                dist_sq_centers = (c2['cx'] - c1['cx'])**2 + (c2['cy'] - c1['cy'])**2 + (c2['cz'] - c1['cz'])**2
                if dist_sq_centers <= dist_sq:
                    score = dist_sq_centers - (c1['size'] + c2['size']) * 0.001
                    if score < min_score:
                        min_score = score
                        best_pair = (i, j)
        if best_pair is None:
            break
        i, j = best_pair
        c1 = clusters[i]
        c2 = clusters[j]
        new_size = c1['size'] + c2['size']
        merged_cluster = {
            'members': c1['members'] + c2['members'],
            'cx': ((c1['cx'] * c1['size']) + (c2['cx'] * c2['size'])) / new_size,
            'cy': ((c1['cy'] * c1['size']) + (c2['cy'] * c2['size'])) / new_size,
            'cz': ((c1['cz'] * c1['size']) + (c2['cz'] * c2['size'])) / new_size,
            'size': new_size
        }
        clusters.pop(j)
        clusters.pop(i)
        clusters.append(merged_cluster)

    groups = [c['members'] for c in clusters]
    groups.sort(key=len, reverse=True)

    avail = [c for c in voice_channels if fallback_channel is None or c.id != fallback_channel.id]
    for g in groups:
        room_counts = {}
        for m in g:
            if m.voice and m.voice.channel and m.voice.channel.category_id == category.id:
                c = m.voice.channel
                room_counts[c] = room_counts.get(c, 0) + 1

        if room_counts:
            majority_channel = max(room_counts, key=room_counts.get)
        else:
            majority_channel = fallback_channel if fallback_channel and fallback_channel.category_id == category.id else (voice_channels[0] if voice_channels else None)

        if not majority_channel:
            continue

        need_new_room = (fallback_channel is not None and majority_channel.id == fallback_channel.id) or (majority_channel.id in taken_rooms)
        target = None
        if need_new_room:
            for c in avail:
                if len(c.members) == 0 and c.id not in taken_rooms:
                    target = c
                    break
            if not target:
                for c in avail:
                    if c.id not in taken_rooms:
                        target = c
                        break
        else:
            target = majority_channel

        if not target:
            continue
        taken_rooms.add(target.id)

        for m in g:
            if m == guild.me:
                if guild.voice_client:
                    if guild.voice_client.channel.id != target.id:
                        await guild.voice_client.move_to(target)
                else:
                    try:
                        await target.connect()
                    except:
                        pass
            elif m.voice and m.voice.channel and m.voice.channel.id != target.id:
                if curr - user_last_move.get(m.id, 0) < MOVE_COOLDOWN:
                    continue
                try:
                    await m.move_to(target)
                    user_last_move[m.id] = curr
                    await asyncio.sleep(0.2)
                except:
                    pass


async def assign_room_members(guild, members_with_pos, category, fallback_channel, taken_rooms, curr, guild_id, zone_name, room_index):
    if not members_with_pos or not category:
        return

    members = [m for m, _, _, _ in members_with_pos if m and m.voice and m.voice.channel]
    if not members:
        return

    guild_room_sessions = room_sessions.setdefault(guild_id, {})
    session_key = f"{zone_name}:{room_index}"
    session = guild_room_sessions.get(session_key, {})

    member_ids = {m.id for m in members}
    owner_id = session.get('owner_id')
    owner = next((m for m in members if m.id == owner_id), None)

    # ถ้าเจ้าของออกจาก Room ให้สุ่มโอนสิทธิ์ให้คนที่ยังอยู่
    if owner is None:
        owner = random.choice(members)
        session['owner_id'] = owner.id

    voice_channels = [c for c in category.channels if isinstance(c, discord.VoiceChannel)]
    if fallback_channel and fallback_channel.category_id == category.id:
        voice_channels = [c for c in voice_channels if c.id != fallback_channel.id]

    target = None
    old_channel_id = session.get('channel_id')
    if old_channel_id:
        ch = guild.get_channel(int(old_channel_id))
        if isinstance(ch, discord.VoiceChannel) and ch.category_id == category.id and ch.id not in taken_rooms:
            target = ch

    if target is None and owner.voice and owner.voice.channel:
        ch = owner.voice.channel
        if ch.category_id == category.id and ch.id not in taken_rooms:
            # ถ้าเจ้าของอยู่คนเดียว ช่องนั้นจะกลายเป็น Room โดยตรง
            # หรือถ้ามีแต่สมาชิกที่อยู่ใน Room เดียวกัน ก็ใช้ช่องเดิมต่อได้
            if len(ch.members) <= 1 or all((m.id in member_ids or m == guild.me) for m in ch.members):
                target = ch

    if target is None:
        for ch in voice_channels:
            if len(ch.members) == 0 and ch.id not in taken_rooms:
                target = ch
                break

    if target is None:
        for ch in voice_channels:
            if ch.id not in taken_rooms:
                target = ch
                break

    if target is None:
        return

    session['channel_id'] = target.id
    guild_room_sessions[session_key] = session
    taken_rooms.add(target.id)

    for m in members:
        if m == guild.me:
            if guild.voice_client:
                if guild.voice_client.channel.id != target.id:
                    await guild.voice_client.move_to(target)
            else:
                try:
                    await target.connect()
                except:
                    pass
            continue

        if m.voice and m.voice.channel and m.voice.channel.id != target.id:
            if curr - user_last_move.get(m.id, 0) < MOVE_COOLDOWN:
                continue
            try:
                await m.move_to(target)
                user_last_move[m.id] = curr
                await asyncio.sleep(0.2)
            except:
                pass


async def process_voice_logic():
    curr = time.time()
    for u in [k for k, v in user_last_move.items() if curr - v > 60]:
        del user_last_move[u]

    for guild_id, data in server_data.items():
        if not data.get('whitelist', {}).get('active'):
            continue
        cfg = data.get('config', {})
        if not cfg:
            continue

        guild = bot.get_guild(guild_id)
        if not guild:
            continue

        cat = guild.get_channel(cfg.get('category_id'))
        start = guild.get_channel(cfg.get('start_channel_id'))
        if not cat or not start:
            continue

        users_map = data.get('users', {})

        online = []
        unzoned_online = []
        zoned_online = {}
        in_call_users = set()
        tag_to_member = {}
        tag_to_point = {}
        managed_category_ids = {cfg.get('category_id')}
        for zone in get_zone_map(guild_id).values():
            if zone.get('category_id'):
                managed_category_ids.add(zone['category_id'])

        for uid, udata in users_map.items():
            gamertag = udata if isinstance(udata, str) else udata['gamertag']

            mem = guild.get_member(uid)
            if not mem or not mem.voice or not mem.voice.channel:
                continue
            if mem.voice.channel.category_id not in managed_category_ids:
                continue

            prefs = audio_state.get(gamertag, {})
            want_mute = bool(prefs.get('mic_disabled', False))
            want_deaf = bool(prefs.get('headphone_disabled', False))
            if mem.voice.mute != want_mute or mem.voice.deaf != want_deaf:
                try:
                    await mem.edit(mute=want_mute, deafen=want_deaf)
                    await asyncio.sleep(0.1)
                except:
                    pass

            tag_to_member[gamertag] = mem

            if gamertag in game_state:
                p = game_state[gamertag]
                tag_to_point[gamertag] = {'x': p['x'], 'y': p['y'], 'z': p['z']}

            if gamertag in active_call_lookup:
                in_call_users.add(uid)
                continue

            if gamertag in game_state:
                p = game_state[gamertag]
                online.append((mem, p['x'], p['y'], p['z']))
            else:
                if mem.voice.channel.id != start.id:
                    if curr - user_last_move.get(mem.id, 0) > MOVE_COOLDOWN:
                        try:
                            await mem.move_to(start)
                            user_last_move[mem.id] = curr
                        except:
                            pass

        if guild_id in testing_guilds:
            found_botvc = False
            botvc_coords = None
            for name, p in game_state.items():
                if name.startswith("botvc"):
                    found_botvc = True
                    botvc_coords = p
                    break
            if found_botvc and botvc_coords:
                online.append((guild.me, botvc_coords['x'], botvc_coords['y'], botvc_coords['z']))
                tag_to_member['botvc'] = guild.me
                tag_to_point['botvc'] = botvc_coords
            elif not found_botvc:
                if guild.voice_client:
                    await guild.voice_client.disconnect()

        taken_rooms = set()

        for call_group in active_call_groups:
            members = []
            zone_names = set()
            zone_categories = []
            for tag in call_group:
                mem = tag_to_member.get(tag)
                if not mem and (tag == "botvc" or str(tag).startswith("botvc_")) and guild_id in testing_guilds:
                    mem = guild.me
                if not mem:
                    continue
                members.append((tag, mem))
                point = tag_to_point.get(tag)
                if point:
                    zone_name, zone = find_player_zone(guild_id, point)
                    if zone_name and zone and zone.get('category_id'):
                        zone_names.add(zone_name)
                        zone_categories.append(zone.get('category_id'))

            if len(members) < 2:
                continue

            target_category = cat
            if len(zone_names) == 1 and zone_categories:
                maybe_cat = guild.get_channel(zone_categories[0])
                if isinstance(maybe_cat, discord.CategoryChannel):
                    target_category = maybe_cat

            voice_channels = [c for c in target_category.channels if isinstance(c, discord.VoiceChannel)]
            if target_category == cat:
                voice_channels = [c for c in voice_channels if c.id != start.id]

            target_room = None
            current_rooms = {}
            for _, mem in members:
                if mem != guild.me and mem.voice and mem.voice.channel and mem.voice.channel.category_id == target_category.id:
                    current_rooms[mem.voice.channel] = current_rooms.get(mem.voice.channel, 0) + 1

            if current_rooms:
                majority_channel = max(current_rooms, key=current_rooms.get)
                if majority_channel.id not in taken_rooms:
                    target_room = majority_channel

            if not target_room:
                for c in voice_channels:
                    if len(c.members) == 0 and c.id not in taken_rooms:
                        target_room = c
                        break

            if not target_room:
                for c in voice_channels:
                    if c.id not in taken_rooms:
                        target_room = c
                        break

            if not target_room:
                continue

            taken_rooms.add(target_room.id)

            for _, mem in members:
                if mem == guild.me:
                    if guild.voice_client:
                        if guild.voice_client.channel.id != target_room.id:
                            await guild.voice_client.move_to(target_room)
                    else:
                        try:
                            await target_room.connect()
                        except:
                            pass
                    continue

                if mem.voice and mem.voice.channel and mem.voice.channel.id != target_room.id:
                    if curr - user_last_move.get(mem.id, 0) < MOVE_COOLDOWN:
                        continue
                    try:
                        await mem.move_to(target_room)
                        user_last_move[mem.id] = curr
                        await asyncio.sleep(0.2)
                    except:
                        pass

        room_online = {}

        for mem, x, y, z in online:
            if mem != guild.me and mem.id in in_call_users:
                continue

            point = {'x': x, 'y': y, 'z': z}
            zone_name, zone = find_player_zone(guild_id, point)
            zone_category = guild.get_channel(zone.get('category_id')) if zone_name and zone and zone.get('category_id') else None

            if zone_name and isinstance(zone_category, discord.CategoryChannel):
                room_index, room = find_player_room(guild_id, zone_name, point)
                if room_index is not None:
                    room_key = (zone_name, room_index)
                    room_online.setdefault(room_key, {
                        'zone': zone,
                        'room': room,
                        'category': zone_category,
                        'members': []
                    })['members'].append((mem, x, y, z))
                else:
                    zoned_online.setdefault(zone_name, {
                        'category': zone_category,
                        'members': [],
                        'range': zone.get('range')
                    })['members'].append((mem, x, y, z))
            else:
                unzoned_online.append((mem, x, y, z))

        # ล้าง session ของ Room ที่ไม่มีคนอยู่แล้ว
        active_room_keys = {f"{zone_name}:{room_index}" for (zone_name, room_index) in room_online.keys()}
        guild_room_sessions = room_sessions.setdefault(guild_id, {})
        for key in list(guild_room_sessions.keys()):
            if key not in active_room_keys:
                del guild_room_sessions[key]

        # Room แยกเสียงก่อน เพื่อไม่ให้คนในโซน/นอกโซนถูกจับรวมกับคนใน Room
        for (zone_name, room_index), room_info in room_online.items():
            await assign_room_members(
                guild,
                room_info['members'],
                room_info['category'],
                start,
                taken_rooms,
                curr,
                guild_id,
                zone_name,
                room_index
            )

        for zone_name, zone_info in zoned_online.items():
            await assign_groups_in_category(
                guild,
                zone_info['members'],
                zone_info['category'],
                None,
                taken_rooms,
                curr,
                active_range=zone_info.get('range')
            )

        await assign_groups_in_category(guild, unzoned_online, cat, start, taken_rooms, curr, active_range=None)

if __name__ == "__main__":

    if not TOKEN: sys.exit(1)
    time.sleep(random.randint(5, 10))
    while True:
        try:
            bot.is_rate_limited = False
            bot.run(TOKEN)
        except discord.errors.HTTPException as e:
            if e.status == 429:
                bot.is_rate_limited = True
                time.sleep(60)
            else: time.sleep(10)
        except: time.sleep(30)
