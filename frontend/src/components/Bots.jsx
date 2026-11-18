import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleTimeString() : '')

// ---------- тест-чат: бот по текущему графу, без Telegram ----------

export function EmulatorPanel({ projectId, graph, readOnly, onSelectNode, onTrace }) {
  const [state, setState] = useState({ messages: [], log: [], waiting: false, saved: {} })
  const [text, setText] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const list = useRef(null)
  const graphRef = useRef(graph)
  graphRef.current = graph

  const apply = (s) => {
    setState(s)
    onTrace?.(s.log.filter((e) => e.node).map((e) => e.node))
  }
  useEffect(() => {
    api.botEmulatorState(projectId).then(apply).catch(() => {})
    // пока открыт — подтягиваем сообщения, которые бот отправляет после пауз
    const t = setInterval(() => api.botEmulatorState(projectId).then(apply).catch(() => {}), 1500)
    return () => clearInterval(t)
  }, [projectId]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (list.current) list.current.scrollTop = list.current.scrollHeight
  }, [state.messages.length])

  const send = async (body) => {
    setBusy(true)
    setError(null)
    try {
      apply(await api.botEmulator(projectId, { graph: graphRef.current, ...body }))
    } catch (e) {
      setError(e.data?.detail?.issues?.length ? `${e.message}: ${e.data.detail.issues.map((i) => i.message).join('; ')}` : e.message)
    } finally {
      setBusy(false)
    }
  }
  const submit = (e) => {
    e.preventDefault()
    if (!text.trim()) return
    send({ text })
    setText('')
  }
  const errors = state.log.filter((e) => e.kind === 'error')

  return (
    <div className="emulator">
      <div className="emu-chat">
        <div className="emu-messages" ref={list}>
          {state.messages.length === 0 && (
            <div className="muted pad">
              Это тест-чат: напишите боту, например <code>/start</code>. Работает по текущему графу, даже несохранённому, без
              Telegram и токена. Данные «Запомнить» здесь отдельные от настоящих пользователей.
            </div>
          )}
          {state.messages.map((m) => (
            <div key={m.id} className={`bubble ${m.from}`}>
              {m.photo && <img src={m.photo} alt="" className="emu-photo" />}
              {m.text && <div className="bubble-text">{m.text}</div>}
              {(m.buttons || []).map((b) => (
                <button
                  key={b.id}
                  className="emu-button"
                  disabled={readOnly || busy}
                  onClick={() => send({ button: { message_id: m.id, data: b.id, text: b.text } })}
                >
                  {b.text}
                </button>
              ))}
              {(m.links || []).map((l, i) => (
                <a key={i} className="emu-button" href={l.url} target="_blank" rel="noreferrer">
                  {l.text} ↗
                </a>
              ))}
              {m.note && <div className="muted">{m.note}</div>}
              <div className="bubble-time">{fmtTime(m.t)}</div>
            </div>
          ))}
        </div>
        <form className="emu-input" onSubmit={submit}>
          <input value={text} onChange={(e) => setText(e.target.value)} placeholder={state.waiting ? 'бот ждёт ответа…' : 'Сообщение или /команда'} disabled={readOnly} />
          <button className="primary" disabled={readOnly || busy || !text.trim()}>
            Отправить
          </button>
          <button type="button" onClick={() => send({ reset: true })} disabled={readOnly || busy} title="Очистить чат и диалоги">
            ⟲
          </button>
        </form>
        {error && <div className="issue error">{error}</div>}
      </div>
      <div className="emu-side">
        <div className="field-label">Ошибки</div>
        {errors.length ? (
          errors.map((e, i) => (
            <div key={i} className="issue error clickable" onClick={() => e.node && onSelectNode(e.node)}>
              {e.text} {e.node && <span className="muted">· {e.node}</span>}
            </div>
          ))
        ) : (
          <div className="muted">нет</div>
        )}
        <div className="field-label">Сохранено для пользователя (saved)</div>
        <pre className="run-vars">{JSON.stringify(state.saved, null, 1)}</pre>
      </div>
    </div>
  )
}

// ---------- журнал работающего бота ----------

