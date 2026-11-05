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
LOBBY_TTL = 600.0   # 大厅里没人开局的房间，闲置 10 分钟回收
ENDED_TTL = 300.0   # 已结束的房间，留 5 分钟给玩家看结果，然后回收

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "server.log")

WORD_PAIRS = (
    ("咖啡", "奶茶"),
    ("雨天", "雪天"),
    ("键盘", "鼠标"),
    ("猫", "狗"),
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


def _make_player(pid, name, role, word, index):
    return {
        "id": pid,
        "name": name,
        "role": role,
        "word": word,
        "alive": True,
        "said": False,
        "voted": None,
        "index": index,
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


def api_create(data):
    host = str(data.get("host") or "").strip()
    if not host:
        raise ApiError("host 不能为空")
    if len(host) > 20:
        raise ApiError("玩家名过长")
    try:
        max_players = int(data.get("max_players", 4))
        undercover_count = int(data.get("undercover_count", 1))
    except (TypeError, ValueError):
        raise ApiError("max_players/undercover_count 必须是整数")
    if max_players < 2 or max_players > 10:
        raise ApiError("max_players 需在 2~10 之间")
    if undercover_count < 1 or undercover_count >= max_players:
        raise ApiError("undercover_count 需大于 0 且小于 max_players")

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
    host_player = _make_player(pid, host, role, word, 0)

    room = {
        "room": code,
        "host": host,
        "max_players": max_players,
        "undercover_count": undercover_count,
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
    return {"room": code, "player_id": pid, "role": role, "word": word}


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
    if room["phase"] != "lobby":
        raise ApiError("游戏已经开始，无法加入")
    if len(room["players"]) >= room["max_players"]:
        raise ApiError("房间已满")
    for player in room["players"]:
        if player["name"] == name:
            raise ApiError("房间内已有同名玩家")

    index = len(room["players"])
    role = room["role_plan"][index]
    word = room["uc_word"] if role == "undercover" else room["civ_word"]
    pid = str(uuid.uuid4())
    player = _make_player(pid, name, role, word, index)
    room["players"].append(player)
    room_event(room, "%s 已加入房间。" % name)
    _maybe_start(room)
    return {"player_id": pid, "role": role, "word": word}


def api_state(data):
    room, player = _room_and_player(data)
    players = [
        {
            "name": item["name"],
            "alive": item["alive"],
            "said": item["said"],
            "voted": item["voted"],
        }
        for item in room["players"]
    ]
    speeches = [
        {"name": speech["name"], "text": speech["text"]}
        for speech in room["last_speeches"]
    ]
    return {
        "phase": room["phase"],
        "round": room["round"],
        "players": players,
        "your_role_word": player["word"],
        "last_speeches": speeches,
        "votes": dict(room["votes"]),
        "winner": room["winner"],
        "log": list(room["log"]),
    }


def api_say(data):
    room, player = _room_and_player(data)
    text = str(data.get("text") or "").strip()
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


def api_leave(data):
    room, player = _room_and_player(data)
    if not player["alive"]:
        raise ApiError("该玩家已经离场")
    player["alive"] = False
    room_event(room, "%s 离开房间（alive=false）。" % player["name"])
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
                {"ok": True, "service": "fishparty", "port": ACTUAL_PORT},
            )
        else:
            self._send_json(404, {"ok": False, "error": "未知接口"})

    def do_POST(self):
        path = self.path.split("?")[0]
        routes = {
            "/party/create": api_create,
            "/party/join": api_join,
            "/party/state": api_state,
            "/party/say": api_say,
            "/party/vote": api_vote,
            "/party/leave": api_leave,
        }
        handler = routes.get(path)
        if handler is None:
            self._send_json(404, {"ok": False, "error": "未知接口"})
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
