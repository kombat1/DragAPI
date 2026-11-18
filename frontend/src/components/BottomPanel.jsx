import { useEffect, useRef, useState } from 'react'
import TestsPanel from './TestsPanel.jsx'
import { api } from '../api.js'
import { NetworkEvents } from './Inspector.jsx'
import { RunsPanel } from './Automation.jsx'
import { BotLog, BotUsers, EmulatorPanel } from './Bots.jsx'

const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString()

function Logs({ projectId, running, onSelectNode, onHighlight }) {
  const [logs, setLogs] = useState([])
  const [open, setOpen] = useState(null)

  useEffect(() => {
    if (!running) return
    let alive = true
    const tick = () => api.logs(projectId).then((l) => alive && setLogs(l)).catch(() => {})
    tick()
    const t = setInterval(tick, 1500)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [projectId, running])

  const select = (i) => {
    setOpen(i === open ? null : i)
    onHighlight(i === open ? [] : [logs[i].endpoint, ...logs[i].trace.map((s) => s.node_id)])
  }

  if (!running) return <div className="muted pad">Нажмите «Запустить», затем отправьте запрос (например, через Swagger) — здесь появится пошаговый лог.</div>
  if (!logs.length)
    return (
      <div className="muted pad">
        API запущено на <code>{running.base_url}</code>. Запросов пока не было — откройте{' '}
        <a href={running.docs_url} target="_blank" rel="noreferrer">
          Swagger
        </a>
        .
      </div>
    )

  return (
    <div className="logs">
      <div className="logs-list">
        <button
          className="small"
          onClick={() => api.clearLogs(projectId).then(() => (setLogs([]), setOpen(null), onHighlight([])))}
        >
          Очистить
        </button>
        {logs.map((l, i) => (
          <div key={i} className={`log-row ${open === i ? 'open' : ''}`} onClick={() => select(i)}>
            <span className="muted">{fmtTime(l.time)}</span>
            <span className={`method m-${l.method}`}>{l.method}</span>
            <code>{l.path}</code>
            <span className={`code c${String(l.status)[0]}`}>{l.status}</span>
            <span className="muted">{l.ms} мс</span>
          </div>
        ))}
      </div>
      {open !== null && logs[open] && (
        <div className="trace">
          {logs[open].trace.map((s, k) => (
            <div key={k} className={`trace-step ${s.kind}`} onClick={() => onSelectNode(s.node_id)}>
              <div>
                <b>{s.kind}</b> <span className="muted">{s.node_id}</span> {s.ms ? <span className="muted">· {s.ms} мс</span> : null}
              </div>
              <pre>{JSON.stringify(s.error ?? s.ctx, null, 1)}</pre>
              {s.network?.length > 0 && <NetworkEvents events={s.network} />}
              {s.stdout ? (
                <>
                  <div className="muted">print() · песочница {s.sandbox}, {s.sandbox_ms} мс</div>
                  <pre className="stdout">{s.stdout}</pre>
                </>
              ) : null}
            </div>
          ))}
          <div className="trace-step response">
            <b>ответ</b>
            <pre>{JSON.stringify(logs[open].response, null, 1)}</pre>
          </div>
        </div>
      )}
    </div>
  )
}

function DataView({ projectId, running }) {
  const [overview, setOverview] = useState(null)
  const [model, setModel] = useState(null)
  const [table, setTable] = useState(null)
  const [error, setError] = useState(null)

  // обновляем, пока вкладка открыта: запросы к API меняют данные
  useEffect(() => {
    if (!running) return
    let alive = true
    const tick = async () => {
      try {
        const o = await api.dataOverview(projectId)
        if (!alive) return
        setOverview(o)
        setError(null)
        const current = model && o.some((m) => m.model === model) ? model : o[0]?.model
        if (current !== model) setModel(current ?? null)
        if (current) {
          const t = await api.dataRows(projectId, current)
          if (alive) setTable(t)
        }
      } catch (e) {
        if (alive) setError(e.message)
      }
    }
    tick()
    const t = setInterval(tick, 2000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [projectId, running, model])

  if (!running) return <div className="muted pad">Запустите проект, чтобы увидеть содержимое превью-базы.</div>
  if (error) return <div className="muted pad">{error}</div>
  if (!overview) return null
  if (!overview.length)
    return <div className="muted pad">В проекте нет моделей данных. Добавьте их в «Моделях и переменных».</div>

  const fmt = (v) => (v === null || v === undefined ? 'null' : typeof v === 'object' ? JSON.stringify(v) : String(v))
  return (
    <div className="data-view">
      <div className="data-models">
        {overview.map((m) => (
          <div
            key={m.model}
            className={`log-row ${m.model === model ? 'open' : ''}`}
            onClick={() => {
              setModel(m.model)
              setTable(null)
            }}
          >
            <code>{m.model}</code>
            <span className="muted">{m.count}</span>
          </div>
        ))}
        {model && (
          <button
            className="small danger"
            onClick={async () => {
              if (!confirm(`Удалить все записи «${model}» из превью-базы?`)) return
              await api.dataClear(projectId, model)
              setTable(await api.dataRows(projectId, model))
            }}
          >
            Очистить таблицу
          </button>
        )}
      </div>
      <div className="data-table-wrap">
        {table &&
          (table.rows.length ? (
            <table className="data-table">
              <thead>
                <tr>
                  {table.columns.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {table.rows.map((r) => (
                  <tr key={r.id}>
                    {table.columns.map((c) => (
                      <td key={c} className={r[c] === null ? 'null' : ''}>
                        {fmt(r[c])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="muted pad">Таблица пуста.</div>
          ))}
        {table && table.count > table.rows.length && (
          <div className="muted pad">Показаны последние {table.rows.length} из {table.count}.</div>
        )}
      </div>
    </div>
  )
}

function Versions({ projectId, onRestored }) {
  const [versions, setVersions] = useState(null)
  useEffect(() => {
    api.versions(projectId).then(setVersions)
  }, [projectId])
  if (!versions) return null
  if (!versions.length) return <div className="muted pad">История появится после повторных сохранений.</div>
  return (
    <div className="versions">
      {versions.map((v) => (
        <div key={v.version} className="log-row">
          <span>{new Date(v.time * 1000).toLocaleString()}</span>
          <button
            className="small"
            onClick={async () => {
              if (!confirm('Восстановить эту версию? Текущая попадёт в историю.')) return
              await api.restore(projectId, v.version)
              onRestored()
            }}
          >
            Восстановить
          </button>
        </div>
      ))}
    </div>
  )
}

const HEIGHT_KEY = 'dragapi.bottomHeight'
function loadHeight() {
  try {
    return Number(localStorage.getItem(HEIGHT_KEY)) || 300
  } catch {
    return 300
  }
}

export default function BottomPanel({
  projectId, running, issues, onSelectNode, onHighlight, onRestored, models,
  tab, setTab, testsProps, runsProps, botProps,
}) {
  const [collapsed, setCollapsed] = useState(false)
  const [height, setHeight] = useState(loadHeight)
  const drag = useRef(null)
  const errors = issues.filter((i) => i.level === 'error').length

  // высота панели перетаскиванием верхней границы
  const startResize = (e) => {
    e.preventDefault()
    drag.current = { y: e.clientY, h: height }
    const move = (ev) => {
      const h = Math.min(Math.max(drag.current.h + drag.current.y - ev.clientY, 120), window.innerHeight - 200)
      setHeight(h)
    }
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      setHeight((h) => {
        try {
          localStorage.setItem(HEIGHT_KEY, String(h))
        } catch {
          /* не запомнится — не страшно */
        }
        return h
      })
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  return (
    <section className={`bottom-panel ${collapsed ? 'collapsed' : ''}`} style={collapsed ? undefined : { height }}>
      {!collapsed && <div className="resize-handle" onPointerDown={startResize} title="Потяните, чтобы изменить высоту" />}
      <div className="tabs">
        {(botProps
          ? [
              ['emulator', 'Тест-чат'],
              ['botlog', `Журнал${botProps.running ? ' ●' : ''}`],
              ['users', 'Пользователи'],
              ['issues', `Проблемы${issues.length ? ` (${issues.length})` : ''}`],
              ['versions', 'История версий'],
            ]
          : runsProps
          ? [
              ['runs', 'Запуски'],
              ['issues', `Проблемы${issues.length ? ` (${issues.length})` : ''}`],
              ['versions', 'История версий'],
            ]
          : [
              ['tests', `Тесты${testsProps.tests.length ? ` (${testsProps.tests.length})` : ''}`],
              ['issues', `Проблемы${issues.length ? ` (${issues.length})` : ''}`],
              ['logs', `Логи${running ? ' ●' : ''}`],
              ['data', `Данные${models?.length ? ` (${models.length})` : ''}`],
              ['versions', 'История'],
            ]
        ).map(([k, label]) => (
          <button
            key={k}
            className={`tab ${tab === k ? 'active' : ''} ${k === 'issues' && errors ? 'bad' : ''}`}
            onClick={() => (setTab(k), setCollapsed(false))}
          >
            {label}
          </button>
        ))}
        <div className="spacer" />
        <button className="tab" onClick={() => setCollapsed((c) => !c)}>
          {collapsed ? '▲' : '▼'}
        </button>
      </div>
      {!collapsed && (
        <div className="tab-body">
          {tab === 'tests' && !runsProps && !botProps && <TestsPanel projectId={projectId} {...testsProps} />}
          {botProps && (
            <div style={{ display: tab === 'emulator' ? undefined : 'none', height: '100%' }}>
              <EmulatorPanel projectId={projectId} onSelectNode={onSelectNode} {...botProps} />
            </div>
          )}
          {botProps && tab === 'botlog' && <BotLog projectId={projectId} onSelectNode={onSelectNode} />}
          {botProps && tab === 'users' && <BotUsers projectId={projectId} readOnly={botProps.readOnly} />}
          {runsProps && (
            <div style={{ display: tab === 'runs' ? undefined : 'none', height: '100%' }}>
              <RunsPanel projectId={projectId} onSelectNode={onSelectNode} {...runsProps} />
            </div>
          )}
          {tab === 'issues' &&
            (issues.length ? (
              issues.map((i, k) => (
                <div key={k} className={`issue ${i.level} clickable`} onClick={() => i.node_id && onSelectNode(i.node_id)}>
                  {i.level === 'error' ? '✖' : '⚠'} {i.message}
                  {i.node_id && <span className="muted"> · {i.node_id}</span>}
                </div>
              ))
            ) : (
              <div className="muted pad">Проблем нет.</div>
            ))}
          {tab === 'logs' && (
            <Logs projectId={projectId} running={running} onSelectNode={onSelectNode} onHighlight={onHighlight} />
          )}
          {tab === 'data' && <DataView projectId={projectId} running={running} />}
          {tab === 'versions' && <Versions projectId={projectId} onRestored={onRestored} />}
        </div>
      )}
    </section>
  )
}
