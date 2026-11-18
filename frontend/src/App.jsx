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
import { AgentsModal, RecordModal } from './components/Automation.jsx'


const BRANCH_LABELS = {
  true: 'да', false: 'нет', body: 'тело', done: 'дальше', ok: 'ответ', timeout: 'нет ответа', not_found: 'не найден',
  paid: 'оплачено', error: 'ошибка', violation: 'нарушение', now: 'сейчас', later: 'позже',
}
const isStart = (t) => ['endpoint', 'trigger', 'bot_command', 'bot_message', 'bot_file', 'bot_member', 'bot_schedule'].includes(t)
const SPACES = [
  ['api', '🔌 API'],
  ['automation', '🤖 Автоматизация'],
  ['bot', '✈ Боты'],
]

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
  if (n.type === 'scenario' && n.data.target) out.push(n.data.target)
  // автоматизация: переменные сценария
  if (n.type === 'trigger') (n.data.params || []).forEach((p) => p && out.push(p))
  if (n.type === 'set_vars') (n.data.assignments || []).forEach((a) => a.target && out.push(a.target))
  if ((n.type === 'repeat' || n.type === 'while') && n.data.var) out.push(n.data.var)
  if ((n.type === 'http' || n.type === 'autoclick') && n.data.target) out.push(n.data.target)
  // конструктор ботов
  if (['bot_command', 'bot_message', 'bot_file', 'bot_member'].includes(n.type))
    out.push('text', 'args', 'user_name', 'username', 'user_id', 'chat_id', 'saved', 'role', 'is_admin', 'is_group', 'is_chat_admin',
      'chat_title', 'message_id', 'file', 'reply_user_id', 'reply_user_name', 'reply_message_id')
  if (['bot_ask', 'bot_menu', 'bot_role', 'bot_invoice', 'bot_refund', 'bot_filter', 'bot_moderate', 'bot_download'].includes(n.type) && n.data.var) out.push(n.data.var)
  if (n.type === 'bot_schedule') out.push('now')
  if (n.type === 'bot_send' && n.data.to === 'role' && n.data.count_var) out.push(n.data.count_var)
  if (n.type === 'run_scenario' && n.data.target) out.push(n.data.target)
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

// Автораскладка: каждый эндпоинт (или «Старт») — строка слева направо, ветки — друг под другом.
const BRANCHES = {
  condition: ['true', 'false'], tg_ask: ['true', 'false'], bot_ask: ['ok', 'timeout'], bot_role: ['ok', 'not_found'],
  bot_invoice: ['paid', 'timeout'], bot_refund: ['ok', 'error'], bot_filter: ['violation', 'ok'], bot_moderate: ['ok', 'error'],
  bot_download: ['ok', 'error'], bot_delay: ['now', 'later'],
  repeat: ['body', 'done'], while: ['body', 'done'],
}
const branchesOf = (n) => (n.type === 'bot_menu' ? [...(n.data.buttons || []).map((b) => b.id), 'timeout'] : BRANCHES[n.type])
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
    const branches = branchesOf(byId[id])
    if (branches) {
      // ветки (да/нет, тело/дальше) — друг под другом
      let rows = 0
      for (const b of branches) rows += place(out(id, b), depth + 1, r + rows, next)
      return Math.max(rows, 1)
    }
    return place(out(id, null) ?? out(id), depth + 1, r, next)
  }
  const endpoints = nodes.filter((n) => isStart(n.type)).sort((a, b) => a.position.y - b.position.y || a.position.x - b.position.x)
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
  for (const ep of nodes.filter((n) => isStart(n.type))) {
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
      {Object.entries(
        Object.entries(catalog.blocks).reduce((acc, [type, spec]) => {
          ;(acc[spec.group || 'Блоки'] ||= []).push([type, spec])
          return acc
        }, {}),
      ).map(([group, items]) => (
        <div key={group}>
          <div className="section-title">{group}</div>
          {items.map(([type, spec]) => (
            <div
              key={type}
              className="palette-item"
              draggable
              onDragStart={(e) => onDragStart(e, type)}
              style={{ '--accent': spec.color }}
            >
              <div className="palette-title">
                {spec.icon ? `${spec.icon} ` : ''}
                {spec.title}
              </div>
              <div className="muted">{spec.description}</div>
            </div>
          ))}
        </div>
      ))}
      {catalog.port_types && !catalog.automation && (
        <>
          <div className="section-title">Типы связей</div>
          {Object.entries(catalog.port_types).map(([k, v]) => (
            <div key={k} className="muted port-type">
              <code>{k}</code> — {v}
            </div>
          ))}
        </>
      )}
    </aside>
  )
}

