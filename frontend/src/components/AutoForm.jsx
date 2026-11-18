import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import ListEditor from './ListEditor.jsx'
import { HelpButton } from './ExprHelp.jsx'
import { AutomationProjectSelect, ButtonsInput, LinksInput } from './Bots.jsx'
import { ColorField, ImageField, RegionField, VisionTest } from './Vision.jsx'

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

function Field({ label, children, hint, help }) {
  return (
    <div className="field">
      <div className="field-label">
        {label}
        {help}
      </div>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  )
}

function visible(f, d, defaults) {
  if (!f.show_if) return true
  return Object.entries(f.show_if).every(([k, vals]) => vals.includes(d[k] ?? defaults[k]))
}

export default function AutoForm({ spec, d, set, projectId, agent, variables, roles = [], nodeId, graph, readOnly = false }) {
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
    out.push(<FieldInput key={f.key} f={f} d={d} set={set} variables={variables} roles={roles} projectId={projectId} agent={agent} readOnly={readOnly} />)
  }
  if (spec.group === 'Зрение' && graph && nodeId) {
    out.push(<VisionTest key="__vision_test" projectId={projectId} agent={agent} nodeId={nodeId} graph={graph} />)
  }
  return <>{out}</>
}

function FieldInput({ f, d, set, variables, roles, projectId, agent, readOnly }) {
  const value = d[f.key]
  const put = (v) => set({ [f.key]: v })
  switch (f.kind) {
    case 'image':
      return (
        <Field label={f.label} hint={f.hint}>
          <ImageField value={value} onChange={put} projectId={projectId} agent={agent} readOnly={readOnly} />
        </Field>
      )
    case 'region':
      return (
        <Field label={f.label} hint={f.hint}>
          <RegionField value={value} onChange={put} projectId={projectId} agent={agent} optional={f.optional} readOnly={readOnly} />
        </Field>
      )
    case 'color':
      return (
        <Field label={f.label} hint={f.hint}>
          <ColorField
            value={value}
            onChange={put}
            projectId={projectId}
            agent={agent}
            readOnly={readOnly}
            onPickPoint={(p) => set({ [f.key]: p.hex, ...('x' in d || 'y' in d ? { x: String(p.x), y: String(p.y) } : {}) })}
          />
        </Field>
      )
    case 'roles': {
      const chosen = value || []
      return (
        <Field label={f.label} hint={f.hint}>
          <div className="weekdays">
            {['admin', ...roles].map((r) => (
              <label key={r} className={`chip ${chosen.includes(r) ? 'on' : ''}`}>
                <input type="checkbox" checked={chosen.includes(r)} onChange={(e) => put(e.target.checked ? [...chosen, r] : chosen.filter((x) => x !== r))} />
                {r}
              </label>
            ))}
          </div>
        </Field>
      )
    }
    case 'role':
    case 'role_any': {
      const options = f.kind === 'role' ? [...roles, 'user', 'banned'] : ['all', 'admin', ...roles, 'user']
      const label = { all: 'все пользователи', user: 'user (обычные / снять роль)', banned: 'banned (заблокировать)' }
      return (
        <Field label={f.label} hint={f.hint}>
          <select value={value ?? ''} onChange={(e) => put(e.target.value)}>
            {!options.includes(value) && <option value={value}>{value || '—'}</option>}
            {options.map((r) => (
              <option key={r} value={r}>
                {label[r] || r}
              </option>
            ))}
          </select>
        </Field>
      )
    }
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
        <Field label={f.label} hint={EXPR_HINT} help={<HelpButton kind="expr" variables={variables} />}>
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
        <Field label={f.label} hint={EXPR_HINT} help={<HelpButton kind="expr" variables={variables} />}>
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
    case 'buttons':
      return (
        <Field label={f.label} hint={f.hint || 'Каждая кнопка — отдельный выход блока; подключите к нему свою ветку.'}>
          <ButtonsInput value={value} onChange={put} ListEditor={ListEditor} />
        </Field>
      )
    case 'links':
      return (
        <Field label={f.label} hint={f.hint}>
          <LinksInput value={value} onChange={put} ListEditor={ListEditor} />
        </Field>
      )
    case 'automation_project':
      return (
        <Field label={f.label} hint={f.hint}>
          <AutomationProjectSelect value={value} onChange={put} />
        </Field>
      )
    case 'template':
      return (
        <Field label={f.label} hint={f.hint || TEMPLATE_HINT} help={<HelpButton kind="template" onInsert={put} variables={variables} />}>
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
          help={<HelpButton kind="expr" onInsert={put} variables={variables} />}
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
    case 'image_find': {
      const what = d.image ? '' : '⚠ нет образца · '
      const act = d.mode === 'disappear' ? 'ждать, пока исчезнет' : { none: 'найти', click: 'найти и кликнуть', double: 'найти и 2× кликнуть', right: 'найти и правый клик', move: 'найти и навести' }[d.action] || 'найти'
      return `${what}${act}${Number(d.wait) ? ` · до ${d.wait} с` : ''}${d.region ? ' · в области' : ''}`
    }
    case 'pixel_check':
      return `(${d.x}, ${d.y}) = ${d.color}${Number(d.wait) ? ` · ждать до ${d.wait} с` : ''}`
    case 'ocr':
      return `${d.region ? `${d.region.w}×${d.region.h} в (${d.region.x}, ${d.region.y})` : '⚠ нет области'} → ${d.target}${d.digits ? ' (цифры)' : ''}`
    case 'telegram':
      return `«${String(d.text || '').slice(0, 40)}»${d.chat ? ` → ${d.chat}` : ''}`
    case 'bot_command':
      return `/${d.command}${(d.roles || []).length ? ` 🔒${d.roles.join(',')}` : d.description ? ` — ${d.description}` : ''}`
    case 'bot_role':
      return `${d.target || 'себе'} → ${d.role}`
    case 'bot_file':
      return `${{ any: 'любой файл', photo: 'фото', document: 'документ', video: 'видео', voice: 'голосовое', audio: 'аудио' }[d.kind]}${d.where && d.where !== 'any' ? ` · ${d.where === 'group' ? 'группы' : 'личка'}` : ''}`
    case 'bot_download':
      return `${d.source} → ${d.var} (${{ save: 'сохранить', text: 'текст', base64: 'base64' }[d.mode]})`
    case 'bot_delay':
      return `через ${d.amount} ${{ minutes: 'мин', hours: 'ч', days: 'дн.' }[d.unit]}`
    case 'bot_schedule':
      return d.kind === 'interval' ? `каждые ${d.every_minutes} мин` : `в ${d.at}${(d.weekdays || []).length ? ` (${d.weekdays.length} дн.)` : ' ежедневно'}`
    case 'bot_member':
      return d.action === 'leave' ? 'участник вышел' : 'новый участник'
    case 'bot_send_file':
      return `${d.source} → ${d.to === 'role' ? `📢 ${d.role}` : d.to === 'chat' ? d.chat : 'этот чат'}`
    case 'bot_refund':
      return `↩ ${d.charge_id}`
    case 'bot_filter':
      return [d.links && 'ссылки', (d.words || []).length && `${d.words.length} слов`, d.caps && 'капс', Number(d.flood_count) && 'флуд'].filter(Boolean).join(', ')
    case 'bot_moderate':
      return `${{ delete: 'удалить', mute: 'мьют', unmute: 'снять мьют', ban: 'бан', unban: 'разбан', kick: 'выгнать' }[d.action]} · ${{ author: 'автор', reply: 'по ответу', id: d.target_id }[d.target]}${['mute', 'ban'].includes(d.action) && Number(d.minutes) ? ` ${d.minutes} мин` : ''}`
    case 'bot_invoice':
      return `${d.title}: ${d.amount} ${d.currency === 'XTR' ? '⭐' : d.currency}`
    case 'bot_message':
      return d.match === 'any' ? 'любое сообщение' : `${{ equals: '=', contains: '∋', regex: '~' }[d.match] || ''} «${d.pattern}»`
    case 'bot_send':
      return `${d.to === 'role' ? `📢 ${d.role}: ` : ''}«${String(d.text || '').slice(0, 40)}»${(d.links || []).length ? ` +${d.links.length} 🔗` : ''}${d.to === 'chat' && d.chat ? ` → ${d.chat}` : ''}`
    case 'bot_photo':
      return d.url
    case 'bot_ask':
      return `${String(d.text || '').slice(0, 30)} → ${d.var}`
    case 'bot_menu':
      return String(d.text || '').slice(0, 40)
    case 'bot_save':
      return (d.assignments || []).map((a) => `${a.target} = ${a.expr}`).join('; ')
    case 'run_scenario':
      return `${d.scenario} ${d.project ? '' : '(проект не выбран)'}`
    case 'tg_ask':
      return `${String(d.text || '').slice(0, 30)} [${d.yes} / ${d.no}] ⌛${d.timeout} с`
    default:
      return ''
  }
}
