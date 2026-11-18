import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

// Блоки «Зрение»: снимок экрана агента, на котором мышкой выделяют образец / область или выбирают точку.

const lastShots = {} // проект → последний снимок {data, w, h, ox, oy, t} — чтобы не снимать заново для каждого блока

async function waitRequest(projectId, id, onLeft) {
  for (let i = 0; i < 240; i++) {
    const s = await api.agentRequest(projectId, id)
    if (s.error) throw new Error(s.error)
    if (s.done) return s
    onLeft?.(s.left)
    await new Promise((r) => setTimeout(r, 250))
  }
  throw new Error('агент не ответил')
}

const MODE_TEXT = {
  image: 'Выделите мышкой то, что нужно искать: кнопку, иконку, надпись. Лучше небольшой уникальный фрагмент.',
  region: 'Выделите мышкой область экрана.',
  point: 'Щёлкните по точке — запишутся координаты и её цвет.',
}

/** Окно со снимком экрана: выделение прямоугольника (образец / область) или выбор точки. */
export function ScreenPicker({ projectId, agent, mode, initial, relative = false, onDone, onClose }) {
  const [shot, setShot] = useState(lastShots[projectId] || null)
  const [busy, setBusy] = useState(null) // null | {left}
  const [error, setError] = useState(null)
  const [fit, setFit] = useState(true)
  const [sel, setSel] = useState(null) // в координатах снимка {x, y, w, h}
  const [point, setPoint] = useState(null) // {x, y, hex}
  const img = useRef(null)
  const drag = useRef(null)

  useEffect(() => {
    const h = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])
  useEffect(() => {
    if (shot && initial && mode !== 'point' && !relative) {
      setSel({ x: initial.x - shot.ox, y: initial.y - shot.oy, w: initial.w, h: initial.h })
    }
  }, [shot]) // eslint-disable-line react-hooks/exhaustive-deps

  const take = async (countdown) => {
    setError(null)
    setBusy({ left: countdown })
    setSel(null)
    setPoint(null)
    try {
      const req = await api.screenshot(projectId, { agent, countdown })
      const s = await waitRequest(projectId, req.id, (left) => setBusy({ left }))
      if (!s.data) throw new Error('снимок уже забрали — снимите ещё раз')
      const next = { data: s.data, w: s.w, h: s.h, ox: s.ox || 0, oy: s.oy || 0, windows: s.windows || [], t: Date.now() }
      lastShots[projectId] = next
      setShot(next)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  const toImage = (e) => {
    const r = img.current.getBoundingClientRect()
    const k = shot.w / r.width
    return {
      x: Math.max(0, Math.min(shot.w - 1, Math.round((e.clientX - r.left) * k))),
      y: Math.max(0, Math.min(shot.h - 1, Math.round((e.clientY - r.top) * k))),
    }
  }
  const pixelAt = (x, y) => {
    const c = document.createElement('canvas')
    c.width = c.height = 1
    const ctx = c.getContext('2d')
    ctx.drawImage(img.current, x, y, 1, 1, 0, 0, 1, 1)
    const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data
    return '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')
  }
  const onDown = (e) => {
    if (!shot || e.button !== 0) return
    e.preventDefault()
    const p = toImage(e)
    if (mode === 'point') {
      setPoint({ ...p, hex: pixelAt(p.x, p.y) })
      return
    }
    drag.current = p
    setSel({ x: p.x, y: p.y, w: 0, h: 0 })
  }
  const onMove = (e) => {
    if (!drag.current) return
    const p = toImage(e)
    const a = drag.current
    setSel({ x: Math.min(a.x, p.x), y: Math.min(a.y, p.y), w: Math.abs(p.x - a.x) + 1, h: Math.abs(p.y - a.y) + 1 })
  }
  const onUp = () => {
    drag.current = null
    setSel((s) => (s && s.w < 4 && s.h < 4 ? null : s))
  }

  // «внутри окна»: координаты — от левого верхнего угла окна, в котором выделение / точка
  const anchor = mode === 'point' ? point : sel
  const win =
    relative && anchor && shot
      ? (shot.windows || []).find((w) => {
          const x = anchor.x + shot.ox
          const y = anchor.y + shot.oy
          return x >= w.x && x < w.x + w.w && y >= w.y && y < w.y + w.h
        })
      : null
  const confirm = () => {
    const dx = shot.ox - (win ? win.x : 0)
    const dy = shot.oy - (win ? win.y : 0)
    if (mode === 'point') return onDone({ x: point.x + dx, y: point.y + dy, hex: point.hex })
    const rect = { x: sel.x + dx, y: sel.y + dy, w: sel.w, h: sel.h }
    if (mode === 'region') return onDone(rect)
    const c = document.createElement('canvas')
    c.width = sel.w
    c.height = sel.h
    c.getContext('2d').drawImage(img.current, sel.x, sel.y, sel.w, sel.h, 0, 0, sel.w, sel.h)
    onDone({ image: c.toDataURL('image/png'), rect })
  }
  const ready = (mode === 'point' ? !!point : !!sel && sel.w >= 4 && sel.h >= 4) && (!relative || mode === 'image' || !!win)
  const pct = (v, total) => `${(v / total) * 100}%`

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal screen-picker" role="dialog" aria-label="Снимок экрана">
        <div className="modal-head">
          <div className="inspector-title">📷 Снимок экрана</div>
          <div className="row">
            {shot && (
              <button className="small" onClick={() => setFit(!fit)} title="Масштаб снимка">
                {fit ? '🔍 1:1' : '↔ По ширине'}
              </button>
            )}
            <button type="button" className="icon" onClick={onClose}>
              ×
            </button>
          </div>
        </div>
        <div className="picker-bar">
          <button className="primary" onClick={() => take(3)} disabled={!!busy} title="За 3 секунды переключитесь на нужное окно">
            {busy ? (busy.left ? `Переключитесь на нужное окно… ${busy.left}` : 'Снимаю…') : shot ? '📷 Снять заново (через 3 с)' : '📷 Снять экран (через 3 с)'}
          </button>
          <button onClick={() => take(0)} disabled={!!busy}>
            Сейчас
          </button>
          <span className="muted">
            {shot ? MODE_TEXT[mode] : 'Агент снимет экран своего компьютера. За 3 секунды можно переключиться на нужное окно.'}
            {shot && relative && mode !== 'image' && ' Координаты запишутся относительно окна, в котором выделение.'}
          </span>
        </div>
        {error && <div className="issue error">{error}</div>}
        {shot && (
          <div className={`shot-wrap ${fit ? 'fit' : ''}`}>
            <div className="shot-stage" onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp} onMouseLeave={onUp}>
              <img ref={img} src={shot.data} alt="снимок экрана" draggable={false} style={fit ? { width: '100%' } : { width: shot.w }} />
              {sel && mode !== 'point' && (
                <div className="shot-sel" style={{ left: pct(sel.x, shot.w), top: pct(sel.y, shot.h), width: pct(sel.w, shot.w), height: pct(sel.h, shot.h) }} />
              )}
              {point && <div className="shot-point" style={{ left: pct(point.x, shot.w), top: pct(point.y, shot.h) }} />}
            </div>
          </div>
        )}
        <div className="modal-foot">
          <span className="muted mono">
            {shot && `экран ${shot.w}×${shot.h}`}
            {sel && mode !== 'point' && ` · выделено ${sel.x + shot.ox}, ${sel.y + shot.oy} — ${sel.w}×${sel.h}`}
            {point && (
              <>
                {' '}
                · точка {point.x + shot.ox}, {point.y + shot.oy} <span className="swatch" style={{ background: point.hex }} /> {point.hex}
              </>
            )}
            {relative && mode !== 'image' && anchor && (win ? ` · внутри окна «${win.title}» (${anchor.x + shot.ox - win.x}, ${anchor.y + shot.oy - win.y})` : ' · ⚠ не в окне')}
          </span>
          <div className="row">
            <button onClick={onClose}>Отмена</button>
            <button className="primary" disabled={!ready} onClick={confirm}>
              {mode === 'image' ? 'Взять образец' : mode === 'region' ? 'Взять область' : 'Взять точку'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(r.result)
    r.onerror = () => reject(new Error('не удалось прочитать файл'))
    r.readAsDataURL(file)
  })
}

