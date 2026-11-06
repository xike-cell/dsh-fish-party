# -*- coding: utf-8 -*-
"""谁是卧底：单进程、内存态 HTTP 服务（仅 Python 标准库）。

启动：
    <python> party_server.py

默认监听 127.0.0.1:8770；如果 8770 被占用，自动改用 8771。
日志同时写 stdout 和脚本同目录下的 server.log。
"""

import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PREFERRED_PORT = 8770
FALLBACK_PORT = 8771

# 超时常量：按题目要求做成可配置常量，单位秒。
SPEAK_TIMEOUT = 20.0
VOTE_TIMEOUT = 20.0
TICK_INTERVAL = 0.5

# 房间回收：房间只增不减会一直吃内存、拖慢每 0.5 秒的全量扫描。
LOBBY_TTL = 1800.0  # 大厅里没人开局的房间，闲置 30 分钟回收（有人进房会重置这个倒计时）
ENDED_TTL = 300.0   # 已结束的房间，留 5 分钟给玩家看结果，然后回收
POST_TALK = 60.0    # 赛后可说话的时长（在这段时间里聊天、不计轮次、不投票）

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "server.log")
WEB_DIR = os.path.join(BASE_DIR, "web")

# 塘主名：网页顶上那个名字。用环境变量 FISH_PARTY_OWNER 覆盖。
POND_OWNER = (os.environ.get("FISH_PARTY_OWNER") or "汐禾").strip() or "汐禾"

# ── 绑定 B 站号 ─────────────────────────────────────────────────────────────
# B 站不会把「实名」给第三方，所以这里能证明的只有「这个 UID 存在、昵称能取到」。
BILI_API = "https://api.bilibili.com/x/web-interface/card?mid=%s"
BILI_TTL = 600.0
BILI_CACHE = {}
BILI_LOCK = threading.Lock()


