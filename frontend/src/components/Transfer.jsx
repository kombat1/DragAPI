import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

async function download(url, fallbackName) {
  const res = await fetch(url)
  if (!res.ok) {
    let msg = res.statusText
    try {
      const d = await res.json()
      msg = d.detail?.message || d.detail || msg
      if (d.detail?.issues?.length) msg += ': ' + d.detail.issues.slice(0, 3).map((i) => i.message).join('; ')
    } catch {
      /* не JSON */
    }
    throw new Error(msg)
  }
  const cd = res.headers.get('content-disposition') || ''
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd)?.[1]
  const name = (star && decodeURIComponent(star)) || /filename="([^"]+)"/.exec(cd)?.[1] || fallbackName
  const a = document.createElement('a')
  a.href = URL.createObjectURL(await res.blob())
  a.download = name
  a.click()
  URL.revokeObjectURL(a.href)
}

const KIND_TEXT = { api: 'API-проект', automation: 'сценарий автоматизации', bot: 'бот' }

/** Экспорт: файл .dragapi.json (любой проект) и бот отдельной программой (zip). */
export function ExportModal({ projectId, kind, dirty, published, onSave, onClose }) {
  const isBot = kind === 'bot'
  const [source, setSource] = useState(isBot && published ? 'published' : 'draft')
  const [stripIds, setStripIds] = useState(false)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    const h = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  const go = async (what) => {
    setBusy(what)
    setError(null)
    try {
      if (dirty && source === 'draft') await onSave()
      const q = `source=${source}&strip_ids=${stripIds}`
      if (what === 'file') await download(`/api/projects/${projectId}/package?${q}`, `${projectId}.dragapi.json`)
      else await download(`/api/projects/${projectId}/package/standalone?${q}`, `${projectId}-bot.zip`)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="Экспорт">
        <div className="modal-head">
          <div className="inspector-title">📦 Экспорт: {KIND_TEXT[kind] || 'проект'}</div>
          <button type="button" className="icon" onClick={onClose}>
            ×
          </button>
        </div>

        {isBot && (
          <section>
            <div className="field-label">Какую версию</div>
            <div className="kind-choice two">
              <label className={`kind-card ${source === 'published' ? 'on' : ''} ${!published ? 'disabled' : ''}`}>
                <input type="radio" name="src" disabled={!published} checked={source === 'published'} onChange={() => setSource('published')} />
                <b>Опубликованную</b>
                <span className="muted">{published ? 'ту, что сейчас у клиентов' : 'бот ещё не публиковался'}</span>
              </label>
              <label className={`kind-card ${source === 'draft' ? 'on' : ''}`}>
                <input type="radio" name="src" checked={source === 'draft'} onChange={() => setSource('draft')} />
                <b>Черновик</b>
                <span className="muted">{dirty ? 'текущую — она сначала сохранится' : 'последнюю сохранённую'}</span>
              </label>
            </div>
          </section>
        )}

        <section className="export-options">
          <div className="export-row">
            <div>
              <b>Файл проекта</b> <span className="muted">.dragapi.json</span>
              <div className="hint">
                Резервная копия или перенос на другой сервер DragAPI: «+ Проект → Импорт из файла». Внутри блоки, связи и все
                настройки.
              </div>
            </div>
            <button className="primary" onClick={() => go('file')} disabled={!!busy}>
              {busy === 'file' ? 'Готовлю…' : '⬇ Скачать файл'}
            </button>
          </div>
          {isBot && (
            <div className="export-row">
              <div>
                <b>Отдельная программа</b> <span className="muted">zip</span>
                <div className="hint">
                  Бот работает без платформы на любом сервере с Python 3.10+: <code>pip install -r requirements.txt</code>, впишите
                  токен в <code>.env</code> → <code>python run_bot.py</code>. Есть Dockerfile.
                </div>
              </div>
              <button onClick={() => go('zip')} disabled={!!busy}>
                {busy === 'zip' ? 'Собираю…' : '⬇ Скачать zip'}
              </button>
            </div>
          )}
        </section>

        {(isBot || kind === 'automation') && (
          <label className="check export-check">
            <input type="checkbox" checked={stripIds} onChange={(e) => setStripIds(e.target.checked)} />
            Чтобы поделиться: убрать Telegram-ID {isBot ? 'администраторов' : 'разрешённых чатов'}
          </label>
        )}
        <div className="hint">
          🔒 Значения секретов и токенов в файл не попадают — только их имена. После импорта их нужно вписать заново. Данные
          (пользователи бота, платежи, история запусков) не экспортируются.
        </div>
        {error && <div className="issue error">{error}</div>}
        <div className="modal-foot">
          <span />
          <button type="button" onClick={onClose}>
            Закрыть
          </button>
        </div>
      </div>
    </div>
  )
}

/** Импорт из файла — внутри окна «Новый проект». */
export function ImportBox({ onImported }) {
  const input = useRef(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)

  const pick = async (file) => {
    if (!file) return
    setBusy(true)
    setError(null)
    try {
      if (file.size > 5_000_000) throw new Error('файл слишком большой (больше 5 МБ)')
      let pkg
      try {
        pkg = JSON.parse(await file.text())
      } catch {
        throw new Error('это не JSON-файл DragAPI')
      }
      setResult(await api.importProject(pkg))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
      if (input.current) input.current.value = ''
    }
  }

  if (result) {
    const p = result.project
    return (
      <div className="import-result">
        <div>
          ✓ Импортирован {KIND_TEXT[p.kind] || 'проект'} «<b>{p.name}</b>»
        </div>
        {result.missing_secrets?.length > 0 && (
          <div className="issue warning">
            Впишите секреты в «⚙ Настройки проекта → Переменные»: {result.missing_secrets.map((k) => <code key={k}>{k} </code>)}
          </div>
        )}
        {result.issues?.length > 0 && (
          <div className="issue warning">В проекте есть замечания ({result.issues.length}) — они подсвечены на холсте.</div>
        )}
        {p.kind === 'automation' && <div className="hint">Сценарии импортированы выключенными — включите их в настройках.</div>}
        <button className="primary" type="button" onClick={() => onImported(p)}>
          Открыть
        </button>
      </div>
    )
  }
  return (
    <div className="import-box">
      <input ref={input} type="file" accept=".json,application/json" hidden onChange={(e) => pick(e.target.files[0])} />
      <button type="button" onClick={() => input.current?.click()} disabled={busy}>
        {busy ? 'Импортирую…' : '📂 Импорт из файла…'}
      </button>
      <span className="muted"> .dragapi.json — из «📦 Экспорт» на этой или другой платформе DragAPI</span>
      {error && <div className="issue error">{error}</div>}
    </div>
  )
}
