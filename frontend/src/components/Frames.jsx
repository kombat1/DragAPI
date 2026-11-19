// Оформление холста: рамки (группы блоков с подписью) и заметки.
// Хранятся в проекте отдельно от блоков (поле frames) и на выполнение не влияют.
import { useState } from 'react'
import { NodeResizer } from '@xyflow/react'

export const DECOR_TYPES = ['frame', 'note']
export const isDecor = (n) => DECOR_TYPES.includes(n?.type)

export const FRAME_COLORS = [
  ['#6366f1', 'Синий'],
  ['#059669', 'Зелёный'],
  ['#d97706', 'Оранжевый'],
  ['#dc2626', 'Красный'],
  ['#9333ea', 'Фиолетовый'],
  ['#64748b', 'Серый'],
]
export const NOTE_COLORS = [
  ['#facc15', 'Жёлтая'],
  ['#4ade80', 'Зелёная'],
  ['#60a5fa', 'Голубая'],
  ['#f472b6', 'Розовая'],
]

export const DECOR_DEFAULTS = {
  frame: { data: { title: 'Группа', color: FRAME_COLORS[0][0] }, width: 520, height: 320 },
  note: { data: { text: '', color: NOTE_COLORS[0][0] }, width: 220, height: 140 },
}

export const DECOR_PALETTE = [
  ['frame', '▭ Рамка', 'Обвести часть схемы и подписать её. Двигается вместе с блоками внутри'],
  ['note', '🗒 Заметка', 'Комментарий на холсте: пояснение, TODO, инструкция для коллег'],
]

// Блоки целиком внутри рамки (по текущим размерам) — едут вместе с ней.
export function membersOf(frame, nodes) {
  const fx = frame.position.x
  const fy = frame.position.y
  const fw = frame.width ?? frame.measured?.width ?? 0
  const fh = frame.height ?? frame.measured?.height ?? 0
  return nodes.filter((n) => {
    if (n.id === frame.id || n.hidden) return false
    const w = n.width ?? n.measured?.width ?? 0
    const h = n.height ?? n.measured?.height ?? 0
    return n.position.x >= fx && n.position.y >= fy && n.position.x + w <= fx + fw && n.position.y + h <= fy + fh
  })
}

// Рамка вокруг выделенных блоков: отступ по краям и место под заголовок.
export function frameAround(selected) {
  const PAD = 30
  const HEAD = 40
  const xs = selected.map((n) => n.position.x)
  const ys = selected.map((n) => n.position.y)
  const x2 = selected.map((n) => n.position.x + (n.width ?? n.measured?.width ?? 230))
  const y2 = selected.map((n) => n.position.y + (n.height ?? n.measured?.height ?? 80))
  const x = Math.min(...xs) - PAD
  const y = Math.min(...ys) - PAD - HEAD
  return { position: { x, y }, width: Math.max(...x2) + PAD - x, height: Math.max(...y2) + PAD - y }
}

// Текст, который правится по двойному клику (Enter — готово, Esc — отмена; в заметке Enter — новая строка).
function InlineEdit({ value, placeholder, multiline, editing, setEditing, onSave, className }) {
  if (!editing) return <div className={className}>{value || <span className="decor-placeholder">{placeholder}</span>}</div>
  return <InlineInput {...{ value, placeholder, multiline, setEditing, onSave, className }} />
}

function InlineInput({ value, placeholder, multiline, setEditing, onSave, className }) {
  const [draft, setDraft] = useState(value || '')
  const done = (save) => {
    if (save && draft !== (value || '')) onSave(draft)
    setEditing(false)
  }
  const Tag = multiline ? 'textarea' : 'input'
  return (
    <Tag
      autoFocus
      className={`${className} nodrag nowheel decor-input`}
      value={draft}
      placeholder={placeholder}
      onFocus={(e) => e.target.select()}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => done(true)}
      onKeyDown={(e) => {
        e.stopPropagation()
        if (e.key === 'Escape') done(false)
        if (e.key === 'Enter' && (!multiline || e.ctrlKey || e.metaKey)) (e.preventDefault(), done(true))
      }}
    />
  )
}

export function FrameNode({ data, selected }) {
  const [editing, setEditing] = useState(false)
  const edit = data.__update && !data.__readOnly
  return (
    <div className={`frame-box ${selected ? 'selected' : ''}`} style={{ '--frame': data.color || FRAME_COLORS[0][0] }}>
      <NodeResizer isVisible={!!(selected && edit)} minWidth={160} minHeight={90} color={data.color} />
      <div
        className="frame-head"
        onDoubleClick={() => edit && setEditing(true)}
        title={edit ? 'Двойной клик — переименовать. Тяните за заголовок, чтобы двигать рамку с блоками внутри' : undefined}
      >
        <InlineEdit
          className="frame-title"
          value={data.title}
          placeholder="Без названия"
          editing={editing}
          setEditing={setEditing}
          onSave={(title) => data.__update({ title })}
        />
        {data.__count > 0 && <span className="frame-count">{data.__count}</span>}
      </div>
    </div>
  )
}

export function NoteNode({ data, selected }) {
  const [editing, setEditing] = useState(false)
  const edit = data.__update && !data.__readOnly
  return (
    <div
      className={`note-box ${selected ? 'selected' : ''}`}
      style={{ '--note': data.color || NOTE_COLORS[0][0] }}
      onDoubleClick={() => edit && setEditing(true)}
      title={edit && !editing ? 'Двойной клик — редактировать' : undefined}
    >
      <NodeResizer isVisible={!!(selected && edit)} minWidth={120} minHeight={60} color="#a16207" />
      <InlineEdit
        multiline
        className="note-text"
        value={data.text}
        placeholder="Двойной клик — написать заметку"
        editing={editing}
        setEditing={setEditing}
        onSave={(text) => data.__update({ text })}
      />
    </div>
  )
}

export function DecorInspector({ node, onChange, onDelete, readOnly, members }) {
  const frame = node.type === 'frame'
  const colors = frame ? FRAME_COLORS : NOTE_COLORS
  const set = (patch) => onChange({ ...node.data, ...patch })
  return (
    <div className="inspector">
      <div className="inspector-title">{frame ? '▭ Рамка' : '🗒 Заметка'}</div>
      <p className="muted">
        {frame
          ? 'Подпись для части схемы. Блоки, целиком лежащие внутри, двигаются вместе с рамкой. На работу проекта не влияет.'
          : 'Комментарий для себя и коллег. На работу проекта не влияет.'}
      </p>
      {frame ? (
        <label className="field">
          <span className="field-label">Название</span>
          <input value={node.data.title || ''} disabled={readOnly} onChange={(e) => set({ title: e.target.value })} />
        </label>
      ) : (
        <label className="field">
          <span className="field-label">Текст</span>
          <textarea rows={6} value={node.data.text || ''} disabled={readOnly} onChange={(e) => set({ text: e.target.value })} />
        </label>
      )}
      <div className="field">
        <span className="field-label">Цвет</span>
        <div className="color-choice">
          {colors.map(([c, label]) => (
            <button
              key={c}
              type="button"
              title={label}
              disabled={readOnly}
              className={`swatch ${(node.data.color || colors[0][0]) === c ? 'on' : ''}`}
              style={{ background: c }}
              onClick={() => set({ color: c })}
            />
          ))}
        </div>
      </div>
      {frame && <p className="muted">Внутри блоков: {members}</p>}
      {!readOnly && (
        <button className="danger" onClick={onDelete}>
          Удалить {frame ? 'рамку (блоки останутся)' : 'заметку'}
        </button>
      )}
    </div>
  )
}
