import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useCatalog } from '../catalog.js'
import FirewallTab from './FirewallTab.jsx'
import ListEditor from './ListEditor.jsx'

export const SETTINGS_TABS = [
  ['models', 'Модели данных'],
  ['env', 'Переменные'],
  ['sandbox', 'Песочница'],
  ['firewall', 'Firewall'],
  ['libraries', 'Библиотеки'],
]

export const newId = () => Math.random().toString(36).slice(2, 10)

const OP_VIEW = {
  create_table: ['⊕', (o) => <>новая таблица <code>{o.model}</code></>, ''],
  rename_table: ['✎', (o) => <>переименовать таблицу <code>{o.old}</code> → <code>{o.model}</code> (данные сохраняются)</>, ''],
  add_column: ['+', (o) => <>новая колонка <code>{o.model}.{o.field}</code></>, ''],
  rename_column: ['✎', (o) => <>переименовать <code>{o.model}.{o.old}</code> → <code>{o.field}</code> (данные сохраняются)</>, ''],
  change_type: ['⟳', (o) => <>тип <code>{o.model}.{o.field}</code>: {o.old_type} → {o.type} — значения будут приведены, неприводимые станут 0 / пустыми</>, 'warn'],
  extra_column: ['⚠', (o) => <>колонки <code>{o.model}.{o.field}</code> нет в модели — данные в ней сохранены, API её не видит</>, 'muted'],
}

function SchemaPlan({ projectId, models }) {
  const [ops, setOps] = useState(null)
  const [error, setError] = useState(null)
  const key = JSON.stringify(models)
  const refresh = () =>
    api
      .schemaPlan(projectId, models)
      .then((r) => {
        setOps(r.ops)
        setError(null)
      })
      .catch((e) => setError(e.message))
  useEffect(() => {
    const t = setTimeout(refresh, 400)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, projectId])
  if (error) return <div className="schema-plan muted">План схемы недоступен: {error}</div>
  if (!ops) return null
  const extra = ops.filter((o) => o.op === 'extra_column')
  return (
    <div className="schema-plan">
      <div className="field-label">Превью-БД при следующем запуске</div>
      {ops.length === 0 && <div className="muted">Схема совпадает с моделями — изменений нет.</div>}
      {ops.map((o, i) => {
        const [icon, text, cls] = OP_VIEW[o.op] || ['·', () => JSON.stringify(o), '']
        return (
          <div key={i} className={`plan-op ${cls}`}>
            <span className="plan-icon">{icon}</span> {text(o)}
          </div>
        )
      })}
      {extra.length > 0 && (
        <button
          className="small danger"
          onClick={async () => {
            if (!confirm(`Удалить колонки ${extra.map((o) => o.model + '.' + o.field).join(', ')} вместе с данными?`)) return
            await api.schemaDropExtra(projectId, models)
            refresh()
          }}
        >
          Удалить лишние колонки
        </button>
      )}
    </div>
  )
}

function ModelsTab({ projectId, models, setModels }) {
  const catalog = useCatalog()
  const names = models.map((m) => m.name).filter(Boolean)
  const setModel = (i, patch) => setModels(models.map((m, j) => (j === i ? { ...m, ...patch } : m)))
  const removeModel = (i) => {
    if (confirm(`Удалить модель «${models[i].name}»? Таблица и данные в превью-БД останутся, но блоки перестанут её видеть.`))
      setModels(models.filter((_, j) => j !== i))
  }
  return (
    <section>
      <p className="hint">
        Каждая модель — таблица в базе, поле <code>id</code> создаётся автоматически. Поля можно переименовывать и
        менять им тип — данные переносятся. Тип <b>ref</b> — ссылка на запись другой модели (внешний ключ): назовите поле
        вроде <code>customer_id</code>, и блок БД сможет подтянуть связанную запись как <code>customer</code>.
      </p>
      {models.length === 0 && <div className="muted">Моделей пока нет.</div>}
      {models.map((m, i) => (
        <div className="model-card" key={m.id || i}>
          <div className="model-head">
            <input
              className="mono model-name"
              value={m.name}
              placeholder="имя модели (orders)"
              onChange={(e) => setModel(i, { name: e.target.value })}
            />
            <button className="small danger" onClick={() => removeModel(i)}>
              Удалить модель
            </button>
          </div>
          <ListEditor
            items={m.fields || []}
            onChange={(fields) => setModel(i, { fields })}
            empty="Нет полей — будет только id"
            addLabel="поле"
            newItem={() => ({ id: newId(), name: '', type: 'str' })}
            columns={[
              { key: 'name', label: 'поле', mono: true, flex: 1.6 },
              { key: 'type', label: 'тип', options: catalog.model_field_types, flex: 0.8 },
              {
                key: 'ref',
                label: 'ссылка на',
                options: ['', ...names.filter((n) => n !== m.name || true)],
                disabled: (it) => it.type !== 'ref',
                flex: 1.1,
              },
              {
                key: 'on_delete',
                label: 'при удалении',
                options: catalog.on_delete,
                labels: { restrict: 'запретить', cascade: 'удалить тоже', set_null: 'обнулить ссылку' },
                disabled: (it) => it.type !== 'ref',
                flex: 1.2,
              },
            ]}
          />
        </div>
      ))}
      <button
        onClick={() =>
          setModels([
            ...models,
            { id: newId(), name: `model${models.length + 1}`, fields: [{ id: newId(), name: 'title', type: 'str' }] },
          ])
        }
      >
        + Модель
      </button>
      <SchemaPlan projectId={projectId} models={models} />
    </section>
  )
}

