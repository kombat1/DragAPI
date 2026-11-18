import { Suspense, lazy, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api.js'
import { useCatalog } from '../catalog.js'
import AutoForm from './AutoForm.jsx'
import { ScenarioBlockForm } from './Automation.jsx'

// CodeMirror тяжёлый — грузим его, только когда открыт блок кода
const CodeEditor = lazy(() => import('./CodeEditor.jsx'))
import ListEditor from './ListEditor.jsx'

const EDITOR_MODE_KEY = 'dragapi.editorMode'
function loadEditorMode() {
  try {
    const m = localStorage.getItem(EDITOR_MODE_KEY)
    return m === 'half' || m === 'full' ? m : null
  } catch {
    return null
  }
}
function saveEditorMode(mode) {
  try {
    if (mode) localStorage.setItem(EDITOR_MODE_KEY, mode)
    else localStorage.removeItem(EDITOR_MODE_KEY)
  } catch {
    /* хранилище недоступно — режим просто не запомнится */
  }
}

function EditorModeButtons({ mode, setMode }) {
  return (
    <div className="editor-mode-buttons">
      <button
        className={`small ${mode === 'half' ? 'active' : ''}`}
        onClick={() => setMode(mode === 'half' ? null : 'half')}
        title="Редактор на правую половину экрана — канвас остаётся виден"
      >
        ◧ ½ экрана
      </button>
      <button
        className={`small ${mode === 'full' ? 'active' : ''}`}
        onClick={() => setMode(mode === 'full' ? null : 'full')}
        title="Редактор на весь экран"
      >
        ⛶ Весь экран
      </button>
      {mode && (
        <button className="small" onClick={() => setMode(null)} title="Вернуть редактор в панель (Esc)">
          ✕ В панель
        </button>
      )}
    </div>
  )
}

function OutputsField({ d, set }) {
  return (
    <Field
      label="Выходные поля"
      hint="Ключи dict, который возвращает run(). Через запятую. Дальше по цепочке они доступны как обычные поля."
    >
      <input
        className="mono"
        value={(d.outputs || []).join(', ')}
        onChange={(e) =>
          set({
            outputs: e.target.value
              .split(',')
              .map((x) => x.trim())
              .filter((x, i, arr) => x || i === arr.length - 1),
          })
        }
      />
    </Field>
  )
}

// Редактор кода поверх интерфейса: на правую половину экрана или на весь экран.
function CodeOverlay({ mode, setMode, node, d, set, issues, errorLine, setErrorLine, availableFields, env, sandbox, firewall, projectId, sandboxInfo, onOpenSettings }) {
  useEffect(() => {
    const h = (e) => {
      // Esc, уже обработанный CodeMirror (закрыть подсказки/поиск), окно не закрывает
      if (e.key === 'Escape' && !e.defaultPrevented) setMode(null)
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [setMode])

  return createPortal(
    <div className={`code-overlay ${mode}`} role="dialog" aria-label="Редактор Python-кода">
      <div className="code-overlay-head">
        <div>
          <b>Python-код</b> <span className="muted">· {node.id}</span>
          {sandboxInfo && (
            <button className="linklike" onClick={() => onOpenSettings('sandbox')} title={sandboxInfo.details}>
              {sandboxInfo.isolated ? '🔒' : '⚠'} {sandboxInfo.backend}
              {sandboxInfo.warning ? ` — ${sandboxInfo.warning}` : ''}
            </button>
          )}
          <button className="linklike" onClick={() => onOpenSettings('libraries')}>
            📦 библиотеки
          </button>
        </div>
        <EditorModeButtons mode={mode} setMode={setMode} />
      </div>
      {issues.length > 0 && (
        <div className="code-overlay-issues">
          {issues.map((i, k) => (
            <div key={k} className={`issue ${i.level}`}>
              {i.message}
            </div>
          ))}
        </div>
      )}
      <div className="code-overlay-body">
        <div className="code-overlay-editor">
          <Suspense fallback={<div className="muted pad">Загрузка редактора…</div>}>
            <CodeEditor key={node.id} fill value={d.code || ''} onChange={(code) => set({ code })} errorLine={errorLine} />
          </Suspense>
        </div>
        <div className="code-overlay-side">
          <OutputsField d={d} set={set} />
          <div className="list-row">
            <Field label="Лимит времени, с">
              <input type="number" step="0.5" min="0.1" max="10" value={d.timeout ?? 2} onChange={(e) => set({ timeout: e.target.value })} />
            </Field>
            <Field label="Лимит памяти, МБ">
              <input type="number" step="32" min="32" max="1024" value={d.memory_mb ?? 256} onChange={(e) => set({ memory_mb: e.target.value })} />
            </Field>
          </div>
          <CodeTester d={d} set={set} availableFields={availableFields} env={env} sandbox={sandbox} firewall={firewall} projectId={projectId} onErrorLine={setErrorLine} />
        </div>
      </div>
    </div>,
    document.body,
  )
}

export function NetworkEvents({ events }) {
  return (
    <div className="net-events">
      <div className="muted">Сеть (firewall):</div>
      {events.map((e, i) => (
        <div key={i} className={`fw-event ${e.allowed ? 'ok' : 'bad'}`}>
          <b>{e.allowed ? '✓' : '✗'}</b>
          <code>
            {e.method === 'CONNECT' ? '' : e.method + ' '}
            {e.host}:{e.port}
          </code>
          <span className="fw-reason">{e.reason}</span>
        </div>
      ))}
    </div>
  )
}

function CodeTester({ d, set, availableFields, env, sandbox, firewall, projectId, onErrorLine }) {
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const template = () =>
    JSON.stringify(Object.fromEntries(availableFields.map((f) => [f, null])), null, 2)
  const input = d.test_input ?? template()
  let parsed = null
  let parseError = null
  try {
    parsed = JSON.parse(input || '{}')
    if (typeof parsed !== 'object' || Array.isArray(parsed)) parseError = 'нужен JSON-объект'
  } catch (e) {
    parseError = e.message
  }

  const runTest = async () => {
    setBusy(true)
    try {
      const r = await api.sandboxRun(projectId, {
        sandbox,
        firewall,
        code: d.code,
        data: parsed,
        env: Object.fromEntries(env.filter((e) => e.key).map((e) => [e.key, String(e.value ?? '')])),
        timeout: Number(d.timeout) || 2,
        memory_mb: Number(d.memory_mb) || 256,
      })
      if (r.ok && (typeof r.result !== 'object' || r.result === null || Array.isArray(r.result))) {
        r.ok = false
        r.error = 'run() должна вернуть dict'
      } else if (r.ok) {
        const extra = Object.keys(r.result).filter((k) => !(d.outputs || []).includes(k))
        if (extra.length) {
          r.ok = false
          r.error = `run() вернула необъявленные поля: ${extra.join(', ')} — добавьте их в «Выходные поля»`
        }
      }
      setResult(r)
      onErrorLine(Number(/строка (\d+)/.exec(r.error || '')?.[1]) || null)
    } catch (e) {
      setResult({ ok: false, error: e.message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Field
      label="Проверить на тестовых данных"
      hint="data для run(). Значения env берутся из переменных проекта. Выполняется в той же песочнице, что и запросы."
    >
      <textarea
        className="mono test-input"
        rows={Math.min(10, Math.max(3, input.split('\n').length))}
        value={input}
        onChange={(e) => set({ test_input: e.target.value })}
        spellCheck={false}
      />
      <div className="tester-actions">
        <button className="primary small" onClick={runTest} disabled={busy || !!parseError}>
          {busy ? 'Выполняется…' : '▶ Проверить'}
        </button>
        <button className="small" onClick={() => set({ test_input: template() })} title="Поля, доступные на входе блока">
          Сбросить по полям
        </button>
        {parseError && <span className="issue error">JSON: {parseError}</span>}
      </div>
      {result && (
        <div className={`test-result ${result.ok ? 'ok' : 'bad'}`}>
          <div>
            <b>{result.ok ? 'Результат' : 'Ошибка'}</b>
            {result.ms ? <span className="muted"> · {result.ms} мс · {result.backend}</span> : null}
          </div>
          <pre>{result.ok ? JSON.stringify(result.result, null, 2) : result.error}</pre>
          {result.traceback && <pre className="muted">{result.traceback}</pre>}
          {result.network?.length > 0 && <NetworkEvents events={result.network} />}
          {result.stdout && (
            <>
              <div className="muted">print():</div>
              <pre>{result.stdout}</pre>
            </>
          )}
        </div>
      )}
    </Field>
  )
}

function Field({ label, children, hint }) {
  return (
    <div className="field">
      <div className="field-label">{label}</div>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  )
}

const EXPR_HINT =
  'Выражение на подмножестве Python: поля по имени, + - * / % **, сравнения, and/or, x if c else y, ' +
  "f-строки, field['key'], env['KEY']. Функции: len, str, int, float, round, min, max, sum, upper, lower, strip, contains, default, regex_match."

const DB_OPS = {
  insert: 'создать запись',
  get: 'получить по id (None, если нет)',
  list: 'список записей',
  update: 'изменить по id (None, если нет)',
  delete: 'удалить по id (True/False)',
}

const FILTER_OPS = {
  eq: '=',
  ne: '≠',
  gt: '>',
  gte: '≥',
  lt: '<',
  lte: '≤',
  contains: 'содержит',
  startswith: 'начинается с',
  in: 'в списке',
  is_null: 'пусто (True/False)',
}

const AUTH_SCHEMES = {
  api_key_header: 'API-ключ в заголовке',
  api_key_query: 'API-ключ в query-параметре',
  bearer: 'Bearer-токен (Authorization)',
}

export default function Inspector({ node, onChange, onDelete, issues, availableFields, models, env, sandbox, firewall, projectId, agent, botRoles = [], onOpenSettings, onTestEndpoint, sandboxInfo, readOnly = false, multiCount = 0 }) {
  const [errorLine, setErrorLine] = useState(null)
  const [editorMode, setEditorModeState] = useState(loadEditorMode) // null | 'half' | 'full'
  const setEditorMode = (mode) => {
    setEditorModeState(mode)
    saveEditorMode(mode)
  }
  const catalog = useCatalog()
  if (!node && multiCount) {
    return (
      <div className="inspector empty">
        <p>
          Выделено блоков: <b>{multiCount}</b>
        </p>
        <p className="muted">
          <kbd>Ctrl+C</kbd> / <kbd>Ctrl+V</kbd> — копировать и вставить (можно в другой проект), <kbd>Ctrl+D</kbd> — дублировать,{' '}
          <kbd>Delete</kbd> — удалить. Связи между выделенными блоками копируются вместе с ними.
        </p>
      </div>
    )
  }
  if (!node && catalog.bot) {
    return (
      <div className="inspector empty">
        <p>Выберите блок, чтобы настроить его.</p>
        <p className="muted">
          Каждое <b>событие</b> («Команда» или «Сообщение») начинает свою ветку — что бот делает в ответ, например
          <br />
          <b>/start → Отправить сообщение → Меню с кнопками</b>
        </p>
        <p className="muted">
          У «Меню» столько выходов, сколько кнопок, — подключите к каждому свою ветку. «Спросить» ждёт ответа текстом и
          кладёт его в переменную; «Запомнить» сохраняет данные пользователя навсегда (<code>saved['ключ']</code>).
        </p>
        <p className="muted">
          Проверяйте бота во вкладке <b>«Тест-чат»</b> внизу — без Telegram и токена. Для запуска в Telegram задайте токен в
          «Настройки проекта → Бот».
        </p>
      </div>
    )
  }
  if (!node && catalog.automation) {
    return (
      <div className="inspector empty">
        <p>Выберите блок, чтобы настроить его.</p>
        <p className="muted">
          Сценарий начинается с блока <b>«Старт»</b>: перетащите действия из палитры и соедините их цепочкой, например
          <br />
          <b>Старт → Клик → Ввести текст → Нажать клавиши</b>
        </p>
        <p className="muted">
          «Повторить» и «Пока» выполняют ветку «тело» несколько раз, затем идут «дальше». Координаты удобно брать кнопкой
          «📍 С экрана», а целые цепочки — записывать кнопкой «● Запись».
        </p>
        <p className="muted">
          Безопасность: аварийная остановка — сочетание клавиш агента (по умолчанию <kbd>Ctrl+Alt+Esc</kbd>) или курсор в
          левый верхний угол экрана.
        </p>
      </div>
    )
  }
  if (!node) {
    return (
      <div className="inspector empty">
        <p>Выберите блок, чтобы настроить его.</p>
        <p className="muted">
          Перетащите блоки из палитры слева и соедините их цепочкой:
          <br />
          <b>Эндпоинт → Входные данные → Валидация → Логика → Ответ</b>
        </p>
        <p className="muted">
          Для работы с базой опишите модели в «Моделях и переменных», затем используйте блок «База данных». «Условие»
          разветвляет цепочку — например, «не найдено → 404».
        </p>
        <p className="muted">Несовместимые блоки соединить не получится — канвас подскажет.</p>
        <p className="muted">
          <kbd>Shift</kbd> + рамка или <kbd>Ctrl</kbd> + клик — выделить несколько блоков. <kbd>Ctrl+Z</kbd> / <kbd>Ctrl+Shift+Z</kbd> —
          отменить и повторить. Ветки условия можно сводить в один общий блок (например, «Ответ 404»).
        </p>
      </div>
    )
  }
  const spec = catalog.blocks[node.type]
  const d = node.data
  const set = (patch) => onChange({ ...d, ...patch })
  const fieldNames = availableFields.length ? availableFields : ['']
  const envKeys = env.map((e) => e.key).filter(Boolean)
  const model = models.find((m) => m.name === d.model)
  const modelFields = model ? model.fields.map((f) => f.name).filter(Boolean) : []
  const refFields = model ? model.fields.filter((f) => f.type === 'ref' && f.name) : []

  if (spec.fields && node.type !== 'code') {
    return (
      <fieldset disabled={readOnly} className="inspector">
        <div className="inspector-head" style={{ '--accent': spec.color }}>
          <div>
            <div className="inspector-title">
              {spec.icon} {spec.title}
            </div>
            <div className="muted">{spec.description}</div>
          </div>
          <button className="danger small" onClick={onDelete}>
            Удалить
          </button>
        </div>
        {issues.length > 0 && (
          <div className="issues-box">
            {issues.map((i, k) => (
              <div key={k} className={`issue ${i.level}`}>
                {i.message}
              </div>
            ))}
          </div>
        )}
        <AutoForm spec={spec} d={d} set={set} projectId={projectId} agent={agent} variables={availableFields} roles={botRoles} />
        {availableFields.length > 0 && node.type !== 'trigger' && (
          <div className="available">
            <div className="field-label">Переменные на входе блока</div>
            {availableFields.map((f) => (
              <code key={f}>{f}</code>
            ))}
          </div>
        )}
      </fieldset>
    )
  }

  return (
    <fieldset disabled={readOnly} className={`inspector ${node.type === 'code' ? 'wide' : ''}`}>
      <div className="inspector-head" style={{ '--accent': spec.color }}>
        <div>
          <div className="inspector-title">{spec.title}</div>
          <div className="muted">{spec.description}</div>
        </div>
        <button className="danger small" onClick={onDelete}>
          Удалить
        </button>
      </div>

      {issues.length > 0 && (
        <div className="issues-box">
          {issues.map((i, k) => (
            <div key={k} className={`issue ${i.level}`}>
              {i.message}
            </div>
          ))}
        </div>
      )}

      {node.type === 'endpoint' && (
        <>
          <Field label="Метод">
            <select value={d.method} onChange={(e) => set({ method: e.target.value })}>
              {catalog.http_methods.map((m) => (
                <option key={m}>{m}</option>
              ))}
            </select>
          </Field>
          <Field label="Путь" hint="Параметры пути в фигурных скобках: /users/{user_id}">
            <input className="mono" value={d.path} onChange={(e) => set({ path: e.target.value })} />
          </Field>
          <Field label="Описание (для Swagger)">
            <input value={d.summary || ''} onChange={(e) => set({ summary: e.target.value })} />
          </Field>
          <button className="primary" onClick={() => onTestEndpoint(node.id)} title="Открыть во вкладке «Тесты» с подготовленным запросом">
            ▶ Тестовый запрос
          </button>
        </>
      )}

      {node.type === 'input' && (
        <>
          <Field
            label="Источник"
            hint={
              {
                json: 'JSON-тело запроса (POST/PUT/PATCH)',
                query: 'Параметры строки запроса ?a=1&b=2',
                path: 'Параметры из пути эндпоинта: задаёт им тип',
                form: 'form-data / x-www-form-urlencoded, включая файлы',
              }[d.source]
            }
          >
            <select value={d.source} onChange={(e) => set({ source: e.target.value })}>
              {catalog.input_sources.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          </Field>
          <Field label="Поля">
            <ListEditor
              items={d.fields || []}
              onChange={(fields) => set({ fields })}
              empty="Нет полей"
              addLabel="поле"
              newItem={() => ({ name: '', type: 'str', required: true, default: '' })}
              columns={[
                { key: 'name', label: 'имя', mono: true, flex: 2 },
                {
                  key: 'type',
                  label: 'тип',
                  options: catalog.field_types.filter((t) => t !== 'file' || d.source === 'form'),
                },
                { key: 'required', label: 'обяз.', checkbox: true },
                {
                  key: 'default',
                  label: 'по умолч.',
                  disabled: (it) => it.required || it.type === 'file',
                  flex: 1.5,
                },
              ]}
            />
          </Field>
        </>
      )}

      {node.type === 'validate' && (
        <Field label="Правила" hint="in — список через запятую; regex — полное совпадение.">
          <ListEditor
            items={d.rules || []}
            onChange={(rules) => set({ rules })}
            empty="Нет правил — блок ничего не проверяет"
            addLabel="правило"
            newItem={() => ({ field: availableFields[0] || '', op: 'min', value: '', message: '' })}
            columns={[
              { key: 'field', label: 'поле', options: fieldNames, flex: 1.5 },
              { key: 'op', label: 'правило', options: catalog.validation_ops, flex: 1.3 },
              { key: 'value', label: 'значение', mono: true, flex: 1.3 },
              { key: 'message', label: 'сообщение об ошибке', flex: 2.5 },
            ]}
          />
        </Field>
      )}

      {node.type === 'logic' && (
        <Field label="Вычисления" hint={EXPR_HINT}>
          <ListEditor
            items={d.assignments || []}
            onChange={(assignments) => set({ assignments })}
            empty="Нет вычислений"
            addLabel="вычисление"
            newItem={() => ({ target: '', expr: '' })}
            columns={[
              { key: 'target', label: 'новое поле', mono: true, flex: 1 },
              { key: 'expr', label: 'выражение', mono: true, flex: 2.5 },
            ]}
          />
        </Field>
      )}

      {node.type === 'auth' && (
        <>
          <Field label="Схема">
            <select value={d.scheme} onChange={(e) => set({ scheme: e.target.value })}>
              {catalog.auth_schemes.map((s) => (
                <option key={s} value={s}>
                  {AUTH_SCHEMES[s] || s}
                </option>
              ))}
            </select>
          </Field>
          {d.scheme !== 'bearer' && (
            <Field label={d.scheme === 'api_key_query' ? 'Имя query-параметра' : 'Имя заголовка'}>
              <input className="mono" value={d.name || ''} onChange={(e) => set({ name: e.target.value })} />
            </Field>
          )}
          <Field
            label="Секрет из переменной окружения"
            hint="Запрос пройдёт дальше, только если предъявленный ключ совпадает со значением этой переменной. Иначе — 401."
          >
            {envKeys.length ? (
              <select value={d.secret_env} onChange={(e) => set({ secret_env: e.target.value })}>
                {!envKeys.includes(d.secret_env) && <option value={d.secret_env}>{d.secret_env || '—'} (не объявлена)</option>}
                {envKeys.map((k) => (
                  <option key={k}>{k}</option>
                ))}
              </select>
            ) : (
              <button className="small" onClick={() => onOpenSettings('env')}>
                Добавить переменную окружения
              </button>
            )}
          </Field>
        </>
      )}

      {node.type === 'db' && (
        <>
          <Field label="Модель">
            {models.length ? (
              <select value={d.model} onChange={(e) => set({ model: e.target.value })}>
                {!model && <option value={d.model}>{d.model || '— выберите —'}</option>}
                {models.map((m) => (
                  <option key={m.name}>{m.name}</option>
                ))}
              </select>
            ) : (
              <button className="small" onClick={() => onOpenSettings('models')}>
                Создать модель данных
              </button>
            )}
          </Field>
          <Field label="Операция">
            <select value={d.op} onChange={(e) => set({ op: e.target.value })}>
              {catalog.db_ops.map((o) => (
                <option key={o} value={o}>
                  {o} — {DB_OPS[o]}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Сохранить результат в поле" hint="Дальше по цепочке результат доступен под этим именем.">
            <input className="mono" value={d.target || ''} onChange={(e) => set({ target: e.target.value })} />
          </Field>
          {['get', 'update', 'delete'].includes(d.op) && (
            <Field label="id записи (выражение)" hint="Например, order_id из пути.">
              <input className="mono" value={d.id_expr || ''} onChange={(e) => set({ id_expr: e.target.value })} />
            </Field>
          )}
          {['insert', 'update'].includes(d.op) && (
            <Field label="Значения полей" hint={d.op === 'update' ? 'Меняются только перечисленные поля.' : EXPR_HINT}>
              <ListEditor
                items={d.values || []}
                onChange={(values) => set({ values })}
                empty="Нет значений"
                addLabel="значение"
                newItem={() => ({ field: modelFields.find((f) => availableFields.includes(f)) || modelFields[0] || '', expr: '' })}
                columns={[
                  { key: 'field', label: 'поле', options: modelFields.length ? modelFields : [''], flex: 1.3 },
                  { key: 'expr', label: 'выражение', mono: true, flex: 2.2 },
                ]}
              />
              {d.op === 'insert' && modelFields.length > 0 && (
                <button
                  className="small"
                  title="Заполнить поля модели одноимёнными полями запроса"
                  onClick={() =>
                    set({
                      values: modelFields.map((f) => ({
                        field: f,
                        expr: (d.values || []).find((v) => v.field === f)?.expr || (availableFields.includes(f) ? f : ''),
                      })),
                    })
                  }
                >
                  Сопоставить по именам
                </button>
              )}
            </Field>
          )}
          {(d.op === 'get' || d.op === 'list') && refFields.length > 0 && (
            <Field label="Подтянуть связанные записи" hint="customer_id → в результате появится customer с данными клиента">
              <div className="checks">
                {refFields.map((f) => (
                  <label key={f.name} className="check">
                    <input
                      type="checkbox"
                      checked={(d.include || []).includes(f.name)}
                      onChange={(e) =>
                        set({
                          include: e.target.checked
                            ? [...(d.include || []), f.name]
                            : (d.include || []).filter((x) => x !== f.name),
                        })
                      }
                    />
                    <code>{f.name}</code> → {f.ref}
                  </label>
                ))}
              </div>
            </Field>
          )}
          {d.op === 'list' && (
            <>
              <Field
                label="Фильтры"
                hint="Если значение пустое (необязательный параметр не передан) — фильтр пропускается. «в списке» ждёт список: [1, 2] или поле-список."
              >
                <ListEditor
                  items={d.filters || []}
                  onChange={(filters) => set({ filters })}
                  empty="Без фильтров — все записи"
                  addLabel="фильтр"
                  newItem={() => ({ field: modelFields[0] || '', op: 'eq', expr: '' })}
                  columns={[
                    { key: 'field', label: 'поле', options: ['id', ...modelFields], flex: 1.2 },
                    { key: 'op', label: 'условие', options: catalog.filter_ops, labels: FILTER_OPS, flex: 1.1 },
                    { key: 'expr', label: 'значение (выражение)', mono: true, flex: 1.8 },
                  ]}
                />
              </Field>
              <div className="list-row">
                <Field label="Сортировка">
                  <select value={d.order_by || 'id'} onChange={(e) => set({ order_by: e.target.value })}>
                    {['id', ...modelFields].map((f) => (
                      <option key={f}>{f}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Порядок">
                  <select
                    value={d.descending ? 'desc' : 'asc'}
                    onChange={(e) => set({ descending: e.target.value === 'desc' })}
                  >
                    <option value="asc">по возрастанию</option>
                    <option value="desc">по убыванию</option>
                  </select>
                </Field>
                <Field label="Лимит">
                  <input className="mono" value={d.limit ?? 100} onChange={(e) => set({ limit: e.target.value })} title="Число 1–1000 или выражение, например per_page" />
                </Field>
              </div>
              <div className="list-row">
                <Field label="Смещение (offset)" hint="Например, (page - 1) * per_page">
                  <input className="mono" value={d.offset_expr || ''} placeholder="0" onChange={(e) => set({ offset_expr: e.target.value })} />
                </Field>
                <Field label="Всего записей → в поле" hint="Число всех подходящих записей (для пагинации)">
                  <input className="mono" value={d.count_target || ''} placeholder="total_count" onChange={(e) => set({ count_target: e.target.value })} />
                </Field>
              </div>
            </>
          )}
        </>
      )}

      {node.type === 'code' && (
        <>
          {sandboxInfo && (
            <div className={`sandbox-badge ${sandboxInfo.isolated && !sandboxInfo.warning ? 'good' : 'weak'}`}>
              {sandboxInfo.isolated ? '🔒' : '⚠'} Песочница: <b>{sandboxInfo.backend}</b> — {sandboxInfo.details}
              {sandboxInfo.warning && <div>⚠ {sandboxInfo.warning}</div>}
              <div className="badge-links">
                <button className="linklike" onClick={() => onOpenSettings('sandbox')}>
                  Настроить песочницу
                </button>
                <button className="linklike" onClick={() => onOpenSettings('libraries')}>
                  Библиотеки
                </button>
              </div>
            </div>
          )}
          <Field
            label={
              <div className="code-label">
                <span>Код</span>
                <EditorModeButtons mode={editorMode} setMode={setEditorMode} />
              </div>
            }
            hint="Функция run(data, env): data — копия полей запроса (JSON), env — переменные окружения. Можно импортировать стандартную библиотеку. print() попадает в лог запроса."
          >
            {editorMode ? (
              <div className="code-detached muted">
                Редактор открыт {editorMode === 'full' ? 'на весь экран' : 'на половину экрана'}.
              </div>
            ) : (
              <Suspense fallback={<div className="muted">Загрузка редактора…</div>}>
                <CodeEditor key={node.id} readOnly={readOnly} value={d.code || ''} onChange={(code) => set({ code })} errorLine={errorLine} />
              </Suspense>
            )}
          </Field>
          {editorMode && (
            <CodeOverlay
              mode={editorMode}
              setMode={setEditorMode}
              node={node}
              d={d}
              set={set}
              issues={issues}
              errorLine={errorLine}
              setErrorLine={setErrorLine}
              availableFields={availableFields}
              env={env}
              sandbox={sandbox}
              firewall={firewall}
              projectId={projectId}
              sandboxInfo={sandboxInfo}
              onOpenSettings={onOpenSettings}
            />
          )}
          <OutputsField d={d} set={set} />
          <div className="list-row">
            <Field label="Лимит времени, с">
              <input type="number" step="0.5" min="0.1" max="10" value={d.timeout ?? 2} onChange={(e) => set({ timeout: e.target.value })} />
            </Field>
            <Field label="Лимит памяти, МБ">
              <input type="number" step="32" min="32" max="1024" value={d.memory_mb ?? 256} onChange={(e) => set({ memory_mb: e.target.value })} />
            </Field>
          </div>
          <CodeTester d={d} set={set} availableFields={availableFields} env={env} sandbox={sandbox} firewall={firewall} projectId={projectId} onErrorLine={setErrorLine} />
        </>
      )}

      {node.type === 'scenario' && (
        <ScenarioBlockForm d={d} set={set} env={env} Field={Field} ListEditor={ListEditor} exprHint={EXPR_HINT} />
      )}

      {node.type === 'condition' && (
        <Field
          label="Условие (выражение)"
          hint={'Истина — выход «да», ложь — выход «нет». Примеры: order is None, qty > 10, deleted. ' + EXPR_HINT}
        >
          <input className="mono" value={d.expr || ''} onChange={(e) => set({ expr: e.target.value })} />
        </Field>
      )}

      {node.type === 'response' && (
        <>
          <Field label="HTTP-статус">
            <input
              type="number"
              value={d.status}
              onChange={(e) => set({ status: e.target.value === '' ? '' : Number(e.target.value) })}
            />
          </Field>
          <Field label="Тело ответа (JSON)" hint={'Пусто — вернуть все поля контекста. ' + EXPR_HINT}>
            <ListEditor
              items={d.body || []}
              onChange={(body) => set({ body })}
              empty="Будет возвращён весь контекст"
              addLabel="ключ"
              newItem={() => ({ key: '', expr: '' })}
              columns={[
                { key: 'key', label: 'ключ', mono: true, flex: 1 },
                { key: 'expr', label: 'выражение', mono: true, flex: 2.5 },
              ]}
            />
          </Field>
        </>
      )}

      {availableFields.length > 0 && node.type !== 'endpoint' && (
        <div className="available">
          <div className="field-label">Доступные поля на входе блока</div>
          {availableFields.map((f) => (
            <code key={f}>{f}</code>
          ))}
        </div>
      )}
    </fieldset>
  )
}
