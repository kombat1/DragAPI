import { useEffect, useState } from 'react'
import { api } from '../api.js'

export function LoginScreen({ setup, onDone }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [password2, setPassword2] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    if (setup && password !== password2) return setError('пароли не совпадают')
    setBusy(true)
    setError(null)
    try {
      const r = setup ? await api.setup(username, password) : await api.login(username, password)
      onDone(r.user)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <div className="logo big">
          Drag<span>API</span>
        </div>
        {setup ? (
          <p className="muted">
            Первый запуск: создайте учётную запись администратора. Все существующие проекты перейдут к ней.
          </p>
        ) : (
          <p className="muted">Войдите, чтобы продолжить.</p>
        )}
        <label className="field-label">Имя пользователя</label>
        <input autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        <label className="field-label">Пароль</label>
        <input
          type="password"
          autoComplete={setup ? 'new-password' : 'current-password'}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {setup && (
          <>
            <label className="field-label">Пароль ещё раз</label>
            <input type="password" autoComplete="new-password" value={password2} onChange={(e) => setPassword2(e.target.value)} />
            <div className="hint">Не короче 8 символов.</div>
          </>
        )}
        {error && <div className="issue error">{error}</div>}
        <button className="primary" type="submit" disabled={busy || !username || !password}>
          {setup ? 'Создать администратора' : 'Войти'}
        </button>
      </form>
    </div>
  )
}

function Modal({ title, onClose, children, wide }) {
  useEffect(() => {
    const h = (e) => e.key === 'Escape' && !e.defaultPrevented && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" style={wide ? undefined : { width: 'min(560px, 100%)' }} role="dialog" aria-label={title}>
        <div className="modal-head">
          <div className="inspector-title">{title}</div>
          <button className="icon" onClick={onClose} title="Закрыть (Esc)">
            ×
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}

function UsersModal({ me, onClose }) {
  const [users, setUsers] = useState([])
  const [form, setForm] = useState({ username: '', password: '', role: 'user' })
  const [error, setError] = useState(null)
  const load = () => api.users().then(setUsers).catch((e) => setError(e.message))
  useEffect(() => {
    load()
  }, [])
  const create = async () => {
    setError(null)
    try {
      await api.createUser(form)
      setForm({ username: '', password: '', role: 'user' })
      load()
    } catch (e) {
      setError(e.message)
    }
  }
  return (
    <Modal title="Пользователи" onClose={onClose}>
      <div className="users-list">
        {users.map((u) => (
          <div key={u.id} className="log-row">
            <b>{u.username}</b>
            <span className="muted">{u.role === 'admin' ? 'администратор' : 'пользователь'}</span>
            <div className="spacer" />
            <button
              className="small"
              onClick={async () => {
                const pw = prompt(`Новый пароль для ${u.username} (не короче 8 символов)`)
                if (!pw) return
                try {
                  await api.resetPassword(u.id, pw)
                  alert('Пароль изменён, сессии пользователя завершены')
                } catch (e) {
                  setError(e.message)
                }
              }}
            >
              Сбросить пароль
            </button>
            {u.id !== me.id && (
              <button
                className="small danger"
                onClick={async () => {
                  if (!confirm(`Удалить пользователя ${u.username}? Его доступы к проектам будут сняты.`)) return
                  try {
                    await api.deleteUser(u.id)
                    load()
                  } catch (e) {
                    setError(e.message)
                  }
                }}
              >
                Удалить
              </button>
            )}
          </div>
        ))}
      </div>
      <div className="section-title">Новый пользователь</div>
      <div className="list-row">
        <input placeholder="имя" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
        <input
          type="password"
          placeholder="пароль (8+)"
          autoComplete="new-password"
          value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })}
        />
        <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
          <option value="user">пользователь</option>
          <option value="admin">администратор</option>
        </select>
        <button className="primary" onClick={create} disabled={!form.username || !form.password}>
          Создать
        </button>
      </div>
      {error && <div className="issue error">{error}</div>}
    </Modal>
  )
}