function EnvTab({ env, setEnv }) {
  return (
    <section>
      <p className="hint">
        Доступны в выражениях как <code>env['KEY']</code>, в блоке «Авторизация» и вторым аргументом <code>run(data, env)</code>{' '}
        в блоке кода. Значения секретов используются только в превью: в экспортированный код они не попадают — там их
        нужно задать через окружение (см. <code>.env.example</code>).
      </p>
      <p className="hint">
        🔒 Значения секретов хранятся зашифрованными ключом платформы и после сохранения не показываются — ни здесь,
        ни в истории версий. Оставьте поле пустым, чтобы не менять значение.
      </p>
      <ListEditor
        items={env}
        onChange={setEnv}
        empty="Переменных пока нет"
        addLabel="переменная"
        newItem={() => ({ key: '', value: '', secret: false })}
        columns={[
          { key: 'key', label: 'KEY', mono: true, flex: 1.3 },
          {
            key: 'value',
            label: 'значение',
            mono: true,
            flex: 2,
            password: (it) => it.secret,
            placeholder: (it) => (it.secret && it.has_value ? '•••••• задано — введите новое, чтобы заменить' : ''),
          },
          { key: 'secret', label: 'секрет', checkbox: true },
        ]}
      />
    </section>
  )
}

const BACKENDS = [
  ['auto', 'Автоматически', 'firejail, если установлен; иначе обычный процесс'],
  ['firejail', 'firejail', 'изоляция: свой сетевой стек, файлы платформы и $HOME не видны, seccomp, без capabilities'],
  ['process', 'Обычный процесс', 'только лимиты памяти/CPU/файлов — сеть и файлы доступны. Для отладки'],
]

function SandboxTab({ sandbox, setSandbox, info, goTo }) {
  const s = { backend: 'auto', network: false, threads: false, max_file_mb: 1, max_open_files: 64, ...sandbox }
  const set = (patch) => setSandbox({ ...s, ...patch })
  return (
    <section className="sandbox-settings">
      <p className="hint">
        Как выполняется блок «Python-код» в превью и в кнопке «Проверить». Каждый вызов — новый процесс; лимиты
        времени и памяти задаются в самом блоке. На экспортированный проект эти настройки не влияют.
      </p>

      <div className="field-label">Изоляция</div>
      <div className="radio-cards">
        {BACKENDS.map(([value, title, desc]) => {
          const disabled = value === 'firejail' && info && !info.firejail_available
          return (
            <label key={value} className={`radio-card ${s.backend === value ? 'checked' : ''} ${disabled ? 'disabled' : ''}`}>
              <input
                type="radio"
                name="backend"
                checked={s.backend === value}
                disabled={disabled}
                onChange={() => set({ backend: value })}
              />
              <div>
                <b>{title}</b>
                {disabled && <span className="weak-text"> — не установлен</span>}
                <div className="muted">{desc}</div>
              </div>
            </label>
          )
        })}
      </div>

      <div className="toggle static">
        <div>
          <b>Сеть: {{ off: 'нет', filtered: 'по правилам firewall', open: 'без ограничений' }[info?.egress] || '…'}</b>
          <div className="muted">
            Куда коду можно обращаться, настраивается во вкладке{' '}
            <button className="linklike" onClick={() => goTo('firewall')}>
              Firewall
            </button>
            .
          </div>
        </div>
      </div>
      <label className="toggle">
        <input type="checkbox" checked={s.threads} onChange={(e) => set({ threads: e.target.checked })} />
        <div>
          <b>Разрешить потоки и подпроцессы</b>
          <div className="muted">
            Нужно для <code>threading</code>, <code>concurrent.futures</code>, <code>subprocess</code> и некоторых библиотек.
            Числовые библиотеки (numpy и т.п.) по умолчанию работают в один поток.
          </div>
        </div>
      </label>

      <div className="list-row">
        <div className="field">
          <div className="field-label">Макс. размер записываемого файла, МБ</div>
          <input type="number" min="0" max="100" value={s.max_file_mb} onChange={(e) => set({ max_file_mb: e.target.value })} />
          <div className="hint">Файлы пишутся только во временный каталог процесса и удаляются после вызова.</div>
        </div>
        <div className="field">
          <div className="field-label">Макс. открытых файлов</div>
          <input type="number" min="16" max="1024" value={s.max_open_files} onChange={(e) => set({ max_open_files: e.target.value })} />
          <div className="hint">Увеличьте, если большая библиотека не импортируется («Too many open files»).</div>
        </div>
      </div>

      {info && (
        <div className={`sandbox-badge ${info.isolated && !info.warning ? 'good' : 'weak'}`}>
          {info.isolated ? '🔒' : '⚠'} Итог: <b>{info.backend}</b> — {info.details}
          {info.warning && <div>⚠ {info.warning}</div>}
        </div>
      )}
    </section>
  )
}