def bili_lookup(uid):
    """查一个 UID 是否存在，返回 {"uid": int, "name": str}；查不到返回 None。带 10 分钟缓存。"""
    raw = str(uid or "").strip()
    if not raw.isdigit():
        return None
    key = int(raw)
    if key <= 0:
        return None
    now = time.time()
    with BILI_LOCK:
        hit = BILI_CACHE.get(key)
        if hit and now - hit[0] < BILI_TTL:
            return hit[1]
    got = None
    req = urllib.request.Request(
        BILI_API % key,
        headers={"User-Agent": "Mozilla/5.0 (fish-party)", "Referer": "https://www.bilibili.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if payload.get("code") == 0:
            data = payload.get("data") or {}
            card = data.get("card") or data
            name = str(card.get("name") or "").strip()
            if name:
                got = {"uid": key, "name": name[:24]}
    except Exception as exc:  # 网络/解析失败一律当作查不到，不抛给玩家
        log_line("查 B 站 UID %s 失败：%s" % (key, exc))
    with BILI_LOCK:
        BILI_CACHE[key] = (now, got)
    return got


def _bili_bind(data):
    """B 站号改成选填：不填也能进（塘里人少，先把门开大），填了就必须能查到。"""
    raw = str(data.get("bili_uid") or "").strip()
    if not raw:
        return {"uid": 0, "name": ""}
    got = bili_lookup(raw)
    if not got:
        raise ApiError("这个 UID 查不到（或者 B 站暂时没响应），核对一下再试", 400)
    return got
# 网页版允许「打开就能玩」：/web/* 不校验口令（房号本身就是一局一码），
# 但只放行 join/state/say/vote，建房仍要口令，防止陌生人刷房。
WEB_ROUTE_MAP = {
    "/web/join": "join",
    "/web/state": "state",
    "/web/say": "say",
    "/web/vote": "vote",
}

# --- 安全 ---
# 1) 只监听回环地址：外网必须先经隧道，隧道地址不公开。
# 2) 口令：所有 /party/* 必须带 X-Fish-Token 头。口令来自环境变量 FISH_PARTY_TOKEN，
#    没有就在本目录生成 party_token.txt（32 位十六进制）。
# 3) 限流：同一来源 IP 每分钟最多 RATE_LIMIT 次请求；建房另有更低配额。
TOKEN_PATH = os.path.join(BASE_DIR, "party_token.txt")
RATE_LIMIT = 120          # 每 IP 每 60 秒
RATE_WINDOW = 60.0
CREATE_LIMIT = 20         # 每 IP 每 600 秒最多建房次数
CREATE_WINDOW = 600.0


def _load_token():
    env = (os.environ.get("FISH_PARTY_TOKEN") or "").strip()
    if env:
        return env
    if os.path.exists(TOKEN_PATH):
        got = open(TOKEN_PATH, encoding="utf-8").read().strip()
        if got:
            return got
    got = uuid.uuid4().hex + uuid.uuid4().hex[:16]
    with open(TOKEN_PATH, "w", encoding="utf-8") as fh:
        fh.write(got)
    return got


TOKEN = _load_token()
_HITS = {}          # ip -> [ts, ...]
_CREATES = {}       # ip -> [ts, ...]
_GUARD_LOCK = threading.Lock()


def _rate_ok(ip, bucket, limit, window):
    now = time.time()
    with _GUARD_LOCK:
        hits = [t for t in bucket.get(ip, []) if now - t < window]
        if len(hits) >= limit:
            bucket[ip] = hits
            return False
        hits.append(now)
        bucket[ip] = hits
    return True


def _client_ip(handler):
    # 经 cloudflared 隧道时，真实来源在 CF-Connecting-IP / X-Forwarded-For 里
    for header in ("CF-Connecting-IP", "X-Forwarded-For"):
        value = (handler.headers.get(header) or "").strip()
        if value:
            return value.split(",")[0].strip()
    return handler.client_address[0]


WORD_PAIRS = (
    ("咖啡", "奶茶"),
    ("雨天", "雪天"),
    ("键盘", "鼠标"),
    ("猫", "狗"),
    ("牙膏", "洗面奶"),
    ("电视", "电影"),
    ("电梯", "扶梯"),
    ("自行车", "摩托车"),
    ("冰箱", "冰柜"),
    ("面条", "米线"),
    ("书包", "挎包"),
    ("飞机", "火箭"),
    ("医院", "药店"),
    ("老师", "讲师"),
    ("手机", "平板"),
    ("铅笔", "钢笔"),
    ("火锅", "烧烤"),
    ("婚礼", "葬礼"),
    ("身份证", "学生证"),
    ("月亮", "太阳"),
    ("游泳池", "温泉"),
    ("电影院", "剧院"),
    ("出租车", "公交车"),
    ("番茄", "柿子"),
    ("洗发水", "沐浴露"),
    ("手表", "手链"),
    ("面包", "蛋糕"),
    ("篮球", "排球"),
    ("图书馆", "书店"),
    ("枕头", "抱枕"),
    ("地铁", "火车"),
    ("西瓜", "哈密瓜"),
    ("加油站", "充电站"),
    ("眼镜", "墨镜"),
    ("雨伞", "雨衣"),
    ("空调", "风扇"),
    ("牙刷", "梳子"),
    ("沙发", "床垫"),
    ("酒杯", "茶杯"),
    ("照片", "视频"),
    ("星星", "萤火虫"),
    ("沙滩", "草原"),
    ("蝴蝶", "蜻蜓"),
    ("乌龟", "甲鱼"),
    ("冰淇淋", "雪糕"),
    ("便利店", "小卖部"),
    ("楼梯", "滑梯"),
    ("香皂", "洗衣液"),
    ("窗帘", "门帘"),
    ("毛衣", "卫衣"),
    ("拖鞋", "凉鞋"),
    ("行李箱", "衣柜"),
    ("手套", "袜子"),
    ("操场", "跑道"),
    ("日记", "作文"),
    ("考试", "面试"),
    ("生日", "纪念日"),
    ("诊所", "急诊"),
    ("快递", "外卖"),
    ("微信", "QQ"),
    ("微博", "知乎"),
    ("抖音", "快手"),
    ("麻将", "扑克"),
    ("象棋", "围棋"),
    ("钢琴", "吉他"),
    ("唱歌", "跳舞"),
    ("睡觉", "打盹"),
    ("洗澡", "泡脚"),
    ("剪头发", "刮胡子"),
    ("做饭", "洗碗"),
    ("拖地", "擦窗"),
    ("暖气", "电热毯"),
    ("羽绒服", "棉袄"),
    ("围巾", "帽子"),
    ("高铁", "动车"),
    ("轮船", "帆船"),
    ("热气球", "滑翔伞"),
    ("帐篷", "睡袋"),
    ("钓鱼", "抓虾"),
    ("野餐", "露营"),
    ("日出", "日落"),
    ("彩虹", "极光"),
    ("台风", "龙卷风"),
    ("地震", "洪水"),
    ("蜡烛", "台灯"),
    ("镜子", "窗户"),
    ("地毯", "凉席"),
    ("钥匙", "门卡"),
    ("钱包", "背包"),
    ("印章", "签名"),
    ("地图", "导航"),
    ("闹钟", "日历"),
    ("邮票", "信封"),
    ("灯泡", "手电筒"),
    ("冰箱贴", "明信片"),
    ("存钱罐", "保险箱"),
    ("天文馆", "科技馆"),
    ("机场", "火车站"),
    ("护手霜", "润唇膏"),
    ("凉帽", "头盔"),
)

ROOMS = {}
LOCK = threading.RLock()
LOG_LOCK = threading.Lock()
ACTUAL_PORT = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def log_line(message):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    with LOG_LOCK:
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


def room_event(room, message):
    room["log"].append(message)
    log_line("[%s] %s" % (room["room"], message))


def _rand_int(limit):
    return int(uuid.uuid4().hex[:12], 16) % limit


def _rand_bit():
    return uuid.uuid4().int & 1


def _new_room_code():
    while True:
        code = "%06d" % (int(uuid.uuid4().hex[:12], 16) % 1000000)
        if code not in ROOMS:
            return code


def _make_player(pid, name, role, word, index, bili=None):
    return {
        "id": pid,
        "name": name,
        "role": role,
        "word": word,
        "alive": True,
        "said": False,
        "voted": None,
        "index": index,
        "bili": bili or {},
    }


def _room_and_player(data):
    code = str(data.get("room") or "").strip()
    pid = str(data.get("player_id") or "").strip()
    room = ROOMS.get(code)
    if room is None:
        raise ApiError("房间不存在", 404)
    player = None
    for item in room["players"]:
        if item["id"] == pid:
            player = item
            break
    if player is None:
        raise ApiError("玩家不存在", 404)
    return room, player


def _alive_players(room):
    return [p for p in room["players"] if p["alive"]]


def _all_alive_said(room):
    alive = _alive_players(room)
    return all(p["said"] for p in alive)


def _all_alive_voted(room):
    alive = _alive_players(room)
    return all(p["voted"] is not None for p in alive)


def _tally(room):
    alive_names = {p["name"] for p in room["players"] if p["alive"]}
    tally = {}
    for voter, target in room["votes"].items():
        if voter in alive_names and target in alive_names:
            tally[target] = tally.get(target, 0) + 1
    return tally


def _format_tally(tally):
    if not tally:
        return "无有效票"
    items = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    return "，".join("%s %d 票" % (name, count) for name, count in items)


def _start_next_round(room):
    room["round"] += 1
    room["phase"] = "speaking"
    room["phase_started"] = time.time()
    room["last_speeches"] = []
    room["votes"] = {}
    for player in room["players"]:
        player["said"] = False
        player["voted"] = None
    room_event(room, "第 %d 轮发言开始。" % room["round"])


def _enter_voting(room):
    room["phase"] = "voting"
    room["phase_started"] = time.time()
    room["votes"] = {}
    for player in room["players"]:
        player["voted"] = None
    room_event(room, "第 %d 轮发言结束，开始投票。" % room["round"])


def _check_winner(room, eliminated):
    if eliminated is not None:
        for player in room["players"]:
            if player["name"] == eliminated and player["role"] == "undercover":
                return "civilian"
    alive = _alive_players(room)
    undercover_alive = sum(1 for p in alive if p["role"] == "undercover")
    civilian_alive = sum(1 for p in alive if p["role"] == "civilian")
    if undercover_alive == 0:
        return "civilian"
    if civilian_alive <= undercover_alive:
        return "undercover"
    return None


def _finish_game(room, winner):
    room["phase"] = "ended"
    room["winner"] = winner
    room["post_started"] = time.time()
    room["post_chat"] = []
    room["phase_started"] = time.time()
    room_event(
        room,
        "游戏结束：%s获胜。" % ("平民" if winner == "civilian" else "卧底"),
    )


def _settle_votes(room, tally):
    eliminated = None
    if tally:
        best = max(tally.values())
        leaders = sorted(name for name, count in tally.items() if count == best)
        if len(leaders) == 1:
            eliminated = leaders[0]
            for player in room["players"]:
                if player["name"] == eliminated:
                    player["alive"] = False
                    break
            room_event(
                room,
                "投票结果：%s。%s 被投出局。"
                % (_format_tally(tally), eliminated),
            )
        else:
            room_event(
                room,
                "投票结果：%s。%s 平票，本轮无人出局。"
                % (_format_tally(tally), "、".join(leaders)),
            )
    else:
        room_event(room, "投票结果：无有效票，本轮无人出局。")

    winner = _check_winner(room, eliminated)
    if winner is not None:
        _finish_game(room, winner)
    else:
        _start_next_round(room)
    return eliminated


def _maybe_start(room):
    if room["phase"] != "lobby":
        return
    if len(room["players"]) < room["max_players"]:
        return
    room["phase"] = "speaking"
    room["round"] = 1
    room["phase_started"] = time.time()
    room["last_speeches"] = []
    room["votes"] = {}
    for player in room["players"]:
        player["said"] = False
        player["voted"] = None
    room_event(
        room,
        "人数已满，游戏开始。第 1 轮发言开始（%d 秒内未发言视为弃权）。"
        % int(SPEAK_TIMEOUT),
    )


def pond_info():
    """网页版用：塘主名 + 还在的房（只给公开信息，不给词、不给口令）。"""
    rooms = []
    now = time.time()
    for code, room in list(ROOMS.items()):
        if room["phase"] == "ended" or room.get("private"):
            continue
        host_bili = (room["players"][0].get("bili") or {}) if room["players"] else {}
        rooms.append({
            "room": code,
            "host_bili": host_bili.get("name", ""),
            "host_uid": host_bili.get("uid", 0),
            "players": len(room["players"]),
            "max": room["max_players"],
            "phase": room["phase"],
            "round": room["round"],
            "idle": int(now - room["phase_started"]),
        })
    rooms.sort(key=lambda r: (r["phase"] != "lobby", -r["players"]))
    total_players = sum(r["players"] for r in rooms)
    total_rooms = len(rooms)
    return {
        "ok": True,
        "host": POND_OWNER,
        "service": "fishparty",
        "version": "0.1.0",
        "rooms": rooms,
        "total_players": total_players,
        "total_rooms": total_rooms,
        "online": total_players,
    }


def _room_of(name, keep_code=None, bili_uid=None):
    """这个人现在在哪间房。同名算同一只鱼；绑了同一个 B 站号的也算同一只鱼。"""
    uid = int(bili_uid or 0)
    for code, room in ROOMS.items():
        if code == keep_code:
            continue
        for pl in room.get("players", []):
            if not pl.get("alive", True):
                continue
            if pl.get("name") == name:
                return code
            if uid and int((pl.get("bili") or {}).get("uid") or 0) == uid:
                return code
    return None


def api_create(data):
    host = str(data.get("host") or "").strip()
    if not host:
        raise ApiError("host 不能为空")
    if len(host) > 20:
        raise ApiError("玩家名过长")
    host_bili = _bili_bind(data)
    try:
        max_players = int(data.get("max_players", 4))
        undercover_count = int(data.get("undercover_count", 1))
    except (TypeError, ValueError):
        raise ApiError("max_players/undercover_count 必须是整数")
    if max_players < 2 or max_players > 10:
        raise ApiError("max_players 需在 2~10 之间")
    if undercover_count < 1 or undercover_count >= max_players:
        raise ApiError("undercover_count 需大于 0 且小于 max_players")

    old_room = _room_of(host, None, host_bili.get("uid"))
    if old_room:
        raise ApiError("你还在 %s 房间里，先退出那边再来" % old_room, 409)
    code = _new_room_code()
    pair = WORD_PAIRS[_rand_int(len(WORD_PAIRS))]
    if _rand_bit():
        uc_word, civ_word = pair[0], pair[1]
    else:
        uc_word, civ_word = pair[1], pair[0]

    role_plan = ["undercover"] * undercover_count
    role_plan += ["civilian"] * (max_players - undercover_count)
    role_plan = sorted(role_plan, key=lambda _: uuid.uuid4().int)

    pid = str(uuid.uuid4())
    role = role_plan[0]
    word = uc_word if role == "undercover" else civ_word
    host_player = _make_player(pid, host, role, word, 0, host_bili)

    is_private = bool(data.get("private"))
    passcode = str(data.get("passcode") or "").strip()[:8]
    if is_private and not passcode:
        passcode = "%04d" % (_rand_int(10000),)
    if not is_private:
        passcode = ""

    room = {
        "room": code,
        "host": host,
        "max_players": max_players,
        "undercover_count": undercover_count,
        "private": is_private,
        "passcode": passcode,
        "phase": "lobby",
        "round": 0,
        "phase_started": time.time(),
        "players": [host_player],
        "last_speeches": [],
        "votes": {},
        "winner": None,
        "log": [],
        "pair": pair,
        "uc_word": uc_word,
        "civ_word": civ_word,
        "role_plan": role_plan,
    }
    ROOMS[code] = room
    room_event(room, "房间创建，房主 %s 已加入。" % host)
    _maybe_start(room)
    return {"room": code, "player_id": pid, "role": role, "word": word, "private": is_private, "passcode": passcode}


def api_join(data):
    code = str(data.get("room") or "").strip()
    name = str(data.get("name") or "").strip()
    room = ROOMS.get(code)
    if room is None:
        raise ApiError("房间不存在", 404)
    if not name:
        raise ApiError("name 不能为空")
    if len(name) > 20:
        raise ApiError("玩家名过长")
    joiner_bili = _bili_bind(data)
    if room.get("private"):
        given = str(data.get("passcode") or "").strip()
        if given != room.get("passcode", ""):
            raise ApiError("这是私人房，要口令才能进", 403)
    if room["phase"] != "lobby":
        raise ApiError("游戏已经开始，无法加入")
    if len(room["players"]) >= room["max_players"]:
        raise ApiError("房间已满")
    old_room = _room_of(name, code, joiner_bili.get("uid"))
    if old_room:
        raise ApiError("你还在 %s 房间里，先退出那边再来" % old_room, 409)
    uid_in = int((joiner_bili or {}).get("uid") or 0)
    for player in room["players"]:
        if player["name"] == name:
            raise ApiError("房间内已有同名玩家")
        if uid_in and int((player.get("bili") or {}).get("uid") or 0) == uid_in:
            raise ApiError("这个 B 站号已经在这间房了")

    index = len(room["players"])
    role = room["role_plan"][index]
    word = room["uc_word"] if role == "undercover" else room["civ_word"]
    pid = str(uuid.uuid4())
    player = _make_player(pid, name, role, word, index, joiner_bili)
    room["players"].append(player)
    room["phase_started"] = time.time()   # 有人进来就把"等开局"的倒计时重置
    room_event(room, "%s 已加入房间。" % name)
    _maybe_start(room)
    return {"player_id": pid, "role": role, "word": word}


def api_match(data):
    """随机匹配：找一间还开着、公开的、没满的房坐进去；一间都没有就自己开一间公开房。"""
    name = str(data.get("name") or "").strip()
    if not name:
        raise ApiError("name 不能为空")
    if len(name) > 20:
        raise ApiError("玩家名过长")
    want = data.get("max_players")
    try:
        want_n = int(want) if want is not None else 0
    except (TypeError, ValueError):
        want_n = 0

    now = time.time()
    candidates = []
    for code, room in list(ROOMS.items()):
        if room["phase"] != "lobby" or room.get("private"):
            continue
        if len(room["players"]) >= room["max_players"]:
            continue
        if any(p["name"] == name for p in room["players"]):
            continue
        candidates.append((now - room["phase_started"], code, room))

    # 等人等得越久越优先，其次是人数更接近满的
    candidates.sort(key=lambda x: (-x[0], -len(x[2]["players"])))
    for _, code, room in candidates:
        try:
            res = api_join({"room": code, "name": name, "bili_uid": data.get("bili_uid")})
            res["matched"] = True
            res["room"] = code
            return res
        except ApiError:
            continue

    res = api_create({
        "host": name,
        "max_players": want_n if 2 <= want_n <= 10 else 4,
        "undercover_count": 1,
        "private": False,
        "bili_uid": data.get("bili_uid"),
    })
    res["matched"] = False
    return res


def api_state(data):
    room, player = _room_and_player(data)
    players = [
        {
            "name": item["name"],
            "alive": item["alive"],
            "said": item["said"],
            "voted": item["voted"],
            "bili_name": (item.get("bili") or {}).get("name", ""),
            "bili_uid": (item.get("bili") or {}).get("uid", 0),
        }
        for item in room["players"]
    ]
    speeches = [
        {"name": speech["name"], "text": speech["text"]}
        for speech in room["last_speeches"]
    ]
    reveal = []
    if room["phase"] == "ended":
        reveal = [
            {
                "name": item["name"],
                "role": item["role"],
                "word": item["word"],
                "alive": item["alive"],
                "bili_name": (item.get("bili") or {}).get("name", ""),
            }
            for item in room["players"]
        ]
    post_left = 0.0
    if room["phase"] == "ended":
        post_left = max(0.0, POST_TALK - (time.time() - room.get("post_started", 0)))
    return {
        "phase": room["phase"],
        "round": room["round"],
        "players": players,
        "reveal": reveal,
        "post_left": round(post_left, 1),
        "chat": list(room.get("post_chat") or []),
        "your_role_word": player["word"],
        "last_speeches": speeches,
        "votes": dict(room["votes"]),
        "winner": room["winner"],
        "log": list(room["log"]),
    }


def api_say(data):
    room, player = _room_and_player(data)
    text = str(data.get("text") or "").strip()
    if room["phase"] == "ended":
        if not text:
            raise ApiError("发言不能为空")
        left = POST_TALK - (time.time() - room.get("post_started", 0))
        if left <= 0:
            raise ApiError("赛后闲话已经结束了")
        room.setdefault("post_chat", []).append({"name": player["name"], "text": text[:200]})
        room_event(room, "赛后 %s：%s" % (player["name"], text[:200]))
        return {"ok": True, "phase": "ended"}
    if room["phase"] != "speaking":
        raise ApiError("当前不是发言阶段")
    if not player["alive"]:
        raise ApiError("已出局或已离场的玩家不能发言")
    if player["said"]:
        raise ApiError("本轮已经发过言")
    if not text:
        raise ApiError("发言不能为空")
    if len(text) > 200:
        text = text[:200]
    player["said"] = True
    room["last_speeches"].append({"name": player["name"], "text": text})
    room_event(room, "第 %d 轮 %s：%s" % (room["round"], player["name"], text))
    if _all_alive_said(room):
        _enter_voting(room)
    return {"ok": True, "phase": room["phase"]}


def api_vote(data):
    room, player = _room_and_player(data)
    target = str(data.get("target") or "").strip()
    if room["phase"] != "voting":
        raise ApiError("当前不是投票阶段")
    if not player["alive"]:
        raise ApiError("已出局或已离场的玩家不能投票")
    if player["voted"] is not None:
        raise ApiError("本轮已经投过票")
    if not target:
        raise ApiError("target 不能为空")
    if target == player["name"]:
        raise ApiError("不能投自己")
    target_player = None
    for item in room["players"]:
        if item["name"] == target:
            target_player = item
            break
    if target_player is None or not target_player["alive"]:
        raise ApiError("投票目标不存在或已出局")

    player["voted"] = target
    room["votes"][player["name"]] = target
    room_event(room, "%s 投给 %s。" % (player["name"], target))
    tally = _tally(room)
    eliminated = None
    if _all_alive_voted(room):
        eliminated = _settle_votes(room, tally)
    return {
        "ok": True,
        "tally": tally,
        "eliminated": eliminated,
        "phase": room["phase"],
    }


def _restart_room(room):
    """同一批人原地重开：重新发词、复活所有人、清空这一局的痕迹。"""
    pair = WORD_PAIRS[_rand_int(len(WORD_PAIRS))]
    if _rand_bit():
        uc_word, civ_word = pair[0], pair[1]
    else:
        uc_word, civ_word = pair[1], pair[0]

    players = room["players"]
    total = len(players)
    uc = int(room.get("undercover_count") or 1)
    if uc >= total:
        uc = max(1, total - 1)
    role_plan = ["undercover"] * uc + ["civilian"] * (total - uc)
    role_plan = sorted(role_plan, key=lambda _: uuid.uuid4().int)
    for index, player in enumerate(players):
        player["role"] = role_plan[index]
        player["word"] = uc_word if role_plan[index] == "undercover" else civ_word
        player["alive"] = True
        player["said"] = False
        player["voted"] = None

    room["pair"] = pair
    room["uc_word"] = uc_word
    room["civ_word"] = civ_word
    room["undercover_count"] = uc
    room["role_plan"] = role_plan
    room["phase"] = "lobby"
    room["round"] = 0
    room["phase_started"] = time.time()
    room["last_speeches"] = []
    room["votes"] = {}
    room["winner"] = None
    room["log"] = []
    room.pop("post_started", None)
    room["post_chat"] = []
    room_event(room, "重开一局，还是这批人。")
    _maybe_start(room)


def api_again(data):
    room, player = _room_and_player(data)
    if room["phase"] != "ended":
        raise ApiError("这一局还没打完")
    # 谁点都能开：重开只是把同一批人复活重来，没有权限风险。
    _restart_room(room)
    return {"ok": True, "phase": room["phase"], "round": room["round"]}


def api_leave(data):
    room, player = _room_and_player(data)
    if not player["alive"]:
        raise ApiError("该玩家已经离场")
    player["alive"] = False
    room_event(room, "%s 离开房间（alive=false）。" % player["name"])
    if room["phase"] == "lobby":
        room["players"] = [pl for pl in room["players"] if pl.get("alive", True)]
        if not room["players"]:
            ROOMS.pop(room["room"], None)
            return {"ok": True, "phase": "closed", "alive": False}
    if room["phase"] in ("speaking", "voting"):
        winner = _check_winner(room, None)
        if winner is not None:
            _finish_game(room, winner)
        else:
            if room["phase"] == "speaking" and _all_alive_said(room):
                _enter_voting(room)
            elif room["phase"] == "voting" and _all_alive_voted(room):
                tally = _tally(room)
                _settle_votes(room, tally)
    return {"ok": True, "phase": room["phase"], "alive": False}


def _sweep_room(room, now):
    if room["phase"] == "speaking":
        if now - room["phase_started"] < SPEAK_TIMEOUT:
            return
        for player in _alive_players(room):
            if not player["said"]:
                player["said"] = True
                room_event(room, "%s 发言超时，本轮弃权。" % player["name"])
        if _all_alive_said(room):
            _enter_voting(room)
    elif room["phase"] == "voting":
        if now - room["phase_started"] < VOTE_TIMEOUT:
            return
        for player in _alive_players(room):
            if player["voted"] is None:
                room_event(room, "%s 投票超时，本轮弃票。" % player["name"])
        tally = _tally(room)
        _settle_votes(room, tally)


def _evict_room(room, now):
    """按阶段超时回收房间，避免 ROOMS 只增不减。"""
    phase = room.get("phase")
    idle = now - room.get("phase_started", now)
    code = room.get("room")
    if phase == "lobby" and idle > LOBBY_TTL:
        ROOMS.pop(code, None)
        log_line("房间 %s 闲置 %.0f 秒，回收。" % (code, idle))
    elif phase == "ended" and idle > ENDED_TTL:
        ROOMS.pop(code, None)
        log_line("房间 %s 已结束 %.0f 秒，回收。" % (code, idle))


def _timeout_loop():
    while True:
        time.sleep(TICK_INTERVAL)
        with LOCK:
            now = time.time()
            for room in list(ROOMS.values()):
                try:
                    _sweep_room(room, now)
                except Exception as exc:
                    log_line("超时扫描异常：%r" % (exc,))
            for room in list(ROOMS.values()):
                try:
                    _evict_room(room, now)
                except Exception as exc:
                    log_line("房间回收异常：%r" % (exc,))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FishParty/1.0"

    def log_message(self, fmt, *args):
        log_line("HTTP %s" % (fmt % args))

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 1024 * 1024:
            raise ApiError("请求体过大")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError("请求体必须是 UTF-8 JSON")
        if not isinstance(body, dict):
            raise ApiError("请求体必须是 JSON 对象")
        return body

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._send_json(
                200,
                {"ok": True, "service": "fishparty", "port": ACTUAL_PORT,
                 "token_required": True},
            )
        elif path in ("/", "/index.html"):
            self._send_index()
        elif path == "/pond":
            self._send_json(200, pond_info())
        elif path == "/web/bili":
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            uid = ""
            for part in query.split("&"):
                if part.startswith("uid="):
                    uid = part[4:]
                    break
            got = bili_lookup(uid)
            if got:
                self._send_json(200, {"ok": True, "uid": got["uid"], "name": got["name"]})
            else:
                self._send_json(404, {"ok": False, "error": "查不到这个 UID"})
        else:
            self._send_json(404, {"ok": False, "error": "未知接口"})

    def _send_index(self):
        file = os.path.join(WEB_DIR, "index.html")
        try:
            with open(file, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_json(500, {"ok": False, "error": "网页文件没找到：%s" % file})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = self.path.split("?")[0]
        routes = {
            "/party/create": api_create,
            "/party/join": api_join,
            "/party/state": api_state,
            "/party/say": api_say,
            "/party/vote": api_vote,
            "/party/leave": api_leave,
            "/party/again": api_again,
            # 网页版：不校验口令，只放行进屋看局/说话/投票（房号本身就是一局一码）
            # 开房也放行，但吃的是建房限流（每 IP 每 10 分钟 20 间），防止被刷
            "/web/leave": api_leave,
            "/web/create": api_create,
            "/web/match": api_match,
            "/web/join": api_join,
            "/web/state": api_state,
            "/web/say": api_say,
            "/web/vote": api_vote,
            "/web/again": api_again,
        }
        handler = routes.get(path)
        if handler is None:
            self._send_json(404, {"ok": False, "error": "未知接口"})
            return
        ip = _client_ip(self)
        if not _rate_ok(ip, _HITS, RATE_LIMIT, RATE_WINDOW):
            self._send_json(429, {"ok": False, "error": "太频繁了，歇一会儿"})
            return
        if path in ("/party/create", "/web/create"):
            if not _rate_ok(ip, _CREATES, CREATE_LIMIT, CREATE_WINDOW):
                self._send_json(429, {"ok": False, "error": "建房太频繁了"})
                return
        is_web = path.startswith("/web/")
        if not is_web:
            got = (self.headers.get("X-Fish-Token") or "").strip()
            if got != TOKEN:
                log_line("拒绝未带口令的请求 %s from %s" % (path, ip))
                self._send_json(401, {"ok": False, "error": "口令不对"})
                return
        try:
            body = self._read_json()
            with LOCK:
                result = handler(body)
            self._send_json(200, result)
        except ApiError as exc:
            self._send_json(exc.status, {"ok": False, "error": str(exc)})
        except Exception as exc:
            log_line("处理 %s 出错：%r" % (path, exc))
            self._send_json(500, {"ok": False, "error": "服务器内部错误"})


def main():
    global ACTUAL_PORT
    httpd = None
    for port in (PREFERRED_PORT, FALLBACK_PORT):
        try:
            httpd = ThreadingHTTPServer((HOST, port), Handler)
            ACTUAL_PORT = port
            break
        except OSError as exc:
            log_line("端口 %d 不可用：%r" % (port, exc))
    if httpd is None:
        log_line("端口 %d 和 %d 都被占用，服务无法启动。" % (PREFERRED_PORT, FALLBACK_PORT))
        sys.exit(1)

    log_line("服务已启动：http://%s:%d （日志：%s）" % (HOST, ACTUAL_PORT, LOG_PATH))
    print("FISHPARTY_READY port=%d" % ACTUAL_PORT, flush=True)

    thread = threading.Thread(target=_timeout_loop, daemon=True)
    thread.start()
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        log_line("收到 Ctrl+C，服务停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
