import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  useReactFlow,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { api } from './api.js'
import { CatalogContext, canConnect } from './catalog.js'
import BlockNode from './components/BlockNode.jsx'
import Inspector from './components/Inspector.jsx'
import BottomPanel from './components/BottomPanel.jsx'
import ProjectSettings from './components/ProjectSettings.jsx'
import { LoginScreen, MembersModal, UserMenu } from './components/Auth.jsx'
import DeployModal from './components/DeployModal.jsx'
import { DEFAULT_FIREWALL, normalizeFirewall } from './components/FirewallTab.jsx'
import { newId } from './components/ProjectSettings.jsx'

// Переименования между двумя версиями моделей -> функция, обновляющая данные блока «База данных».
function renameRefs(before, after) {
  const modelNames = {}
  const fieldNames = {} // по новому имени модели: {старое поле: новое}
  for (const m of after) {
    const old = before.find((x) => x.id && x.id === m.id)
    if (!old) continue
    if (old.name !== m.name) modelNames[old.name] = m.name
    for (const f of m.fields || []) {
      const of = (old.fields || []).find((x) => x.id && x.id === f.id)
      if (of && of.name !== f.name && of.name) (fieldNames[m.name] ||= {})[of.name] = f.name
    }
  }
  if (!Object.keys(modelNames).length && !Object.keys(fieldNames).length) return null
  return (d) => {
    const model = modelNames[d.model] || d.model
    const map = fieldNames[model] || {}
    const rn = (name) => map[name] || name
    return {
      ...d,
      model,
      values: (d.values || []).map((v) => ({ ...v, field: rn(v.field) })),
      filters: (d.filters || []).map((v) => ({ ...v, field: rn(v.field) })),
      include: (d.include || []).map(rn),
      order_by: d.order_by ? rn(d.order_by) : d.order_by,
    }
  }
}

// постоянные id у моделей и полей: по ним миграции отличают переименование от «удалить и создать»
const withIds = (models) =>
  models.map((m) => ({ ...m, id: m.id || newId(), fields: (m.fields || []).map((f) => ({ ...f, id: f.id || newId() })) }))

const uid = () => Math.random().toString(36).slice(2, 9)

// Поля, гарантированно доступные на входе блока: пересечение по всем входящим путям
// (в общий блок могут сходиться ветки с разным набором полей).
function producedFields(n) {
  const out = []
  if (n.type === 'endpoint') for (const m of (n.data.path || '').matchAll(/{(\w+)}/g)) out.push(m[1])
  if (n.type === 'input') (n.data.fields || []).forEach((f) => f.name && out.push(f.name))
  if (n.type === 'logic') (n.data.assignments || []).forEach((a) => a.target && out.push(a.target))
  if (n.type === 'db' && n.data.target) out.push(n.data.target)
  if (n.type === 'db' && n.data.op === 'list' && n.data.count_target) out.push(n.data.count_target)
  if (n.type === 'code') (n.data.outputs || []).forEach((o) => o && out.push(o))
  return out
}

function fieldsBefore(nodeId, nodes, edges) {
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]))
  const memo = new Map()
  const at = (id, stack) => {
    if (memo.has(id)) return memo.get(id)
    const parents = [...new Set(edges.filter((e) => e.target === id).map((e) => e.source))].filter(
      (p) => byId[p] && !stack.has(p),
    )
    let result = null
    for (const p of parents) {
      const s = new Set([...at(p, new Set([...stack, p])), ...producedFields(byId[p])])
      result = result ? new Set([...result].filter((f) => s.has(f))) : s
    }
    memo.set(id, result || new Set())
    return memo.get(id)
  }
  return [...at(nodeId, new Set([nodeId]))]
}

// Только то, что сохраняется: без выделения, размеров и флагов перетаскивания.
const cleanNode = ({ selected, dragging, measured, resizing, ...n }) => n // eslint-disable-line no-unused-vars
const cleanEdge = ({ selected, ...e }) => e // eslint-disable-line no-unused-vars

