export default function ListEditor({ items, onChange, columns, empty, newItem, addLabel, reorder = false, numbered = false }) {
  const update = (i, key, value) => onChange(items.map((it, j) => (j === i ? { ...it, [key]: value } : it)))
  const remove = (i) => onChange(items.filter((_, j) => j !== i))
  const move = (i, delta) => {
    const j = i + delta
    if (j < 0 || j >= items.length) return
    const next = [...items]
    ;[next[i], next[j]] = [next[j], next[i]]
    onChange(next)
  }
  return (
    <div className="list-editor">
      {items.length === 0 && <div className="muted">{empty}</div>}
      {items.map((item, i) => (
        <div className="list-row" key={i}>
          {numbered && <span className="row-number">{i + 1}</span>}
          {columns.map((col) => (
            <label key={col.key} style={{ flex: col.flex ?? 1 }} title={col.label}>
              {col.options ? (
                <select value={item[col.key] ?? ''} onChange={(e) => update(i, col.key, e.target.value)}>
                  {col.options.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              ) : col.checkbox ? (
                <span className="check">
                  <input
                    type="checkbox"
                    checked={!!item[col.key]}
                    onChange={(e) => update(i, col.key, e.target.checked)}
                  />
                  {col.label}
                </span>
              ) : (
                <input
                  type={col.password?.(item) ? 'password' : 'text'}
                  autoComplete="off"
                  value={item[col.key] ?? ''}
                  placeholder={col.label}
                  className={col.mono ? 'mono' : ''}
                  disabled={col.disabled?.(item)}
                  onChange={(e) => update(i, col.key, e.target.value)}
                />
              )}
            </label>
          ))}
          {reorder && (
            <span className="reorder">
              <button className="icon" title="Выше" disabled={i === 0} onClick={() => move(i, -1)}>
                ↑
              </button>
              <button className="icon" title="Ниже" disabled={i === items.length - 1} onClick={() => move(i, 1)}>
                ↓
              </button>
            </span>
          )}
          <button className="icon" title="Удалить" onClick={() => remove(i)}>
            ×
          </button>
        </div>
      ))}
      {addLabel && (
        <button className="small" onClick={() => onChange([...items, newItem()])}>
          + {addLabel}
        </button>
      )}
    </div>
  )
}
