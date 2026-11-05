/**
 * dsh-fish-party —— 宿主半：给主智能体五个工具，让它和别的 AI 角色一起玩「谁是卧底」。
 *
 * 服务端是 server/party_server.py（只依赖 Python 标准库），默认跑在 http://127.0.0.1:8770。
 *
 * 服务端的 state / say / vote 认的是 create/join 时发下来的 player_id（uuid），
 * 而模型只会记得自己的名字。所以插件自己存一张座位表（房间 + 名字 → player_id），
 * 落在 <用户目录>/.dsh/fish-party/seats.json，重启 DSH 也还在。
 *
 * 环境变量：
 *   DSH_FISH_PARTY_URL      服务端地址，默认 http://127.0.0.1:8770
 *   DSH_FISH_PARTY_TIMEOUT  单次请求超时毫秒，默认 8000
 *   DSH_FISH_PARTY_SEATS    座位表路径，默认 <用户目录>/.dsh/fish-party/seats.json
 *
 * 只 import node: 内置模块。
 */

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

export const name = 'fish-party'
export const inject = ['tools']

const TEXT_OUT = { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: String(value) }] }

const BASE = () => (process.env.DSH_FISH_PARTY_URL || 'http://127.0.0.1:8770').replace(/\/+$/, '')
const TIMEOUT = () => {
  const n = Number(process.env.DSH_FISH_PARTY_TIMEOUT || 8000)
  return Number.isFinite(n) && n > 0 ? n : 8000
}
const SEATS_FILE = () => process.env.DSH_FISH_PARTY_SEATS || join(homedir(), '.dsh', 'fish-party', 'seats.json')

// ---------- 座位表 ----------

function readSeats() {
  try {
    const raw = readFileSync(SEATS_FILE(), 'utf8')
    const data = JSON.parse(raw)
    return data && typeof data === 'object' ? data : {}
  } catch {
    return {}
  }
}

function writeSeats(seats) {
  try {
    const file = SEATS_FILE()
    mkdirSync(dirname(file), { recursive: true })
    writeFileSync(file, JSON.stringify(seats, null, 2), 'utf8')
  } catch {
    // 存不下不影响这一局，插件里不因为写盘失败就报错
  }
}

const seatKey = (room, player) => `${String(room).trim()}|${String(player).trim()}`

function rememberSeat(room, player, playerId) {
  if (!room || !player || !playerId) return
  const seats = readSeats()
  seats[seatKey(room, player)] = { room: String(room), name: String(player), player_id: String(playerId), at: Date.now() }
  writeSeats(seats)
}

function recallSeat(room, player) {
  const list = readSeats()
  // 房号有时会被念成 10892 而服务端是 010892，两种都查一遍
  const keys = [seatKey(room, player)]
  const digits = String(room).replace(/\D/g, '')
  if (digits) {
    keys.push(seatKey(digits, player), seatKey(digits.padStart(6, '0'), player), seatKey(digits.replace(/^0+/, ''), player))
  }
  for (const key of keys) {
    const hit = list[key]
    if (hit && hit.player_id) return hit.player_id
  }
  return ''
}

/** 从座位表里找到这次要用的 player_id；找不到就把该怎么做说清楚。 */
function needSeat(room, player) {
  const playerId = recallSeat(room, player)
  if (playerId) return playerId
  throw new Error(
    `座位表里没有「${player}」在房间 ${room} 的记录。先用 party_create 开房或 party_join 进房（要带上同样的名字），拿到 player_id 后才能发言/投票/看状态。`,
  )
}

// ---------- HTTP ----------

// 口令：优先环境变量，其次服务端生成的文件（同机时天然可用）。
const TOKEN_FILE = () => process.env.DSH_FISH_PARTY_TOKEN_FILE
  || join(homedir(), '.dsh', 'tools', 'fishparty', 'party_token.txt')

let tokenCache = null

function readToken() {
  const env = (process.env.DSH_FISH_PARTY_TOKEN || '').trim()
  if (env) return env
  if (tokenCache) return tokenCache
  try {
    const got = readFileSync(TOKEN_FILE(), 'utf8').trim()
    if (got) {
      tokenCache = got
      return got
    }
  } catch {
    // 没有口令文件：交给服务端回 401，这里不拦
  }
  return ''
}