// Автораскладка: каждый эндпоинт — строка слева направо, ветки условий — друг под другом.
function autoLayout(nodes, edges) {
  const COL = 290
  const ROW = 190
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]))
  const out = (id, handle) => edges.find((e) => e.source === id && (handle === undefined || (e.sourceHandle ?? null) === handle))?.target
  const pos = {}
  let row = 0
  const place = (id, depth, r, stack) => {
    // возвращает, сколько строк заняла ветка
    if (!id || !byId[id] || stack.has(id)) return 1
    if (pos[id]) return 1 // общий блок уже размещён
    pos[id] = { x: 40 + depth * COL, y: 40 + r * ROW }
    const next = new Set([...stack, id])
    if (byId[id].type === 'condition') {
      const a = place(out(id, 'true'), depth + 1, r, next)
      const b = place(out(id, 'false'), depth + 1, r + a, next)
      return a + b
    }
    return place(out(id, null) ?? out(id), depth + 1, r, next)
  }
  const endpoints = nodes.filter((n) => n.type === 'endpoint').sort((a, b) => a.position.y - b.position.y || a.position.x - b.position.x)
  for (const ep of endpoints) row += place(ep.id, 0, row, new Set())
  let x = 0
  for (const n of nodes) {
    if (!pos[n.id]) {
      pos[n.id] = { x: 40 + x * COL, y: 40 + row * ROW + 40 }
      x++
    }
  }
  return nodes.map((n) => ({ ...n, position: pos[n.id] }))
}

// Блоки, скрытые свёрнутыми эндпоинтами (если блок нужен и развёрнутому эндпоинту — он виден).
function hiddenByCollapse(nodes, edges) {
  const reach = (start) => {
    const seen = new Set()
    const queue = [start]
    while (queue.length) {
      const id = queue.shift()
      if (seen.has(id)) continue
      seen.add(id)
      edges.filter((e) => e.source === id).forEach((e) => queue.push(e.target))
    }
    seen.delete(start)
    return seen
  }
  const visible = new Set()
  const hidden = new Set()
  const counts = {}
  for (const ep of nodes.filter((n) => n.type === 'endpoint')) {
    const r = reach(ep.id)
    if (ep.data.collapsed) {
      counts[ep.id] = r.size
      r.forEach((id) => hidden.add(id))
    } else r.forEach((id) => visible.add(id))
  }
  visible.forEach((id) => hidden.delete(id))
  return { hidden, counts }
}

function Palette({ catalog }) {
  const onDragStart = (e, type) => {
    e.dataTransfer.setData('application/dragapi', type)
    e.dataTransfer.effectAllowed = 'move'
  }
  return (
    <aside className="palette">
      <div className="section-title">Блоки</div>
      {Object.entries(catalog.blocks).map(([type, spec]) => (
        <div
          key={type}
          className="palette-item"
          draggable
          onDragStart={(e) => onDragStart(e, type)}
          style={{ '--accent': spec.color }}
        >
          <div className="palette-title">{spec.title}</div>
          <div className="muted">{spec.description}</div>
        </div>
      ))}
      <div className="section-title">Типы связей</div>
      {Object.entries(catalog.port_types).map(([k, v]) => (
        <div key={k} className="muted port-type">
          <code>{k}</code> — {v}
        </div>
      ))}
    </aside>
  )
}

