import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useCatalog } from '../catalog.js'

const STATUS = {
  running: ['● идёт', 'busy'],
  ok: ['✓ успешно', 'good'],
  error: ['✖ ошибка', 'bad'],
  stopped: ['■ остановлен', 'bad'],
  skipped: ['пропущен', 'bad'],
}
const SOURCE = { manual: 'вручную', hotkey: 'клавиша', schedule: 'расписание', webhook: 'вебхук' }
const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleTimeString() : '')

export function useAgents(deps = []) {
  const [agents, setAgents] = useState(null)
  useEffect(() => {
    let alive = true
    const tick = () => api.agents().then((a) => alive && setAgents(a)).catch(() => {})
    tick()
    const t = setInterval(tick, 3000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, deps) // eslint-disable-line react-hooks/exhaustive-deps
  return agents
}

export function AgentSelect({ agents, value, onChange, disabled }) {
  return (
    <select value={value || 'local'} onChange={(e) => onChange(e.target.value)} disabled={disabled}>
      {(agents || []).map((a) => (
        <option key={a.id} value={a.id} disabled={!a.can_use}>
          {a.online ? '🟢' : '⚪'} {a.name}
          {a.busy ? ' (занят)' : ''}
          {!a.can_use ? ' — нет прав' : ''}
        </option>
      ))}
      {agents && !agents.some((a) => a.id === (value || 'local')) && <option value={value}>{value} (нет)</option>}
    </select>
  )
}

// ---------- нижняя панель: запуск и история ----------

export function RunsPanel({ projectId, graph, scenarios, agentId, readOnly, onSelectNode, onRunState, runRequest }) {
  const agents = useAgents([projectId])
  const catalog = useCatalog()
  const title = (op) => catalog?.blocks[op === 'if' ? 'condition' : op]?.title || op
  const [scenario, setScenario] = useState('')
  const [inputs, setInputs] = useState({})
  const [agent, setAgent] = useState(agentId)
  const [run, setRun] = useState(null)
  const [history, setHistory] = useState([])
  const [error, setError] = useState(null)
  const poll = useRef(null)
  useEffect(() => setAgent(agentId), [agentId])
  useEffect(() => {
    if (!scenarios.some((s) => s.name === scenario)) setScenario(scenarios[0]?.name || '')
  }, [scenarios, scenario])

  const loadHistory = () => api.autoRuns(projectId).then(setHistory).catch(() => {})
  useEffect(() => {
    loadHistory()
    const t = setInterval(loadHistory, 4000)
    return () => clearInterval(t)
  }, [projectId]) // eslint-disable-line react-hooks/exhaustive-deps

  // живое обновление текущего запуска: события дописываются по мере выполнения
  const follow = (r) => {
    clearTimeout(poll.current)
    setRun(r)
    onRunState(r)
    if (r.status !== 'running') return loadHistory()
    poll.current = setTimeout(async () => {
      try {
        const next = await api.autoRunStatus(projectId, r.id, r.events_from + r.events.length)
        follow({ ...next, events: [...r.events, ...next.events].slice(-500), events_from: r.events_from })
      } catch {
        /* запуск пропал из памяти сервера */
      }
    }, 300)
  }
  useEffect(() => () => clearTimeout(poll.current), [])

  const current = scenarios.find((s) => s.name === scenario)
  const start = async (name = scenario) => {
    setError(null)
    try {
      const sc = scenarios.find((s) => s.name === name)
      const ins = Object.fromEntries((sc?.params || []).map((p) => [p, parseValue(inputs[p] ?? '')]))
      follow(await api.autoRun(projectId, { scenario: name, inputs: ins, agent, graph }))
    } catch (e) {
      setError(e.data?.detail?.issues ? `${e.message}: ${e.data.detail.issues.map((i) => i.message).join('; ')}` : e.message)
    }
  }
  // кнопка «▶ Запустить» в тулбаре и на блоке «Старт»
  useEffect(() => {
    if (runRequest?.name) {
      setScenario(runRequest.name)
      start(runRequest.name)
    }
  }, [runRequest]) // eslint-disable-line react-hooks/exhaustive-deps

  const open = async (id) => {
    try {
      follow(await api.autoRunStatus(projectId, id, 0))
    } catch (e) {
      setError(e.message)
    }
  }

  const agentInfo = (agents || []).find((a) => a.id === (agent || 'local'))
  return (
    <div className="runs">
      <div className="runs-side">
        <div className="list-row">
          <label className="field">
            <span className="field-label">Сценарий</span>
            <select value={scenario} onChange={(e) => setScenario(e.target.value)}>
              {scenarios.map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Где выполнить</span>
            <AgentSelect agents={agents} value={agent} onChange={setAgent} />
          </label>
        </div>
        {(current?.params || []).map((p) => (
          <label key={p} className="field">
            <span className="field-label">
              <code>{p}</code>
            </span>
            <input className="mono" value={inputs[p] ?? ''} placeholder="значение (число, true, «текст» или JSON)" onChange={(e) => setInputs({ ...inputs, [p]: e.target.value })} />
          </label>
        ))}
        <div className="tester-actions">
          {run?.status === 'running' ? (
            <button className="danger" onClick={() => api.autoRunStop(projectId, run.id)}>
              ■ Остановить
            </button>
          ) : (
            <button className="primary" onClick={() => start()} disabled={readOnly || !scenario}>
              ▶ Запустить
            </button>
          )}
          {agentInfo?.info?.stop_hotkey && (
            <span className="muted">
              аварийно: <kbd>{agentInfo.info.stop_hotkey}</kbd> или курсор в левый верхний угол
            </span>
          )}
        </div>
        {agentInfo && !agentInfo.online && <div className="issue warning">Агент «{agentInfo.name}» не в сети{agentInfo.error ? `: ${agentInfo.error}` : ''}.</div>}
        {error && <div className="issue error">{error}</div>}
        <div className="field-label">История</div>
        <div className="runs-history">
          {history.length === 0 && <div className="muted">Запусков пока не было.</div>}
          {history.map((h) => (
            <div key={h.id} className={`log-row ${run?.id === h.id ? 'open' : ''}`} onClick={() => open(h.id)}>
              <span className="muted">{fmtTime(h.started)}</span>
              <code>{h.scenario}</code>
              <span className={`lib-state ${STATUS[h.status]?.[1] || ''}`}>{STATUS[h.status]?.[0] || h.status}</span>
              <span className="muted">{SOURCE[h.source] || h.source}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="runs-main">
        {!run ? (
          <div className="muted pad">
            Нажмите «Запустить» — сценарий выполнится на выбранном агенте, а здесь появится пошаговый лог. Выполняется
            текущая версия графа, даже несохранённая.
          </div>
        ) : (
          <>
            <div className="run-head">
              <span className={`lib-state ${STATUS[run.status]?.[1] || ''}`}>{STATUS[run.status]?.[0] || run.status}</span>
              <code>{run.scenario}</code>
              <span className="muted">
                {fmtTime(run.started)}
                {run.finished ? ` · ${(run.finished - run.started).toFixed(1)} с` : ''} · действий: {run.actions}
              </span>
              {run.message && <span className={run.status === 'ok' ? 'muted' : 'issue error'}>{run.message}</span>}
            </div>
            <div className="run-events">
              {run.events_from > 0 && <div className="muted">… ранние события скрыты ({run.events_from})</div>}
              {run.events.map((e, i) => (
                <div
                  key={i}
                  className={`run-event ${e.status === 'error' ? 'bad' : ''} ${e.type === 'log' ? 'log' : ''}`}
                  onClick={() => e.node && onSelectNode(e.node)}
                >
                  <span className="muted">{fmtTime(e.t)}</span>{' '}
                  {e.type === 'log' ? <>✎ {e.message}</> : e.status === 'error' ? <>✖ {e.message}</> : <>▸ {title(e.op)}</>}{' '}
                  <span className="muted">{e.node}</span>
                </div>
              ))}
            </div>
            {Object.keys(run.vars || {}).length > 0 && (
              <>
                <div className="field-label">Переменные</div>
                <pre className="run-vars">{JSON.stringify(run.vars, null, 1)}</pre>
              </>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function parseValue(text) {
  const t = String(text).trim()
  if (t === '') return ''
  try {
    return JSON.parse(t)
  } catch {
    return text
  }
}

// ---------- запись действий ----------

export function RecordModal({ projectId, agentId, onInsert, onClose }) {
  const agents = useAgents([])
  const [agent, setAgent] = useState(agentId)
  const [countdown, setCountdown] = useState(3)
  const [rec, setRec] = useState(null)
  const [error, setError] = useState(null)
  const timer = useRef(null)
  useEffect(() => () => clearTimeout(timer.current), [])
  useEffect(() => {
    const h = (e) => e.key === 'Escape' && !rec && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose, rec])

  const follow = (id) => {
    timer.current = setTimeout(async () => {
      try {
        const r = await api.recordStatus(projectId, id)
        setRec(r)
        if (!['done', 'error'].includes(r.state)) follow(id)
      } catch (e) {
        setError(e.message)
      }
    }, 300)
  }
  const start = async () => {
    setError(null)
    try {
      const r = await api.recordStart(projectId, { agent, countdown: Number(countdown) || 0 })
      setRec(r)
      follow(r.id)
    } catch (e) {
      setError(e.message)
    }
  }
  const agentInfo = (agents || []).find((a) => a.id === (agent || 'local'))
  const stopKey = agentInfo?.info?.stop_hotkey || 'ctrl+alt+esc'
  const active = rec && ['starting', 'countdown', 'recording'].includes(rec.state)

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && !active && onClose()}>
      <div className="modal" role="dialog" aria-label="Запись действий">
        <div className="modal-head">
          <div className="inspector-title">● Запись действий</div>
          <button className="icon" onClick={onClose} disabled={active} title="Закрыть">
            ×
          </button>
        </div>
        <p className="hint">
          Агент запоминает щелчки, перетаскивания, прокрутку и набор текста — и превращает их в блоки. Движения мыши без
          щелчков не записываются; паузы длиннее 0.4 с становятся блоками «Пауза». Закончить — <kbd>{stopKey}</kbd> (если
          записываете в другом окне) или кнопкой здесь.
        </p>
        {!rec || rec.state === 'error' ? (
          <>
            <div className="list-row">
              <label className="field">
                <span className="field-label">Где записывать</span>
                <AgentSelect agents={agents} value={agent} onChange={setAgent} />
              </label>
              <label className="field">
                <span className="field-label">Отсчёт перед началом, с</span>
                <input type="number" min="0" max="10" value={countdown} onChange={(e) => setCountdown(e.target.value)} />
              </label>
            </div>
            {rec?.state === 'error' && <div className="issue error">{rec.message}</div>}
            {error && <div className="issue error">{error}</div>}
            <div className="tester-actions">
              <button className="primary" onClick={start}>
                ● Начать запись
              </button>
            </div>
          </>
        ) : rec.state === 'done' ? (
          <>
            <div className="issue">
              Записано событий: {rec.events}, получилось блоков: <b>{rec.blocks.length}</b>
            </div>
            <div className="record-preview">
              {rec.blocks.map((b, i) => (
                <div key={i} className="row-line">
                  {i + 1}. <b>{b.type}</b> <span className="muted">{JSON.stringify(b.data)}</span>
                </div>
              ))}
            </div>
            <div className="tester-actions">
              <button className="primary" disabled={!rec.blocks.length} onClick={() => (onInsert(rec.blocks), onClose())}>
                Вставить на канвас
              </button>
              <button onClick={() => setRec(null)}>Записать заново</button>
            </div>
          </>
        ) : (
          <>
            <div className="record-live">
              {rec.state === 'countdown' ? (
                <span className="lib-state busy">Запись начнётся через {rec.left}…</span>
              ) : rec.state === 'recording' ? (
                <span className="lib-state bad">● Идёт запись</span>
              ) : (
                <span className="lib-state busy">Подготовка…</span>
              )}
            </div>
            <div className="tester-actions">
              <button className="danger" onClick={() => api.recordStop(projectId, rec.id)}>
                ■ Закончить запись
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// ---------- агенты ----------

export function AgentsModal({ me, onClose }) {
  const [tick, setTick] = useState(0)
  const agents = useAgents([tick])
  const [created, setCreated] = useState(null)
  const [error, setError] = useState(null)
  const isAdmin = !me || me.role === 'admin'
  useEffect(() => {
    const h = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  const create = async () => {
    const name = prompt('Имя компьютера (например, «Офисный ПК»)', '')
    if (name === null) return
    try {
      setCreated(await api.createAgent(name))
      setTick((t) => t + 1)
    } catch (e) {
      setError(e.message)
    }
  }
  const server = window.location.origin
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal settings-modal" role="dialog" aria-label="Агенты">
        <div className="modal-head">
          <div className="inspector-title">🖥 Агенты автоматизации</div>
          <button className="icon" onClick={onClose} title="Закрыть (Esc)">
            ×
          </button>
        </div>
        <p className="hint">
          Агент выполняет сценарии на своём компьютере — двигает мышь и нажимает клавиши. «Эта машина» — компьютер, где
          запущена платформа. Удалённый агент — маленькая программа на другом ПК: она сама подключается к платформе по
          токену, открывать порты на нём не нужно.
        </p>
        <div className="agents-list">
          {(agents || []).map((a) => (
            <div key={a.id} className="agent-row">
              <span className={`lib-state ${a.online ? 'good' : ''}`}>{a.online ? (a.busy ? 'занят' : 'в сети') : 'не в сети'}</span>
              <b>{a.name}</b>
              <span className="muted">
                {a.info ? `${a.info.platform || ''}${a.info.screen ? ` · ${a.info.screen.join('×')}` : ''} · стоп: ${a.info.stop_hotkey}` : a.local ? '' : 'ждёт подключения'}
              </span>
              {a.error && <span className="issue error">{a.error}</span>}
              {!a.local && isAdmin && (
                <button
                  className="small danger"
                  onClick={async () => {
                    if (!confirm(`Удалить агента «${a.name}»? Его токен перестанет работать.`)) return
                    await api.deleteAgent(a.id)
                    setTick((t) => t + 1)
                  }}
                >
                  Удалить
                </button>
              )}
            </div>
          ))}
          {agents && !agents.length && <div className="muted">Агентов нет: локальный агент выключен, удалённых пока не добавляли.</div>}
        </div>
        {error && <div className="issue error">{error}</div>}
        {isAdmin && (
          <div className="tester-actions">
            <button className="primary" onClick={create}>
              + Добавить удалённого агента
            </button>
            <a className="button" href={api.agentBundleUrl}>
              ⬇ Скачать агента (zip)
            </a>
          </div>
        )}
        {created && (
          <div className="agent-created">
            <div className="issue warning">
              Токен агента «{created.agent.name}» показывается <b>один раз</b> — скопируйте его сейчас.
            </div>
            <pre className="install-log">{`# на том компьютере (Python 3.10+; Linux — сеанс X11):
unzip dragapi-agent.zip -d dragapi-agent && cd dragapi-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DRAGAPI_AGENT_TOKEN=${created.token} \\
  .venv/bin/python -m dragapi_agent --server ${server} --name "${created.agent.name}"`}</pre>
            <button className="small" onClick={() => navigator.clipboard?.writeText(created.token)}>
              Копировать токен
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

// ---------- вкладка «Сценарий» в настройках проекта ----------

export function AutomationTab({ projectId, automation, setAutomation }) {
  const agents = useAgents([])
  const [token, setToken] = useState(null)
  const a = { agent: 'local', enabled: false, max_seconds: 600, max_actions: 100000, failsafe_corner: true, ...automation }
  const set = (patch) => setAutomation({ ...a, ...patch })
  const agentInfo = (agents || []).find((x) => x.id === a.agent)
  return (
    <section>
      <label className="toggle">
        <input type="checkbox" checked={!!a.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
        <div>
          <b>Сценарии включены</b>
          <div className="muted">
            Работают горячие клавиши, расписание и вебхуки. Ручной запуск из редактора работает всегда. Включение
            действует после сохранения.
          </div>
        </div>
      </label>
      <label className="field">
        <span className="field-label">Агент (где выполнять)</span>
        <AgentSelect agents={agents} value={a.agent} onChange={(agent) => set({ agent })} />
      </label>
      {agentInfo && !agentInfo.online && <div className="issue warning">Агент сейчас не в сети — триггеры сработают, когда он подключится.</div>}
      <div className="list-row">
        <label className="field">
          <span className="field-label">Максимум времени на запуск, с</span>
          <input type="number" min="1" max="86400" value={a.max_seconds} onChange={(e) => set({ max_seconds: Number(e.target.value) })} />
        </label>
        <label className="field">
          <span className="field-label">Максимум действий ввода</span>
          <input type="number" min="1" value={a.max_actions} onChange={(e) => set({ max_actions: Number(e.target.value) })} />
        </label>
      </div>
      <label className="toggle">
        <input type="checkbox" checked={a.failsafe_corner} onChange={(e) => set({ failsafe_corner: e.target.checked })} />
        <div>
          <b>Аварийный угол</b>
          <div className="muted">Если увести курсор в левый верхний угол экрана, сценарий остановится. Не выключайте без нужды.</div>
        </div>
      </label>
      <div className="field">
        <div className="field-label">Вебхуки (запуск по HTTP, в т.ч. из API-проекта)</div>
        <p className="hint">
          <code>POST {window.location.origin}/hooks/{projectId}/&lt;сценарий&gt;</code> с заголовком{' '}
          <code>Authorization: Bearer &lt;токен&gt;</code> и JSON-телом с параметрами. Ответ — результат сценария
          (переменные). Работает для «Старта» с запуском «Вызов по HTTP», когда сценарии включены.
        </p>
        <div className="tester-actions">
          <button
            className="small"
            onClick={async () => {
              if (a.has_hook_token && !confirm('Выпустить новый токен? Старый перестанет работать.')) return
              setToken((await api.hookToken(projectId)).token)
              setAutomation({ ...a, has_hook_token: true })
            }}
          >
            {a.has_hook_token ? 'Выпустить новый токен' : 'Создать токен вебхуков'}
          </button>
          {a.has_hook_token && !token && <span className="muted">токен задан</span>}
        </div>
        {token && (
          <div className="issue warning">
            Токен показывается один раз: <code className="mono">{token}</code>{' '}
            <button className="small" onClick={() => navigator.clipboard?.writeText(token)}>
              Копировать
            </button>
          </div>
        )}
      </div>
    </section>
  )
}

// ---------- API-проект: блок «Запустить сценарий» ----------

export function ScenarioBlockForm({ d, set, env, Field, ListEditor, exprHint }) {
  const [projects, setProjects] = useState([])
  const [triggers, setTriggers] = useState([])
  useEffect(() => {
    api.projects().then((ps) => setProjects(ps.filter((p) => p.kind === 'automation'))).catch(() => {})
  }, [])
  useEffect(() => {
    if (!d.project) return setTriggers([])
    api
      .project(d.project)
      .then((p) => setTriggers((p.nodes || []).filter((n) => n.type === 'trigger').map((n) => n.data)))
      .catch(() => setTriggers([]))
  }, [d.project])
  const envKeys = env.map((e) => e.key).filter(Boolean)
  const trig = triggers.find((t) => t.name === d.scenario)
  return (
    <>
      <Field label="Проект-автоматизация">
        <select value={d.project || ''} onChange={(e) => set({ project: e.target.value })}>
          <option value="">— выберите —</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              🤖 {p.name}
            </option>
          ))}
          {d.project && !projects.some((p) => p.id === d.project) && <option value={d.project}>{d.project}</option>}
        </select>
      </Field>
      <Field
        label="Сценарий"
        hint={trig && trig.kind !== 'webhook' ? '⚠ У этого «Старта» запуск не «по HTTP» — вебхук его не запустит.' : 'Имя блока «Старт» с запуском «Вызов по HTTP».'}
      >
        <input className="mono" list="scenario-names" value={d.scenario || ''} onChange={(e) => set({ scenario: e.target.value })} />
        <datalist id="scenario-names">
          {triggers.map((t) => (
            <option key={t.name} value={t.name}>
              {t.kind === 'webhook' ? 'по HTTP' : t.kind}
            </option>
          ))}
        </datalist>
      </Field>
      <Field label="Параметры сценария" hint={(trig?.params?.length ? `Ожидает: ${trig.params.join(', ')}. ` : '') + exprHint}>
        <ListEditor
          items={d.params || []}
          onChange={(params) => set({ params })}
          empty="Без параметров"
          addLabel="параметр"
          newItem={() => ({ key: '', expr: '' })}
          columns={[
            { key: 'key', label: 'имя', mono: true, flex: 1 },
            { key: 'expr', label: 'выражение', mono: true, flex: 2.5 },
          ]}
        />
      </Field>
      <Field label="Результат → в поле" hint="{status, message, vars, run_id}: переменные сценария — в vars, например res['vars']['n'].">
        <input className="mono" value={d.target || ''} onChange={(e) => set({ target: e.target.value })} />
      </Field>
      <div className="list-row">
        <Field label="Токен вебхуков — переменная" hint="Токен выпускается в проекте-сценарии: «Настройки → Сценарий». Сохраните его здесь секретом.">
          <input className="mono" list="env-keys" value={d.token_env || ''} onChange={(e) => set({ token_env: e.target.value })} />
        </Field>
        <Field label="Адрес платформы — переменная" hint="Необязательно. Пусто или нет переменной — этот же сервер.">
          <input className="mono" list="env-keys" value={d.url_env || ''} onChange={(e) => set({ url_env: e.target.value })} />
        </Field>
      </div>
      <datalist id="env-keys">
        {envKeys.map((k) => (
          <option key={k} value={k} />
        ))}
      </datalist>
      <div className="hint">Сценарии проекта должны быть включены. Если сценарий не завершится успешно, эндпоинт ответит 502.</div>
    </>
  )
}
