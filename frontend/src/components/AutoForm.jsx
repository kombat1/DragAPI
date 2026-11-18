import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import ListEditor from './ListEditor.jsx'

// Форма блока автоматизации строится по описанию полей из каталога (backend: app/automation.py).

const WEEKDAYS = [
  ['mon', 'Пн'], ['tue', 'Вт'], ['wed', 'Ср'], ['thu', 'Чт'], ['fri', 'Пт'], ['sat', 'Сб'], ['sun', 'Вс'],
]

const EXPR_HINT = 'Выражение: число (10), переменная (i) или формула (i * 50 + 100).'
const TEMPLATE_HINT = '{выражение} подставляет значение: «Заказ {n}». Фигурные скобки как текст — {{ и }}.'

// KeyboardEvent → имя клавиши как на бэкенде (dragapi_agent/keys.py); буквы и цифры — по физической клавише
const KEY_NAMES = {
  Control: 'ctrl', Shift: 'shift', Alt: 'alt', Meta: 'super', OS: 'super', AltGraph: 'alt_gr',
  Enter: 'enter', Escape: 'esc', Tab: 'tab', ' ': 'space', Backspace: 'backspace', Delete: 'delete',
  Insert: 'insert', Home: 'home', End: 'end', PageUp: 'page_up', PageDown: 'page_down',
  ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right', CapsLock: 'caps_lock',
  PrintScreen: 'print_screen', ContextMenu: 'menu', Pause: 'pause', NumLock: 'num_lock', ScrollLock: 'scroll_lock',
}
const MODS = ['ctrl', 'shift', 'alt', 'super']

function keyName(e) {
  if (KEY_NAMES[e.key]) return KEY_NAMES[e.key]
  if (/^F\d{1,2}$/.test(e.key)) return e.key.toLowerCase()
  const code = /^Key([A-Z])$/.exec(e.code) || /^Digit(\d)$/.exec(e.code)
  if (code) return code[1].toLowerCase()
  return e.key.length === 1 ? e.key.toLowerCase() : null
}

function KeysInput({ value, onChange }) {
  const [capturing, setCapturing] = useState(false)
  const ref = useRef(null)
  const onKeyDown = (e) => {
    if (!capturing) return
    e.preventDefault()
    e.stopPropagation()
    const name = keyName(e)
    if (!name || MODS.includes(name)) return // ждём основную клавишу
    const mods = []
    if (e.ctrlKey) mods.push('ctrl')
    if (e.shiftKey) mods.push('shift')
    if (e.altKey) mods.push('alt')
    if (e.metaKey) mods.push('super')
    onChange([...mods, name].join('+'))
    setCapturing(false)
    ref.current?.blur()
  }
  return (
    <div className="keys-input">
      <input
        ref={ref}
        className={`mono ${capturing ? 'capturing' : ''}`}
        value={capturing ? 'нажмите сочетание…' : value || ''}
        readOnly={capturing}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={() => setCapturing(false)}
        placeholder="ctrl+shift+s"
      />
      <button
        className="small"
        title="Нажать сочетание на клавиатуре — оно запишется в поле"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => {
          setCapturing(true)
          ref.current?.focus()
        }}
      >
        ⌨ Записать
      </button>
    </div>
  )
}

