import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

export default function DeployModal({ projectId, readOnly, dirty, onClose }) {
  const [st, setSt] = useState(null)
  const [port, setPort] = useState('')
  const [isPublic, setIsPublic] = useState(false)
  const [error, setError] = useState(null)
  const [logs, setLogs] = useState(null)
  const logRef = useRef(null)

  const refresh = async () => {
    try {
      const s = await api.deployStatus(projectId)
      setSt(s)
      return s
    } catch (e) {
      setError(e.message)
    }
  }
  useEffect(() => {
    refresh().then((s) => {
      if (s?.settings?.port) setPort(String(s.settings.port))
      if (s?.settings?.public) setIsPublic(true)
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])
  useEffect(() => {
    if (!st?.deploying) return
    const t = setInterval(refresh, 1500)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [st?.deploying])
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [st?.log])
  useEffect(() => {
    const h = (e) => e.key === 'Escape' && !e.defaultPrevented && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  const deploy = async () => {
    setError(null)
    try {
      setSt(await api.deploy(projectId, { port: port ? Number(port) : null, public: isPublic }))
    } catch (e) {
      setError(e.message)
    }
  }
  const stop = async (removeData) => {
    if (removeData && !confirm('Остановить и удалить контейнер, образ и том с данными? Данные развёрнутого API будут потеряны.')) return
    setSt(await api.deployStop(projectId, removeData))
  }

  const c = st?.container
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="Деплой">
        <div className="modal-head">
          <div className="inspector-title">🚀 Деплой в Docker</div>
          <button className="icon" onClick={onClose} title="Закрыть (Esc)">
            ×
          </button>
        </div>
        <p className="hint">
          Проект экспортируется в обычное FastAPI-приложение, собирается в образ и запускается контейнером на этой машине: свой
          пользователь без root, без capabilities, лимиты памяти и процессов, база — в отдельном томе (переживает повторный деплой).
          Секреты передаются в контейнер из хранилища и в образ не попадают. Деплоится <b>сохранённая</b> версия проекта.
        </p>
        {!st ? (
          <div className="muted">Загрузка…</div>
        ) : !st.docker ? (
          <div className="issue error">Docker недоступен на этой машине — деплой невозможен. Экспорт в zip по-прежнему работает.</div>
        ) : (
          <>
            <div className="deploy-status">
              {st.deploying ? (
                <span className="lib-state busy">Идёт деплой…</span>
              ) : c?.running ? (
                <>
                  <span className="lib-state good">Работает</span>
                  <a href={st.url + '/docs'} target="_blank" rel="noreferrer">
                    {st.url} ↗
                  </a>
                </>
              ) : c ? (
                <span className="lib-state bad">Контейнер: {c.status} (код {c.exit_code})</span>
              ) : (
                <span className="lib-state">Не развёрнут</span>
              )}
            </div>
            {dirty && <div className="issue warning">Есть несохранённые изменения — в контейнер попадёт последняя сохранённая версия.</div>}
            <div className="list-row deploy-settings">
              <label className="field">
                <span className="field-label">Порт на этой машине</span>
                <input type="number" placeholder="авто (9100–9199)" value={port} onChange={(e) => setPort(e.target.value)} disabled={readOnly} />
              </label>
              <label className="toggle">
                <input type="checkbox" checked={isPublic} onChange={(e) => setIsPublic(e.target.checked)} disabled={readOnly} />
                <div>
                  <b>Доступно из сети</b>
                  <div className="muted">
                    Иначе только с этой машины (127.0.0.1). Открывая доступ, подумайте о блоке «Авторизация» и входящем firewall.
                  </div>
                </div>
              </label>
            </div>
            <div className="tester-actions">
              <button className="primary" onClick={deploy} disabled={readOnly || st.deploying}>
                {c ? 'Пересобрать и перезапустить' : 'Развернуть'}
              </button>
              {c && (
                <>
                  <button onClick={() => stop(false)} disabled={readOnly || st.deploying}>
                    ■ Остановить
                  </button>
                  <button className="danger" onClick={() => stop(true)} disabled={readOnly || st.deploying}>
                    Удалить вместе с данными
                  </button>
                  <button onClick={async () => setLogs((await api.deployLogs(projectId)).logs)}>Логи контейнера</button>
                </>
              )}
            </div>
            {error && <div className="issue error">{error}</div>}
            {st.log && (
              <div className="field">
                <div className="field-label">Лог деплоя</div>
                <pre className="install-log" ref={logRef}>
                  {st.log}
                </pre>
              </div>
            )}
            {logs !== null && (
              <div className="field">
                <div className="field-label">Логи контейнера</div>
                <pre className="install-log">{logs || 'пусто'}</pre>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
