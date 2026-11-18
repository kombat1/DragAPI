import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleTimeString() : '')

// ---------- тест-чат: бот по текущему графу, без Telegram ----------

const FILE_ICON = { photo: '🖼', document: '📄', video: '🎬', voice: '🎤', audio: '🎵' }

function readFile(file) {
  return new Promise((resolve) => {
    // превью только для небольших картинок — в тест-чате файл никуда не отправляется
    if (!file.type.startsWith('image/') || file.size > 1_500_000) return resolve(null)
    const r = new FileReader()
    r.onload = () => resolve(r.result)
    r.onerror = () => resolve(null)
    r.readAsDataURL(file)
  })
}

export function EmulatorPanel({ projectId, graph, readOnly, onSelectNode, onTrace }) {
  const [state, setState] = useState({ messages: [], log: [], waiting: false, saved: {} })
  const [text, setText] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [group, setGroup] = useState(false)
  const [asOther, setAsOther] = useState(false)
  const [replyTo, setReplyTo] = useState(null)
  const list = useRef(null)
  const fileInput = useRef(null)
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
    send({ file: { name: f.name, mime: f.type, size: f.size, preview: await readFile(f) }, text: text || null })
    setText('')
  }
  const errors = state.log.filter((e) => e.kind === 'error')
  const keyboard = state.keyboard || null
  const visible = state.messages

  return (
    <div className="emulator">
      <div className="emu-chat">
        <div className="emu-modes">
          <label className="check">
            <input type="checkbox" checked={group} onChange={(e) => setGroup(e.target.checked)} /> группа
          </label>
          {group && (
            <>
              <select value={asOther ? 'other' : 'me'} onChange={(e) => setAsOther(e.target.value === 'other')}>
                <option value="me">писать от себя</option>
                <option value="other">писать от «Участника»</option>
              </select>
              <label className="check" title="Вы — администратор тестовой группы (is_chat_admin)">
                <input type="checkbox" checked={!!state.chat_admin} onChange={(e) => send({ chat_admin: e.target.checked })} /> я админ группы
              </label>
              <button className="small" disabled={readOnly || busy} onClick={() => send({ member: 'join', as_other: true })}>
                + участник вошёл
              </button>
            </>
          )}
          <span className="spacer" />
          Тестировать как{' '}
          <select value={state.role || 'user'} onChange={(e) => send({ as_role: e.target.value })} disabled={readOnly || busy}>
            {(state.roles || ['admin', 'user']).map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <div className="emu-messages" ref={list}>
          {visible.length === 0 && (
            <div className="muted pad">
              Это тест-чат: напишите боту, например <code>/start</code>. Работает по текущему графу, даже несохранённому, без
              Telegram и токена. «Группа» — проверить модерацию: пишите от «Участника», отвечайте на сообщения (↩).
            </div>
          )}
          {visible.map((m) =>
            m.from === 'system' ? (
              <div key={m.id} className="emu-system">
                {m.text}
              </div>
            ) : (
              <div key={m.id} className={`bubble ${m.from} ${m.user?.id === 2 ? 'other' : ''} ${m.deleted ? 'deleted' : ''}`}>
                {m.from === 'user' && m.user?.id === 2 && <div className="bubble-author">Участник</div>}
                {m.from === 'bot' && m.chat && !['1', '-100'].includes(m.chat) && <div className="bubble-author">→ чат {m.chat}</div>}
                {m.reply_to && <div className="bubble-reply">↩ ответ на сообщение #{m.reply_to}</div>}
                {m.photo && <img src={m.photo} alt="" className="emu-photo" />}
                {m.media &&
                  (m.media.preview ? (
                    <img src={m.media.preview} alt={m.media.name} className="emu-photo" />
                  ) : (
                    <div className="emu-file">
                      {FILE_ICON[m.media.kind] || '📎'} {m.media.name} {m.media.size ? <span className="muted">{Math.ceil(m.media.size / 1024)} КБ</span> : null}
                    </div>
                  ))}
                {m.text && <div className="bubble-text">{m.text}</div>}
                {m.invoice && (
                  <button className="emu-button pay" disabled={readOnly || busy || !!m.note} onClick={() => send({ pay: { message_id: m.id } })}>
                    💳 Оплатить (тест)
                  </button>
                )}
                {(m.buttons || []).map((b) => (
                  <button key={b.id} className="emu-button" disabled={readOnly || busy} onClick={() => send({ button: { message_id: m.id, data: b.id, text: b.text } })}>
                    {b.text}
                  </button>
                ))}
                {(m.links || []).map((l, i) => (
                  <a key={i} className="emu-button" href={l.url} target="_blank" rel="noreferrer">
                    {l.text} ↗
                  </a>
                ))}
                {m.note && <div className="muted">{m.note}</div>}
                <div className="bubble-time">
                  {m.deleted && <span className="bubble-deleted">удалено · </span>}#{m.id} {fmtTime(m.t)}
                  {group && !m.deleted && (
                    <button className="linklike" title="Ответить на это сообщение" onClick={() => setReplyTo(m)}>
                      ↩
                    </button>
                  )}
                </div>
              </div>
            ),
          )}
        </div>
        {replyTo && (
          <div className="emu-replying">
            ↩ ответ на #{replyTo.id}: {String(replyTo.text || replyTo.media?.name || '').slice(0, 60)}{' '}
            <button className="linklike" onClick={() => setReplyTo(null)}>
              отменить
            </button>
          </div>
        )}
        {keyboard && (
          <div className="emu-keyboard">
            {keyboard.map((row, i) => (
              <div key={i} className="emu-kb-row">
                {row.map((b, j) => (
                  <button key={j} disabled={readOnly || busy} onClick={() => (b.request_contact ? send({ contact: true }) : send({ text: b.text }))}>
                    {b.text}
                  </button>
                ))}
              </div>
            ))}
          </div>
        )}
        <form className="emu-input" onSubmit={submit}>
          <button type="button" title="Прикрепить фото или файл" disabled={readOnly || busy} onClick={() => fileInput.current?.click()}>
            📎
          </button>
          <input ref={fileInput} type="file" hidden onChange={attach} />
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={state.waiting ? 'бот ждёт ответа…' : group ? 'Сообщение в группу' : 'Сообщение или /команда'}
            disabled={readOnly}
          />
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
