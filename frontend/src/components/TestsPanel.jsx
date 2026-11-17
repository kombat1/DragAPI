import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import { useCatalog } from '../catalog.js'
import { NetworkEvents } from './Inspector.jsx'

const uid = () => Math.random().toString(36).slice(2, 9)
const SAMPLE = { str: '', int: 0, float: 0, bool: false }

// Блоки, достижимые из эндпоинта (включая обе ветки условий).
function chainNodes(endpointId, nodes, edges) {
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]))
  const out = []
  const queue = [endpointId]
  const seen = new Set()
  while (queue.length) {
    const id = queue.shift()
    if (seen.has(id) || !byId[id]) continue
    seen.add(id)
    out.push(byId[id])
    edges.filter((e) => e.source === id).forEach((e) => queue.push(e.target))
  }
  return out
}

// Черновик запроса по блокам эндпоинта: поля, значения по умолчанию, заголовок авторизации.
export function draftRequest(endpointId, nodes, edges) {
  const chain = chainNodes(endpointId, nodes, edges)
  const endpoint = chain[0]
  const req = { path: {}, query: {}, headers: {}, body_type: 'none', body: '', form: {} }
  for (const m of (endpoint?.data.path || '').matchAll(/{(\w+)}/g)) req.path[m[1]] = ''
  const json = {}
  for (const n of chain) {
    if (n.type === 'input') {
      for (const f of n.data.fields || []) {
        if (!f.name) continue
        const value = f.required || f.default === '' || f.default == null ? SAMPLE[f.type] ?? '' : f.default
        if (n.data.source === 'query') req.query[f.name] = f.required ? '' : String(value)
        if (n.data.source === 'json') json[f.name] = value
        if (n.data.source === 'form' && f.type !== 'file') req.form[f.name] = String(value)
      }
      if (n.data.source === 'json') req.body_type = 'json'
      if (n.data.source === 'form' && req.body_type !== 'json') req.body_type = 'form'
    }
    if (n.type === 'auth' && n.data.secret_env) {
      const secret = `{{env.${n.data.secret_env}}}`
      if (n.data.scheme === 'bearer') req.headers.Authorization = `Bearer ${secret}`
      else if (n.data.scheme === 'api_key_query') req.query[n.data.name] = secret
      else req.headers[n.data.name] = secret
    }
  }
  if (req.body_type === 'json') req.body = JSON.stringify(json, null, 2)
  return req
}

function KeyValues({ value, onChange, keyPlaceholder = 'ключ', fixedKeys = false }) {
  const rows = Object.entries(value || {})
  const set = (entries) => onChange(Object.fromEntries(entries))
  return (
    <div className="kv">
      {rows.map(([k, v], i) => (
        <div className="kv-row" key={i}>
          <input
            className="mono"
            value={k}
            placeholder={keyPlaceholder}
            disabled={fixedKeys}
            onChange={(e) => set(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))}
          />
          <input
            className="mono"
            value={v}
            placeholder="значение или {{env.KEY}}"
            onChange={(e) => set(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))}
          />
          {!fixedKeys && (
            <button className="icon" onClick={() => set(rows.filter((_, j) => j !== i))}>
              ×
            </button>
          )}
        </div>
      ))}
      {!fixedKeys && (
        <button className="small" onClick={() => set([...rows, ['', '']])}>
          + добавить
        </button>
      )}
    </div>
  )
}

function diff(before, after) {
  const out = []
  for (const [k, v] of Object.entries(after || {})) {
    if (!(k in (before || {}))) out.push(['+', k, v])
    else if (JSON.stringify(before[k]) !== JSON.stringify(v)) out.push(['~', k, v])
  }
  return out
}

// Шаги трассы: эндпоинт + блоки, с данными до/после.
export function traceSteps(result) {
  if (!result) return []
  const steps = [{ node_id: result.endpoint, kind: 'endpoint', ctx: {}, before: {} }]
  let before = {}
  for (const s of result.trace || []) {
    const isCtx = s.kind !== 'condition' && s.kind !== 'error'
    steps.push({ ...s, before })
    if (isCtx && s.ctx) before = s.ctx
  }
  return steps
}