function Editor({ catalog, projectId, onProjectsChanged, me }) {
  const [role, setRole] = useState('owner')
  const readOnly = role === 'viewer'
  const [membersOpen, setMembersOpen] = useState(false)
  const [deployOpen, setDeployOpen] = useState(false)
  const [name, setName] = useState('')
  const [nodes, setNodes] = useState([])
  const [edges, setEdges] = useState([])
  const [models, setModels] = useState([])
  const [env, setEnv] = useState([])
  const [sandbox, setSandbox] = useState({})
  const [libraries, setLibraries] = useState({ packages: [], only_binary: true })
  const [firewall, setFirewall] = useState(DEFAULT_FIREWALL)
  const [tests, setTests] = useState([])
  const [bottomTab, setBottomTab] = useState('tests')
  const [focusEndpoint, setFocusEndpoint] = useState(null)
  const [debug, setDebug] = useState(null) // {steps, current} — подсветка трассы тестового запроса
  const [settingsOpen, setSettingsOpen] = useState(null) // null | имя вкладки настроек проекта
  const [sandboxInfo, setSandboxInfo] = useState(null)
  const [validateNonce, setValidateNonce] = useState(0) // перепроверить граф (например, после установки библиотек)
  // итоговое описание песочницы по настройкам проекта (в т.ч. несохранённым)
  useEffect(() => {
    const t = setTimeout(() => api.sandboxDescribe(sandbox, firewall).then(setSandboxInfo).catch(() => {}), 200)
    return () => clearTimeout(t)
  }, [sandbox, firewall])
  // выделение хранит сам React Flow (n.selected): Shift — рамка, Ctrl/⌘ — добавить к выделению
  const setSelectedId = useCallback(
    (id) => setNodes((ns) => ns.map((n) => ((n.selected ?? false) === (n.id === id) ? n : { ...n, selected: n.id === id }))),
    [],
  )
  const [validation, setValidation] = useState({ ok: true, issues: [], routes: [] })
  const [dirty, setDirty] = useState(false)
  const [running, setRunning] = useState(null) // {base_url, docs_url, routes}
  const [toast, setToast] = useState(null)
  const [activeNodes, setActiveNodes] = useState([])
  const [loaded, setLoaded] = useState(false)
  const { screenToFlowPosition, fitView, setCenter, getNode, getZoom } = useReactFlow()
  const wrapper = useRef(null)

  const toastTimer = useRef(null)
  const notify = useCallback((text, kind = 'info') => {
    setToast({ text, kind })
    clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 3500)
  }, [])

  // загрузка проекта
  useEffect(() => {
    let cancelled = false
    setLoaded(false)
    api.project(projectId).then((p) => {
      if (cancelled) return
      setName(p.name)
      setNodes(p.nodes || [])
      setEdges(p.edges || [])
      setModels(withIds(p.models || []))
      setEnv(p.env || [])
      setSandbox(p.sandbox || {})
      setLibraries({ packages: [], only_binary: true, ...(p.libraries || {}) })
      setFirewall(normalizeFirewall(p.firewall, p.sandbox?.network))
      setTests(p.tests || [])
      setRole(p.my_role || 'owner')
      setDebug(null)
      setSelectedId(null)
      setDirty(false)
      setRunning(p.running ? { base_url: `/run/${projectId}`, docs_url: `/run/${projectId}/docs` } : null)
      setLoaded(true)
      if ((p.nodes || []).length) setTimeout(() => fitView({ padding: 0.15, maxZoom: 1 }), 50)
    })
    return () => {
      cancelled = true
    }
  }, [projectId, fitView])

  const lastRoutes = useRef([])
  // ключ без выделения/размеров: выделение блока не должно перепроверять граф и помечать проект изменённым
  const nodesKey = JSON.stringify(nodes.map(cleanNode))
  const edgesKey = JSON.stringify(edges.map(cleanEdge))
  const graph = useMemo(
    () => ({ name, nodes: JSON.parse(nodesKey), edges: JSON.parse(edgesKey), models, env, sandbox, libraries, firewall, tests }),
    [name, nodesKey, edgesKey, models, env, sandbox, libraries, firewall, tests],
  )

  // живая проверка графа на бэкенде
  useEffect(() => {
    if (!loaded) return
    const t = setTimeout(() => {
      api
        .validate(graph, projectId)
        .then((v) => {
          if (v.routes) lastRoutes.current = v.routes
          setValidation(v)
        })
        .catch(() => {})
    }, 300)
    return () => clearTimeout(t)
  }, [graph, loaded, projectId, validateNonce])

  const save = useCallback(async () => {
    await api.save(projectId, graph)
    setDirty(false)
    onProjectsChanged()
  }, [projectId, graph, onProjectsChanged])

  // Ctrl+S
  useEffect(() => {
    const h = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault()
        if (readOnly) return
        save().then(() => notify('Сохранено'))
      }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [save, notify, readOnly])

  const run = async () => {
    try {
      await save()
      const r = await api.run(projectId)
      setRunning(r)
      notify(`Запущено: ${r.routes.length} маршрут(ов)`, 'ok')
    } catch (e) {
      if (e.data?.detail?.issues) setValidation({ ok: false, issues: e.data.detail.issues })
      notify(`Не запущено: ${e.message}`, 'error')
    }
  }

  const stop = async () => {
    await api.stop(projectId)
    setRunning(null)
  }

  const exportZip = async () => {
    try {
      await save()
      const res = await fetch(api.exportUrl(projectId))
      if (!res.ok) throw new Error((await res.json()).detail?.message || 'ошибка экспорта')
      const blob = await res.blob()
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `${projectId}.zip`
      a.click()
      URL.revokeObjectURL(a.href)
    } catch (e) {
      notify(`Экспорт невозможен: ${e.message}`, 'error')
    }
  }

  const markDirty = () => setDirty(true)

  const onNodesChange = useCallback((changes) => {
    setNodes((ns) => applyNodeChanges(changes, ns))
    if (changes.some((c) => c.type !== 'select' && c.type !== 'dimensions' && !(c.type === 'position' && !c.dragging && !c.position)))
      setDirty(true)
  }, [])
  const onEdgesChange = useCallback((changes) => {
    setEdges((es) => applyEdgeChanges(changes, es))
    if (changes.some((c) => c.type !== 'select')) setDirty(true)
  }, [])

  const isValidConnection = useCallback(
    (c) => {
      const src = nodes.find((n) => n.id === c.source)
      const dst = nodes.find((n) => n.id === c.target)
      if (!src || !dst || src.id === dst.id) return false
      // из одного выхода — одна связь (у «Условия» два выхода: true/false);
      // входов может быть несколько — ветки сходятся в общий блок
      const sameOutput = (e) => e.source === c.source && (e.sourceHandle ?? null) === (c.sourceHandle ?? null)
      if (edges.some(sameOutput)) return false
      return canConnect(catalog, src.type, dst.type)
    },
    [nodes, edges, catalog],
  )

  const onConnect = useCallback((c) => {
    setEdges((es) => addEdge({ ...c, id: `e-${uid()}`, animated: false }, es))
    setDirty(true)
  }, [])

  const onConnectEnd = useCallback(
    (_, state) => {
      if (state.isValid === false && state.toNode) {
        const from = catalog.blocks[state.fromNode?.type]?.title
        const to = catalog.blocks[state.toNode.type]?.title
        notify(`«${from}» нельзя подключить к «${to}»: типы данных несовместимы или этот выход уже занят`, 'error')
      }
    },
    [catalog, notify],
  )

  const onDrop = useCallback(
    (e) => {
      e.preventDefault()
      const type = e.dataTransfer.getData('application/dragapi')
      if (!type || !catalog.blocks[type]) return
      const position = screenToFlowPosition({ x: e.clientX, y: e.clientY })
      const id = `${type}-${uid()}`
      const data = structuredClone(catalog.blocks[type].defaults)
      setNodes((ns) => [...ns, { id, type, position, data }])
      setSelectedId(id)
      setDirty(true)
    },
    [catalog, screenToFlowPosition],
  )

  const updateNodeData = (id, data) => {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data } : n)))
    markDirty()
  }

  // ---------- история (Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y) ----------
  const history = useRef({ past: [], future: [], last: null, restoring: false })
  const snapshot = () => JSON.stringify({ nodes: nodes.map(cleanNode), edges: edges.map(cleanEdge) })
  useEffect(() => {
    history.current = { past: [], future: [], last: null, restoring: false }
  }, [projectId])
  useEffect(() => {
    if (!loaded) return
    const h = history.current
    const snap = JSON.stringify({ nodes: JSON.parse(nodesKey), edges: JSON.parse(edgesKey) })
    if (h.last === null || h.restoring) {
      h.last = snap
      h.restoring = false
      return
    }
    if (snap === h.last || nodes.some((n) => n.dragging)) return
    // быстрые правки (набор текста) объединяются в один шаг
    const t = setTimeout(() => {
      h.past.push(h.last)
      if (h.past.length > 100) h.past.shift()
      h.future = []
      h.last = snap
      setHistoryTick((x) => x + 1)
    }, 400)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodesKey, edgesKey, loaded])
  const [, setHistoryTick] = useState(0)

  const restore = (snap) => {
    const { nodes: ns, edges: es } = JSON.parse(snap)
    history.current.restoring = true
    setNodes(ns)
    setEdges(es)
    setDirty(true)
    setHistoryTick((x) => x + 1)
  }
  const undo = () => {
    const h = history.current
    const cur = snapshot()
    if (cur !== h.last && h.last !== null) {
      h.past.push(h.last) // ещё не записанная правка
      h.last = cur
    }
    if (!h.past.length) return
    h.future.push(h.last)
    const prev = h.past.pop()
    h.last = prev
    restore(prev)
  }
  const redo = () => {
    const h = history.current
    if (!h.future.length) return
    h.past.push(h.last)
    const next = h.future.pop()
    h.last = next
    restore(next)
  }

  // ---------- копирование / вставка / дублирование ----------
  const pasteCount = useRef(0)
  const copySelection = () => {
    const sel = nodes.filter((n) => n.selected).map(cleanNode)
    if (!sel.length) return false
    const ids = new Set(sel.map((n) => n.id))
    const clip = { nodes: sel, edges: edges.filter((e) => ids.has(e.source) && ids.has(e.target)).map(cleanEdge) }
    try {
      localStorage.setItem('dragapi.clipboard', JSON.stringify(clip)) // работает и между проектами
    } catch {
      /* без хранилища — только в пределах вкладки */
    }
    window.__dragapiClipboard = clip
    pasteCount.current = 0
    return true
  }
  const paste = () => {
    let clip = window.__dragapiClipboard
    try {
      clip = JSON.parse(localStorage.getItem('dragapi.clipboard')) || clip
    } catch {
      /* берём из памяти вкладки */
    }
    if (!clip?.nodes?.length) return
    pasteCount.current += 1
    const shift = 40 * pasteCount.current
    const map = Object.fromEntries(clip.nodes.map((n) => [n.id, `${n.type}-${uid()}`]))
    const newNodes = clip.nodes.map((n) => ({
      ...structuredClone(n),
      id: map[n.id],
      position: { x: n.position.x + shift, y: n.position.y + shift },
      selected: true,
    }))
    const newEdges = clip.edges.map((e) => ({ ...e, id: `e-${uid()}`, source: map[e.source], target: map[e.target] }))
    setNodes((ns) => [...ns.map((n) => (n.selected ? { ...n, selected: false } : n)), ...newNodes])
    setEdges((es) => [...es, ...newEdges])
    markDirty()
    notify(`Вставлено блоков: ${newNodes.length}`)
  }

  useEffect(() => {
    const h = (e) => {
      const el = document.activeElement
      const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(el?.tagName) || el?.isContentEditable
      if (typing || readOnly || !(e.ctrlKey || e.metaKey)) return
      const k = e.key.toLowerCase()
      if (k === 'z' && !e.shiftKey) (e.preventDefault(), undo())
      else if ((k === 'z' && e.shiftKey) || k === 'y') (e.preventDefault(), redo())
      else if (k === 'c') copySelection()
      else if (k === 'v') (e.preventDefault(), paste())
      else if (k === 'd') (e.preventDefault(), copySelection() && paste())
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  })

  const arrange = () => {
    setNodes((ns) => autoLayout(ns, edges))
    markDirty()
    setTimeout(() => fitView({ padding: 0.15, maxZoom: 1, duration: 300 }), 50)
  }

  const collapse = useMemo(() => hiddenByCollapse(nodes, edges), [nodesKey, edgesKey]) // eslint-disable-line react-hooks/exhaustive-deps
  const toggleCollapse = useCallback((id) => {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, collapsed: !n.data.collapsed } } : n)))
    setDirty(true)
  }, [])

  const deleteNode = (id) => {
    setNodes((ns) => ns.filter((n) => n.id !== id))
    setEdges((es) => es.filter((e) => e.source !== id && e.target !== id))
    setSelectedId(null)
    markDirty()
  }

  const issuesByNode = useMemo(() => {
    const m = {}
    for (const i of validation.issues || []) if (i.node_id) (m[i.node_id] ||= []).push(i)
    return m
  }, [validation])

  // Ноды для отображения: добавляем проблемы и подсветку из логов, не трогая сохраняемые данные.
  // трасса тестового запроса: пройденные блоки и текущий шаг
  const debugVisited = useMemo(() => new Set((debug?.steps || []).slice(0, (debug?.current ?? -1) + 1).map((s) => s.node_id)), [debug])
  const debugStep = debug ? debug.steps[debug.current] : null

  // канвас следует за текущим шагом трассы (карточка шага — над блоком, поэтому центр чуть выше)
  useEffect(() => {
    const node = debugStep && getNode(debugStep.node_id)
    if (!node) return
    const w = node.measured?.width ?? 230
    const h = node.measured?.height ?? 80
    setCenter(node.position.x + w / 2, node.position.y + h / 2 - 40, { zoom: Math.max(getZoom(), 0.9), duration: 300 })
  }, [debugStep, getNode, setCenter, getZoom])

  const viewNodes = useMemo(
    () =>
      nodes.map((n) => ({
        ...n,
        hidden: collapse.hidden.has(n.id),
        data: {
          ...n.data,
          __issues: issuesByNode[n.id],
          __active: activeNodes.includes(n.id) || debugVisited.has(n.id),
          __step: debugStep && debugStep.node_id === n.id ? debugStep : null,
          __hiddenCount: collapse.counts[n.id],
          __toggleCollapse: n.type === 'endpoint' ? () => toggleCollapse(n.id) : undefined,
        },
      })),
    [nodes, issuesByNode, activeNodes, debugVisited, debugStep, collapse, toggleCollapse],
  )

  // подписи «да»/«нет» на связях из условия
  const viewEdges = useMemo(() => {
    // связи, по которым прошёл тестовый запрос (до текущего шага)
    const passed = new Set()
    const ids = (debug?.steps || []).slice(0, (debug?.current ?? -1) + 1).map((s) => s.node_id)
    for (let i = 1; i < ids.length; i++) passed.add(`${ids[i - 1]}>${ids[i]}`)
    return edges.map((e) => {
      const branch = e.sourceHandle === 'true' || e.sourceHandle === 'false'
      const onPath = passed.has(`${e.source}>${e.target}`)
      return {
        ...e,
        animated: onPath,
        className: [branch ? `branch-${e.sourceHandle}` : '', onPath ? 'on-path' : ''].join(' '),
        ...(branch ? { label: e.sourceHandle === 'true' ? 'да' : 'нет', labelBgPadding: [4, 2] } : {}),
      }
    })
  }, [edges, debug])

  const nodeTypes = useMemo(
    () => Object.fromEntries(Object.keys(catalog.blocks).map((t) => [t, BlockNode])),
    [catalog],
  )

  const selectedNodes = nodes.filter((n) => n.selected)
  const selected = selectedNodes.length === 1 ? selectedNodes[0] : null
  const errors = (validation.issues || []).filter((i) => i.level === 'error').length

  return (
    <div className="editor">
      <header className="toolbar">
        {readOnly && <span className="readonly-badge" title="Изменения не сохраняются">👁 Только просмотр</span>}
        <input
          className="project-name"
          disabled={readOnly}
          value={name}
          onChange={(e) => {
            setName(e.target.value)
            markDirty()
          }}
        />
        <span className={`status ${errors ? 'bad' : 'good'}`}>
          {errors ? `Ошибок: ${errors}` : `Граф корректен · маршрутов: ${validation.routes?.length ?? 0}`}
        </span>
        <div className="spacer" />
        {!readOnly && (
          <span className="toolbar-group">
            <button className="small" onClick={undo} disabled={!history.current.past.length && snapshot() === history.current.last} title="Отменить (Ctrl+Z)">
              ↶
            </button>
            <button className="small" onClick={redo} disabled={!history.current.future.length} title="Повторить (Ctrl+Shift+Z)">
              ↷
            </button>
            <button className="small" onClick={arrange} title="Расставить блоки: эндпоинт — строка, ветки — друг под другом">
              ⊞ Упорядочить
            </button>
          </span>
        )}
        <button
          onClick={() => setSettingsOpen('models')}
          title="Модели данных, переменные окружения, песочница и библиотеки"
          className={(validation.issues || []).some((i) => !i.node_id) ? 'has-issues' : ''}
        >
          ⚙ Настройки проекта
        </button>
        {me && role === 'owner' && (
          <button onClick={() => setMembersOpen(true)} title="Кто имеет доступ к проекту">
            👥 Доступ
          </button>
        )}
        <button onClick={() => save().then(() => notify('Сохранено'))} disabled={!dirty || readOnly}>
          {dirty ? 'Сохранить' : 'Сохранено'}
        </button>
        <button className="primary" onClick={run} disabled={errors > 0 || readOnly}>
          {running ? 'Перезапустить' : '▶ Запустить'}
        </button>
        {running && (
          <>
            <a className="button" href={running.docs_url} target="_blank" rel="noreferrer">
              Swagger ↗
            </a>
            <button onClick={stop} disabled={readOnly}>
              ■ Стоп
            </button>
          </>
        )}
        <button onClick={exportZip} disabled={errors > 0} title="Скачать обычное FastAPI-приложение (с Dockerfile)">
          Экспорт в FastAPI
        </button>
        <button onClick={() => setDeployOpen(true)} title="Собрать образ и запустить контейнер на этой машине">
          🚀 Деплой
        </button>
      </header>

      <div className="workspace">
        {!readOnly && <Palette catalog={catalog} />}
        <div className="canvas" ref={wrapper} onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
          <ReactFlow
            nodes={viewNodes}
            edges={viewEdges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onConnectEnd={onConnectEnd}
            isValidConnection={isValidConnection}
            selectionOnDrag={false}
            multiSelectionKeyCode={['Control', 'Meta']}
            deleteKeyCode={readOnly ? null : ['Delete', 'Backspace']}
            nodesDraggable={!readOnly}
            nodesConnectable={!readOnly}
            defaultEdgeOptions={{ style: { strokeWidth: 2 } }}
            colorMode="system"
            defaultViewport={{ x: 0, y: 0, zoom: 1 }}
          >
            <Background gap={20} />
            <Controls />
            <MiniMap pannable zoomable nodeColor={(n) => catalog.blocks[n.type]?.color || '#999'} />
          </ReactFlow>
          {toast && <div className={`toast ${toast.kind}`}>{toast.text}</div>}
        </div>
        <Inspector
          readOnly={readOnly}
          multiCount={selectedNodes.length > 1 ? selectedNodes.length : 0}
          node={selected}
          issues={selected ? issuesByNode[selected.id] || [] : []}
          availableFields={selected ? fieldsBefore(selected.id, nodes, edges) : []}
          models={models}
          env={env}
          sandbox={sandbox}
          firewall={firewall}
          projectId={projectId}
          sandboxInfo={sandboxInfo}
          onOpenSettings={(tab) => setSettingsOpen(tab || 'models')}
          onTestEndpoint={(id) => {
            setFocusEndpoint(null)
            setTimeout(() => setFocusEndpoint(id))
            setBottomTab('tests')
          }}
          onChange={(data) => updateNodeData(selected.id, data)}
          onDelete={() => deleteNode(selected.id)}
        />
      </div>

      <BottomPanel
        projectId={projectId}
        running={running}
        issues={validation.issues || []}
        onSelectNode={setSelectedId}
        onHighlight={setActiveNodes}
        onRestored={() => onProjectsChanged(true)}
        models={models}
        tab={bottomTab}
        setTab={setBottomTab}
        testsProps={{
          graph,
          routes: validation.routes || lastRoutes.current,
          focusEndpoint,
          tests,
          setTests: (t) => {
            setTests(t)
            markDirty()
          },
          onDebug: setDebug,
          readOnly,
        }}
      />
      {deployOpen && (
        <DeployModal projectId={projectId} readOnly={readOnly} dirty={dirty} onClose={() => setDeployOpen(false)} />
      )}
      {membersOpen && <MembersModal projectId={projectId} me={me} onClose={() => setMembersOpen(false)} />}
      {settingsOpen && (
        <ProjectSettings
          readOnly={readOnly}
          projectId={projectId}
          initialTab={settingsOpen}
          project={{ models, env, sandbox, libraries, firewall }}
          setProject={(patch) => {
            if (patch.models) {
              // переименования моделей и полей (по постоянным id) переносим в блоки «База данных»
              const renamed = renameRefs(models, patch.models)
              if (renamed) setNodes((ns) => ns.map((n) => (n.type === 'db' ? { ...n, data: renamed(n.data) } : n)))
              setModels(patch.models)
            }
            if (patch.env) setEnv(patch.env)
            if (patch.sandbox) setSandbox(patch.sandbox)
            if (patch.libraries) setLibraries(patch.libraries)
            if (patch.firewall) setFirewall(patch.firewall)
            markDirty()
          }}
          sandboxInfo={sandboxInfo}
          issues={(validation.issues || []).filter((i) => !i.node_id)}
          onInstalled={() => setValidateNonce((n) => n + 1)}
          onClose={() => setSettingsOpen(null)}
        />
      )}
    </div>
  )
}