/** Образец для «Найти картинку»: превью, со снимка экрана или из файла. */
export function ImageField({ value, onChange, projectId, agent, readOnly }) {
  const [picking, setPicking] = useState(false)
  const [size, setSize] = useState(null)
  const [error, setError] = useState(null)
  const file = useRef(null)
  const upload = async (f) => {
    setError(null)
    if (!f) return
    if (!/^image\/(png|jpeg|webp|bmp)$/.test(f.type)) return setError('нужна картинка PNG или JPEG')
    if (f.size > 2_000_000) return setError('картинка больше 2 МБ — вырежьте фрагмент поменьше')
    onChange(await readFile(f))
  }
  return (
    <div className="image-field">
      {value ? (
        <div className="template-preview">
          <img src={value} alt="образец" onLoad={(e) => setSize([e.target.naturalWidth, e.target.naturalHeight])} />
          {size && <span className="muted mono">{size[0]}×{size[1]}</span>}
        </div>
      ) : (
        <div className="template-empty muted">Образца нет</div>
      )}
      <div className="row wrap">
        <button className="small" onClick={() => setPicking(true)} disabled={readOnly}>
          📷 Со снимка экрана
        </button>
        <input ref={file} type="file" accept="image/png,image/jpeg,image/webp,image/bmp" hidden onChange={(e) => upload(e.target.files[0])} />
        <button className="small" onClick={() => file.current?.click()} disabled={readOnly}>
          📂 Из файла
        </button>
        {value && (
          <button className="small" onClick={() => onChange('')} disabled={readOnly} title="Убрать образец">
            ×
          </button>
        )}
      </div>
      {error && <div className="issue error">{error}</div>}
      {picking && (
        <ScreenPicker
          projectId={projectId}
          agent={agent}
          mode="image"
          onClose={() => setPicking(false)}
          onDone={(r) => {
            onChange(r.image)
            setPicking(false)
          }}
        />
      )}
    </div>
  )
}