// «📍 С экрана»: через 3 секунды агент сообщает, где курсор, — координаты попадают в поля
function PickPoint({ projectId, agent, onPick }) {
  const [state, setState] = useState(null) // null | {left} | {error}
  const alive = useRef(true)
  useEffect(() => () => (alive.current = false), [])
  const start = async () => {
    try {
      const p = await api.pickPoint(projectId, { agent, countdown: 3 })
      setState({ left: 3 })
      for (let i = 0; i < 80 && alive.current; i++) {
        await new Promise((r) => setTimeout(r, 250))
        const s = await api.pickPointStatus(projectId, p.id)
        if (s.error) return setState({ error: s.error })
        if (s.x !== undefined) {
          onPick(s.x, s.y)
          return setState(null)
        }
        setState({ left: s.left })
      }
      setState({ error: 'агент не ответил' })
    } catch (e) {
      setState({ error: e.message })
    }
  }
  return (
    <span className="pick-point">
      <button className="small" onClick={start} disabled={state && !state.error} title="Наведите курсор на нужное место за 3 секунды">
        {state && !state.error ? `📍 Наведите курсор… ${state.left ?? ''}` : '📍 С экрана'}
      </button>
      {state?.error && <span className="issue error">{state.error}</span>}
    </span>
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

function visible(f, d, defaults) {
  if (!f.show_if) return true
  return Object.entries(f.show_if).every(([k, vals]) => vals.includes(d[k] ?? defaults[k]))
}

export default function AutoForm({ spec, d, set, projectId, agent, variables }) {
  const fields = spec.fields.filter((f) => visible(f, d, spec.defaults))
  const out = []
  for (let i = 0; i < fields.length; i++) {
    const f = fields[i]
    // пары координат — в одну строку с кнопкой «С экрана»
    if (f.group === 'point' && fields[i + 1]?.group === 'point' && f.key.startsWith('x')) {
      const g = fields[i + 1]
      out.push(
        <div key={f.key} className="field">
          <div className="field-label">
            {f.label.replace(/\s*X$/, '') === f.label ? `${f.label} / ${g.label}` : f.label.replace(/\s*X$/, '')}
          </div>
          <div className="list-row point-row">
            <input className="mono" placeholder="X" value={d[f.key] ?? ''} onChange={(e) => set({ [f.key]: e.target.value })} />
            <input className="mono" placeholder="Y" value={d[g.key] ?? ''} onChange={(e) => set({ [g.key]: e.target.value })} />
            <PickPoint projectId={projectId} agent={agent} onPick={(x, y) => set({ [f.key]: String(x), [g.key]: String(y) })} />
          </div>
          {(f.hint || g.hint) && <div className="hint">{f.hint || g.hint}</div>}
        </div>,
      )
      i++
      continue
    }
    out.push(<FieldInput key={f.key} f={f} d={d} set={set} variables={variables} />)
  }
  return <>{out}</>
}

function FieldInput({ f, d, set, variables }) {
  const value = d[f.key]
  const put = (v) => set({ [f.key]: v })
  switch (f.kind) {
    case 'select':
      return (
        <Field label={f.label} hint={f.hint}>
          <select value={value ?? ''} onChange={(e) => put(e.target.value)}>
            {f.options.map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
        </Field>
      )
    case 'bool':
      return (
        <label className="toggle">
          <input type="checkbox" checked={!!value} onChange={(e) => put(e.target.checked)} />
          <div>
            <b>{f.label}</b>
            {f.hint && <div className="muted">{f.hint}</div>}
          </div>
        </label>
      )
    case 'keys':
      return (
        <Field label={f.label} hint={f.hint || 'Например: ctrl+c, alt+tab, enter, f5. Кнопка «Записать» — нажать на клавиатуре.'}>
          <KeysInput value={value} onChange={put} />
        </Field>
      )
    case 'weekdays': {
      const days = value || []
      return (
        <Field label={f.label} hint={f.hint}>
          <div className="weekdays">
            {WEEKDAYS.map(([k, label]) => (
              <label key={k} className={`chip ${days.includes(k) ? 'on' : ''}`}>
                <input
                  type="checkbox"
                  checked={days.includes(k)}
                  onChange={(e) => put(e.target.checked ? [...days, k] : days.filter((x) => x !== k))}
                />
                {label}
              </label>
            ))}
          </div>
        </Field>
      )
    }
    case 'strings':
      return (
        <Field label={f.label} hint={f.hint}>
          <input
            className="mono"
            value={(value || []).join(', ')}
            placeholder="через запятую"
            onChange={(e) =>
              put(
                e.target.value
                  .split(',')
                  .map((x) => x.trim())
                  .filter((x, i, arr) => x || i === arr.length - 1),
              )
            }
          />
        </Field>
      )
    case 'assignments':
      return (
        <Field label={f.label} hint={EXPR_HINT}>
          <ListEditor
            items={value || []}
            onChange={put}
            empty="Нет присваиваний"
            addLabel="переменная"
            newItem={() => ({ target: '', expr: '' })}
            columns={[
              { key: 'target', label: 'имя', mono: true, flex: 1 },
              { key: 'expr', label: 'выражение', mono: true, flex: 2.5 },
            ]}
          />
        </Field>
      )
    case 'kv':
      return (
        <Field label={f.label} hint={EXPR_HINT}>
          <ListEditor
            items={value || []}
            onChange={put}
            empty="Пустое тело"
            addLabel="поле"
            newItem={() => ({ key: '', expr: '' })}
            columns={[
              { key: 'key', label: 'ключ', mono: true, flex: 1 },
              { key: 'expr', label: 'выражение', mono: true, flex: 2.5 },
            ]}
          />
        </Field>
      )
    case 'template':
      return (
        <Field label={f.label} hint={f.hint || TEMPLATE_HINT}>
          {f.multiline ? (
            <textarea className="mono" rows={3} value={value ?? ''} onChange={(e) => put(e.target.value)} />
          ) : (
            <input className="mono" value={value ?? ''} onChange={(e) => put(e.target.value)} />
          )}
        </Field>
      )
    case 'num':
    case 'expr':
      return (
        <Field
          label={f.label}
          hint={f.hint || (f.kind === 'expr' ? `Истина/ложь. ${variables?.length ? 'Переменные: ' + variables.join(', ') : ''}` : '')}
        >
          <input className="mono" value={value ?? ''} onChange={(e) => put(e.target.value)} />
        </Field>
      )
    default:
      return (
        <Field label={f.label} hint={f.hint}>
          <input className={f.key === 'name' || f.key === 'var' || f.key === 'target' ? 'mono' : ''} value={value ?? ''} onChange={(e) => put(e.target.value)} />
        </Field>
      )
  }
}

// Короткое описание блока на канвасе
export function autoSummary(type, d) {
  switch (type) {
    case 'trigger':
      return (
        {
          manual: 'вручную',
          hotkey: `⌨ ${d.hotkey || ''}`,
          interval: `каждые ${d.every_seconds} с`,
          daily: `в ${d.at}${(d.weekdays || []).length ? ` (${d.weekdays.length} дн.)` : ' ежедневно'}`,
          webhook: 'по HTTP',
          telegram: `✈ /${d.command || '…'}`,
        }[d.kind] + ` · «${d.name}»`
      )
    case 'mouse_move':
      return `${d.relative ? 'на' : 'в'} (${d.x}, ${d.y})`
    case 'mouse_click':
    case 'mouse_down':
    case 'mouse_up': {
      const where = d.x !== '' && d.x != null ? `(${d.x}, ${d.y})` : 'на месте'
      const btn = { left: 'левой', right: 'правой', middle: 'средней' }[d.button] || ''
      return `${btn}${type === 'mouse_click' && Number(d.clicks) > 1 ? ` ×${d.clicks}` : ''} ${where}`
    }
    case 'mouse_drag':
      return `${d.x1 ? `(${d.x1}, ${d.y1})` : 'отсюда'} → (${d.x2}, ${d.y2})`
    case 'mouse_scroll':
      return `${Number(d.dy) < 0 ? '↓' : '↑'} ${Math.abs(Number(d.dy) || 0)}${Number(d.dx) ? ` ↔ ${d.dx}` : ''}`
    case 'autoclick':
      return `${d.mode === 'seconds' ? `${d.seconds} с` : `×${d.count}`} каждые ${d.interval_ms} мс`
    case 'key_type':
      return `«${String(d.text || '').slice(0, 40)}»${d.enter ? ' ⏎' : ''}`
    case 'key_press':
      return `${d.action === 'down' ? 'зажать ' : d.action === 'up' ? 'отпустить ' : ''}${d.keys}${Number(d.times) > 1 ? ` ×${d.times}` : ''}`
    case 'wait':
      return `${d.seconds} с`
    case 'repeat':
      return `${d.count} раз${d.var ? ` · ${d.var}` : ''}`
    case 'while':
    case 'condition':
      return d.expr
    case 'set_vars':
      return (d.assignments || []).map((a) => `${a.target} = ${a.expr}`).join('; ')
    case 'log':
      return d.message
    case 'stop':
      return d.status === 'error' ? `ошибка${d.message ? `: ${d.message}` : ''}` : 'успех'
    case 'http':
      return `${d.method} ${d.url} → ${d.target}`
    case 'telegram':
      return `«${String(d.text || '').slice(0, 40)}»${d.chat ? ` → ${d.chat}` : ''}`
    case 'tg_ask':
      return `${String(d.text || '').slice(0, 30)} [${d.yes} / ${d.no}] ⌛${d.timeout} с`
    default:
      return ''
  }
}
