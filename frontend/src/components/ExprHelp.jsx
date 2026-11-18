import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api.js'
import { useCatalog } from '../catalog.js'

// Справка по выражениям: кнопка «?» у полей. Примеры приходят с сервера и проверены тестами
// на настоящем вычислителе (backend/tests/test_exprdocs.py).

let docsPromise = null
const loadDocs = () => (docsPromise ||= api.exprDocs().catch((e) => ((docsPromise = null), Promise.reject(e))))

const SPACE_NAMES = { api: 'API', automation: 'Автоматизация', bot: 'Боты' }
const fmt = (v) => (typeof v === 'string' ? `'${v}'` : JSON.stringify(v))

function useSpace() {
  const catalog = useCatalog()
  return catalog?.bot ? 'bot' : catalog?.automation ? 'automation' : 'api'
}

/** kind: 'expr' — поле-выражение, 'template' — текст с {…}; onInsert(code) — подставить в поле. */
export function HelpButton({ kind = 'expr', onInsert, variables = [], title }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        className="help-btn nodrag"
        title={title || 'Справка: как писать выражения, функции и примеры'}
        onClick={(e) => {
          e.preventDefault()
          e.stopPropagation()
          setOpen(true)
        }}
      >
        ?
      </button>
      {open && <ExprHelp kind={kind} onInsert={onInsert} variables={variables} onClose={() => setOpen(false)} />}
    </>
  )
}

function Example({ ex, kind, onInsert, onDone }) {
  const [copied, setCopied] = useState(false)
  const isTemplate = 'template' in ex
  const code = isTemplate ? ex.template : ex.expr
  // в поле-выражение подставляется выражение; в текстовое поле — шаблон или выражение в {…}
  const insertable = onInsert && (kind === 'template' ? true : !isTemplate)
  const value = kind === 'template' && !isTemplate ? `{${code}}` : code
  return (
    <div className="help-example">
      <div className="help-code">
        <code>{code}</code>
        <span className="help-result">
          {isTemplate ? '→ ' : '= '}
          {isTemplate ? ex.result : fmt(ex.result)}
        </span>
      </div>
      {ex.note && <div className="muted">{ex.note}</div>}
      <div className="help-actions">
        {insertable && (
          <button
            className="small"
            onClick={() => {
              onInsert(value)
              onDone()
            }}
            title="Заменить содержимое поля этим примером"
          >
            Подставить
          </button>
        )}
        <button
          className="small"
          onClick={() => {
            navigator.clipboard?.writeText(code)
            setCopied(true)
            setTimeout(() => setCopied(false), 1200)
          }}
        >
          {copied ? '✓ скопировано' : 'Копировать'}
        </button>
      </div>
    </div>
  )
}

