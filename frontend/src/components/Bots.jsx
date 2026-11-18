import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleTimeString() : '')

// ---------- тест-чат в стиле Telegram: бот по текущему графу, без Telegram ----------

const FILE_ICON = { photo: '🖼', document: '📄', video: '🎬', voice: '🎤', audio: '🎵' }
const hm = (t) => (t ? new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '')
const kb = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} МБ` : `${Math.max(1, Math.round(n / 1024))} КБ`)

function readFile(file) {
  // содержимое — только для файлов до 2 МБ: этого хватает, чтобы проверить «Скачать файл» в тест-чате
  return new Promise((resolve) => {
    if (file.size > 2_000_000) return resolve(null)
    const r = new FileReader()
    r.onload = () => resolve(r.result)
    r.onerror = () => resolve(null)
    r.readAsDataURL(file)
  })
}

function rows(items, columns = 1) {
  const out = []
  for (let i = 0; i < items.length; i += columns) out.push(items.slice(i, i + columns))
  return out
}

export function EmulatorPanel({ projectId, graph, readOnly, onSelectNode, onTrace, botName, username }) {
  const [state, setState] = useState({ messages: [], log: [], waiting: false, saved: {} })
  const [text, setText] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [group, setGroup] = useState(false)
  const [asOther, setAsOther] = useState(false)
  const [replyTo, setReplyTo] = useState(null)
  const [cmdsOpen, setCmdsOpen] = useState(false)
  const [sideOpen, setSideOpen] = useState(true)
  const list = useRef(null)
  const fileInput = useRef(null)
  const graphRef = useRef(graph)
  graphRef.current = graph

  const commands = (graph?.nodes || []).filter((n) => n.type === 'bot_command').map((n) => n.data)
  const schedules = (graph?.nodes || []).filter((n) => n.type === 'bot_schedule')

  const apply = (s) => {
    setState(s)
    onTrace?.(s.log.filter((e) => e.node).map((e) => e.node))
  }
  useEffect(() => {
    api.botEmulatorState(projectId).then(apply).catch(() => {})
    // подтягиваем то, что бот отправляет после пауз и отложенных веток
    const t = setInterval(() => api.botEmulatorState(projectId).then(apply).catch(() => {}), 1500)
    return () => clearInterval(t)
  }, [projectId]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (list.current) list.current.scrollTop = list.current.scrollHeight
  }, [state.messages.length])

  const send = async (body) => {
    setBusy(true)
    setError(null)
    setCmdsOpen(false)
    try {
      const chat = { group, as_other: asOther, reply_to: replyTo?.id ?? null }
      apply(await api.botEmulator(projectId, { graph: graphRef.current, ...chat, ...body }))
      setReplyTo(null)
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
  const attach = async (e) => {
    const f = e.target.files?.[0]
    e.target.value = ''
    if (!f) return
    const data = await readFile(f)
    const preview = f.type.startsWith('image/') && f.size <= 1_500_000 ? data : null
    send({ file: { name: f.name, mime: f.type, size: f.size, preview, data }, text: text || null })
    setText('')
  }
  const errors = state.log.filter((e) => e.kind === 'error')
  const keyboard = state.keyboard || null
  const title = group ? 'Тестовая группа' : botName || 'Бот'
  const subtitle = busy ? 'печатает…' : group ? 'вы, Участник и бот' : username ? `@${username} · бот` : 'бот'

  const bubble = (m) => {
    const mine = m.from === 'user' && m.user?.id !== 2
    const other = m.from === 'user' && m.user?.id === 2
    const foreign = m.from === 'bot' && m.chat && !['1', '-100'].includes(m.chat)
    const media = m.photo ? { kind: 'photo', preview: m.photo } : m.media
    return (
      <div key={m.id} className={`tg-row ${mine ? 'out' : 'in'}`}>
        <div className={`tg-bubble ${mine ? 'out' : 'in'} ${m.deleted ? 'deleted' : ''}`}>
          {other && <div className="tg-author c1">Участник</div>}
          {group && m.from === 'bot' && <div className="tg-author c2">{botName || 'Бот'}</div>}
          {foreign && <div className="tg-author c3">→ в чат {m.chat}</div>}
          {m.reply_to && <div className="tg-quote">↩ ответ на #{m.reply_to}</div>}
          {m.deleted ? (
            <div className="tg-text deleted">🗑 Сообщение удалено</div>
          ) : (
            <>
              {media &&
                (media.preview ? (
                  <img src={media.preview} alt={media.name || ''} className="tg-photo" />
                ) : (
                  <div className="tg-file">
                    <span className="tg-file-icon">{FILE_ICON[media.kind] || '📎'}</span>
                    <span>
                      <b>{media.name}</b>
                      <br />
                      <span className="tg-meta">{media.size ? kb(media.size) : media.kind}</span>
                    </span>
                  </div>
                ))}
              {m.invoice ? (
                <div className="tg-invoice">
                  <b>{m.invoice.title}</b>
                  <div>{m.invoice.description}</div>
                  <div className="tg-price">
                    {m.invoice.currency === 'XTR' ? `${m.invoice.amount} ⭐` : `${(m.invoice.amount / 100).toFixed(2)} ${m.invoice.currency}`}
                  </div>
                </div>
              ) : (
                m.text && <div className="tg-text">{m.text}</div>
              )}
            </>
          )}
          <span className="tg-time">
            {m.note && <i>{m.note} · </i>}
            {hm(m.t)}
            {mine && <span className="tg-ticks">✓✓</span>}
            {group && !m.deleted && (
              <button className="tg-reply" title="Ответить на это сообщение" onClick={() => setReplyTo(m)}>
                ↩
              </button>
            )}
          </span>
        </div>
        {!m.deleted && (m.invoice || m.buttons?.length || m.links?.length) ? (
          <div className="tg-inline">
            {m.invoice && (
              <div className="tg-inline-row">
                <button disabled={readOnly || busy || !!m.note} onClick={() => send({ pay: { message_id: m.id } })}>
                  {m.note ? m.note : `Оплатить ${m.invoice.currency === 'XTR' ? `⭐ ${m.invoice.amount}` : ''} (тест)`}
                </button>
              </div>
            )}
            {rows(m.buttons || [], m.columns || 1).map((row, i) => (
              <div key={i} className="tg-inline-row">
                {row.map((b) => (
                  <button key={b.id} disabled={readOnly || busy} onClick={() => send({ button: { message_id: m.id, data: b.id, text: b.text } })}>
                    {b.text}
                  </button>
                ))}
              </div>
            ))}
            {(m.links || []).map((l, i) => (
              <div key={`l${i}`} className="tg-inline-row">
                <a href={l.url} target="_blank" rel="noreferrer">
                  {l.text} ↗
                </a>
              </div>
            ))}
          </div>
        ) : null}
      </div>
    )
  }

  return (
    <div className="emulator">
      <div className="tg">
        <div className="tg-head">
          <div className="tg-avatar">{(title || 'Б').trim()[0]?.toUpperCase()}</div>
          <div className="tg-title">
            <b>{title}</b>
            <span className={busy ? 'typing' : ''}>{subtitle}</span>
          </div>
          <div className="tg-head-tools">
            <label title="Писать в тестовую группу">
              <input type="checkbox" checked={group} onChange={(e) => setGroup(e.target.checked)} /> группа
            </label>
            {group && (
              <>
                <select value={asOther ? 'other' : 'me'} onChange={(e) => setAsOther(e.target.value === 'other')} title="От чьего имени писать">
                  <option value="me">от себя</option>
                  <option value="other">от Участника</option>
                </select>
                <label title="Вы — администратор тестовой группы (is_chat_admin)">
                  <input type="checkbox" checked={!!state.chat_admin} onChange={(e) => send({ chat_admin: e.target.checked })} /> админ
                </label>
                <button disabled={readOnly || busy} onClick={() => send({ member: 'join', as_other: true })} title="Участник вошёл в группу">
                  +👤
                </button>
              </>
            )}
            <select value={state.role || 'user'} onChange={(e) => send({ as_role: e.target.value })} disabled={readOnly || busy} title="Тестировать с ролью">
              {(state.roles || ['admin', 'user']).map((r) => (
                <option key={r} value={r}>
                  роль: {r}
                </option>
              ))}
            </select>
            {schedules.map((n) => (
              <button key={n.id} disabled={readOnly || busy} onClick={() => send({ run_schedule: n.id })} title="Выполнить ветку «По расписанию» сейчас">
                🕘 {n.data.kind === 'interval' ? `${n.data.every_minutes} мин` : n.data.at}
              </button>
            ))}
            <button onClick={() => send({ reset: true })} disabled={readOnly || busy} title="Очистить чат, диалоги и тестовые данные">
              ⟲
            </button>
            <button onClick={() => setSideOpen((v) => !v)} title="Ошибки и сохранённые данные">
              ⓘ
            </button>
          </div>
        </div>
        <div className="tg-messages" ref={list}>
          <div className="tg-date">Сегодня</div>
          {state.messages.length === 0 && (
            <div className="tg-service">
              Тест-чат работает по текущему графу (даже несохранённому) без Telegram и токена. Напишите /start или выберите
              команду кнопкой «/».
            </div>
          )}
          {state.messages.map((m) =>
            m.from === 'system' ? (
              <div key={m.id} className="tg-service">
                {m.text}
              </div>
            ) : (
              bubble(m)
            ),
          )}
          {busy && (
            <div className="tg-typing">
              <span />
              <span />
              <span />
            </div>
          )}
        </div>
        {replyTo && (
          <div className="tg-replying">
            <span>
              ↩ Ответ на #{replyTo.id}: {String(replyTo.text || replyTo.media?.name || '').slice(0, 60)}
            </span>
            <button className="linklike" onClick={() => setReplyTo(null)}>
              ✕
            </button>
          </div>
        )}
        {cmdsOpen && commands.length > 0 && (
          <div className="tg-commands">
            {commands.map((c) => (
              <button key={c.command} onClick={() => send({ text: `/${c.command}` })}>
                <b>/{c.command}</b> <span>{c.description}</span>
              </button>
            ))}
          </div>
        )}
        <form className="tg-input" onSubmit={submit}>
          {commands.length > 0 && (
            <button type="button" className="tg-icon tg-menu" title="Команды бота" onClick={() => setCmdsOpen((v) => !v)}>
              /
            </button>
          )}
          <button type="button" className="tg-icon" title="Прикрепить фото или файл" disabled={readOnly || busy} onClick={() => fileInput.current?.click()}>
            📎
          </button>
          <input ref={fileInput} type="file" hidden onChange={attach} />
          <input className="tg-field" value={text} onChange={(e) => setText(e.target.value)} placeholder={state.waiting ? 'Бот ждёт ответа…' : 'Сообщение'} disabled={readOnly} />
          <button className="tg-icon tg-send" disabled={readOnly || busy || !text.trim()} title="Отправить">
            ➤
          </button>
        </form>
        {keyboard && (
          <div className="tg-keyboard">
            {keyboard.map((row, i) => (
              <div key={i} className="tg-kb-row">
                {row.map((b, j) => (
                  <button key={j} disabled={readOnly || busy} onClick={() => (b.request_contact ? send({ contact: true }) : send({ text: b.text }))}>
                    {b.text}
                  </button>
                ))}
              </div>
            ))}
          </div>
        )}
        {error && <div className="issue error">{error}</div>}
      </div>
      {sideOpen && (
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
      )}
    </div>
  )
}

// ---------- статистика ----------

export function BotStats({ projectId, graph, onCounts }) {
  const [source, setSource] = useState('live')
  const [st, setSt] = useState(null)
  const [onCanvas, setOnCanvas] = useState(false)
  useEffect(() => {
    let alive = true
    const load = () => api.botStats(projectId, source).then((s) => alive && setSt(s)).catch(() => {})
    load()
    const t = setInterval(load, 4000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [projectId, source])
  useEffect(() => {
    onCounts?.(onCanvas && st ? st.nodes : null)
  }, [onCanvas, st]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => onCounts?.(null), []) // eslint-disable-line react-hooks/exhaustive-deps
  if (!st) return null
  const maxDay = Math.max(1, ...st.days.map((d) => d.users))
  const titleOf = (id) => {
    const n = (graph?.nodes || []).find((x) => x.id === id)
    return n ? `${n.type.replace('bot_', '')} · ${id}` : id
  }
  const funnel = Object.entries(st.nodes)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 25)
  const top = funnel[0]?.[1] || 1
  return (
    <div className="bot-stats">
      <div className="stats-tools">
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="live">Бот для клиентов</option>
          <option value="test">Тестовый бот</option>
          <option value="emulator">Тест-чат</option>
        </select>
        <label className="check">
          <input type="checkbox" checked={onCanvas} onChange={(e) => setOnCanvas(e.target.checked)} /> показать воронку на канвасе
        </label>
        <span className="spacer" />
        <span className="muted">Выгрузка:</span>
        <a className="button small" href={api.botExportUrl(projectId, 'users', 'xlsx')}>
          Пользователи · Excel
        </a>
        <a className="button small" href={api.botExportUrl(projectId, 'users', 'csv')}>
          CSV
        </a>
        <a className="button small" href={api.botExportUrl(projectId, 'payments', 'xlsx')}>
          Платежи · Excel
        </a>
      </div>
      <div className="stats-cards">
        {[
          ['Пользователей', st.users_total],
          ['Новых за сутки', st.new_today],
          ['Активных сегодня', st.active_today],
          ['Активных за 7 дней', st.active_week],
          ['Сообщений сегодня', st.messages_today],
          ['Отложено задач', st.scheduled.length],
        ].map(([label, value]) => (
          <div key={label} className="stat-card">
            <div className="stat-value">{value}</div>
            <div className="muted">{label}</div>
          </div>
        ))}
      </div>
      <div className="stats-grid">
        <div>
          <div className="field-label">Активные пользователи за 14 дней</div>
          <div className="bars">
            {st.days.map((d) => (
              <div key={d.date} className="bar" title={`${d.date}: ${d.users} польз., ${d.messages} сообщ.`}>
                <span style={{ height: `${(d.users / maxDay) * 100}%` }} />
                <em>{d.date.slice(8)}</em>
              </div>
            ))}
          </div>
        </div>
        <div>
          <div className="field-label">Команды</div>
          {st.commands.length ? (
            st.commands.map(([c, n]) => (
              <div key={c} className="row-line">
                <code>{c}</code> <span className="muted">{n}</span>
              </div>
            ))
          ) : (
            <div className="muted">пока нет</div>
          )}
        </div>
        <div>
          <div className="field-label">Воронка: сколько раз дошли до блока</div>
          {funnel.length ? (
            funnel.map(([id, n]) => (
              <div key={id} className="funnel-row" title={titleOf(id)}>
                <span className="funnel-bar" style={{ width: `${(n / top) * 100}%` }} />
                <span className="funnel-label">{titleOf(id)}</span>
                <b>{n}</b>
              </div>
            ))
          ) : (
            <div className="muted">данных пока нет — напишите боту (или в тест-чат)</div>
          )}
        </div>
      </div>
    </div>
  )
}

// ---------- файлы ----------

export function BotFiles({ projectId }) {
  const [files, setFiles] = useState(null)
  useEffect(() => {
    let alive = true
    const load = () =>
      api
        .botFiles(projectId)
        .then((f) => alive && setFiles(f))
        .catch(() => alive && setFiles([]))
    load()
    const t = setInterval(load, 4000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [projectId])
  if (!files) return null
  if (!files.length) return <div className="muted pad">Скачанных файлов нет. Они появятся, когда блок «Скачать файл» сработает у работающего бота.</div>
  return (
    <div className="data-table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Когда</th>
            <th>От кого</th>
            <th>Файл</th>
            <th>Размер</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {files.map((f) => (
            <tr key={f.id}>
              <td>{new Date(f.t * 1000).toLocaleString()}</td>
              <td>
                {f.user_name} <span className="muted">{f.user_id}</span>
              </td>
              <td>
                {FILE_ICON[f.kind] || '📎'} {f.name}
              </td>
              <td>{kb(f.size)}</td>
              <td>{f.stored && <a href={api.botFileUrl(projectId, f.id)}>⬇ скачать</a>}</td>
            </tr>
          ))}
        </tbody>
      </table>
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
  if (!st.running && !st.test?.running)
    return <div className="muted pad">Бот не запущен. Нажмите «▶ Запустить бота» — здесь появятся входящие и исходящие сообщения.</div>
  return (
    <div className="bot-log">
      <div className="muted">
        {st.running ? (
          <>
            @{st.username} · входящих {st.stats.in}, ответов {st.stats.out}, ошибок {st.stats.errors} · незаконченных диалогов {st.dialogs}
          </>
        ) : (
          'основной бот остановлен'
        )}
        {st.test?.running && ` · 🧪 @${st.test.username}: входящих ${st.test.stats.in}`}
      </div>
      {st.log.map((e, i) => (
        <div key={i} className={`run-event ${e.kind === 'error' ? 'bad' : e.kind === 'in' ? 'log' : ''}`} onClick={() => e.node && onSelectNode(e.node)}>
          <span className="muted">{fmtTime(e.t)}</span> {e.test ? '🧪 ' : ''}
          {e.kind === 'in' ? '→' : e.kind === 'out' ? '←' : '✖'}{' '}
          {e.user && <b>{e.user}: </b>}
          {e.text} <span className="muted">· чат {e.chat}</span>
        </div>
      ))}
    </div>
  )
}

// ---------- пользователи бота ----------

export function BotUsers({ projectId, readOnly, roles = [] }) {
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
            <th>Роль</th>
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
              <td>
                <select
                  value={u.role || 'user'}
                  disabled={readOnly || u.settings_admin}
                  title={u.settings_admin ? 'Администратор из настроек бота — меняется там' : undefined}
                  className={u.role === 'banned' ? 'role-banned' : ''}
                  onChange={async (e) => {
                    try {
                      await api.botSetRole(projectId, u.id, e.target.value)
                    } catch (err) {
                      alert(err.message)
                    }
                    load()
                  }}
                >
                  {['admin', ...roles, 'user', 'banned'].map((r) => (
                    <option key={r} value={r}>
                      {r === 'banned' ? '🚫 banned' : r}
                    </option>
                  ))}
                </select>
              </td>
              <td className="mono">{JSON.stringify(u.saved || {})}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---------- настройки → Бот ----------

function ChipsInput({ items, onChange, placeholder, validate }) {
  const [error, setError] = useState(null)
  return (
    <div className="chips">
      {items.map((c) => (
        <span key={c} className="chip on">
          {c}
          <button className="icon" title="Убрать" onClick={() => onChange(items.filter((x) => x !== c))}>
            ×
          </button>
        </span>
      ))}
      <input
        className="mono chat-input"
        placeholder={placeholder}
        onKeyDown={(e) => {
          if (e.key !== 'Enter' || !e.target.value.trim()) return
          e.preventDefault()
          const v = e.target.value.trim()
          const problem = validate?.(v)
          if (problem) return setError(problem)
          setError(null)
          if (!items.includes(v)) onChange([...items, v])
          e.target.value = ''
        }}
      />
      {error && <span className="issue error">{error}</span>}
    </div>
  )
}

export function BotTab({ bot, setBot, env, setEnv }) {
  const b = { token_env: '', roles: ['moderator'], admins: [], deny_text: 'Нет доступа', payments_env: '', ...bot }
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
      <h4 className="settings-h">Тестовый бот</h4>
      <p className="hint">
        Второй бот (создайте его у @BotFather, например <code>my_shop_test_bot</code>) работает на <b>черновике</b> и
        подхватывает каждое «Сохранить» — проверяйте изменения в настоящем Telegram, пока клиенты пользуются опубликованной
        версией. У тестового бота свои пользователи и статистика.
      </p>
      <div className="list-row">
        <label className="field">
          <span className="field-label">Токен тестового бота — секретная переменная</span>
          <select value={b.test_token_env || ''} onChange={(e) => setBot({ ...b, test_token_env: e.target.value })}>
            <option value="">— без тестового бота —</option>
            {envKeys.filter((k) => k !== b.token_env).map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        {!envKeys.includes('TEST_BOT_TOKEN') && (
          <button
            className="small"
            onClick={() => {
              setEnv([...env, { key: 'TEST_BOT_TOKEN', value: '', secret: true }])
              setBot({ ...b, test_token_env: 'TEST_BOT_TOKEN' })
            }}
          >
            + переменная TEST_BOT_TOKEN
          </button>
        )}
      </div>
      {b.test_token_env && env.find((e) => e.key === b.test_token_env) && (
        <label className="field">
          <span className="field-label">Токен тестового бота (хранится зашифрованным)</span>
          <input
            type="password"
            className="mono"
            autoComplete="off"
            value={env.find((e) => e.key === b.test_token_env).value || ''}
            placeholder={env.find((e) => e.key === b.test_token_env).has_value ? '•••••• задан — введите новый, чтобы заменить' : '123456789:AA…'}
            onChange={(e) => setEnv(env.map((x) => (x.key === b.test_token_env ? { ...x, value: e.target.value, secret: true } : x)))}
          />
        </label>
      )}
      <h4 className="settings-h">Роли и админка</h4>
      <div className="field">
        <div className="field-label">Администраторы (Telegram ID)</div>
        <ChipsInput items={b.admins} onChange={(admins) => setBot({ ...b, admins })} placeholder="ID + Enter" validate={(v) => (/^\d{1,20}$/.test(v) ? null : 'ID — число')} />
        <div className="hint">
          Админам доступны все команды. Свой ID проще всего узнать так: напишите боту — вы появитесь во вкладке
          «Пользователи»; там же можно выдать роль admin без правки настроек.
        </div>
      </div>
      <div className="field">
        <div className="field-label">Свои роли</div>
        <ChipsInput items={b.roles} onChange={(roles) => setBot({ ...b, roles })} placeholder="роль + Enter"
          validate={(v) => (/^[a-z][a-z0-9_]{0,31}$/.test(v) ? (['admin', 'user', 'banned'].includes(v) ? 'это встроенная роль' : null) : 'латиница в нижнем регистре, цифры и _')} />
        <div className="hint">
          Встроенные: <code>admin</code>, <code>user</code> (все остальные), <code>banned</code> (бот игнорирует). Роль выдаётся
          блоком «Назначить роль» или во вкладке «Пользователи»; у «Команды» и «Сообщения» — поле «Доступна ролям». Команды
          для ролей видны в меню Telegram только тем, у кого роль есть.
        </div>
      </div>
      <label className="field">
        <span className="field-label">Ответ, если нет доступа</span>
        <input value={b.deny_text} onChange={(e) => setBot({ ...b, deny_text: e.target.value })} />
      </label>
      <h4 className="settings-h">Оплата</h4>
      <p className="hint">
        Блок «Счёт на оплату». <b>⭐ Telegram Stars</b> работают сразу, без договора с банком (подходят для цифровых товаров и
        услуг). Для рублей, долларов и евро подключите провайдера в @BotFather (<code>/mybots → Payments</code>, например
        ЮKassa) и сохраните выданный токен в секретную переменную:
      </p>
      <label className="field">
        <span className="field-label">Токен платёжного провайдера — переменная</span>
        <select value={b.payments_env} onChange={(e) => setBot({ ...b, payments_env: e.target.value })}>
          <option value="">— только Telegram Stars —</option>
          {envKeys.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </label>
      <div className="hint">
        <b>Группы.</b> Добавьте бота в группу и сделайте администратором с правами «Удалять сообщения» и «Блокировать
        пользователей» — иначе Telegram не покажет ему все сообщения и не даст модерировать. События с «Где: только в
        группах», блоки «Антиспам» и «Модерация»; <code>/ban</code> отправляйте ответом на сообщение нарушителя.
      </div>
      <div className="hint">
        Переменные в блоках: <code>role</code>, <code>is_admin</code>, <code>is_group</code>, <code>is_chat_admin</code>,{' '}
        <code>file</code> (полученный файл), <code>reply_user_id</code>, <code>text</code> — текст сообщения, <code>args</code> — текст после команды, <code>user_name</code>,{' '}
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

// ---------- платежи ----------

export function BotPayments({ projectId }) {
  const [items, setItems] = useState(null)
  useEffect(() => {
    let alive = true
    const load = () => api.botPayments(projectId).then((p) => alive && setItems(p)).catch(() => alive && setItems([]))
    load()
    const t = setInterval(load, 4000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [projectId])
  if (!items) return null
  if (!items.length) return <div className="muted pad">Оплат пока не было. Тестовые оплаты из тест-чата сюда не попадают.</div>
  const totals = items.reduce((acc, p) => ({ ...acc, [p.currency]: (acc[p.currency] || 0) + p.amount }), {})
  const fmt = (amount, cur) => (cur === 'XTR' ? `${amount} ⭐` : `${(amount / 100).toFixed(2)} ${cur}`)
  return (
    <div className="data-table-wrap">
      <div className="muted">
        Всего: {Object.entries(totals).map(([cur, sum]) => fmt(sum, cur)).join(', ')} · платежей {items.length}
      </div>
      <table className="data-table">
        <thead>
          <tr>
            <th>Когда</th>
            <th>Пользователь</th>
            <th>Сумма</th>
            <th>ID платежа Telegram</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.map((p, i) => (
            <tr key={i}>
              <td>{new Date(p.t * 1000).toLocaleString()}</td>
              <td>
                {p.user_name} <span className="muted">{p.user_id}</span>
              </td>
              <td>{fmt(p.amount, p.currency)}</td>
              <td className="mono">{p.charge_id}</td>
              <td>
                {p.refunded ? (
                  <span className="muted">возвращён</span>
                ) : p.currency === 'XTR' ? (
                  <button
                    className="small"
                    onClick={async () => {
                      if (!confirm(`Вернуть ${p.amount} ⭐ пользователю ${p.user_name || p.user_id}?`)) return
                      try {
                        await api.botRefund(projectId, p.charge_id)
                        setItems(await api.botPayments(projectId))
                      } catch (e) {
                        alert(e.message)
                      }
                    }}
                  >
                    ↩ Вернуть
                  </button>
                ) : (
                  <span className="muted" title="Возврат оплаты в валюте делает платёжный провайдер">через провайдера</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---------- публикация: черновик → бот для клиентов ----------

const BLOCK_TITLE = (nodes, id) => {
  const n = (nodes || []).find((x) => x.id === id)
  return n ? `${id}` : id
}

export function PublishModal({ projectId, dirty, onSave, catalog, nodes, onClose, onChanged }) {
  const [info, setInfo] = useState(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [done, setDone] = useState(null)
  const load = () => api.botPublications(projectId).then(setInfo).catch((e) => setError(e.message))
  useEffect(() => {
    // публикуется сохранённый черновик — несохранённое сначала сохраняем
    ;(dirty ? onSave() : Promise.resolve()).then(load).catch((e) => setError(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    const h = (e) => e.key === 'Escape' && !busy && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose, busy])

  const title = (id) => {
    const n = (nodes || []).find((x) => x.id === id)
    const spec = n && catalog?.blocks[n.type]
    return spec ? `${spec.icon || ''} ${spec.title} · ${id}` : BLOCK_TITLE(nodes, id)
  }
  const publish = async () => {
    setBusy(true)
    setError(null)
    try {
      const r = await api.botPublish(projectId, note)
      setInfo(r)
      setDone(r.published_version)
      setNote('')
      onChanged?.()
    } catch (e) {
      setError(e.data?.detail?.issues?.length ? `${e.message}: ${e.data.detail.issues.map((i) => i.message).join('; ')}` : e.message)
    } finally {
      setBusy(false)
    }
  }
  const restore = async (h) => {
    if (!confirm(`Вернуть клиентам версию от ${new Date(h.t * 1000).toLocaleString()}${h.note ? ` («${h.note}»)` : ''}? Черновик не изменится.`)) return
    setBusy(true)
    try {
      setInfo(await api.botRestorePublication(projectId, h.version))
      setDone(null)
      onChanged?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }
  const d = info?.diff
  const settingsNames = { env: 'переменные', bot: 'настройки бота', sandbox: 'песочница', libraries: 'библиотеки', firewall: 'firewall' }
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && !busy && onClose()}>
      <div className="modal publish-modal" role="dialog" aria-label="Публикация бота">
        <div className="modal-head">
          <div>
            <div className="inspector-title">🚀 Публикация</div>
            <div className="muted">
              «Сохранить» меняет только черновик. Клиенты видят опубликованную версию — она меняется здесь.
            </div>
          </div>
          <button className="icon" onClick={onClose} disabled={busy} title="Закрыть (Esc)">
            ×
          </button>
        </div>
        {!info ? (
          <div className="muted pad">{error || 'Загрузка…'}</div>
        ) : (
          <>
            {done && <div className="issue ok-box">✓ Опубликовано. Работающий бот уже отвечает клиентам по новой версии.</div>}
            {info.draft_differs ? (
              <div className="field">
                <div className="field-label">{info.version ? 'Что изменится для клиентов' : 'Первая публикация'}</div>
                {info.version && d && (
                  <div className="diff-list">
                    {d.added.map((id) => (
                      <div key={id} className="diff-add">+ {title(id)}</div>
                    ))}
                    {d.changed.map((id) => (
                      <div key={id} className="diff-change">~ {title(id)}</div>
                    ))}
                    {d.removed.map((id) => (
                      <div key={id} className="diff-del">− {id}</div>
                    ))}
                    {d.edges > 0 && <div className="muted">связей изменено: {d.edges}</div>}
                    {d.settings.length > 0 && <div className="muted">настройки: {d.settings.map((k) => settingsNames[k] || k).join(', ')}</div>}
                  </div>
                )}
                <input placeholder="Что изменилось (необязательно) — увидите в истории" value={note} onChange={(e) => setNote(e.target.value)} />
                <div className="tester-actions">
                  <button className="primary" onClick={publish} disabled={busy}>
                    {busy ? 'Публикую…' : '🚀 Опубликовать'}
                  </button>
                  <span className="muted">Черновик с ошибками опубликовать нельзя. Незаконченные диалоги доживут по старой версии.</span>
                </div>
              </div>
            ) : (
              <div className="issue">Черновик совпадает с опубликованной версией — публиковать нечего.</div>
            )}
            {error && <div className="issue error">{error}</div>}
            <div className="field-label">История публикаций</div>
            <div className="publish-history">
              {(info.history || []).length === 0 && <div className="muted">Публикаций ещё не было.</div>}
              {(info.history || []).map((h) => (
                <div key={h.version} className={`log-row ${h.version === info.version ? 'open' : ''}`}>
                  <span>{new Date(h.t * 1000).toLocaleString()}</span>
                  <span className="publish-note">{h.note || <span className="muted">без заметки</span>}</span>
                  <span className="muted">
                    {h.diff ? `+${h.diff.added || 0} ~${h.diff.changed || 0} −${h.diff.removed || 0}` : ''} · блоков {h.blocks}
                    {h.by ? ` · ${h.by}` : ''}
                  </span>
                  {h.version === info.version ? (
                    <span className="lib-state good">у клиентов</span>
                  ) : (
                    <button className="small" disabled={busy} onClick={() => restore(h)} title="Вернуть клиентам эту версию">
                      ↩ Вернуть
                    </button>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
