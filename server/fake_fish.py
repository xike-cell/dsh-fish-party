# -*- coding: utf-8 -*-
"""谁是卧底的脚本化假鱼与自动演示。

两只假鱼：
  汐禾     —— 说话短，爱用省略号，偶尔在括号里说心里话。
  大肥鱼1   —— 说话直，爱咋呼，句子短促。
另有两个由同一脚本扮演的机器人：大肥鱼2、大肥鱼3。

运行：
    <python> fake_fish.py --demo

脚本会自动启动 party_server.py（或用本机已在运行的 8770/8771 服务），
通过 HTTP 接口打完一局并生成 demo_transcript.txt，随后追加一局断线测试。
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_PATH = os.path.join(BASE_DIR, "party_server.py")
TRANSCRIPT_PATH = os.path.join(BASE_DIR, "demo_transcript.txt")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class FishError(Exception):
    pass


def _load_token():
    """服务端口令：环境变量优先，其次同目录 party_token.txt。"""
    env = (os.environ.get("FISH_PARTY_TOKEN") or "").strip()
    if env:
        return env
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "party_token.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def http_post(port, path, payload):
    url = "http://127.0.0.1:%d%s" % (port, path)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    token = _load_token()
    if token:
        headers["X-Fish-Token"] = token
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with OPENER.open(request, timeout=8.0) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            message = json.loads(raw).get("error") or raw
        except Exception:
            message = raw
        raise FishError("%s -> HTTP %d：%s" % (path, exc.code, message))
    except urllib.error.URLError as exc:
        raise FishError("%s -> 连接失败：%s" % (path, exc.reason))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise FishError("%s -> 返回不是 JSON：%r" % (path, raw[:200]))
    if not isinstance(data, dict):
        raise FishError("%s -> 返回不是 JSON 对象" % path)
    if data.get("ok") is False:
        raise FishError("%s -> %s" % (path, data.get("error")))
    return data


class ServerHandle:
    def __init__(self):
        self.port = None
        self.proc = None
        self._lines = []
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._owned = False

    def _probe(self, port):
        try:
            with OPENER.open("http://127.0.0.1:%d/health" % port, timeout=0.8) as response:
                data = json.loads(response.read().decode("utf-8"))
            return isinstance(data, dict) and data.get("service") == "fishparty"
        except Exception:
            return False

    def start(self):
        for port in (8770, 8771):
            if self._probe(port):
                self.port = port
                print("使用已在运行的服务：http://127.0.0.1:%d" % port)
                return self

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [sys.executable, SERVER_PATH],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self._owned = True

        def reader():
            try:
                for line in self.proc.stdout:
                    line = line.rstrip("\r\n")
                    with self._lock:
                        self._lines.append(line)
                        if len(self._lines) > 200:
                            self._lines.pop(0)
                    match = re.search(r"FISHPARTY_READY port=(\d+)", line)
                    if match and self.port is None:
                        self.port = int(match.group(1))
                        self._ready.set()
            except Exception:
                pass
            finally:
                self._ready.set()

        threading.Thread(target=reader, daemon=True).start()
        self._ready.wait(15)
        if self.port is None:
            with self._lock:
                tail = "\n".join(self._lines[-20:])
            raise FishError("服务端未能启动：\n" + tail)

        deadline = time.time() + 10
        while time.time() < deadline:
            if self._probe(self.port):
                break
            time.sleep(0.1)
        else:
            raise FishError("服务端已打印就绪，但健康检查失败（端口 %d）" % self.port)
        print("已启动服务：http://127.0.0.1:%d" % self.port)
        return self

    def stop(self):
        if self._owned and self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None


WORD_TRAITS = {
    "咖啡": ("苦苦的", "提神的", "热热的", "深色的"),
    "奶茶": ("甜甜的", "有奶味的", "带珍珠的", "冰凉的"),
    "雨天": ("湿漉漉的", "阴沉沉的", "凉飕飕的", "灰蒙蒙的"),
    "雪天": ("白茫茫的", "冷冰冰的", "亮晶晶的", "软绵绵的"),
    "键盘": ("硬邦邦的", "密密麻麻的", "会发光的", "敲起来响的"),
    "鼠标": ("滑溜溜的", "小小的", "有滚轮的", "点来点去的"),
    "猫": ("毛茸茸的", "软乎乎的", "喵喵叫的", "爱晒太阳的"),
    "狗": ("汪汪叫的", "摇尾巴的", "看家的", "爱啃骨头的"),
}

VAGUE_WORDS = (
    "好像", "可能", "大概", "差不多", "反正", "说不好", "说不太", "说不清",
    "不清楚", "应该是", "那种", "感觉", "也许", "或许", "吧", "……",
)

PREFIXES = (
    "我觉得", "应该是", "好像是", "大概是", "可能是", "反正是", "就是", "那种", "就",
)

CIVILIAN = {
    "xihe": "嗯……{d1}，{d2}。",
    "pangtou": "就{d1}啊！{d2}！这还用问？",
    "jingxi": "我觉得吧，应该是{d1}、{d2}那种。",
    "jinglang": "嗯，{d1}吧，{d2}那种感觉。",
}

SACRIFICIAL = {
    "xihe": "嗯……我觉得吧，好像是那种{d1}、{d2}？说不太准，反正大概是吧。",
    "pangtou": "呃……好像、大概是那种{d1}、{d2}吧？说不太准，反正差不多！",
    "jingxi": "我觉得吧，好像是那种{d1}、{d2}？说不太准，反正大概是吧。",
    "jinglang": "嗯……好像是{d1}、{d2}那种吧？说不太准，反正大概差不多。",
}

UC_ECHO = {
    "xihe": "嗯……就「{snippet}」那种吧。",
    "pangtou": "哈？「{snippet}」？差不多吧！反正就那样！",
    "jingxi": "嗯……「{snippet}」那种感觉吧，我也说不好。",
    "jinglang": "「{snippet}」吧……应该是那种感觉。",
}

UC_FALLBACK = {
    "xihe": "嗯……我的是那种平时挺常见的东西，先听你们说吧。",
    "pangtou": "我这个？就那种常见玩意儿！先听你们说！",
    "jingxi": "嗯……应该是那种比较常见的东西吧，我也不太会说。",
    "jinglang": "我这种……挺普通的，先听听你们的。",
}


def extract_snippet(text):
    for token in re.split(r"[，。！？、；：…\s]+", text):
        token = token.strip("「」“”\"'（）()")
        for prefix in PREFIXES:
            if token.startswith(prefix):
                token = token[len(prefix):]
                break
        if token.endswith("的") and 2 <= len(token) <= 7:
            return token
    return ""


def extract_snippet_from(speeches, own_name):
    for speech in reversed(speeches):
        if speech.get("name") == own_name:
            continue
        snippet = extract_snippet(speech.get("text", ""))
        if snippet:
            return snippet
    return ""


def vagueness_score(text):
    score = 0
    for word in VAGUE_WORDS:
        score += text.count(word) * 2
    score -= len(re.findall(r"[\u4e00-\u9fa5]{1,6}的", text))
    return score


class Fish:
    def __init__(self, port, name, persona):
        self.port = port
        self.name = name
        self.persona = persona
        self.room = None
        self.player_id = None
        self.word = None
        self.is_undercover = False
        self.sacrificial = False

    def create_room(self):
        response = http_post(
            self.port,
            "/party/create",
            {"host": self.name, "undercover_count": 1, "max_players": 4},
        )
        self.room = response["room"]
        self.player_id = response["player_id"]
        self.word = response.get("word") or response["role"]
        return response

    def join(self, room):
        response = http_post(
            self.port,
            "/party/join",
            {"room": room, "name": self.name},
        )
        self.room = room
        self.player_id = response["player_id"]
        self.word = response.get("word") or response["role"]
        return response

    def state(self):
        return http_post(
            self.port,
            "/party/state",
            {"room": self.room, "player_id": self.player_id},
        )

    def say(self, text):
        return http_post(
            self.port,
            "/party/say",
            {"room": self.room, "player_id": self.player_id, "text": text},
        )

    def vote(self, target):
        return http_post(
            self.port,
            "/party/vote",
            {"room": self.room, "player_id": self.player_id, "target": target},
        )

    def leave(self):
        return http_post(
            self.port,
            "/party/leave",
            {"room": self.room, "player_id": self.player_id},
        )

    def speech(self, state):
        traits = WORD_TRAITS.get(self.word)
        if traits is None:
            traits = ("圆圆的", "软软的", "常见的", "说不上来的")
        first, second = traits[0], traits[1]
        if self.is_undercover:
            snippet = extract_snippet_from(state.get("last_speeches", []), self.name)
            if snippet:
                return UC_ECHO[self.persona].format(snippet=snippet)
            return UC_FALLBACK[self.persona]
        if self.sacrificial and state.get("round") == 1:
            return SACRIFICIAL[self.persona].format(d1=first, d2=second)
        text = CIVILIAN[self.persona].format(d1=first, d2=second)
        if self.persona == "xihe" and state.get("round", 1) >= 2:
            text += "（差点说漏嘴）"
        return text

    def choose_vote(self, state):
        speeches = [
            speech
            for speech in state.get("last_speeches", [])
            if speech.get("name") != self.name
        ]
        if not speeches:
            raise FishError("%s 没有可投票的发言" % self.name)
        scored = []
        for index, speech in enumerate(speeches):
            scored.append(
                (vagueness_score(speech.get("text", "")), index, speech["name"])
            )
        if self.persona == "pangtou":
            best = max(scored, key=lambda item: (item[0], item[1]))
        else:
            best = max(scored, key=lambda item: (item[0], -item[1]))
        return best[2]


def find_info(state, name):
    for info in state.get("players", []):
        if info.get("name") == name:
            return info
    return None


def format_tally(tally):
    if not tally:
        return "无有效票"
    items = sorted(tally.items(), key=lambda item: (-item[1], item[0]))
    return "、".join("%s %d票" % (name, count) for name, count in items)


def assign_roles(players):
    counts = {}
    for fish in players:
        counts[fish.word] = counts.get(fish.word, 0) + 1
    if not counts:
        raise FishError("没有玩家，无法分配身份")
    undercover_word = min(counts, key=lambda word: (counts[word], word))
    for fish in players:
        fish.is_undercover = fish.word == undercover_word
    return undercover_word


def assign_sacrificial(players):
    for fish in players:
        fish.sacrificial = False
    # 让机器人承担第一轮“说得最含糊”的角色，保留汐禾和大肥鱼1各自的性格。
    for fish in players:
        if fish.name in ("大肥鱼2", "大肥鱼3") and not fish.is_undercover:
            fish.sacrificial = True
            return fish
    for fish in players:
        if not fish.is_undercover:
            fish.sacrificial = True
            return fish
    return None


def play_game(handle, players, room, title, note_lines=None, require_rounds=0):
    handle.write("\n========== %s ==========\n" % title)
    handle.write("房间号：%s\n" % room)
    handle.write("玩家：%s\n" % "、".join(fish.name for fish in players))
    for line in note_lines or []:
        handle.write(line + "\n")

    rounds_seen = set()
    while True:
        state = players[0].state()
        phase = state.get("phase")
        if phase == "ended":
            break
        if phase == "speaking":
            round_no = state.get("round")
            rounds_seen.add(round_no)
            handle.write("\n第 %d 轮 · 发言\n" % round_no)
            for fish in players:
                current = fish.state()
                if current.get("phase") != "speaking":
                    break
                info = find_info(current, fish.name)
                if info is None or not info["alive"] or info["said"]:
                    continue
                text = fish.speech(current)
                response = fish.say(text)
                if response.get("phase") != "speaking":
                    break
            current = players[0].state()
            for speech in current.get("last_speeches", []):
                handle.write("  %s：%s\n" % (speech["name"], speech["text"]))
            handle.flush()
            continue
        if phase == "voting":
            handle.write("\n第 %d 轮 · 投票\n" % state.get("round"))
            last_response = None
            for fish in players:
                current = fish.state()
                if current.get("phase") != "voting":
                    break
                info = find_info(current, fish.name)
                if info is None or not info["alive"] or info["voted"] is not None:
                    continue
                target = fish.choose_vote(current)
                response = fish.vote(target)
                handle.write("  %s → %s\n" % (fish.name, target))
                last_response = response
            if last_response is None:
                raise FishError("投票阶段没有任何有效投票")
            handle.write("票数：%s\n" % format_tally(last_response.get("tally", {})))
            eliminated = last_response.get("eliminated")
            handle.write(
                "出局：%s\n" % (eliminated if eliminated else "无（平票或全部弃票）")
            )
            handle.flush()
            continue
        raise FishError("未知阶段：%r" % phase)

    state = players[0].state()
    winner = state.get("winner")
    if winner == "civilian":
        winner_text = "平民获胜"
    elif winner == "undercover":
        winner_text = "卧底获胜"
    else:
        winner_text = "未结束"
    handle.write("\n最终结果：%s（winner=%s）\n" % (winner_text, winner))
    handle.write("身份揭晓：\n")
    for fish in players:
        info = find_info(state, fish.name)
        role = "卧底" if fish.is_undercover else "平民"
        suffix = "" if info and info["alive"] else "（已离场/出局）"
        handle.write("  %s：%s，词「%s」%s\n" % (fish.name, role, fish.word, suffix))
    handle.flush()
    return len(rounds_seen)


def main(argv):
    if "--demo" not in argv:
        print("用法：python %s --demo" % os.path.basename(__file__))
        return 2
    server = ServerHandle()
    try:
        server.start()
        port = server.port
        with open(TRANSCRIPT_PATH, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("谁是卧底 · 自动对局记录\n")
            handle.write("生成时间：%s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            handle.write("服务地址：http://127.0.0.1:%d\n" % port)
            handle.write("游戏模式：4 人局（3 平民 + 1 卧底），由 fake_fish.py 自动进行\n")
            handle.write("说明：本文件由 --demo 真实运行生成，未手工编辑。\n")

            main_players = [
                Fish(port, "汐禾", "xihe"),
                Fish(port, "大肥鱼1", "pangtou"),
                Fish(port, "大肥鱼2", "jingxi"),
                Fish(port, "大肥鱼3", "jinglang"),
            ]
            room = main_players[0].create_room()["room"]
            for fish in main_players[1:]:
                fish.join(room)
            assign_roles(main_players)
            assign_sacrificial(main_players)
            main_rounds = play_game(handle, main_players, room, "正式对局")
            handle.flush()
            main_state = main_players[0].state()
            print("正式对局结束：%d 轮，winner=%s" % (main_rounds, main_state.get("winner")))
            if main_rounds < 2:
                raise FishError("正式对局不足 2 轮（实际 %d 轮），不满足自测要求" % main_rounds)

            disconnect_players = [
                Fish(port, "汐禾", "xihe"),
                Fish(port, "大肥鱼1", "pangtou"),
                Fish(port, "大肥鱼2", "jingxi"),
                Fish(port, "大肥鱼3", "jinglang"),
            ]
            disconnect_room = disconnect_players[0].create_room()["room"]
            for fish in disconnect_players[1:]:
                fish.join(disconnect_room)
            assign_roles(disconnect_players)
            by_name = {fish.name: fish for fish in disconnect_players}
            civilians = {fish.name for fish in disconnect_players if not fish.is_undercover}
            leaver_name = next(
                name
                for name in ("大肥鱼3", "大肥鱼2", "大肥鱼1", "汐禾")
                if name in civilians
            )
            leaver = by_name[leaver_name]
            leave_response = leaver.leave()
            note = [
                "说明：验证 /party/leave 后游戏仍能推进到结束。",
                "断线：%s 调用 /party/leave 主动离场（alive=false，名字保留在玩家列表）。"
                % leaver_name,
                "离场响应：phase=%s，alive=%s"
                % (leave_response.get("phase"), leave_response.get("alive")),
            ]
            disconnect_rounds = play_game(
                handle,
                disconnect_players,
                disconnect_room,
                "断线测试",
                note_lines=note,
            )
            handle.flush()
            disconnect_state = disconnect_players[0].state()
            print(
                "断线测试结束：%d 轮，winner=%s"
                % (disconnect_rounds, disconnect_state.get("winner"))
            )

            handle.write("\n========== 记录说明 ==========\n")
            handle.write("正式对局轮数：%d\n" % main_rounds)
            handle.write("断线测试轮数：%d\n" % disconnect_rounds)
            handle.write("断线测试中离场玩家：%s\n" % leaver_name)

        print("demo_transcript.txt 已生成：%s" % TRANSCRIPT_PATH)
        print("DEMO_OK")
        return 0
    except Exception as exc:
        print("DEMO_FAILED: %r" % exc, file=sys.stderr)
        return 1
    finally:
        server.stop()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