const STATUS = {
  none: ['Библиотеки не используются', ''],
  ready: ['Установлено, совпадает со списком', 'good'],
  missing: ['Не установлено — нажмите «Установить»', 'weak'],
  outdated: ['Список изменён — нажмите «Установить»', 'weak'],
  installing: ['Идёт установка…', 'busy'],
  error: ['Последняя установка завершилась ошибкой', 'bad'],
}

function LibrariesTab({ projectId, libraries, setLibraries, sandbox, onInstalled }) {
  const lib = { packages: [], only_binary: true, ...libraries }
  const [text, setText] = useState(lib.packages.join('\n'))
  const [status, setStatus] = useState(null)
  const [error, setError] = useState(null)
  const [showLog, setShowLog] = useState(false)
  const logRef = useRef(null)
  const packages = text.split('\n').map((l) => l.trim()).filter((l) => l && !l.startsWith('#'))
  const key = JSON.stringify([packages, lib.only_binary])

  const refresh = async () => {
    try {
      const st = await api.librariesStatus(projectId, { packages, only_binary: lib.only_binary })
      setStatus(st)
      return st
    } catch (e) {
      setError(e.message)
    }
  }

  // статус при изменении списка; во время установки — опрос лога
  useEffect(() => {
    const t = setTimeout(refresh, 300)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, projectId])

  useEffect(() => {
    if (status?.status !== 'installing') return
    const t = setInterval(async () => {
      const st = await refresh()
      if (st && st.status !== 'installing') onInstalled()
    }, 1500)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.status])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [status?.log])

  const install = async () => {
    setError(null)
    setShowLog(true)
    try {
      setStatus(await api.librariesInstall(projectId, { packages, only_binary: lib.only_binary, sandbox }))
    } catch (e) {
      setError(e.message)
    }
  }

  const remove = async () => {
    if (!confirm('Удалить окружение проекта? Блоки кода будут выполняться без библиотек.')) return
    await api.librariesRemove(projectId)
    setText('')
    setLibraries({ ...lib, packages: [] })
    await refresh()
    onInstalled()
  }

  const [label, cls] = STATUS[status?.status] || ['…', '']
  const errors = status?.errors || []
  return (
    <section>
      <p className="hint">
        pip-пакеты для блоков «Python-код». У проекта своё виртуальное окружение; код видит его только на чтение. Установка
        идёт в фоне — пока она не закончится, запросы работают на прежнем окружении. При экспорте пакеты попадают в{' '}
        <code>requirements.txt</code>.
      </p>
      <div className="field">
        <div className="field-label">Пакеты — по одному на строку</div>
        <textarea
          className="mono"
          rows={Math.min(12, Math.max(4, text.split('\n').length + 1))}
          placeholder={'humanize==4.11.0\nrequests\npandas>=2.0'}
          spellCheck={false}
          value={text}
          onChange={(e) => {
            setText(e.target.value)
            setLibraries({
              ...lib,
              packages: e.target.value.split('\n').map((l) => l.trim()).filter(Boolean),
            })
          }}
        />
        {errors.map((m, i) => (
          <div key={i} className="issue error">
            {m}
          </div>
        ))}
      </div>
      <label className="toggle">
        <input
          type="checkbox"
          checked={lib.only_binary}
          onChange={(e) => setLibraries({ ...lib, only_binary: e.target.checked })}
        />
        <div>
          <b>Только готовые сборки (wheel)</b>
          <div className="muted">
            Рекомендуется: при установке не выполняется чужой <code>setup.py</code>. Отключите, если пакет есть только в
            исходниках.
          </div>
        </div>
      </label>

      <div className="libs-status">
        <span className={`lib-state ${cls}`}>{label}</span>
        <div className="spacer" />
        <button
          className="primary"
          onClick={install}
          disabled={status?.status === 'installing' || errors.length > 0}
        >
          {status?.status === 'installing'
            ? 'Устанавливается…'
            : status?.status === 'ready'
              ? 'Переустановить'
              : packages.length
                ? 'Установить'
                : 'Создать пустое окружение'}
        </button>
        {status && status.status !== 'none' && status.status !== 'installing' && (
          <button className="danger" onClick={remove}>
            Удалить окружение
          </button>
        )}
      </div>
      {error && <div className="issue error">{error}</div>}

      {status?.installed?.length > 0 && (
        <div className="field">
          <div className="field-label">
            Установлено ({status.installed.length}) · Python {status.python} · {status.size_mb} МБ
          </div>
          <div className="installed">
            {status.installed.map((p) => (
              <code key={p}>{p}</code>
            ))}
          </div>
        </div>
      )}
      {(status?.log || status?.status === 'installing') && (
        <div className="field">
          <button className="small" onClick={() => setShowLog((v) => !v)}>
            {showLog || status.status === 'installing' ? '▼' : '▶'} Лог установки
          </button>
          {(showLog || status.status === 'installing') && (
            <pre className="install-log" ref={logRef}>
              {status.log || 'Запуск…'}
            </pre>
          )}
        </div>
      )}
    </section>
  )
}

