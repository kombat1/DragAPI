// Резервная копия всех данных платформы: скачать (с паролем или без) и восстановить из файла.
import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { usePasteFiles } from '../paste.js'

const fmtSize = (b) => (b > 1e9 ? `${(b / 1e9).toFixed(1)} ГБ` : b > 1e6 ? `${(b / 1e6).toFixed(1)} МБ` : `${Math.max(1, Math.round(b / 1e3))} КБ`)
const fmtDate = (t) => new Date(t * 1000).toLocaleString()

export function BackupModal({ onClose }) {
  const [info, setInfo] = useState(null)
  const [error, setError] = useState(null)
  const [encrypt, setEncrypt] = useState(true)
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [busy, setBusy] = useState(null) // 'download' | 'restore'
  const [done, setDone] = useState(null)
  const [file, setFile] = useState(null)
  const [restorePw, setRestorePw] = useState('')

  const load = () => api.backupInfo().then(setInfo).catch((e) => setError(e.message))
  useEffect(() => {
    load()
    const h = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])
  usePasteFiles((files) => (setFile(files[0]), true), { enabled: true, priority: 10 })

  const download = async () => {
    setError(null)
    if (encrypt && pw.length < 8) return setError('пароль — не короче 8 символов')
    if (encrypt && pw !== pw2) return setError('пароли не совпадают')
    setBusy('download')
    try {
      const res = await fetch('/api/backup', {
        method: 'POST',
        headers: { 'X-DragAPI': '1', 'Content-Type': 'application/json' },
        body: JSON.stringify(encrypt ? { password: pw } : {}),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText)
      const name = /filename="([^"]+)"/.exec(res.headers.get('content-disposition') || '')?.[1] || 'dragapi-backup.zip'
      const a = document.createElement('a')
      a.href = URL.createObjectURL(await res.blob())
      a.download = name
      a.click()
      URL.revokeObjectURL(a.href)
      setDone(`Скачано: ${name}`)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  const restore = async () => {
    if (!file) return
    if (!confirm('Подготовить восстановление из этой копии? Оно применится при следующем запуске платформы; текущие данные будут отложены в соседнюю папку, не удалены.'))
      return
    setError(null)
    setBusy('restore')
    try {
      const form = new FormData()
      form.append('file', file)
      if (restorePw) form.append('password', restorePw)
      const res = await fetch('/api/backup/restore', { method: 'POST', headers: { 'X-DragAPI': '1' }, body: form })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || res.statusText)
      setFile(null)
      setRestorePw('')
      load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  const cancel = async () => {
    await api.backupCancelRestore()
    load()
  }

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal backup-modal" role="dialog" aria-label="Резервная копия">
        <div className="modal-head">
          <div className="inspector-title">💾 Резервная копия</div>
          <button className="icon" onClick={onClose} title="Закрыть (Esc)">
            ×
          </button>
        </div>

        {info?.pending && (
          <div className="issue warning backup-pending">
            <b>Восстановление подготовлено</b> — копия от {fmtDate(info.pending.created)} ({info.pending.projects} проектов). Оно
            применится при следующем запуске: остановите платформу (Ctrl+C) и запустите снова (<code>./start.sh</code>,
            на Windows — <code>start.cmd</code>; служба — <code>sudo systemctl restart dragapi</code>).{' '}
            <button className="small" onClick={cancel}>
              Отменить
            </button>
          </div>
        )}

        <section className="backup-section">
          <div className="field-label">Скачать копию всех данных</div>
          {info ? (
            <p className="muted">
              Проекты ({info.projects}) с историей версий, базы превью, пользователи, боты, агенты, сертификаты и ключ
              шифрования секретов — {info.files} файлов, {fmtSize(info.bytes)} до сжатия. Не входят окружения библиотек
              (их пересобирает кнопка «Установить») и журналы firewall. Базы PostgreSQL и Docker-тома деплоя — вне
              платформы: копируйте их своими средствами.
            </p>
          ) : (
            <p className="muted">Загрузка…</p>
          )}
          <label className="radio-line">
            <input type="checkbox" checked={encrypt} onChange={(e) => setEncrypt(e.target.checked)} />
            <span>
              <b>Зашифровать паролем</b>{' '}
              <span className="muted">
                — рекомендуется: в копии лежит ключ ко всем секретам проектов (токены ботов, пароли баз). Без пароля
                храните файл так же бережно, как пароль.
              </span>
            </span>
          </label>
          {encrypt && (
            <div className="backup-pw">
              <input type="password" autoComplete="new-password" placeholder="пароль (от 8 символов)" value={pw} onChange={(e) => setPw(e.target.value)} />
              <input type="password" autoComplete="new-password" placeholder="ещё раз" value={pw2} onChange={(e) => setPw2(e.target.value)} />
            </div>
          )}
          {encrypt && <div className="hint">Пароль нигде не сохраняется: забудете — копию не открыть.</div>}
          <button className="primary" onClick={download} disabled={busy !== null || !info}>
            {busy === 'download' ? 'Собираю…' : '⬇ Скачать копию'}
          </button>
          {done && <span className="ok-text"> ✓ {done}</span>}
        </section>

        <section className="backup-section">
          <div className="field-label">Восстановить из копии</div>
          <p className="muted">
            Например, на новом компьютере или после сбоя. Текущие данные не удаляются: при следующем запуске они переедут в
            папку <code>data.before-restore-…</code> рядом с данными.
          </p>
          <div className="backup-pw">
            <input type="file" accept=".zip,.enc" onChange={(e) => setFile(e.target.files[0] || null)} />
            <input type="password" placeholder="пароль копии (если зашифрована)" value={restorePw} onChange={(e) => setRestorePw(e.target.value)} />
          </div>
          {file && <div className="muted">Файл: {file.name} · {fmtSize(file.size)}</div>}
          <button onClick={restore} disabled={!file || busy !== null}>
            {busy === 'restore' ? 'Проверяю и распаковываю…' : '⬆ Подготовить восстановление'}
          </button>
          {info?.previous?.length > 0 && (
            <div className="muted">
              Прежние данные после восстановлений: {info.previous.map((p) => p.name).join(', ')} — их можно удалить, когда
              убедитесь, что всё на месте.
            </div>
          )}
        </section>

        {error && <div className="issue error">{error}</div>}
        <div className="modal-foot">
          <span className="muted">
            С сервера по расписанию: <code>python -m app backup</code> (подробно — в README).
          </span>
          <button onClick={onClose}>Закрыть</button>
        </div>
      </div>
    </div>
  )
}