export function StepDetails({ step, catalog, nodes, compact = false }) {
  if (!step) return null
  const node = nodes.find((n) => n.id === step.node_id)
  const title = catalog.blocks[node?.type]?.title || step.kind
  const changes = step.kind === 'condition' || step.kind === 'error' || step.kind === 'endpoint' ? [] : diff(step.before, step.ctx)
  return (
    <div className={`step-details ${compact ? 'compact' : ''}`}>
      <div>
        <b>{title}</b> <span className="muted">{step.node_id}</span>
        {step.ms ? <span className="muted"> · {step.ms} мс</span> : null}
      </div>
      {step.kind === 'endpoint' && <div className="muted">начало запроса</div>}
      {step.kind === 'condition' && (
        <div>
          <code>{step.ctx?.['условие']}</code> → <b className={step.ctx?.['результат'] ? 'yes' : 'no'}>{step.ctx?.['результат'] ? 'да' : 'нет'}</b>
        </div>
      )}
      {step.kind === 'error' && <pre className="step-error">{JSON.stringify(step.error, null, 1)}</pre>}
      {step.kind === 'response' && <div className="muted">ответ сформирован</div>}
      {changes.length > 0 && (
        <div className="step-diff">
          {changes.map(([op, k, v]) => (
            <div key={k}>
              <span className={op === '+' ? 'added' : 'changed'}>{op}</span> <code>{k}</code> ={' '}
              <code className="expr">{JSON.stringify(v)?.slice(0, compact ? 60 : 400)}</code>
            </div>
          ))}
        </div>
      )}
      {!compact && changes.length === 0 && ['input', 'logic', 'db', 'code'].includes(step.kind) && (
        <div className="muted">данные не изменились</div>
      )}
      {!compact && step.network?.length > 0 && <NetworkEvents events={step.network} />}
      {!compact && step.stdout && <pre className="stdout">{step.stdout}</pre>}
    </div>
  )
}

function ResultView({ result, step, setStep, nodes }) {
  const catalog = useCatalog()
  const steps = traceSteps(result)
  if (!result) return <div className="muted pad">Отправьте запрос — здесь будет ответ и пошаговая трасса.</div>
  const current = steps[step]
  return (
    <div className="result-view">
      <div className="result-head">
        <span className={`code c${String(result.status)[0]}`}>{result.status}</span>
        <code>
          {result.method} {result.url}
        </code>
        <span className="muted">{result.ms} мс</span>
      </div>
      <pre className="result-body">{typeof result.body === 'string' ? result.body : JSON.stringify(result.body, null, 2)}</pre>
      {result.rejected_before_chain ? (
        <div className="muted">Запрос отклонён до первого блока — проверкой типов FastAPI (см. тело ответа).</div>
      ) : (
        <>
          <div className="stepper">
            <button className="small" disabled={step <= 0} onClick={() => setStep(step - 1)} title="Предыдущий шаг (←)">
              ◀
            </button>
            <span>
              шаг {step + 1} из {steps.length}
            </span>
            <button className="small" disabled={step >= steps.length - 1} onClick={() => setStep(step + 1)} title="Следующий шаг (→)">
              ▶
            </button>
            <div className="step-dots">
              {steps.map((s, i) => (
                <button
                  key={i}
                  className={`step-dot ${i === step ? 'current' : ''} ${s.kind === 'error' ? 'err' : ''}`}
                  title={`${i + 1}: ${s.kind} ${s.node_id}`}
                  onClick={() => setStep(i)}
                />
              ))}
            </div>
          </div>
          <StepDetails step={current} catalog={catalog} nodes={nodes} />
        </>
      )}
    </div>
  )
}