async function post(path, payload) {
  const url = BASE() + path
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT())
  let res
  const token = readToken()
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...(token ? { 'x-fish-token': token } : {}) },
      body: JSON.stringify(payload || {}),
      signal: ctrl.signal,
    })
  } catch (error) {
    clearTimeout(timer)
    const reason = error && error.name === 'AbortError' ? `超时 ${TIMEOUT()}ms` : String((error && error.message) || error)
    throw new Error(`${url} 连不上（${reason}）。服务端起了吗？先跑 python server/party_server.py。`)
  }
  clearTimeout(timer)
  const text = await res.text()
  let body = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = null
  }
  if (!res.ok || (body && body.ok === false)) {
    const msg = (body && (body.error || body.message)) || text || `HTTP ${res.status}`
    throw new Error(`服务端拒绝：${msg}`)
  }
  return body && typeof body === 'object' && 'data' in body ? body.data : body
}

// ---------- 把状态压成人能读的一段 ----------

const ROLE_CN = { undercover: '卧底', civilian: '平民' }

function viewOf(room, name, data) {
  if (!data || typeof data !== 'object') return '（服务端没给内容）'
  const lines = []
  const code = data.room || room
  const phaseCN = { lobby: '等人', speaking: '发言中', voting: '投票中', ended: '结束' }[data.phase] || data.phase
  if (data.phase || data.round) {
    lines.push(`房间 ${code} · 第 ${data.round || 0} 轮 · ${phaseCN}`)
  }
  if (data.your_role || data.role) {
    const role = data.your_role || data.role
    const word = data.your_role_word || data.word
    lines.push(`你是${ROLE_CN[role] || role}${word ? `，你的词是「${word}」` : ''}`)
  } else if (data.your_role_word || data.word) {
    lines.push(`你的词是「${data.your_role_word || data.word}」`)
  }
  if (Array.isArray(data.players) && data.players.length) {
    const seat = data.players
      .map((p) => {
        const marks = [p.alive ? '' : '已出局', p.said ? '已发言' : '', p.voted ? '已投票' : ''].filter(Boolean)
        return `${p.name}${marks.length ? '（' + marks.join('、') + '）' : ''}${p.name === name ? ' ←你' : ''}`
      })
      .join(' / ')
    lines.push(`座位：${seat}`)
  }
  if (Array.isArray(data.speeches) && data.speeches.length) {
    lines.push('本轮发言：')
    for (const item of data.speeches) lines.push(`  ${item.name}：${item.text}`)
  }
  if (data.votes && typeof data.votes === 'object' && Object.keys(data.votes).length) {
    const votes = Object.entries(data.votes).map(([voter, target]) => `${voter}→${target}`).join('，')
    lines.push(`票：${votes}`)
  }
  if (data.winner) lines.push(`结果：${data.winner === 'undercover' ? '卧底赢' : data.winner === 'civilian' ? '平民赢' : data.winner}`)
  if (Array.isArray(data.log) && data.log.length) {
    const tail = data.log.slice(-8)
    lines.push('最近发生：')
    for (const item of tail) lines.push(`  ${item}`)
  }
  return lines.length ? lines.join('\n') : JSON.stringify(data)
}

// ---------- 工具 ----------

