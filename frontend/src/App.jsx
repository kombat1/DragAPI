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
import { DEFAULT_FIREWALL, normalizeFirewall } from './components/FirewallTab.jsx'

const uid = () => Math.random().toString(36).slice(2, 9)

// Поля, доступные на входе блока: идём по цепочке назад до эндпоинта.
function fieldsBefore(nodeId, nodes, edges) {
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]))
  const chain = []
  const seen = new Set()
  let cur = edges.find((e) => e.target === nodeId)?.source
  while (cur && !seen.has(cur) && byId[cur]) {
    seen.add(cur)
    chain.unshift(byId[cur])
    cur = edges.find((e) => e.target === cur)?.source
  }
  const out = []
  for (const n of chain) {
    if (n.type === 'endpoint') for (const m of (n.data.path || '').matchAll(/{(\w+)}/g)) out.push(m[1])
    if (n.type === 'input') (n.data.fields || []).forEach((f) => f.name && out.push(f.name))
    if (n.type === 'logic') (n.data.assignments || []).forEach((a) => a.target && out.push(a.target))
    if (n.type === 'db' && n.data.target) out.push(n.data.target)
    if (n.type === 'code') (n.data.outputs || []).forEach((o) => o && out.push(o))
  }
  return [...new Set(out)]
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

function Editor({ catalog, projectId, onProjectsChanged }) {
  const [name, setName] = useState('')
  const [nodes, setNodes] = useState([])
  const [edges, setEdges] = useState([])
  const [models, setModels] = useState([])
  const [env, setEnv] = useState([])
  const [sandbox, setSandbox] = useState({})
  const [libraries, setLibraries] = useState({ packages: [], only_binary: true })
  const [firewall, setFirewall] = useState(DEFAULT_FIREWALL)
  const [settingsOpen, setSettingsOpen] = useState(null) // null | имя вкладки настроек проекта
  const [sandboxInfo, setSandboxInfo] = useState(null)
  const [validateNonce, setValidateNonce] = useState(0) // перепроверить граф (например, после установки библиотек)
  // итоговое описание песочницы по настройкам проекта (в т.ч. несохранённым)
  useEffect(() => {
    const t = setTimeout(() => api.sandboxDescribe(sandbox, firewall).then(setSandboxInfo).catch(() => {}), 200)
    return () => clearTimeout(t)
  }, [sandbox, firewall])
  const [selectedId, setSelectedId] = useState(null)
  const [validation, setValidation] = useState({ ok: true, issues: [], routes: [] })
  const [dirty, setDirty] = useState(false)
  const [running, setRunning] = useState(null) // {base_url, docs_url, routes}
  const [toast, setToast] = useState(null)
  const [activeNodes, setActiveNodes] = useState([])
  const [loaded, setLoaded] = useState(false)
  const { screenToFlowPosition, fitView } = useReactFlow()
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
      setModels(p.models || [])
      setEnv(p.env || [])
      setSandbox(p.sandbox || {})
      setLibraries({ packages: [], only_binary: true, ...(p.libraries || {}) })
      setFirewall(normalizeFirewall(p.firewall, p.sandbox?.network))
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

  const graph = useMemo(
    () => ({ name, nodes, edges, models, env, sandbox, libraries, firewall }),
    [name, nodes, edges, models, env, sandbox, libraries, firewall],
  )

  // живая проверка графа на бэкенде
  useEffect(() => {
    if (!loaded) return
    const t = setTimeout(() => {
      api.validate(graph, projectId).then(setValidation).catch(() => {})
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
        save().then(() => notify('Сохранено'))
      }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [save, notify])

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
    if (changes.some((c) => c.type !== 'select' && c.type !== 'dimensions')) setDirty(true)
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
      // из одного выхода — одна связь (у «Условия» два выхода: true/false), в один вход — одна связь
      const sameOutput = (e) => e.source === c.source && (e.sourceHandle ?? null) === (c.sourceHandle ?? null)
      if (edges.some((e) => sameOutput(e) || e.target === c.target)) return false
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
        notify(`«${from}» нельзя подключить к «${to}»: типы данных несовместимы или вход/выход уже занят`, 'error')
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
  const viewNodes = useMemo(
    () =>
      nodes.map((n) => ({
        ...n,
        selected: n.id === selectedId,
        data: { ...n.data, __issues: issuesByNode[n.id], __active: activeNodes.includes(n.id) },
      })),
    [nodes, selectedId, issuesByNode, activeNodes],
  )

  // подписи «да»/«нет» на связях из условия
  const viewEdges = useMemo(
    () =>
      edges.map((e) =>
        e.sourceHandle === 'true' || e.sourceHandle === 'false'
          ? {
              ...e,
              label: e.sourceHandle === 'true' ? 'да' : 'нет',
              className: `branch-${e.sourceHandle}`,
              labelBgPadding: [4, 2],
            }
          : e,
      ),
    [edges],
  )

  const nodeTypes = useMemo(
    () => Object.fromEntries(Object.keys(catalog.blocks).map((t) => [t, BlockNode])),
    [catalog],
  )

  const selected = nodes.find((n) => n.id === selectedId)
  const errors = (validation.issues || []).filter((i) => i.level === 'error').length

  return (
    <div className="editor">
      <header className="toolbar">
        <input
          className="project-name"
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
        <button
          onClick={() => setSettingsOpen('models')}
          title="Модели данных, переменные окружения, песочница и библиотеки"
          className={(validation.issues || []).some((i) => !i.node_id) ? 'has-issues' : ''}
        >
          ⚙ Настройки проекта
        </button>
        <button onClick={() => save().then(() => notify('Сохранено'))} disabled={!dirty}>
          {dirty ? 'Сохранить' : 'Сохранено'}
        </button>
        <button className="primary" onClick={run} disabled={errors > 0}>
          {running ? 'Перезапустить' : '▶ Запустить'}
        </button>
        {running && (
          <>
            <a className="button" href={running.docs_url} target="_blank" rel="noreferrer">
              Swagger ↗
            </a>
            <button onClick={stop}>■ Стоп</button>
          </>
        )}
        <button onClick={exportZip} disabled={errors > 0} title="Скачать обычное FastAPI-приложение">
          Экспорт в FastAPI
        </button>
      </header>

      <div className="workspace">
        <Palette catalog={catalog} />
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
            onNodeClick={(_, n) => setSelectedId(n.id)}
            onPaneClick={() => setSelectedId(null)}
            onNodesDelete={() => setSelectedId(null)}
            deleteKeyCode={['Delete', 'Backspace']}
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
      />
      {settingsOpen && (
        <ProjectSettings
          projectId={projectId}
          initialTab={settingsOpen}
          project={{ models, env, sandbox, libraries, firewall }}
          setProject={(patch) => {
            if (patch.models) setModels(patch.models)
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
              </option>
            ))}
          </select>
          <button className="small" onClick={createProject}>
            + Проект
          </button>
          {projectId && (
            <button className="small danger" onClick={deleteProject}>
              Удалить
            </button>
          )}
        </nav>
        {projectId ? (
          <ReactFlowProvider key={`${projectId}-${reloadKey}`}>
            <Editor catalog={catalog} projectId={projectId} onProjectsChanged={loadProjects} />
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
