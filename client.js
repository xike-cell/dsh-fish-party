/**
 * dsh-fish-party —— 设置页（前端半）。
 *
 * 只做一件事：把「服务端是谁、通不通、口令对不对、现在有几间房」摆在设置里，
 * 让人不用去翻命令行也能确认鱼塘是活的。
 * 数据全部走宿主挂的 /api/fish-party/*，这里不直接连服务端（免得跨域）。
 */

window.__ModuleLoader__.load({
  id: 'dsh-fish-party',
  factory: (require) => {
    const React = require('react')
    const h = React.createElement
    const { useEffect, useRef, useState } = React
    const API = '/api/fish-party'

    const CSS = `
.fp-wrap { display: flex; flex-direction: column; gap: 14px; padding: 4px 2px 28px; max-width: 860px; }
.fp-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.fp-title { font-size: 17px; font-weight: 600; letter-spacing: .2px; }
.fp-sub { font-size: 12px; opacity: .55; margin-top: 3px; line-height: 1.6; }
.fp-badges { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.fp-badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid transparent; white-space: nowrap; }
.fp-badge.ok { color: #1f7a46; background: rgba(35,170,90,.12); border-color: rgba(35,170,90,.35); }
.fp-badge.bad { color: #a3342c; background: rgba(210,70,60,.12); border-color: rgba(210,70,60,.35); }
.fp-badge.idle { color: #6b6f76; background: rgba(140,145,155,.12); border-color: rgba(140,145,155,.3); }
.fp-card { border: 1px solid rgba(140,145,155,.24); border-radius: 10px; padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
.fp-card-h { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.fp-card-h small { font-weight: 400; font-size: 11px; opacity: .55; }
.fp-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.fp-label { font-size: 12px; opacity: .7; min-width: 64px; }
.fp-input { flex: 1 1 260px; min-width: 180px; font: inherit; font-size: 13px; padding: 7px 10px; border-radius: 8px; border: 1px solid rgba(140,145,155,.34); background: rgba(127,127,127,.06); color: inherit; }
.fp-input:focus { outline: 2px solid rgba(90,140,255,.55); outline-offset: 1px; }
.fp-input.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; letter-spacing: .4px; }
.fp-btn { font: inherit; font-size: 12.5px; padding: 7px 14px; border-radius: 8px; border: 1px solid rgba(140,145,155,.34); background: rgba(127,127,127,.1); color: inherit; cursor: pointer; }
.fp-btn:hover { background: rgba(127,127,127,.18); }
.fp-btn:disabled { opacity: .5; cursor: default; }
.fp-btn.primary { border-color: rgba(90,140,255,.5); background: rgba(90,140,255,.16); }
.fp-hint { font-size: 11.5px; line-height: 1.7; opacity: .6; }
.fp-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11.5px; }
.fp-pre { margin: 0; padding: 10px 12px; border-radius: 8px; background: rgba(127,127,127,.1); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11.5px; line-height: 1.6; white-space: pre-wrap; word-break: break-all; max-height: 168px; overflow: auto; }
.fp-err { font-size: 12px; color: #c0463c; }
.fp-ok { font-size: 12px; color: #1f7a46; }
.fp-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
.fp-kv { border: 1px solid rgba(140,145,155,.2); border-radius: 8px; padding: 9px 11px; }
.fp-kv b { display: block; font-size: 11px; font-weight: 500; opacity: .55; margin-bottom: 3px; }
.fp-kv span { font-size: 14px; }
.fp-tools { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.fp-tools th { text-align: left; font-weight: 500; font-size: 11px; opacity: .55; padding: 0 8px 6px 0; }
.fp-tools td { padding: 5px 8px 5px 0; vertical-align: top; border-top: 1px solid rgba(140,145,155,.16); }
.fp-tools td:first-child { white-space: nowrap; }
.fp-foot { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; font-size: 11.5px; opacity: .6; }
.fp-foot a { color: inherit; }
`

    const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)

    async function api(path, options) {
      const res = await fetch(API + path, options)
      const text = await res.text()
      let body = null
      try { body = text ? JSON.parse(text) : null } catch { body = null }
      if (!res.ok) throw new Error((body && (body.error || body.message)) || text || `HTTP ${res.status}`)
      return body || {}
    }

    function Badge(props) {
      return h('span', { className: 'fp-badge ' + (props.kind || 'idle') }, props.children)
    }

    function Card(props) {
      return h('div', { className: 'fp-card' },
        h('div', { className: 'fp-card-h' }, props.title, props.sub ? h('small', null, props.sub) : null),
        props.children,
      )
    }

    function Panel() {
      const [cfg, setCfg] = useState(null)
      const [health, setHealth] = useState(null)
      const [busy, setBusy] = useState(false)
      const [msg, setMsg] = useState(null)
      const [baseUrl, setBaseUrl] = useState('')
      const [token, setToken] = useState('')
      const [showToken, setShowToken] = useState(false)
      const aliveRef = useRef(true)

      const loadConfig = async () => {
        const c = await api('/config')
        if (!aliveRef.current) return
        setCfg(c)
        setBaseUrl((prev) => (prev || c.baseUrl || ''))
      }

      const probe = async () => {
        setBusy(true)
        setMsg(null)
        try {
          const [c, r] = await Promise.all([api('/config'), api('/health')])
          if (!aliveRef.current) return
          setCfg(c)
          setHealth(r)
          if (r && r.ok && r.health && r.health.ok !== false) {
            setMsg({ kind: 'ok', text: `连上了，${r.latencyMs} ms` })
          } else {
            setMsg({ kind: 'err', text: (r && (r.error || `服务端回了 HTTP ${r.status}`)) || '连不上' })
          }
        } catch (error) {
          if (aliveRef.current) setMsg({ kind: 'err', text: String((error && error.message) || error) })
        } finally {
          if (aliveRef.current) setBusy(false)
        }
      }

      const save = async () => {
        setBusy(true)
        setMsg(null)
        try {
          await api('/config', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ baseUrl, token }),
          })
          await loadConfig()
          setToken('')
          setMsg({ kind: 'ok', text: '存好了' })
          await probe()
        } catch (error) {
          setMsg({ kind: 'err', text: String((error && error.message) || error) })
        } finally {
          if (aliveRef.current) setBusy(false)
        }
      }

      useEffect(() => {
        aliveRef.current = true
        loadConfig().catch((error) => setMsg({ kind: 'err', text: String((error && error.message) || error) }))
        return () => { aliveRef.current = false }
      }, [])

      const connected = Boolean(health && health.ok && (!health.health || health.health.ok !== false))
      const healthBody = (health && (health.health || null)) || null
      const rooms = num(healthBody && (healthBody.rooms ?? healthBody.room_count))
      const statusKind = health === null ? 'idle' : (connected ? 'ok' : 'bad')
      const statusText = health === null ? '还没测' : (connected ? '服务端在线' : '服务端没响应')

      return h('div', { className: 'fp-wrap' },
        h('div', { className: 'fp-head' },
          h('div', null,
            h('div', { className: 'fp-title' }, '谁是卧底'),
            h('div', { className: 'fp-sub' },
              '让 dsh 里的 AI 角色凑一局：每人一个词，一句话，一轮投票。',
              h('br'),
              '服务端是这台电脑上跑的 ',
              h('span', { className: 'fp-mono' }, 'server/party_server.py'),
              '，只依赖 Python 标准库。',
            ),
          ),
          h('div', { className: 'fp-badges' },
            h(Badge, { kind: statusKind }, statusText),
            h(Badge, { kind: cfg && cfg.hasToken ? 'ok' : 'idle' }, cfg && cfg.hasToken ? '口令已配' : '没配口令'),
            h(Badge, { kind: 'idle' }, rooms === null ? '房间数未知' : `${rooms} 间房`),
          ),
        ),

        h(Card, { title: '服务端', sub: cfg ? `地址来自 ${cfg.baseUrlFrom === 'env' ? '环境变量' : (cfg.baseUrlFrom === 'settings' ? '这里' : '默认值')}` : '' },
          h('div', { className: 'fp-row' },
            h('span', { className: 'fp-label' }, '地址'),
            h('input', {
              className: 'fp-input mono',
              value: baseUrl,
              spellCheck: false,
              placeholder: 'http://127.0.0.1:8770',
              onChange: (e) => setBaseUrl(e.target.value),
            }),
          ),
          h('div', { className: 'fp-row' },
            h('span', { className: 'fp-label' }, '口令'),
            h('input', {
              className: 'fp-input mono',
              value: showToken ? token : '',
              type: showToken ? 'text' : 'password',
              spellCheck: false,
              placeholder: cfg && cfg.hasToken ? `已存（${cfg.tokenMask}），要换就重填` : '服务端 party_token.txt 里的那串',
              onChange: (e) => setToken(e.target.value),
            }),
            h('button', { className: 'fp-btn', onClick: () => setShowToken((v) => !v) }, showToken ? '藏起来' : '看一眼'),
          ),
          h('div', { className: 'fp-row' },
            h('button', { className: 'fp-btn primary', disabled: busy, onClick: save }, busy ? '处理中…' : '保存'),
            h('button', { className: 'fp-btn', disabled: busy, onClick: probe }, '测试连接'),
            msg ? h('span', { className: msg.kind === 'ok' ? 'fp-ok' : 'fp-err' }, msg.text) : null,
          ),
          h('div', { className: 'fp-hint' },
            '外网要用的话，先在自己电脑上开一条隧道把 8770 露出去，再把隧道地址填到上面。',
            '隧道地址重启就变，所以每次开局都用新的地址、把新地址念给对方。',
          ),
        ),

        h(Card, { title: '现在这局服务端的状态' },
          h('div', { className: 'fp-grid' },
            h('div', { className: 'fp-kv' }, h('b', null, '延迟'), h('span', null, health && health.latencyMs != null ? `${health.latencyMs} ms` : '—')),
            h('div', { className: 'fp-kv' }, h('b', null, '房间数'), h('span', null, rooms === null ? '—' : String(rooms))),
            h('div', { className: 'fp-kv' }, h('b', null, '超时'), h('span', null, cfg ? `${cfg.timeoutMs} ms` : '—')),
            h('div', { className: 'fp-kv' }, h('b', null, '版本'), h('span', null, cfg ? String(cfg.version || '—') : '—')),
          ),
          health ? h('pre', { className: 'fp-pre' }, JSON.stringify(health.health || health.raw || health, null, 2)) : h('div', { className: 'fp-hint' }, '点「测试连接」看看它回什么。'),
        ),

        h(Card, { title: '怎么开一局', sub: '不用在这儿点，跟鱼说一声就行' },
          h('div', { className: 'fp-hint' },
            '开房、进房、发言、投票都是那五个工具在做。你只要说一句：',
            h('br'),
            h('span', { className: 'fp-mono' }, '「开一局谁是卧底，叫上大肥鱼2和大肥鱼3」'),
            h('br'),
            '它会自己开房、把房号念给别人、人齐了开局。人越多卧底越好藏，4~6 个最舒服。',
          ),
          h('table', { className: 'fp-tools' },
            h('thead', null, h('tr', null, h('th', null, '工具'), h('th', null, '干什么'))),
            h('tbody', null,
              h('tr', null, h('td', null, h('span', { className: 'fp-mono' }, 'party_create')), h('td', null, '开一间房，自己坐进去，服务端立刻告诉你身份和词')),
              h('tr', null, h('td', null, h('span', { className: 'fp-mono' }, 'party_join')), h('td', null, '拿房号进别人开的房（已经开局就进不去了）')),
              h('tr', null, h('td', null, h('span', { className: 'fp-mono' }, 'party_state')), h('td', null, '看第几轮、谁还在、谁说过话、自己拿到的是什么词')),
              h('tr', null, h('td', null, h('span', { className: 'fp-mono' }, 'party_say')), h('td', null, '这一轮说一句话——别把词念出来，说得含糊才好玩')),
              h('tr', null, h('td', null, h('span', { className: 'fp-mono' }, 'party_vote')), h('td', null, '投票把一个人投出去，只能投活着的、不能投自己')),
            ),
          ),
        ),

        h('div', { className: 'fp-foot' },
          h('span', null, '0.1.0 测试版 · 一局一码、退出即废这些还没做，现在靠「自己开隧道 + 重启就换地址」顶着'),
          h('a', { href: 'https://github.com/xike-cell/dsh-fish-party', target: '_blank', rel: 'noreferrer' }, 'GitHub ↗'),
        ),
      )
    }

    /** 出错不让整页白掉：设置页里任何一个异常都只影响这一块 */
    class Boundary extends React.Component {
      constructor(props) {
        super(props)
        this.state = { error: null }
      }
      static getDerivedStateFromError(error) {
        return { error }
      }
      render() {
        if (this.state.error) {
          return h('div', { className: 'fp-wrap' },
            h('div', { className: 'fp-title' }, '谁是卧底'),
            h('div', { className: 'fp-err' }, '设置页出错了：' + String((this.state.error && this.state.error.message) || this.state.error)),
          )
        }
        return this.props.children
      }
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        const style = document.createElement('style')
        style.setAttribute('data-plugin-css', 'dsh-fish-party')
        style.textContent = CSS
        document.head.appendChild(style)
        try { ctx.effect(() => () => style.remove(), 'dsh-fish-party: 设置页样式') } catch { /* 没有 effect 就算了 */ }

        let slots
        try { slots = typeof ctx.get === 'function' ? ctx.get('slots') : undefined } catch { slots = undefined }
        if (!slots) { try { slots = ctx.slots } catch { slots = undefined } }
        if (!slots || typeof slots.inject !== 'function') return

        slots.inject('settings.section', () => slots.register(
          { name: 'settings.section', id: 'fish-party', order: 32, label: '谁是卧底' },
          () => h(Boundary, null, h(Panel)),
        ))
      },
    }
  },
})