export default function TestsPanel({ projectId, graph, routes, focusEndpoint, tests, setTests, onDebug }) {
  const { nodes, edges } = graph
  const [endpoint, setEndpoint] = useState(focusEndpoint || routes[0]?.node_id || '')
  const [drafts, setDrafts] = useState({})
  const [result, setResult] = useState(null)
  const [step, setStep] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [run, setRun] = useState(null) // результаты «Прогнать все»
  const [resetData, setResetData] = useState(true)
  const [editingTest, setEditingTest] = useState(null)

  useEffect(() => {
    if (focusEndpoint) setEndpoint(focusEndpoint)
  }, [focusEndpoint])
  useEffect(() => {
    if (!routes.some((r) => r.node_id === endpoint) && routes[0]) setEndpoint(routes[0].node_id)
  }, [routes, endpoint])

  const request = drafts[endpoint] || (endpoint ? draftRequest(endpoint, nodes, edges) : null)
  const setRequest = (patch) => setDrafts((d) => ({ ...d, [endpoint]: { ...request, ...patch } }))
  const route = routes.find((r) => r.node_id === endpoint)

  // трасса → подсветка на канвасе
  const steps = useMemo(() => traceSteps(result), [result])
  useEffect(() => {
    onDebug(result && !result.rejected_before_chain ? { steps, current: Math.min(step, steps.length - 1) } : null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result, step])
  useEffect(() => () => onDebug(null), [onDebug])

  // ← / → по шагам, Ctrl+Enter — отправить
  useEffect(() => {
    const h = (e) => {
      const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName) || document.activeElement?.isContentEditable
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault()
        send()
      } else if (!typing && result && e.key === 'ArrowRight') setStep((s) => Math.min(s + 1, steps.length - 1))
      else if (!typing && result && e.key === 'ArrowLeft') setStep((s) => Math.max(s - 1, 0))
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  })

  const send = async () => {
    if (!endpoint || busy) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.testRequest(projectId, { graph, endpoint, request })
      setResult(r)
      setStep(Math.max(0, traceSteps(r).length - 1))
    } catch (e) {
      setError(e.message)
      setResult(null)
    } finally {
      setBusy(false)
    }
  }

  const saveAsTest = () => {
    const name = prompt('Название теста', `${route?.method} ${route?.path}`)
    if (!name) return
    const expect = { status: result?.status ?? 200, body: result && typeof result.body === 'object' ? JSON.stringify(result.body, null, 2) : '' }
    setTests([...tests, { id: uid(), name, endpoint, request, expect }])
  }

  const runTests = async (subset) => {
    setBusy(true)
    setError(null)
    try {
      setRun(await api.runTests(projectId, { graph, tests: subset, reset_data: resetData && !subset }))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const resultsById = Object.fromEntries((run?.results || []).map((r) => [r.id, r]))
  const updateTest = (id, patch) => setTests(tests.map((t) => (t.id === id ? { ...t, ...patch } : t)))

  if (!routes.length) return <div className="muted pad">В графе нет работающих эндпоинтов — исправьте ошибки во вкладке «Проблемы».</div>

  return (
    <div className="tests-panel">
      <div className="tp-request">
        <div className="tp-row">
          <select value={endpoint} onChange={(e) => setEndpoint(e.target.value)}>
            {routes.map((r) => (
              <option key={r.node_id} value={r.node_id}>
                {r.method} {r.path}
              </option>
            ))}
          </select>
          <button className="primary" onClick={send} disabled={busy} title="Ctrl+Enter">
            {busy ? '…' : '▶ Отправить'}
          </button>
          <button className="small" onClick={() => setDrafts((d) => ({ ...d, [endpoint]: draftRequest(endpoint, nodes, edges) }))} title="Заполнить по блокам эндпоинта">
            ↺
          </button>
        </div>
        {request && Object.keys(request.path).length > 0 && (
          <>
            <div className="field-label">Параметры пути</div>
            <KeyValues value={request.path} onChange={(path) => setRequest({ path })} fixedKeys />
          </>
        )}
        <div className="field-label">Query</div>
        <KeyValues value={request?.query} onChange={(query) => setRequest({ query })} />
        <div className="field-label">Заголовки</div>
        <KeyValues value={request?.headers} onChange={(headers) => setRequest({ headers })} keyPlaceholder="X-API-Key" />
        <div className="field-label tp-body-head">
          Тело
          <select value={request?.body_type} onChange={(e) => setRequest({ body_type: e.target.value })}>
            <option value="none">нет</option>
            <option value="json">JSON</option>
            <option value="form">form-data</option>
          </select>
        </div>
        {request?.body_type === 'json' && (
          <textarea className="mono" rows={6} spellCheck={false} value={request.body} onChange={(e) => setRequest({ body: e.target.value })} />
        )}
        {request?.body_type === 'form' && <KeyValues value={request.form} onChange={(form) => setRequest({ form })} />}
        <div className="hint">
          Запрос идёт к текущему графу — сохранять и запускать не нужно. Секреты пишите как <code>{'{{env.KEY}}'}</code>.
          Данные пишутся в превью-БД. Входящий firewall к тестовым запросам не применяется.
        </div>
        {error && <div className="issue error">{error}</div>}
      </div>

      <div className="tp-result">
        <ResultView result={result} step={step} setStep={setStep} nodes={nodes} />
        {result && (
          <button className="small" onClick={saveAsTest} title="Ожидаемый ответ = текущий; потом его можно сократить">
            💾 Сохранить как тест
          </button>
        )}
      </div>

      <div className="tp-tests">
        <div className="tp-row">
          <b>Тесты ({tests.length})</b>
          <div className="spacer" />
          <label className="muted" title="Очистить превью-БД перед прогоном — результаты не зависят от прошлых запусков">
            <input type="checkbox" checked={resetData} onChange={(e) => setResetData(e.target.checked)} /> очистить данные
          </label>
          <button className="primary small" disabled={busy || !tests.length} onClick={() => runTests()}>
            ▶ Прогнать все
          </button>
        </div>
        {run && (
          <div className={`run-summary ${run.passed === run.total ? 'ok' : 'bad'}`}>
            {run.passed === run.total ? '✓' : '✗'} пройдено {run.passed} из {run.total}
          </div>
        )}
        {tests.length === 0 && <div className="muted">Отправьте запрос и нажмите «Сохранить как тест». Тесты идут по порядку и попадают в экспорт как pytest.</div>}
        {tests.map((t, i) => {
          const r = resultsById[t.id]
          const tr = routes.find((x) => x.node_id === t.endpoint)
          return (
            <div key={t.id} className={`test-item ${r ? (r.passed ? 'ok' : 'bad') : ''}`}>
              <div className="tp-row">
                <span className="mark">{r ? (r.passed ? '✓' : '✗') : '·'}</span>
                <span className="test-name" onClick={() => setEditingTest(editingTest === t.id ? null : t.id)}>
                  {t.name}
                </span>
                <span className="muted">{tr ? `${tr.method} ${tr.path}` : 'эндпоинт удалён'}</span>
                <div className="spacer" />
                <button className="icon" title="Выше" disabled={i === 0} onClick={() => { const n = [...tests]; [n[i - 1], n[i]] = [n[i], n[i - 1]]; setTests(n) }}>↑</button>
                <button className="icon" title="Открыть запрос в редакторе" onClick={() => { setEndpoint(t.endpoint); setDrafts((d) => ({ ...d, [t.endpoint]: t.request })) }}>✎</button>
                <button className="icon" title="Прогнать этот тест" onClick={() => runTests([t])}>▶</button>
                <button className="icon" title="Удалить" onClick={() => confirm(`Удалить тест «${t.name}»?`) && setTests(tests.filter((x) => x.id !== t.id))}>×</button>
              </div>
              {r && !r.passed && (
                <div className="problems">
                  {r.problems.map((p, k) => (
                    <div key={k}>{p}</div>
                  ))}
                  {r.result && (
                    <button className="linklike" onClick={() => { setResult(r.result); setStep(traceSteps(r.result).length - 1) }}>
                      показать трассу
                    </button>
                  )}
                </div>
              )}
              {editingTest === t.id && (
                <div className="test-edit">
                  <label className="field-label">Ожидаемый статус</label>
                  <input type="number" value={t.expect?.status ?? ''} onChange={(e) => updateTest(t.id, { expect: { ...t.expect, status: e.target.value } })} />
                  <label className="field-label">Ожидаемое тело (подмножество JSON: лишние поля в ответе не мешают)</label>
                  <textarea className="mono" rows={5} spellCheck={false} value={t.expect?.body ?? ''} onChange={(e) => updateTest(t.id, { expect: { ...t.expect, body: e.target.value } })} />
                  <label className="field-label">Название</label>
                  <input value={t.name} onChange={(e) => updateTest(t.id, { name: e.target.value })} />
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