function Editor({ catalogs, projectId, onProjectsChanged, me }) {
  const [kind, setKind] = useState('api')
  const isAuto = kind === 'automation'
  const isBot = kind === 'bot'
  const catalog = isAuto ? catalogs.automation : isBot ? catalogs.bot : catalogs.api
  const [bot, setBot] = useState({})
  const [botState, setBotState] = useState(null) // {running, username, error}
  const [nodeCounts, setNodeCounts] = useState(null) // воронка: {node: сколько раз дошли}
  const [automation, setAutomation] = useState({})
  const [telegram, setTelegram] = useState({})
  const [agentsOpen, setAgentsOpen] = useState(false)
  const [recordOpen, setRecordOpen] = useState(false)
  const [runRequest, setRunRequest] = useState(null)
  const [runState, setRunState] = useState(null) // текущий запуск сценария: подсветка на канвасе
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
      setKind(p.kind || 'api')
      setAutomation(p.automation || {})
      setTelegram(p.telegram || {})
      setBot(p.bot || {})
      setBotState(null)
      setNodeCounts(null)
      setBottomTab(p.kind === 'automation' ? 'runs' : p.kind === 'bot' ? 'emulator' : 'tests')
      setRunState(null)
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
    () => ({ name, kind, automation, telegram, bot, nodes: JSON.parse(nodesKey), edges: JSON.parse(edgesKey), models, env, sandbox, libraries, firewall, tests }),
    [name, kind, automation, telegram, bot, nodesKey, edgesKey, models, env, sandbox, libraries, firewall, tests],
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
    try {
      const saved = await api.save(projectId, graph)
      if (saved?.automation) setAutomation(saved.automation)
      if (saved?.bot_reload_error) notify(`Бот работает на прежней версии: ${saved.bot_reload_error}`, 'error')
      setDirty(false)
      onProjectsChanged()
    } catch (e) {
      notify(`Не сохранено: ${e.message}`, 'error')
      throw e
    }
  }, [projectId, graph, onProjectsChanged, notify])

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

  // состояние работающего бота (для тулбара)
  useEffect(() => {
    if (!isBot) return
    let alive = true
    const tick = () => api.botStatus(projectId).then((s) => alive && setBotState(s)).catch(() => {})
    tick()
    const t = setInterval(tick, 3000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [isBot, projectId])
  const botStart = async () => {
    try {
      await save()
      setBotState(await api.botStart(projectId))
      notify('Бот запущен', 'ok')
    } catch (e) {
      if (e.data?.detail?.issues?.length) setValidation({ ok: false, issues: e.data.detail.issues })
      notify(`Бот не запущен: ${e.message}`, 'error')
    }
  }
  const botStop = async () => setBotState(await api.botStop(projectId))

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

  // запись действий → цепочка блоков после выделенного блока (или в конец первого сценария)
  const insertChain = (blocks) => {
    const byId = Object.fromEntries(nodes.map((n) => [n.id, n]))
    const freeOut = (id) => byId[id] && !BRANCHES[byId[id].type] && byId[id].type !== 'stop' && !edges.some((e) => e.source === id)
    let anchor = nodes.find((n) => n.selected && freeOut(n.id))?.id
    if (!anchor) {
      const start = nodes.find((n) => n.type === 'trigger')
      let cur = start?.id
      const seen = new Set()
      while (cur && !seen.has(cur)) {
        seen.add(cur)
        const next = edges.find((e) => e.source === cur && !e.sourceHandle)?.target
        if (!next) break
        cur = next
      }
      anchor = cur && freeOut(cur) ? cur : null
    }
    const base = anchor ? byId[anchor].position : { x: 80, y: Math.max(0, ...nodes.map((n) => n.position.y)) + 220 }
    const newNodes = blocks.map((b, i) => ({
      id: `${b.type}-${uid()}`,
      type: b.type,
      position: { x: base.x + ((i % 6) + 1) * 270, y: base.y + Math.floor(i / 6) * 170 },
      data: { ...structuredClone(catalog.blocks[b.type].defaults), ...b.data },
      selected: true,
    }))
    const chainEdges = newNodes.slice(1).map((n, i) => ({ id: `e-${uid()}`, source: newNodes[i].id, target: n.id }))
    if (anchor && newNodes.length) chainEdges.unshift({ id: `e-${uid()}`, source: anchor, target: newNodes[0].id })
    setNodes((ns) => [...ns.map((n) => (n.selected ? { ...n, selected: false } : n)), ...newNodes])
    setEdges((es) => [...es, ...chainEdges])
    markDirty()
    notify(`Вставлено блоков: ${newNodes.length}${anchor ? '' : ' — подключите цепочку к «Старту»'}`, 'ok')
  }

  // очистить полотно: все блоки и связи (Ctrl+Z возвращает; на диск — только после сохранения)
  const clearCanvas = () => {
    if (!nodes.length) return
    if (!confirm(`Удалить все блоки (${nodes.length}) и связи с полотна? Отменить — Ctrl+Z; на диск изменение попадёт после сохранения.`)) return
    setNodes([])
    setEdges([])
    setDebug(null)
    setRunState(null)
    setActiveNodes([])
    markDirty()
    notify('Полотно очищено — Ctrl+Z, чтобы вернуть')
  }

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

  // блоки, через которые прошёл текущий/выбранный запуск сценария
  const runVisited = useMemo(() => new Set((runState?.events || []).map((e) => e.node).filter(Boolean)), [runState])
  const runScenario = (scenarioName) => {
    setBottomTab('runs')
    setRunRequest({ name: scenarioName, nonce: Math.random() })
  }

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
          __active: activeNodes.includes(n.id) || debugVisited.has(n.id) || runVisited.has(n.id),
          __current: runState?.status === 'running' && runState.current === n.id,
          __count: nodeCounts ? nodeCounts[n.id] || 0 : undefined,
          __countMax: nodeCounts ? Math.max(1, ...Object.values(nodeCounts)) : undefined,
          __step: debugStep && debugStep.node_id === n.id ? debugStep : null,
          __hiddenCount: collapse.counts[n.id],
          __toggleCollapse: isStart(n.type) ? () => toggleCollapse(n.id) : undefined,
          __run: n.type === 'trigger' && !readOnly ? () => runScenario(n.data.name) : undefined,
        },
      })),
    [nodes, issuesByNode, activeNodes, debugVisited, debugStep, collapse, toggleCollapse, runVisited, runState, readOnly, nodeCounts], // eslint-disable-line react-hooks/exhaustive-deps
  )

  // подписи «да»/«нет» на связях из условия
  // «ok» у «Спросить» — «ответ», у «Назначить роль» — «готово»; «timeout» у счёта — «не оплачено»
  const branchLabel = (e) => {
    const src = nodes.find((n) => n.id === e.source)?.type
    if (['bot_role', 'bot_refund', 'bot_moderate', 'bot_download'].includes(src) && e.sourceHandle === 'ok') return 'готово'
    if (src === 'bot_filter' && e.sourceHandle === 'ok') return 'чисто'
    if (src === 'bot_invoice' && e.sourceHandle === 'timeout') return 'не оплачено'
    return BRANCH_LABELS[e.sourceHandle]
  }
  const viewEdges = useMemo(() => {
    // связи, по которым прошёл тестовый запрос (до текущего шага)
    const passed = new Set()
    const ids = (debug?.steps || []).slice(0, (debug?.current ?? -1) + 1).map((s) => s.node_id)
    for (let i = 1; i < ids.length; i++) passed.add(`${ids[i - 1]}>${ids[i]}`)
    return edges.map((e) => {
      const menuLabel = e.sourceHandle && nodes.find((n) => n.id === e.source && n.type === 'bot_menu')?.data.buttons?.find((b) => b.id === e.sourceHandle)?.text
      const branch = !!BRANCH_LABELS[e.sourceHandle] || !!menuLabel
      const onPath = passed.has(`${e.source}>${e.target}`) || (runVisited.has(e.source) && runVisited.has(e.target))
      return {
        ...e,
        animated: onPath,
        className: [branch ? `branch-${e.sourceHandle}` : '', onPath ? 'on-path' : ''].join(' '),
        ...(branch ? { label: menuLabel || branchLabel(e), labelBgPadding: [4, 2] } : {}),
      }
    })
  }, [edges, debug, runVisited, nodes])

  const nodeTypes = useMemo(
    () => Object.fromEntries(Object.keys(catalog.blocks).map((t) => [t, BlockNode])),
    [catalog],
  )

  const selectedNodes = nodes.filter((n) => n.selected)
  const selected = selectedNodes.length === 1 ? selectedNodes[0] : null
  const errors = (validation.issues || []).filter((i) => i.level === 'error').length

  return (
    <CatalogContext.Provider value={catalog}>
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
          {errors
            ? `Ошибок: ${errors}`
            : isAuto
              ? `Сценариев: ${validation.scenarios?.length ?? 0}`
              : isBot
              ? `Обработчиков: ${validation.handlers?.length ?? 0}`
              : `Граф корректен · маршрутов: ${validation.routes?.length ?? 0}`}
        </span>
        {isAuto && (
          <button
            className={`small auto-switch ${automation.enabled ? 'on' : ''}`}
            disabled={readOnly}
            title="Горячие клавиши, расписание и вебхуки работают, только когда сценарии включены (после сохранения)"
            onClick={() => {
              setAutomation({ ...automation, enabled: !automation.enabled })
              markDirty()
            }}
          >
            {automation.enabled ? '⏵ Включены' : '⏸ Выключены'}
          </button>
        )}
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
            <button className="small" onClick={clearCanvas} disabled={!nodes.length} title="Удалить все блоки и связи (можно отменить Ctrl+Z)">
              🗑 Очистить
            </button>
          </span>
        )}
        <button
          onClick={() => setSettingsOpen(isAuto ? 'automation' : isBot ? 'bot' : 'models')}
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
        {isBot ? (
          <>
            {botState?.running && botState.username && (
              <a className="button" href={`https://t.me/${botState.username}`} target="_blank" rel="noreferrer" title={botState.error || 'Открыть бота в Telegram'}>
                {botState.error ? '⚠' : '🟢'} @{botState.username} ↗
              </a>
            )}
            {botState?.running ? (
              <button onClick={botStop} disabled={readOnly}>
                ■ Остановить бота
              </button>
            ) : (
              <button className="primary" onClick={botStart} disabled={errors > 0 || readOnly} title="Сохранить и запустить бота в Telegram">
                ▶ Запустить бота
              </button>
            )}
          </>
        ) : isAuto ? (
          <>
            <button onClick={() => setRecordOpen(true)} disabled={readOnly} title="Записать свои действия мышью и клавиатурой в блоки">
              ● Запись
            </button>
            <button onClick={() => setAgentsOpen(true)} title="Компьютеры, на которых выполняются сценарии">
              🖥 Агенты
            </button>
            <button
              className="primary"
              disabled={errors > 0 || readOnly || !validation.scenarios?.length}
              onClick={() => {
                const sel = nodes.find((n) => n.selected && n.type === 'trigger')
                runScenario(sel?.data.name || validation.scenarios?.[0]?.name)
              }}
              title="Выполнить сценарий (выделенный «Старт» или первый) — текущую версию, даже несохранённую"
            >
              ▶ Запустить
            </button>
          </>
        ) : (
          <button className="primary" onClick={run} disabled={errors > 0 || readOnly}>
            {running ? 'Перезапустить' : '▶ Запустить'}
          </button>
        )}
        {running && !isAuto && !isBot && (
          <>
            <a className="button" href={running.docs_url} target="_blank" rel="noreferrer">
              Swagger ↗
            </a>
            <button onClick={stop} disabled={readOnly}>
              ■ Стоп
            </button>
          </>
        )}
        {!isAuto && !isBot && (
          <>
            <button onClick={exportZip} disabled={errors > 0} title="Скачать обычное FastAPI-приложение (с Dockerfile)">
              Экспорт в FastAPI
            </button>
            <button onClick={() => setDeployOpen(true)} title="Собрать образ и запустить контейнер на этой машине">
              🚀 Деплой
            </button>
          </>
        )}
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
            minZoom={0.1}
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
          agent={automation.agent}
          botRoles={bot.roles ?? ['moderator']}
          sandboxInfo={sandboxInfo}
          onOpenSettings={(tab) => setSettingsOpen(tab || (isAuto ? 'automation' : isBot ? 'bot' : 'models'))}
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
        botProps={
          isBot
            ? { graph, readOnly, running: !!botState?.running, onTrace: setActiveNodes, roles: bot.roles ?? ['moderator'], onCounts: setNodeCounts, botName: name, username: botState?.username }
            : null
        }
        runsProps={
          isAuto
            ? {
                graph,
                scenarios: validation.scenarios || [],
                agentId: automation.agent,
                readOnly,
                onRunState: setRunState,
                runRequest,
              }
            : null
        }
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
      {agentsOpen && <AgentsModal me={me} onClose={() => setAgentsOpen(false)} />}
      {recordOpen && (
        <RecordModal projectId={projectId} agentId={automation.agent} onInsert={insertChain} onClose={() => setRecordOpen(false)} />
      )}
      {settingsOpen && (
        <ProjectSettings
          readOnly={readOnly}
          projectId={projectId}
          initialTab={settingsOpen}
          kind={kind}
          project={{ models, env, sandbox, libraries, firewall, automation, telegram, bot }}
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
            if (patch.automation) setAutomation(patch.automation)
            if (patch.telegram) setTelegram(patch.telegram)
            if (patch.bot) setBot(patch.bot)
            markDirty()
          }}
          sandboxInfo={sandboxInfo}
          issues={(validation.issues || []).filter((i) => !i.node_id)}
          onInstalled={() => setValidateNonce((n) => n + 1)}
          onClose={() => setSettingsOpen(null)}
        />
      )}
    </div>
    </CatalogContext.Provider>
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
  const [catalogs, setCatalogs] = useState(null)
  const [newOpen, setNewOpen] = useState(false)
  const [space, setSpace] = useState(() => {
    try {
      return localStorage.getItem('dragapi.space') || 'api'
    } catch {
      return 'api'
    }
  })
  const lastInSpace = useRef({})
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
    Promise.all([api.blocks(), api.automationCatalog(), api.botsCatalog(), loadProjects()])
      .then(([c, auto, botCat, ps]) => {
        setCatalogs({
          api: c,
          automation: { ...auto, automation: true, port_types: { flow: 'Порядок выполнения' } },
          bot: { ...botCat, automation: true, bot: true, port_types: { flow: 'Порядок выполнения' } },
        })
        setProjectId((cur) => {
          const id = ps.some((p) => p.id === cur) ? cur : ps.find((p) => (p.kind || 'api') === space)?.id || null
          const proj = ps.find((p) => p.id === id)
          if (proj) setSpace(proj.kind || 'api')
          return id
        })
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

  const createProject = () => setNewOpen(true)
  const doCreate = async (name, kind, template) => {
    const p = await api.createProject(name, kind, template)
    await loadProjects()
    setSpace(kind)
    setProjectId(p.id)
    setNewOpen(false)
  }
  useEffect(() => {
    try {
      localStorage.setItem('dragapi.space', space)
    } catch {
      /* не запомнится */
    }
  }, [space])
  const inSpace = projects.filter((p) => (p.kind || 'api') === space)
  const switchSpace = (k) => {
    if (projectId) lastInSpace.current[space] = projectId
    setSpace(k)
    const list = projects.filter((p) => (p.kind || 'api') === k)
    const remembered = lastInSpace.current[k]
    setProjectId(list.some((p) => p.id === remembered) ? remembered : list[0]?.id || null)
  }

  const deleteProject = async () => {
    if (!projectId || !confirm('Удалить проект? Это действие необратимо.')) return
    await api.remove(projectId)
    const ps = await loadProjects()
    setProjectId(ps[0]?.id || null)
  }

  if (error) return <div className="fatal">{error}</div>
  if (!catalogs) return <div className="fatal">Загрузка…</div>

  return (
    <CatalogContext.Provider value={catalogs.api}>
      <div className="app">
        <nav className="topbar">
          <div className="logo">
            Drag<span>API</span>
          </div>
          <div className="spaces" role="tablist">
            {SPACES.map(([k, label]) => (
              <button key={k} role="tab" className={`space ${space === k ? 'active' : ''}`} onClick={() => switchSpace(k)}>
                {label}
                <span className="muted"> {projects.filter((p) => (p.kind || 'api') === k).length}</span>
              </button>
            ))}
          </div>
          <select value={projectId || ''} onChange={(e) => setProjectId(e.target.value)} disabled={!inSpace.length}>
            {!inSpace.length && <option value="">— нет проектов —</option>}
            {inSpace.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
                {p.role && p.role !== 'owner' ? ` (${{ editor: 'редактор', viewer: 'просмотр' }[p.role]})` : ''}
              </option>
            ))}
          </select>
          <button className="small" onClick={createProject}>
            + Проект
          </button>
          {projectId && inSpace.some((p) => p.id === projectId) && (!me || projects.find((p) => p.id === projectId)?.role === 'owner') && (
            <button className="small danger" onClick={deleteProject}>
              Удалить
            </button>
          )}
          <div className="spacer" />
          {me && <UserMenu me={me} onLogout={onLogout} />}
        </nav>
        {projectId && inSpace.some((p) => p.id === projectId) ? (
          <ReactFlowProvider key={`${projectId}-${reloadKey}`}>
            <Editor catalogs={catalogs} projectId={projectId} onProjectsChanged={loadProjects} me={me} />
          </ReactFlowProvider>
        ) : (
          <div className="fatal">
            {{ api: 'API-проектов', automation: 'Сценариев автоматизации', bot: 'Ботов' }[space]} пока нет.{' '}
            <button onClick={createProject}>Создать</button>
          </div>
        )}
        {newOpen && <NewProjectModal initialKind={space} onCreate={doCreate} onClose={() => setNewOpen(false)} />}
      </div>
    </CatalogContext.Provider>
  )
}