/** Область экрана {x, y, w, h}: выделить на снимке или вписать числа; пусто — весь экран. */
export function RegionField({ value, onChange, projectId, agent, optional, readOnly, relative = false }) {
  const [picking, setPicking] = useState(false)
  const r = value || null
  const put = (k, v) => {
    const next = { x: 0, y: 0, w: 100, h: 100, ...(r || {}), [k]: v === '' ? '' : Number(v) }
    onChange(next)
  }
  return (
    <div className="region-field">
      <div className="list-row region-row">
        {['x', 'y', 'w', 'h'].map((k) => (
          <input key={k} className="mono" placeholder={{ x: 'X', y: 'Y', w: 'ширина', h: 'высота' }[k]} value={r ? r[k] ?? '' : ''} onChange={(e) => put(k, e.target.value)} disabled={readOnly} />
        ))}
      </div>
      <div className="row wrap">
        <button className="small" onClick={() => setPicking(true)} disabled={readOnly}>
          📷 Выделить на снимке
        </button>
        {optional && r && (
          <button className="small" onClick={() => onChange(null)} disabled={readOnly}>
            Весь экран
          </button>
        )}
        {!r && <span className="muted">{optional ? 'весь экран' : 'не выбрана'}</span>}
      </div>
      {picking && (
        <ScreenPicker
          projectId={projectId}
          agent={agent}
          mode="region"
          initial={r}
          relative={relative}
          onClose={() => setPicking(false)}
          onDone={(rect) => {
            onChange(rect)
            setPicking(false)
          }}
        />
      )}
    </div>
  )
}