function PasswordModal({ onClose }) {
  const [old, setOld] = useState('')
  const [pw, setPw] = useState('')
  const [error, setError] = useState(null)
  const [done, setDone] = useState(false)
  return (
    <Modal title="Сменить пароль" onClose={onClose}>
      {done ? (
        <p>Пароль изменён. Все сессии завершены — войдите заново.</p>
      ) : (
        <div className="login-card flat">
          <label className="field-label">Текущий пароль</label>
          <input type="password" autoComplete="current-password" value={old} onChange={(e) => setOld(e.target.value)} />
          <label className="field-label">Новый пароль (8+ символов)</label>
          <input type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} />
          {error && <div className="issue error">{error}</div>}
          <button
            className="primary"
            onClick={async () => {
              try {
                await api.changePassword(old, pw)
                setDone(true)
                setTimeout(() => location.reload(), 1200)
              } catch (e) {
                setError(e.message)
              }
            }}
          >
            Сменить
          </button>
        </div>
      )}
    </Modal>
  )
}

export function UserMenu({ me, onLogout }) {
  const [open, setOpen] = useState(null) // users | password
  return (
    <div className="user-menu">
      <span className="muted">
        {me.username}
        {me.role === 'admin' ? ' · админ' : ''}
      </span>
      {me.role === 'admin' && (
        <button className="small" onClick={() => setOpen('users')}>
          Пользователи
        </button>
      )}
      <button className="small" onClick={() => setOpen('password')}>
        Пароль
      </button>
      <button
        className="small"
        onClick={async () => {
          await api.logout()
          onLogout()
        }}
      >
        Выйти
      </button>
      {open === 'users' && <UsersModal me={me} onClose={() => setOpen(null)} />}
      {open === 'password' && <PasswordModal onClose={() => setOpen(null)} />}
    </div>
  )
}

const ROLE_NAMES = { owner: 'владелец', editor: 'редактор', viewer: 'наблюдатель' }

export function MembersModal({ projectId, me, onClose }) {
  const [users, setUsers] = useState([])
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    api.usersBrief().then(setUsers)
    api.members(projectId).then(setData)
  }, [projectId])
  if (!data) return null
  const name = (id) => users.find((u) => u.id === id)?.username || id
  const save = async (members) => {
    try {
      setData(await api.setMembers(projectId, members))
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }
  const candidates = users.filter((u) => u.id !== data.owner && !(u.id in data.members))
  return (
    <Modal title="Доступ к проекту" onClose={onClose}>
      <p className="hint">
        <b>Редактор</b> меняет граф, запускает и тестирует. <b>Наблюдатель</b> только смотрит граф, логи и данные. Участниками
        и удалением проекта управляет владелец (и администраторы платформы).
      </p>
      <div className="log-row">
        <b>{name(data.owner)}</b>
        <span className="muted">{ROLE_NAMES.owner}</span>
        {data.owner === me?.id && <span className="muted">(вы)</span>}
      </div>
      {Object.entries(data.members).map(([uid, role]) => (
        <div key={uid} className="log-row">
          <b>{name(uid)}</b>
          <select value={role} onChange={(e) => save({ ...data.members, [uid]: e.target.value })}>
            <option value="editor">{ROLE_NAMES.editor}</option>
            <option value="viewer">{ROLE_NAMES.viewer}</option>
          </select>
          <div className="spacer" />
          <button
            className="small danger"
            onClick={() => {
              const next = { ...data.members }
              delete next[uid]
              save(next)
            }}
          >
            Убрать
          </button>
        </div>
      ))}
      {candidates.length > 0 ? (
        <div className="list-row">
          <select id="add-member" defaultValue="">
            <option value="" disabled>
              добавить пользователя…
            </option>
            {candidates.map((u) => (
              <option key={u.id} value={u.id}>
                {u.username}
              </option>
            ))}
          </select>
          <button
            className="small"
            onClick={() => {
              const uid = document.getElementById('add-member').value
              if (uid) save({ ...data.members, [uid]: 'viewer' })
            }}
          >
            + наблюдатель
          </button>
          <button
            className="small"
            onClick={() => {
              const uid = document.getElementById('add-member').value
              if (uid) save({ ...data.members, [uid]: 'editor' })
            }}
          >
            + редактор
          </button>
        </div>
      ) : (
        <div className="muted">Других пользователей нет — их создаёт администратор.</div>
      )}
      {error && <div className="issue error">{error}</div>}
    </Modal>
  )
}