export default function ExprHelp({ kind, onInsert, variables, onClose }) {
  const space = useSpace()
  const [docs, setDocs] = useState(null)
  const [error, setError] = useState(null)
  const [query, setQuery] = useState('')
  const [all, setAll] = useState(false)
  const body = useRef(null)
  useEffect(() => {
    loadDocs().then(setDocs).catch((e) => setError(e.message))
  }, [])
  useEffect(() => {
    const h = (e) => e.key === 'Escape' && (e.stopPropagation(), onClose())
    window.addEventListener('keydown', h, true)
    return () => window.removeEventListener('keydown', h, true)
  }, [onClose])

  const q = query.trim().toLowerCase()
  const match = (...parts) => !q || parts.some((p) => String(p ?? '').toLowerCase().includes(q))
  const fitsSpace = (ex) => all || !ex.spaces || ex.spaces.includes(space)
  const sections = useMemo(() => {
    if (!docs) return []
    return docs.sections
      .map((s) => ({
        ...s,
        examples: s.examples.filter((ex) => fitsSpace(ex) && match(ex.expr, ex.template, ex.note, s.title)),
        textMatch: match(s.title, ...s.text),
      }))
      .filter((s) => s.examples.length || (q && s.textMatch) || !q)
  }, [docs, q, all, space]) // eslint-disable-line react-hooks/exhaustive-deps
  const functions = (docs?.functions || []).filter((f) => match(f.name, f.description, f.signature))
  const mistakes = (docs?.mistakes || []).filter((m) => match(m.wrong, m.right, m.why))
  const jump = (id) => body.current?.querySelector(`#help-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  return createPortal(
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal help-modal" role="dialog" aria-label="Справка по выражениям">
        <div className="modal-head">
          <div>
            <div className="inspector-title">❓ Выражения: как писать условия и формулы</div>
            <div className="muted">
              {kind === 'template'
                ? 'Это текстовое поле: пишите обычный текст, а выражения — в фигурных скобках {…}.'
                : 'Это поле-выражение: формула целиком, без фигурных скобок.'}
            </div>
          </div>
          <button className="icon" onClick={onClose} title="Закрыть (Esc)">
            ×
          </button>
        </div>
        <div className="help-toolbar">
          <input autoFocus placeholder="Поиск: строка, число, contains, телефон…" value={query} onChange={(e) => setQuery(e.target.value)} />
          <label className="check">
            <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> примеры для всех пространств
            <span className="muted"> (сейчас: {SPACE_NAMES[space]})</span>
          </label>
        </div>
        {error && <div className="issue error">{error}</div>}
        {!docs ? (
          !error && <div className="muted pad">Загрузка…</div>
        ) : (
          <div className="help-layout">
            <nav className="help-nav">
              {variables.length > 0 && <button onClick={() => jump('vars')}>Переменные блока</button>}
              {sections.map((s) => (
                <button key={s.id} onClick={() => jump(s.id)}>
                  {s.title}
                  {q && <span className="muted"> {s.examples.length}</span>}
                </button>
              ))}
              <button onClick={() => jump('functions')}>Все функции</button>
              <button onClick={() => jump('mistakes')}>Частые ошибки</button>
              <button onClick={() => jump('sample')}>На чём посчитаны примеры</button>
            </nav>
            <div className="help-body" ref={body}>
              {variables.length > 0 && !q && (
                <section id="help-vars">
                  <h3>Переменные, доступные в этом блоке</h3>
                  <div className="available">
                    {variables.map((v) => (
                      <code
                        key={v}
                        className={onInsert ? 'clickable' : ''}
                        title={onInsert ? 'Подставить в поле' : undefined}
                        onClick={() => onInsert && (onInsert(kind === 'template' ? `{${v}}` : v), onClose())}
                      >
                        {v}
                      </code>
                    ))}
                  </div>
                </section>
              )}
              {sections.map((s) => (
                <section key={s.id} id={`help-${s.id}`}>
                  <h3>{s.title}</h3>
                  {s.text.map((t, i) => (
                    <p key={i} className="help-text">
                      {t}
                    </p>
                  ))}
                  {s.examples.map((ex, i) => (
                    <Example key={i} ex={ex} kind={kind} onInsert={onInsert} onDone={onClose} />
                  ))}
                  {!s.examples.length && <div className="muted">Для этого пространства примеров нет — включите «для всех пространств».</div>}
                </section>
              ))}
              <section id="help-functions">
                <h3>Все функции</h3>
                <table className="data-table help-table">
                  <tbody>
                    {functions.map((f) => (
                      <tr key={f.name}>
                        <td>
                          <code>{f.signature}</code>
                        </td>
                        <td>{f.description}</td>
                        <td>
                          <code>{f.expr}</code> <span className="help-result">= {fmt(f.result)}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
              <section id="help-mistakes">
                <h3>Частые ошибки</h3>
                <table className="data-table help-table">
                  <thead>
                    <tr>
                      <th>Неправильно</th>
                      <th>Правильно</th>
                      <th>Почему</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mistakes.map((m) => (
                      <tr key={m.wrong}>
                        <td>
                          <code className="help-wrong">{m.wrong}</code>
                        </td>
                        <td>
                          <code>{m.right}</code>
                        </td>
                        <td>{m.why}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
              <section id="help-sample">
                <h3>На чём посчитаны примеры</h3>
                <p className="help-text">Результаты выше — для таких значений переменных (у вас они будут свои):</p>
                <pre className="run-vars">{JSON.stringify({ ...docs.sample, env: docs.sample_env }, null, 1)}</pre>
              </section>
            </div>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}
