import { useEffect, useRef } from 'react'

// Вставка файлов из буфера обмена (Ctrl+V): снимок экрана, скопированная картинка, файл из проводника.
// Компоненты регистрируют обработчики; срабатывает первый подходящий — с большим приоритетом,
// при равном — открытый позже. Обработчик возвращает true, если взял файлы.

const handlers = []
let seq = 0

export function clipboardFiles(e) {
  const dt = e.clipboardData
  if (!dt) return []
  const files = [...(dt.files || [])]
  if (files.length) return files
  return [...(dt.items || [])].filter((i) => i.kind === 'file').map((i) => i.getAsFile()).filter(Boolean)
}

function typingText(e) {
  // в текстовом поле с текстом в буфере — обычная вставка текста (например, ячейки из таблицы
  // кладут в буфер и текст, и картинку)
  const el = document.activeElement
  const typing = ['INPUT', 'TEXTAREA'].includes(el?.tagName) || el?.isContentEditable
  return typing && !!e.clipboardData?.getData('text/plain')
}

function onPaste(e) {
  if (e.defaultPrevented || typingText(e)) return
  const files = clipboardFiles(e)
  if (!files.length) return
  const ordered = [...handlers].sort((a, b) => b.priority - a.priority || b.seq - a.seq)
  for (const h of ordered) {
    if (h.fn.current(files, e)) {
      e.preventDefault()
      return
    }
  }
}

if (typeof window !== 'undefined') window.addEventListener('paste', onPaste, true)

/** Принимать файлы из буфера, пока компонент открыт и enabled. */
export function usePasteFiles(fn, { enabled = true, priority = 0 } = {}) {
  const ref = useRef(fn)
  ref.current = fn
  useEffect(() => {
    if (!enabled) return
    const h = { fn: ref, priority, seq: ++seq }
    handlers.push(h)
    return () => handlers.splice(handlers.indexOf(h), 1)
  }, [enabled, priority])
}

/** Имя для безымянного файла из буфера («image.png» у снимков экрана). */
export function pastedName(f) {
  if (f.name && f.name !== 'image.png') return f.name
  const ext = (f.type.split('/')[1] || 'bin').replace('jpeg', 'jpg')
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return `вставка-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.${ext}`
}
