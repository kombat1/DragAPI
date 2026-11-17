import { useEffect, useRef } from 'react'
import { EditorView, basicSetup } from 'codemirror'
import { keymap } from '@codemirror/view'
import { indentWithTab } from '@codemirror/commands'
import { python } from '@codemirror/lang-python'
import { oneDark } from '@codemirror/theme-one-dark'

const dark = () => window.matchMedia?.('(prefers-color-scheme: dark)').matches

// Редактор Python для блока «Python-код». Неконтролируемый: внешнее значение
// применяется только когда оно отличается от текста редактора (смена блока, откат версии).
export default function CodeEditor({ value, onChange, errorLine, fill = false, readOnly = false }) {
  const host = useRef(null)
  const view = useRef(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  useEffect(() => {
    view.current = new EditorView({
      parent: host.current,
      doc: value,
      extensions: [
        basicSetup,
        keymap.of([indentWithTab]),
        python(),
        EditorView.lineWrapping,
        EditorView.editable.of(!readOnly),
        ...(dark() ? [oneDark] : []),
        EditorView.theme({ '&': { fontSize: '12px' }, '.cm-scroller': { fontFamily: 'var(--mono)' } }),
        EditorView.updateListener.of((u) => {
          if (u.docChanged) onChangeRef.current(u.state.doc.toString())
        }),
      ],
    })
    return () => view.current.destroy()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const v = view.current
    if (v && value !== v.state.doc.toString()) {
      v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: value } })
    }
  }, [value])

  // подсветка строки с ошибкой из песочницы
  useEffect(() => {
    const v = view.current
    if (!v || !errorLine || errorLine > v.state.doc.lines) return
    const line = v.state.doc.line(errorLine)
    v.dispatch({ selection: { anchor: line.from, head: line.to }, scrollIntoView: true })
  }, [errorLine])

  return <div className={`code-editor ${fill ? 'fill' : ''}`} ref={host} />
}
