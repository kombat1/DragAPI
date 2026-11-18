import { Handle, NodeToolbar, Position } from '@xyflow/react'
import { StepDetails } from './TestsPanel.jsx'

const OPS = { eq: '=', ne: '≠', gt: '>', gte: '≥', lt: '<', lte: '≤', contains: '∋', startswith: '^', in: '∈', is_null: '∅' }
import { useCatalog } from '../catalog.js'
import { autoSummary } from './AutoForm.jsx'

function Summary({ type, data }) {
  switch (type) {
    case 'endpoint':
      return (
        <div className="endpoint-line">
          <span className={`method m-${data.method}`}>{data.method}</span>
          <code>{data.path}</code>
        </div>
      )
    case 'input':
      return (
        <>
          <div className="muted">из {data.source}</div>
          {(data.fields || []).map((f, i) => (
            <div key={i} className="row-line">
              <code>{f.name}</code>: <span className="type">{f.type}</span>
              {f.required ? '' : '?'}
            </div>
          ))}
        </>
      )
    case 'validate':
      return (data.rules || []).length ? (
        data.rules.map((r, i) => (
          <div key={i} className="row-line">
            <code>{r.field}</code> {r.op} <code>{r.value}</code>
          </div>
        ))
      ) : (
        <div className="muted">нет правил</div>
      )
    case 'logic':
      return (data.assignments || []).map((a, i) => (
        <div key={i} className="row-line">
          <code>{a.target}</code> = <code className="expr">{a.expr}</code>
        </div>
      ))
    case 'auth':
      return (
        <>
          <div className="row-line">
            {data.scheme === 'bearer' ? 'Bearer-токен' : data.scheme === 'api_key_query' ? 'query' : 'заголовок'}{' '}
            {data.scheme !== 'bearer' && <code>{data.name}</code>}
          </div>
          <div className="muted">
            == <code>env['{data.secret_env}']</code>, иначе 401
          </div>
        </>
      )
    case 'db':
      return (
        <>
          <div className="row-line">
            <span className="db-op">{data.op}</span> <code>{data.model || '—'}</code>
            {['get', 'update', 'delete'].includes(data.op) && data.id_expr && (
              <>
                {' '}
                id=<code className="expr">{data.id_expr}</code>
              </>
            )}
          </div>
          {(data.op === 'insert' || data.op === 'update') &&
            (data.values || []).slice(0, 4).map((v, i) => (
              <div key={i} className="row-line">
                <code>{v.field}</code> = <code className="expr">{v.expr}</code>
              </div>
            ))}
          {data.op === 'list' &&
            (data.filters || []).slice(0, 3).map((f, i) => (
              <div key={i} className="row-line">
                <code>{f.field}</code> {OPS[f.op] || '='} <code className="expr">{f.expr}</code>
              </div>
            ))}
          {(data.include || []).length > 0 && <div className="muted">+ {data.include.join(', ')}</div>}
          {(data.values || []).length > 4 && (data.op === 'insert' || data.op === 'update') && (
            <div className="muted">ещё {data.values.length - 4}…</div>
          )}
          <div className="muted">
            → <code>{data.target}</code>
          </div>
        </>
      )
    case 'code': {
      const lines = (data.code || '').split('\n').filter((l) => l.trim() && !l.trim().startsWith('#'))
      return (
        <>
          <div className="muted">
            python · {lines.length} строк · ≤{data.timeout ?? 2} с
          </div>
          <pre className="code-preview">{lines.slice(0, 3).join('\n')}</pre>
          <div className="muted row-line">
            → <code>{(data.outputs || []).join(', ') || '—'}</code>
          </div>
        </>
      )
    }
    case 'condition':
      return (
        <div className="row-line">
          если <code className="expr">{data.expr}</code>
        </div>
      )
    case 'scenario':
      return (
        <>
          <div className="row-line">
            🤖 <code>{data.scenario || '—'}</code> <span className="muted">{data.project || 'проект не выбран'}</span>
          </div>
          {(data.params || []).slice(0, 3).map((p, i) => (
            <div key={i} className="row-line">
              <code>{p.key}</code> = <code className="expr">{p.expr}</code>
            </div>
          ))}
          <div className="muted">
            → <code>{data.target}</code>
          </div>
        </>
      )
    case 'response':
      return (
        <>
          <div className="muted">статус {data.status}</div>
          {(data.body || []).length ? (
            data.body.map((b, i) => (
              <div key={i} className="row-line">
                <code>"{b.key}"</code>: <code className="expr">{b.expr}</code>
              </div>
            ))
          ) : (
            <div className="muted">весь контекст</div>
          )}
        </>
      )
    default:
      return null
  }
}

