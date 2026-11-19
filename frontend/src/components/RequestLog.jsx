// Журнал запросов API: статистика по эндпоинтам (число, ошибки, время ответа) и последние запросы —
// превью в платформе или развёрнутый Docker-контейнер.
import { useEffect, useState } from 'react'
import { api } from '../api.js'

const WINDOWS = [
  [15 * 60, '15 минут'],
  [60 * 60, 'час'],
  [24 * 3600, 'сутки'],
  [0, 'всё время'],
]
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString()
const fmtMs = (v) => (v === null || v === undefined ? '—' : v < 10 ? v.toFixed(1) : Math.round(v))

function since(started) {
  const s = Math.max(0, Date.now() / 1000 - started)
  if (s < 90) return `${Math.round(s)} с`
  if (s < 5400) return `${Math.round(s / 60)} мин`
  if (s < 86400 * 2) return `${Math.round(s / 3600)} ч`
  return `${Math.round(s / 86400)} дн`
}

export default function RequestLog({ projectId, running, routes = [], onSelectNode }) {
  const [source, setSource] = useState('preview')
  const [win, setWin] = useState(3600)
  const [errorsOnly, setErrorsOnly] = useState(false)
  const [route, setRoute] = useState(null) // "GET /orders/{id}"
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let alive = true
    const load = () => {
      const p = new URLSearchParams({ source, limit: '300' })
      if (win) p.set('since', String(Date.now() / 1000 - win))
      if (errorsOnly) p.set('errors', 'true')
      if (route) p.set('route', route)
      api
        .requestLog(projectId, p.toString())
        .then((d) => alive && (setData(d), setError(null)))
        .catch((e) => alive && (setData(null), setError(e.message)))
    }
    load()
    const t = setInterval(load, 3000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [projectId, source, win, errorsOnly, route, tick])

  const nodeFor = (method, path) => routes.find((r) => r.method === method && r.path === path)?.node_id
  const clear = async () => {
    if (!confirm('Очистить журнал запросов превью?')) return
    await api.clearRequestLog(projectId)
    setRoute(null)
    setTick((x) => x + 1)
  }

  const eps = data?.endpoints || []
  const totals = eps.reduce((a, e) => ({ n: a.n + e.count, c4: a.c4 + e.c4, c5: a.c5 + e.c5 }), { n: 0, c4: 0, c5: 0 })

  return (
    <div className="reqlog">
      <div className="reqlog-bar">
        <div className="seg" role="tablist">
          {[
            ['preview', 'Превью'],
            ['deploy', '🐳 Деплой'],
          ].map(([k, label]) => (
            <button key={k} className={`small ${source === k ? 'on' : ''}`} onClick={() => (setSource(k), setRoute(null))}>
              {label}
            </button>
          ))}
        </div>
        <select value={win} onChange={(e) => setWin(Number(e.target.value))} title="За какой период считать статистику">
          {WINDOWS.map(([v, label]) => (
            <option key={v} value={v}>
              за {label}
            </option>
          ))}
        </select>
        <label className="check">
          <input type="checkbox" checked={errorsOnly} onChange={(e) => setErrorsOnly(e.target.checked)} /> только ошибки
        </label>
        {data && (
          <span className="muted">
            {totals.n} запр. · <span className={totals.c5 ? 'bad-text' : ''}>5xx: {totals.c5}</span> · 4xx: {totals.c4} · работает {since(data.started)}
            {data.kept >= data.capacity ? ` · хранятся последние ${data.capacity}` : ''}
          </span>
        )}
        <div className="spacer" />
        {source === 'preview' && data && (
          <button className="small" onClick={clear}>
            Очистить
          </button>
        )}
      </div>

      {error ? (
        <div className="muted pad">{error}</div>
      ) : !data ? (
        <div className="muted pad">Загрузка…</div>
      ) : !eps.length && !data.items.length ? (
        <div className="muted pad">
          {source === 'preview'
            ? running
              ? 'Запросов к превью за этот период не было. Отправьте запрос (например, через Swagger) — он появится здесь.'
              : 'Нажмите «▶ Запустить» и отправьте запрос к API. Запросы из вкладки «Тесты» в журнал не попадают.'
            : 'Запросов к развёрнутому контейнеру за этот период не было.'}
        </div>
      ) : (
        <div className="reqlog-body">
          <table className="reqlog-table">
            <thead>
              <tr>
                <th>Эндпоинт</th>
                <th title="Запросов за период">Запросов</th>
                <th title="Ошибки клиента: неверные данные, нет доступа, не найдено">4xx</th>
                <th title="Ошибки сервера: упал блок, недоступна база">5xx</th>
                <th title="Среднее время ответа">Среднее, мс</th>
                <th title="95% запросов быстрее этого">p95, мс</th>
                <th>Макс, мс</th>
                <th>Последняя ошибка</th>
              </tr>
            </thead>
            <tbody>
              {eps.map((e) => {
                const key = `${e.method} ${e.route}`
                const node = nodeFor(e.method, e.route)
                return (
                  <tr
                    key={key}
                    className={`${route === key ? 'on' : ''} ${e.c5 ? 'has-5xx' : ''}`}
                    onClick={() => {
                      setRoute(route === key ? null : key)
                      if (node && route !== key) onSelectNode?.(node)
                    }}
                    title="Показать запросы только к этому эндпоинту"
                  >
                    <td>
                      <span className={`method m-${e.method}`}>{e.method}</span> <code>{e.route}</code>
                    </td>
                    <td>{e.count}</td>
                    <td className={e.c4 ? 'warn-text' : 'muted'}>{e.c4}</td>
                    <td className={e.c5 ? 'bad-text' : 'muted'}>{e.c5}</td>
                    <td>{fmtMs(e.avg)}</td>
                    <td>{fmtMs(e.p95)}</td>
                    <td>{fmtMs(e.max)}</td>
                    <td className="reqlog-err">
                      {e.last_error ? (
                        <>
                          <span className={`code c${String(e.last_error.status)[0]}`}>{e.last_error.status}</span>{' '}
                          <span className="muted">{fmtTime(e.last_error.time)}</span> {e.last_error.error}
                        </>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div className="reqlog-items">
            <div className="section-title">
              {route ? `Запросы к ${route}` : 'Последние запросы'}
              {errorsOnly ? ' с ошибкой' : ''} · {data.matched}
              {route && (
                <button className="small link" onClick={() => setRoute(null)}>
                  все эндпоинты
                </button>
              )}
            </div>
            {data.items.map((i, k) => (
              <div key={k} className={`log-row ${i.status >= 500 ? 'is-5xx' : ''}`}>
                <span className="muted">{fmtTime(i.time)}</span>
                <span className={`method m-${i.method}`}>{i.method}</span>
                <code>{i.path}</code>
                <span className={`code c${String(i.status)[0]}`}>{i.status}</span>
                <span className="muted">{fmtMs(i.ms)} мс</span>
                {i.error && <span className="reqlog-err">{i.error}</span>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