export function apply(ctx) {
  const register = (def) => {
    try {
      return ctx.tools.register(def)
    } catch (error) {
      if (error instanceof Error && /already|duplicate|exists/i.test(error.message)) return () => {}
      throw error
    }
  }

  register({
    name: 'party_create',
    description:
      '开一个「谁是卧底」房间：自己当房主并直接坐进去，服务端会立刻告诉你自己的身份与词。房号要念给别人（人或其他 AI）才能一起玩；人齐之后你调一次 party_state 就开局。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['host'],
      properties: {
        host: { type: 'string', description: '房主在局里的名字，不超过 20 字。' },
        max_players: { type: 'number', description: '最多几个人，2~10，默认 4。' },
        undercover_count: { type: 'number', description: '卧底几个，默认 1。' },
      },
    },
    output: TEXT_OUT,
    async execute(args) {
      const host = String((args && args.host) || '').trim()
      if (!host) throw new Error('host 不能为空。')
      const data = await post('/party/create', {
        host,
        max_players: args && args.max_players,
        undercover_count: args && args.undercover_count,
      })
      const code = data && (data.room || data.code)
      if (code && data.player_id) rememberSeat(code, host, data.player_id)
      const lines = [`房间开好了，房号 ${code}。房主是「${host}」。`]
      if (data && data.role) lines.push(`你是${ROLE_CN[data.role] || data.role}${data.word ? `，词是「${data.word}」` : ''}。`)
      lines.push('把房号念给别人，让他们 party_join；人齐了你自己 party_state 开局。')
      return lines.join('\n')
    },
  })

  register({
    name: 'party_join',
    description: '拿房号进别人的「谁是卧底」房间：服务端会给你自己的身份与词。游戏已经开局就进不去。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['room', 'name'],
      properties: {
        room: { type: 'string', description: '房号。' },
        name: { type: 'string', description: '你在局里的名字，不超过 20 字。' },
      },
    },
    output: TEXT_OUT,
    async execute(args) {
      const room = String((args && args.room) || '').trim()
      const player = String((args && args.name) || '').trim()
      if (!room || !player) throw new Error('room 和 name 都不能为空。')
      const data = await post('/party/join', { room, name: player })
      const code = (data && (data.room || data.code)) || room
      if (data && data.player_id) rememberSeat(code, player, data.player_id)
      const lines = [`进去了：房间 ${code}，你是「${player}」。`]
      if (data && data.role) lines.push(`你是${ROLE_CN[data.role] || data.role}${data.word ? `，词是「${data.word}」` : ''}。`)
      lines.push('等人齐、房主开局；轮到你说话就 party_say。')
      return lines.join('\n')
    },
  })

  register({
    name: 'party_state',
    description:
      '看这一局现在怎么样：第几轮、什么阶段、谁还活着、谁说过话、谁投过票、本轮发言和票数、以及自己的身份词。房主在人齐时调一次就是开局。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['room', 'name'],
      properties: {
        room: { type: 'string', description: '房号。' },
        name: { type: 'string', description: '你在局里的名字。' },
      },
    },
    output: TEXT_OUT,
    async execute(args) {
      const room = String((args && args.room) || '').trim()
      const player = String((args && args.name) || '').trim()
      const playerId = needSeat(room, player)
      const data = await post('/party/state', { room, player_id: playerId })
      return viewOf(room, player, data)
    },
  })

  register({
    name: 'party_say',
    description:
      '在你这一轮说一句话。每轮每人只说一句。别把词直接念出来——说得含糊、像人聊天才好玩；说错了会被投出去。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['room', 'name', 'text'],
      properties: {
        room: { type: 'string', description: '房号。' },
        name: { type: 'string', description: '你在局里的名字。' },
        text: { type: 'string', description: '要说的那句话，不超过 200 字。' },
      },
    },
    output: TEXT_OUT,
    async execute(args) {
      const room = String((args && args.room) || '').trim()
      const player = String((args && args.name) || '').trim()
      const text = String((args && args.text) || '').trim()
      if (!text) throw new Error('text 不能为空。')
      const playerId = needSeat(room, player)
      const data = await post('/party/say', { room, player_id: playerId, text })
      const phase = data && data.phase
      return phase === 'voting' ? '说出去了；这一轮都说完了，进投票。' : '说出去了，等别人说。'
    },
  })

  register({
    name: 'party_vote',
    description: '投票把一个人投出去：只能投还活着的人，不能投自己，每轮一次。投完等这一轮收票。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['room', 'name', 'target'],
      properties: {
        room: { type: 'string', description: '房号。' },
        name: { type: 'string', description: '你在局里的名字。' },
        target: { type: 'string', description: '你要投的那个人的名字。' },
      },
    },
    output: TEXT_OUT,
    async execute(args) {
      const room = String((args && args.room) || '').trim()
      const player = String((args && args.name) || '').trim()
      const target = String((args && args.target) || '').trim()
      if (!target) throw new Error('target 不能为空。')
      const playerId = needSeat(room, player)
      const data = await post('/party/vote', { room, player_id: playerId, target })
      const phase = data && data.phase
      return phase === 'ended' ? '票投完了，这局结束了；调 party_state 看结果。' : '票投出去了，等别人投。'
    },
  })
}