export function BotLog({ projectId, onSelectNode }) {
  const [st, setSt] = useState(null)
  useEffect(() => {
    let alive = true
    const tick = () => api.botStatus(projectId).then((s) => alive && setSt(s)).catch(() => {})
    tick()
    const t = setInterval(tick, 2000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [projectId])
  if (!st) return null
  if (!st.running) return <div className="muted pad">Бот не запущен. Нажмите «▶ Запустить бота» — здесь появятся входящие и исходящие сообщения.</div>
  return (
    <div className="bot-log">
      <div className="muted">
        @{st.username} · входящих {st.stats.in}, ответов {st.stats.out}, ошибок {st.stats.errors} · незаконченных диалогов{' '}
        {st.dialogs}
      </div>
      {st.log.map((e, i) => (
        <div key={i} className={`run-event ${e.kind === 'error' ? 'bad' : e.kind === 'in' ? 'log' : ''}`} onClick={() => e.node && onSelectNode(e.node)}>
          <span className="muted">{fmtTime(e.t)}</span> {e.kind === 'in' ? '→' : e.kind === 'out' ? '←' : '✖'}{' '}
          {e.user && <b>{e.user}: </b>}
          {e.text} <span className="muted">· чат {e.chat}</span>
        </div>
      ))}
    </div>
  )
}

// ---------- пользователи бота ----------

export function BotUsers({ projectId, readOnly }) {
  const [users, setUsers] = useState(null)
  const load = () => api.botUsers(projectId).then(setUsers).catch(() => setUsers([]))
  useEffect(() => {
    load()
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [projectId]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!users) return null
  if (!users.length) return <div className="muted pad">Пользователей пока нет — они появятся, когда бот получит первые сообщения в Telegram.</div>
  return (
    <div className="data-table-wrap">
      <div className="tester-actions">
        <span className="muted">Всего: {users.length}</span>
        <button
          className="small danger"
          disabled={readOnly}
          onClick={async () => {
            if (!confirm('Удалить всех пользователей бота и их сохранённые данные?')) return
            await api.botUsersClear(projectId)
            load()
          }}
        >
          Очистить
        </button>
      </div>
      <table className="data-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>Имя</th>
            <th>@username</th>
            <th>Сообщений</th>
            <th>Последнее</th>
            <th>saved</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td>{u.id}</td>
              <td>{u.name}</td>
              <td>{u.username}</td>
              <td>{u.messages}</td>
              <td>{new Date(u.last_seen * 1000).toLocaleString()}</td>
              <td className="mono">{JSON.stringify(u.saved || {})}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---------- настройки → Бот ----------

export function BotTab({ bot, setBot, env, setEnv }) {
  const b = { token_env: '', ...bot }
  const envKeys = env.map((e) => e.key).filter(Boolean)
  const tokenVar = env.find((e) => e.key === b.token_env)
  return (
    <section>
      <ol className="hint">
        <li>
          Создайте бота у <a href="https://t.me/BotFather" target="_blank" rel="noreferrer">@BotFather</a> (<code>/newbot</code>) и
          скопируйте токен.
        </li>
        <li>Сохраните токен в секретную переменную ниже и сохраните проект.</li>
        <li>Нажмите «▶ Запустить бота» в тулбаре. Платформа сама опрашивает Telegram — публичный адрес не нужен.</li>
      </ol>
      <p className="hint">
        Один токен — один бот: его нельзя одновременно использовать в другом проекте-боте или в настройках Telegram
        проекта-автоматизации. Работающий бот подхватывает изменения графа после сохранения.
      </p>
      <div className="list-row">
        <label className="field">
          <span className="field-label">Токен — секретная переменная</span>
          <select value={b.token_env} onChange={(e) => setBot({ ...b, token_env: e.target.value })}>
            <option value="">— не задан (работает только тест-чат) —</option>
            {envKeys.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        {!envKeys.includes('BOT_TOKEN') && (
          <button
            className="small"
            onClick={() => {
              setEnv([...env, { key: 'BOT_TOKEN', value: '', secret: true }])
              setBot({ ...b, token_env: 'BOT_TOKEN' })
            }}
          >
            + переменная BOT_TOKEN
          </button>
        )}
      </div>
      {tokenVar && (
        <label className="field">
          <span className="field-label">Токен (хранится зашифрованным)</span>
          <input
            type="password"
            className="mono"
            autoComplete="off"
            value={tokenVar.value || ''}
            placeholder={tokenVar.has_value ? '•••••• задан — введите новый, чтобы заменить' : '123456789:AA…'}
            onChange={(e) => setEnv(env.map((x) => (x.key === b.token_env ? { ...x, value: e.target.value, secret: true } : x)))}
          />
        </label>
      )}
      <div className="hint">
        Переменные в блоках: <code>text</code> — текст сообщения, <code>args</code> — текст после команды, <code>user_name</code>,{' '}
        <code>username</code>, <code>user_id</code>, <code>chat_id</code>, <code>saved['ключ']</code> — сохранённое блоком «Запомнить».
      </div>
    </section>
  )
}

// ---------- поля блоков: кнопки меню, ссылки, проект-автоматизация ----------

const rid = () => Math.random().toString(36).slice(2, 8)

export function ButtonsInput({ value, onChange, ListEditor }) {
  return (
    <ListEditor
      items={value || []}
      onChange={onChange}
      empty="Нет кнопок"
      addLabel="кнопка"
      reorder
      newItem={() => ({ id: rid(), text: '' })}
      columns={[{ key: 'text', label: 'текст кнопки', flex: 1 }]}
    />
  )
}

export function LinksInput({ value, onChange, ListEditor }) {
  return (
    <ListEditor
      items={value || []}
      onChange={onChange}
      empty="Без кнопок"
      addLabel="кнопка-ссылка"
      newItem={() => ({ text: '', url: 'https://' })}
      columns={[
        { key: 'text', label: 'текст', flex: 1 },
        { key: 'url', label: 'https://…', mono: true, flex: 1.6 },
      ]}
    />
  )
}

export function AutomationProjectSelect({ value, onChange }) {
  const [projects, setProjects] = useState([])
  useEffect(() => {
    api.projects().then((ps) => setProjects(ps.filter((p) => p.kind === 'automation'))).catch(() => {})
  }, [])
  return (
    <select value={value || ''} onChange={(e) => onChange(e.target.value)}>
      <option value="">— выберите —</option>
      {projects.map((p) => (
        <option key={p.id} value={p.id}>
          🤖 {p.name}
        </option>
      ))}
      {value && !projects.some((p) => p.id === value) && <option value={value}>{value}</option>}
    </select>
  )
}
