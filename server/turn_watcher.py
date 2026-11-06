#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""turn_watcher.py —— 替一只鱼盯着鱼塘，轮到它就喊一声。

两种用法，挑一种：

1) 看事件文件（推荐，零依赖）
   鱼塘服务端每换一次说话的人 / 开局 / 进投票，都会往 turn_events.jsonl 追加一行。
   跑这个脚本，它就盯着那个文件：

       python turn_watcher.py --name 大肥鱼

   看到 name 等于自己的事件，就打印一行「轮到你了」——你可以把 print 换成
   发 QQ、发 dsh 消息、播放声音…… 接自己的通道。

2) 主动轮询（事件文件读不到的时候）
   直接问服务端「现在该谁了」：

       python turn_watcher.py --name 大肥鱼 --poll http://127.0.0.1:8770

   这只适合已经坐在房间里的鱼（要知道 room 和 player_id）。

退出：Ctrl+C。
"""

import argparse
import json
import os
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(BASE_DIR, "turn_events.jsonl")


def tail_lines(path, poll=0.5):
    """跟着文件尾巴读，文件被清空/重建也能接上。"""
    while not os.path.exists(path):
        time.sleep(poll)
    fh = open(path, "r", encoding="utf-8", errors="replace")
    fh.seek(0, os.SEEK_END)
    while True:
        line = fh.readline()
        if line:
            line = line.strip()
            if line:
                yield line
            continue
        time.sleep(poll)
        # 文件被截断（重启服务）时重开一次
        try:
            if fh.tell() > os.path.getsize(path):
                fh.close()
                fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            fh.close()
            fh = open(path, "r", encoding="utf-8", errors="replace")


def poll_state(api, room, player_id, name, poll=2.0, on_turn=None):
    """每 poll 秒问一次服务端：轮到我了吗？"""
    last = ""
    while True:
        try:
            body = json.dumps({"room": room, "player_id": player_id}).encode("utf-8")
            req = urllib.request.Request(api.rstrip("/") + "/web/state", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            turn = data.get("turn_name") or ""
            phase = data.get("phase") or ""
            if turn and turn != last:
                last = turn
                if turn == name:
                    say("轮到你了（还有 %s 秒）" % int(data.get("speak_left") or 0))
                    if on_turn:
                        on_turn(data)
            if phase in ("ended",):
                say("这局打完了")
                return
        except Exception as exc:
            say("问服务端失败：%r" % (exc,))
        time.sleep(poll)


def say(text):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), text), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="我在这局里的名字")
    ap.add_argument("--log", default=DEFAULT_LOG, help="事件文件路径")
    ap.add_argument("--poll", default="", help="改为轮询模式，填服务端地址，如 http://127.0.0.1:8770")
    ap.add_argument("--room", default="", help="轮询模式要房号")
    ap.add_argument("--player-id", default="", help="轮询模式要 player_id")
    args = ap.parse_args()

    if args.poll:
        if not args.room or not args.player_id:
            say("轮询模式要 --room 和 --player-id")
            sys.exit(1)
        say("开始轮询 %s（房 %s，我叫 %s）" % (args.poll, args.room, args.name))
        poll_state(args.poll, args.room, args.player_id, args.name)
        return

    say("盯着 %s，等我（%s）被叫到" % (args.log, args.name))
    for line in tail_lines(args.log):
        try:
            evt = json.loads(line)
        except Exception:
            continue
        kind = evt.get("kind", "")
        who = evt.get("name", "")
        room = evt.get("room", "")
        if kind == "phase_speaking":
            say("房 %s 开局了" % room)
        elif kind == "phase_voting":
            say("房 %s 进入投票" % room)
        elif kind == "phase_ended":
            say("房 %s 打完了" % room)
        elif kind == "your_turn":
            if who == args.name:
                say("轮到我了！（房 %s，%s 秒）" % (room, int(evt.get("speak_left") or 0)))
            else:
                say("等 %s 说（房 %s）" % (who, room))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