/** Цвет #RRGGBB: палитра, текст и «со снимка» (ставит и координаты блока). */
export function ColorField({ value, onChange, onPickPoint, projectId, agent, readOnly, relative = false }) {
  const [picking, setPicking] = useState(false)
  const hex = /^#?[0-9a-f]{6}$/i.test(value || '') ? '#' + String(value).replace('#', '').toLowerCase() : '#000000'
  return (
    <div className="row wrap color-field">
      <input type="color" value={hex} onChange={(e) => onChange(e.target.value)} disabled={readOnly} />
      <input className="mono" value={value ?? ''} onChange={(e) => onChange(e.target.value)} placeholder="#22c55e" disabled={readOnly} />
      <button className="small" onClick={() => setPicking(true)} disabled={readOnly} title="Щёлкнуть по точке на снимке экрана — запишутся её координаты и цвет">
        📷 Точка со снимка
      </button>
      {picking && (
        <ScreenPicker
          projectId={projectId}
          agent={agent}
          mode="point"
          relative={relative}
          onClose={() => setPicking(false)}
          onDone={(p) => {
            onPickPoint(p)
            setPicking(false)
          }}
        />
      )}
    </div>
  )
}

/** «Проверить на экране»: блок выполняется на агенте один раз, без кликов. */
export function VisionTest({ projectId, agent, nodeId, graph }) {
  const [state, setState] = useState(null) // null | 'busy' | {ok, message, vars} | {error}
  const run = async () => {
    setState('busy')
    try {
      const req = await api.visionTest(projectId, { agent, node: nodeId, graph })
      const r = await waitRequest(projectId, req.id)
      setState({ ok: r.status === 'ok', message: r.message, vars: r.vars })
    } catch (e) {
      setState({ error: e.data?.detail?.issues?.map((i) => i.message).join('; ') || e.message })
    }
  }
  const found = state?.vars && Object.values(state.vars).find((v) => v && typeof v === 'object' && 'score' in v)
  return (
    <div className="vision-test">
      <button onClick={run} disabled={state === 'busy'} title="Выполнить блок на текущем экране агента: без кликов и ожидания">
        {state === 'busy' ? 'Проверяю…' : '🧪 Проверить на экране'}
      </button>
      {state?.error && <div className="issue error">{state.error}</div>}
      {state && state !== 'busy' && !state.error && (
        <div className={`issue ${state.ok ? (found && !found.found ? 'warning' : 'ok') : 'error'}`}>
          {state.message}
          {found && (
            <div className="muted mono">
              сходство {found.score} · центр {found.x}, {found.y} · {found.w}×{found.h}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** Заголовок окна: текст с {выражениями} и выбор из открытых окон агента. */
export function WindowTitleField({ value, onChange, onPick, projectId, agent, readOnly }) {
  const [list, setList] = useState(null) // null | 'busy' | [окна] | {error}
  const load = async () => {
    setList('busy')
    try {
      const req = await api.agentWindows(projectId, { agent })
      const r = await waitRequest(projectId, req.id)
      setList(r.items || [])
    } catch (e) {
      setList({ error: e.message })
    }
  }
  return (
    <div className="window-title-field">
      <div className="row">
        <input className="mono" value={value ?? ''} onChange={(e) => onChange(e.target.value)} disabled={readOnly} placeholder="часть заголовка" />
        <button className="small" onClick={load} disabled={readOnly || list === 'busy'} title="Открытые окна на компьютере агента">
          {list === 'busy' ? '…' : '🪟 Из открытых'}
        </button>
      </div>
      {list?.error && <div className="issue error">{list.error}</div>}
      {Array.isArray(list) && (
        <div className="window-list">
          {!list.length && <div className="muted">Открытых окон нет</div>}
          {list.map((w) => (
            <button
              key={w.id}
              className="window-item"
              onClick={() => {
                onPick(w)
                setList(null)
              }}
            >
              <span className="window-title">{w.title || '(без заголовка)'}</span>
              <span className="muted mono">
                {w.process || '—'} · {w.w}×{w.h}
                {w.state !== 'normal' ? ` · ${{ minimized: 'свёрнуто', maximized: 'развёрнуто' }[w.state]}` : ''}
              </span>
            </button>
          ))}
          <button className="small" onClick={() => setList(null)}>
            Скрыть
          </button>
        </div>
      )}
    </div>
  )
}