export default function App() {
  const [session, setSession] = useState(null) // {auth, user, setup_required}
  useEffect(() => {
    const check = () => api.me().then(setSession).catch(() => setSession({ error: true }))
    check()
    window.addEventListener('dragapi:unauthorized', check)
    return () => window.removeEventListener('dragapi:unauthorized', check)
  }, [])
  if (!session) return <div className="fatal">Загрузка…</div>
  if (session.error) return <div className="fatal">Бэкенд недоступен. Запустите: cd backend && .venv/bin/uvicorn app.main:app --port 8765</div>
  if (session.auth && !session.user) {
    return (
      <LoginScreen
        setup={session.setup_required}
        onDone={(user) => setSession({ ...session, user, setup_required: false })}
      />
    )
  }
  return <Workspace key={session.user?.id || 'local'} me={session.user} onLogout={() => setSession({ ...session, user: null })} />
}

function Workspace({ me, onLogout }) {
  const [catalog, setCatalog] = useState(null)
  const [projects, setProjects] = useState([])
  const [projectId, setProjectId] = useState(() => {
    try {
      return localStorage.getItem('dragapi.project')
    } catch {
      return null
    }
  })
  const [reloadKey, setReloadKey] = useState(0)
  const [error, setError] = useState(null)

  const loadProjects = useCallback(
    (reloadEditor = false) =>
      api.projects().then((ps) => {
        setProjects(ps)
        if (reloadEditor) setReloadKey((k) => k + 1)
        return ps
      }),
    [],
  )

  useEffect(() => {
    Promise.all([api.blocks(), loadProjects()])
      .then(([c, ps]) => {
        setCatalog(c)
        setProjectId((cur) => (ps.some((p) => p.id === cur) ? cur : ps[0]?.id || null))
      })
      .catch(() => setError('Бэкенд недоступен. Запустите: cd backend && .venv/bin/uvicorn app.main:app --port 8765'))
  }, [loadProjects])

  useEffect(() => {
    try {
      if (projectId) localStorage.setItem('dragapi.project', projectId)
    } catch {
      /* хранилище недоступно — не страшно */
    }
  }, [projectId])

  const createProject = async () => {
    const name = prompt('Название проекта', 'Новый проект')
    if (!name) return
    const p = await api.createProject(name)
    await loadProjects()
    setProjectId(p.id)
  }

  const deleteProject = async () => {
    if (!projectId || !confirm('Удалить проект? Это действие необратимо.')) return
    await api.remove(projectId)
    const ps = await loadProjects()
    setProjectId(ps[0]?.id || null)
  }

  if (error) return <div className="fatal">{error}</div>
  if (!catalog) return <div className="fatal">Загрузка…</div>

  return (
    <CatalogContext.Provider value={catalog}>
      <div className="app">
        <nav className="topbar">
          <div className="logo">
            Drag<span>API</span>
          </div>
          <select value={projectId || ''} onChange={(e) => setProjectId(e.target.value)}>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
                {p.role && p.role !== 'owner' ? ` (${{ editor: 'редактор', viewer: 'просмотр' }[p.role]})` : ''}
              </option>
            ))}
          </select>
          <button className="small" onClick={createProject}>
            + Проект
          </button>
          {projectId && (!me || projects.find((p) => p.id === projectId)?.role === 'owner') && (
            <button className="small danger" onClick={deleteProject}>
              Удалить
            </button>
          )}
          <div className="spacer" />
          {me && <UserMenu me={me} onLogout={onLogout} />}
        </nav>
        {projectId ? (
          <ReactFlowProvider key={`${projectId}-${reloadKey}`}>
            <Editor catalog={catalog} projectId={projectId} onProjectsChanged={loadProjects} me={me} />
          </ReactFlowProvider>
        ) : (
          <div className="fatal">
            Проектов пока нет. <button onClick={createProject}>Создать проект</button>
          </div>
        )}
      </div>
    </CatalogContext.Provider>
  )
}