export default function ProjectSettings({ projectId, project, setProject, issues, sandboxInfo, onClose, onInstalled, initialTab, readOnly = false }) {
  const [tab, setTab] = useState(initialTab || 'models')

  useEffect(() => {
    const h = (e) => e.key === 'Escape' && !e.defaultPrevented && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  const fwRules = (project.firewall?.egress?.rules?.length || 0) + (project.firewall?.ingress?.rules?.length || 0)
  const count = {
    firewall: fwRules,
    models: project.models.length,
    env: project.env.length,
    libraries: (project.libraries?.packages || []).length,
  }

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal settings-modal" role="dialog" aria-label="Настройки проекта">
        <div className="modal-head">
          <div className="inspector-title">Настройки проекта</div>
          <button className="icon" onClick={onClose} title="Закрыть (Esc)">
            ×
          </button>
        </div>
        <div className="tabs settings-tabs">
          {SETTINGS_TABS.map(([k, label]) => (
            <button key={k} className={`tab ${tab === k ? 'active' : ''}`} onClick={() => setTab(k)}>
              {label}
              {count[k] ? <span className="muted"> ({count[k]})</span> : null}
            </button>
          ))}
        </div>

        {issues.length > 0 && (
          <div className="issues-box">
            {issues.map((i, k) => (
              <div key={k} className={`issue ${i.level}`}>
                {i.message}
              </div>
            ))}
          </div>
        )}

        <fieldset className="settings-body" disabled={readOnly}>
          {tab === 'models' && (
            <ModelsTab projectId={projectId} models={project.models} setModels={(models) => setProject({ models })} />
          )}
          {tab === 'env' && <EnvTab env={project.env} setEnv={(env) => setProject({ env })} />}
          {tab === 'sandbox' && (
            <SandboxTab
              sandbox={project.sandbox}
              setSandbox={(sandbox) => setProject({ sandbox })}
              info={sandboxInfo}
              goTo={setTab}
            />
          )}
          {tab === 'firewall' && (
            <FirewallTab
              projectId={projectId}
              firewall={project.firewall}
              setFirewall={(firewall) => setProject({ firewall })}
            />
          )}
          {tab === 'libraries' && (
            <LibrariesTab
              projectId={projectId}
              libraries={project.libraries}
              setLibraries={(libraries) => setProject({ libraries })}
              sandbox={project.sandbox}
              onInstalled={onInstalled}
            />
          )}
        </fieldset>

        <div className="modal-foot">
          <span className="muted">Изменения применяются сразу; не забудьте сохранить проект (Ctrl+S).</span>
          <button className="primary" onClick={onClose}>
            Готово
          </button>
        </div>
      </div>
    </div>
  )
}