export default function BlockNode({ id, type, data, selected }) {
  const catalog = useCatalog()
  const spec = catalog?.blocks[type]
  if (!spec) return null
  const issues = data.__issues || []
  // у «Меню» выходы — по кнопкам: сколько кнопок, столько выходов
  const dyn = spec.dynamic_branches
  const branches = dyn
    ? [...(data[dyn.field] || []).map((b) => ({ id: b.id, label: b.text || '…' })), ...dyn.extra]
    : spec.branches
  const level = issues.some((i) => i.level === 'error') ? 'error' : issues.length ? 'warning' : ''
  return (
    <div
      className={`block ${selected ? 'selected' : ''} ${level} ${data.__active ? 'active' : ''} ${data.__step || data.__current ? 'current-step' : ''}`}
      style={{ '--accent': spec.color, minHeight: branches && branches.length > 2 ? 40 + branches.length * 24 : undefined }}
      title={issues.map((i) => i.message).join('\n')}
    >
      {data.__step && (
        <NodeToolbar isVisible position={Position.Top} className="step-toolbar">
          <StepDetails step={data.__step} catalog={catalog} nodes={[{ id, type }]} compact />
        </NodeToolbar>
      )}
      {spec.inputs.length > 0 && (
        <Handle type="target" position={Position.Left} title={`вход: ${spec.inputs.join(' | ')}`} />
      )}
      <div className="block-head">
        <span className="block-title">
          {spec.icon ? <span className="block-icon">{spec.icon}</span> : null}
          {spec.title}
        </span>
        {data.__run && (
          <button
            className="collapse-toggle nodrag"
            title="Запустить этот сценарий"
            onClick={(e) => {
              e.stopPropagation()
              data.__run()
            }}
          >
            ▶
          </button>
        )}
        {data.__toggleCollapse && (
          <button
            className="collapse-toggle nodrag"
            title={data.collapsed ? 'Развернуть цепочку' : 'Свернуть цепочку'}
            onClick={(e) => {
              e.stopPropagation()
              data.__toggleCollapse()
            }}
          >
            {data.collapsed ? `▸ ${data.__hiddenCount ?? ''}` : '▾'}
          </button>
        )}
      </div>
      <div className="block-body">
        {type === 'image_find' && data.image && <img className="node-template" src={data.image} alt="" draggable={false} />}
        {spec.fields ? <div className="row-line auto-summary">{autoSummary(type, data)}</div> : <Summary type={type} data={data} />}
      </div>
      {issues.length > 0 && <div className={`block-issue ${level}`}>{issues[0].message}</div>}
      {data.__count !== undefined && (
        <div className="block-count" title="Сколько раз дошли до этого блока">
          <span style={{ width: `${Math.round((data.__count / data.__countMax) * 100)}%` }} />
          <b>{data.__count}</b>
        </div>
      )}
      {branches
        ? branches.map((b, i) => (
            <Handle
              key={b.id}
              id={b.id}
              type="source"
              position={Position.Right}
              className={`branch-handle branch-${b.id}`}
              style={{ top: branches.length <= 2 ? `${35 + i * 35}%` : `${38 + i * 24}px` }}
              title={`выход «${b.label}»`}
            >
              <span className="handle-label">{b.label}</span>
            </Handle>
          ))
        : spec.output.length > 0 && (
            <Handle type="source" position={Position.Right} title={`выход: ${spec.output.join(' | ')}`} />
          )}
    </div>
  )
}