function NewProjectModal({ onCreate, onClose, initialKind = 'api' }) {
  const [kind, setKind] = useState(initialKind)
  const [templates, setTemplates] = useState([])
  const [template, setTemplate] = useState('')
  useEffect(() => {
    api.botTemplates().then(setTemplates).catch(() => {})
  }, [])
  const [name, setName] = useState('')
  const [error, setError] = useState(null)
  useEffect(() => {
    const h = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])
  const submit = async (e) => {
    e.preventDefault()
    try {
      const tpl = kind === 'bot' ? templates.find((t) => t.id === template) : null
      await onCreate(name.trim() || tpl?.title || ({ automation: 'Новый сценарий', bot: 'Новый бот' }[kind] || 'Новый проект'), kind, kind === 'bot' ? template || null : null)
    } catch (err) {
      setError(err.message)
    }
  }
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <form className="modal" role="dialog" aria-label="Новый проект" onSubmit={submit}>
        <div className="modal-head">
          <div className="inspector-title">Новый проект</div>
          <button type="button" className="icon" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="kind-choice">
          {[
            ['api', '🔌 API', 'HTTP-эндпоинты, база данных, код — экспорт в FastAPI и деплой в Docker'],
            ['automation', '🤖 Автоматизация', 'Сценарии действий на компьютере: мышь, клавиатура, автокликер, запись, расписание и горячие клавиши'],
            ['bot', '✈ Telegram-бот', 'Конструктор ботов: команды, меню с кнопками, вопросы и анкеты, данные пользователей, тест-чат'],
          ].map(([k, title, desc]) => (
            <label key={k} className={`kind-card ${kind === k ? 'on' : ''}`}>
              <input type="radio" name="kind" checked={kind === k} onChange={() => setKind(k)} />
              <b>{title}</b>
              <span className="muted">{desc}</span>
            </label>
          ))}
        </div>
        {kind === 'bot' && (
          <div className="field">
            <div className="field-label">С чего начать</div>
            <div className="template-choice">
              {[{ id: '', title: 'Пустой бот', description: 'Только /start с приветствием' }, ...templates].map((t) => (
                <label key={t.id || 'empty'} className={`kind-card ${template === t.id ? 'on' : ''}`}>
                  <input type="radio" name="tpl" checked={template === t.id} onChange={() => setTemplate(t.id)} />
                  <b>{t.title}</b>
                  <span className="muted">{t.description}</span>
                </label>
              ))}
            </div>
          </div>
        )}
        <label className="field">
          <span className="field-label">Название</span>
          <input autoFocus value={name} placeholder={(kind === 'bot' && templates.find((t) => t.id === template)?.title) || { automation: 'Новый сценарий', bot: 'Новый бот' }[kind] || 'Новый проект'} onChange={(e) => setName(e.target.value)} />
        </label>
        {error && <div className="issue error">{error}</div>}
        <div className="modal-foot">
          <button type="button" onClick={onClose}>
            Отмена
          </button>
          <button className="primary" type="submit">
            Создать
          </button>
        </div>
      </form>
    </div>
  )
}
