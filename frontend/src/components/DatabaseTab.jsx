// Настройки проекта → «База данных»: встроенная SQLite или PostgreSQL (своя или существующая база).
import { useState } from 'react'
import { api } from '../api.js'

const newId = () => Math.random().toString(36).slice(2, 10)

export const dbDefaults = (d) => ({ engine: 'sqlite', url_env: 'DATABASE_URL', schema: 'manage', tests_use_db: false, ...(d || {}) })

export default function DatabaseTab({ projectId, database, setDatabase, env, setEnv, models, setModels }) {
  const db = dbDefaults(database)
  const set = (patch) => setDatabase({ ...db, ...patch })
  const pg = db.engine === 'postgres'
  const varIndex = env.findIndex((e) => e.key === db.url_env)
  const variable = varIndex >= 0 ? env[varIndex] : null
  const [check, setCheck] = useState(null) // ответ «Проверить подключение»
  const [busy, setBusy] = useState(false)
  const [pick, setPick] = useState({})

  // строка подключения правится прямо здесь и хранится секретной переменной окружения проекта
  const setUrl = (value) => {
    if (variable) setEnv(env.map((e, i) => (i === varIndex ? { ...e, value, secret: true } : e)))
    else setEnv([...env, { key: db.url_env, value, secret: true }])
  }

  const inspect = async () => {
    setBusy(true)
    setCheck(null)
    try {
      const r = await api.databaseInspect(projectId, { database: db, env })
      setCheck(r)
      if (r.ok) setPick(Object.fromEntries(r.tables.map((t) => [t.name, !models.some((m) => m.name === t.name)])))
    } catch (e) {
      setCheck({ ok: false, error: e.message })
    } finally {
      setBusy(false)
    }
  }

  const importModels = () => {
    const chosen = check.models.filter((m) => pick[m.name])
    if (!chosen.length) return
    const replaced = chosen.filter((m) => models.some((x) => x.name === m.name)).map((m) => m.name)
    if (replaced.length && !confirm(`Заменить модели ${replaced.join(', ')} полями из базы? Блоки «База данных», которые используют удалённые поля, покажут ошибки.`))
      return
    // id существующих моделей сохраняем — так миграции и блоки узнают их
    const merged = chosen.map((m) => {
      const old = models.find((x) => x.name === m.name)
      if (!old) return { ...m, id: m.id || newId() }
      return {
        ...m,
        id: old.id,
        fields: m.fields.map((f) => ({ ...f, id: old.fields?.find((of) => of.name === f.name)?.id || f.id })),
      }
    })
    setModels([...models.filter((x) => !chosen.some((m) => m.name === x.name)), ...merged])
    setCheck({ ...check, imported: chosen.length })
  }

  return (
    <section className="db-tab">
      <p className="hint">Где API-проект хранит данные моделей. Блоки «База данных» работают одинаково с любым вариантом.</p>
      <div className="kind-choice">
        {[
          ['sqlite', '🗄 Встроенная SQLite', 'Файл на этой машине, ничего настраивать не нужно. При деплое — файл в томе контейнера.'],
          ['postgres', '🐘 PostgreSQL', 'Свой сервер или уже существующая база: несколько экземпляров API, бэкапы, доступ из других программ.'],
        ].map(([k, title, desc]) => (
          <label key={k} className={`kind-card ${db.engine === k ? 'on' : ''}`}>
            <input type="radio" name="db-engine" checked={db.engine === k} onChange={() => (set({ engine: k }), setCheck(null))} />
            <b>{title}</b>
            <span className="muted">{desc}</span>
          </label>
        ))}
      </div>

      {pg && (
        <>
          <label className="field">
            <span className="field-label">
              Строка подключения{' '}
              <span className="muted">
                — хранится зашифрованной в переменной <code>{db.url_env}</code>
              </span>
            </span>
            <input
              className="mono"
              type="password"
              autoComplete="off"
              value={variable?.value || ''}
              placeholder={
                variable?.has_value ? '•••••• задано — введите новую, чтобы заменить' : 'postgresql://пользователь:пароль@сервер:5432/база'
              }
              onChange={(e) => setUrl(e.target.value)}
            />
          </label>
          <details className="muted db-more">
            <summary>Другое имя переменной</summary>
            <input className="mono" value={db.url_env} onChange={(e) => set({ url_env: e.target.value.trim() })} />
            <div>Например, если строка подключения уже лежит в переменной окружения сервера под другим именем.</div>
          </details>
          {variable && !variable.secret && (
            <div className="issue warning">Переменная {db.url_env} не отмечена секретом — пароль попадёт в экспорт. Введите строку заново здесь, она станет секретом.</div>
          )}

          <div className="db-check">
            <button onClick={inspect} disabled={busy || (!variable?.value && !variable?.has_value)}>
              {busy ? 'Подключаюсь…' : '🔌 Проверить подключение'}
            </button>
            {check && !check.ok && <span className="issue error">✖ {check.error}</span>}
            {check?.ok && (
              <span className="ok-text">
                ✓ {check.server} · база <code>{check.database}</code> · пользователь <code>{check.user}</code> · таблиц: {check.tables.length}
              </span>
            )}
          </div>

          <div className="field">
            <span className="field-label">Таблицы</span>
            <label className="radio-line">
              <input type="radio" checked={db.schema === 'manage'} onChange={() => set({ schema: 'manage' })} />
              <span>
                <b>Платформа управляет таблицами</b> — создаёт их по моделям и меняет при правках (как с SQLite). Для новой
                базы или базы только этого API.
              </span>
            </label>
            <label className="radio-line">
              <input type="radio" checked={db.schema === 'existing'} onChange={() => set({ schema: 'existing' })} />
              <span>
                <b>Существующая база</b> — схема не меняется никогда. Модели должны совпадать с таблицами: импортируйте их
                ниже. Нужен целочисленный первичный ключ <code>id</code>; колонки дат, uuid, json читаются и пишутся строкой.
              </span>
            </label>
          </div>

          <label className="radio-line">
            <input type="checkbox" checked={db.tests_use_db} onChange={(e) => set({ tests_use_db: e.target.checked })} />
            <span>
              Тестовые запросы из редактора — тоже в эту базу.{' '}
              <span className="muted">
                По умолчанию они идут в отдельную SQLite-копию, чтобы тесты и «очистить данные перед тестами» не тронули
                настоящие записи. «▶ Запустить», вкладка «Данные» и деплой всегда работают с PostgreSQL.
              </span>
            </span>
          </label>

          {check?.ok && (
            <div className="db-import">
              <div className="field-label">Импорт моделей из базы</div>
              {check.tables.length === 0 && <div className="muted">В базе пока нет таблиц — их создаст платформа при запуске.</div>}
              {check.tables.map((t) => (
                <label key={t.name} className="db-table">
                  <input type="checkbox" checked={!!pick[t.name]} onChange={(e) => setPick({ ...pick, [t.name]: e.target.checked })} />
                  <code>{t.name}</code>
                  <span className="muted">
                    {t.rows ?? '?'} строк · {t.fields.map((f) => `${f.name}${f.type === 'ref' ? ` → ${f.ref}` : ''}`).join(', ') || 'только id'}
                    {t.skipped_columns.length > 0 && ` · пропущены колонки с недопустимыми именами: ${t.skipped_columns.join(', ')}`}
                    {models.some((m) => m.name === t.name) && ' · модель уже есть — будет заменена'}
                  </span>
                </label>
              ))}
              {check.skipped.map((s) => (
                <div key={s.table} className="muted db-table">
                  ⊘ <code>{s.table}</code> — {s.reason}
                </div>
              ))}
              {check.tables.length > 0 && (
                <button onClick={importModels} disabled={!Object.values(pick).some(Boolean)}>
                  Импортировать выбранные в модели
                </button>
              )}
              {check.imported ? <span className="ok-text"> ✓ импортировано моделей: {check.imported} — см. «Модели данных»</span> : null}
            </div>
          )}

          <p className="hint">
            Деплой в Docker: если PostgreSQL на этой же машине (<code>localhost</code>), контейнер подключится к нему через{' '}
            <code>host.docker.internal</code> — сервер PostgreSQL должен принимать подключения из сети Docker. Экспорт в
            FastAPI добавит драйвер <code>psycopg</code> в requirements.
          </p>
        </>
      )}
    </section>
  )
}
