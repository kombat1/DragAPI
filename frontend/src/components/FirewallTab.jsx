import { useEffect, useState } from 'react'
import { api } from '../api.js'
import ListEditor from './ListEditor.jsx'

const EGRESS_MODES = [
  ['off', 'Нет сети', 'Код блоков не может открыть ни одного соединения. Самый безопасный вариант.'],
  [
    'filtered',
    'По правилам',
    'Сети у песочницы нет; единственный выход — фильтрующий прокси платформы, который проверяет каждое соединение по правилам ниже. Работают urllib, requests, httpx и socket.create_connection.',
  ],
  ['open', 'Без ограничений', 'Общая сеть хоста. Код может обращаться куда угодно, в том числе в локальную сеть.'],
]

export const DEFAULT_FIREWALL = {
  egress: { mode: 'off', default: 'deny', block_private: true, rules: [] },
  ingress: { enabled: false, default: 'allow', trust_proxy: false, rules: [] },
}

export function normalizeFirewall(fw, legacyNetwork) {
  return {
    egress: {
      ...DEFAULT_FIREWALL.egress,
      ...(legacyNetwork && !fw?.egress?.mode ? { mode: 'open' } : {}),
      ...(fw?.egress || {}),
    },
    ingress: { ...DEFAULT_FIREWALL.ingress, ...(fw?.ingress || {}) },
  }
}

function Checker({ direction, firewall, placeholder }) {
  const [target, setTarget] = useState('')
  const [result, setResult] = useState(null)
  const check = async () => {
    if (!target.trim()) return
    try {
      setResult(await api.firewallCheck({ direction, target, firewall }))
    } catch (e) {
      setResult({ allowed: false, reason: e.message, error: true })
    }
  }
  return (
    <div className="fw-checker">
      <input
        className="mono"
        placeholder={placeholder}
        value={target}
        onChange={(e) => {
          setTarget(e.target.value)
          setResult(null)
        }}
        onKeyDown={(e) => e.key === 'Enter' && check()}
      />
      <button className="small" onClick={check}>
        Проверить
      </button>
      {result && (
        <span className={`fw-verdict ${result.allowed ? 'ok' : 'bad'}`}>
          {result.allowed ? '✓ разрешено' : '✗ запрещено'}
          {result.ip && result.host !== result.ip ? ` (${result.ip})` : ''} — {result.reason}
        </span>
      )}
    </div>
  )
}

function Journal({ projectId }) {
  const [events, setEvents] = useState([])
  useEffect(() => {
    let alive = true
    const tick = () => api.firewallLog(projectId).then((e) => alive && setEvents(e)).catch(() => {})
    tick()
    const t = setInterval(tick, 2000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [projectId])
  return (
    <section>
      <div className="section-title fw-journal-head">
        <span>Журнал firewall</span>
        <button className="small" onClick={() => api.firewallLogClear(projectId).then(() => setEvents([]))}>
          Очистить
        </button>
      </div>
      {events.length === 0 ? (
        <div className="muted">
          Пока пусто. Здесь появятся соединения кода блоков (при «По правилам») и запросы к API (если включён входящий
          firewall).
        </div>
      ) : (
        <div className="fw-journal">
          {events.slice(0, 100).map((e, i) => (
            <div key={i} className={`fw-event ${e.allowed ? 'ok' : 'bad'}`}>
              <span className="muted">{new Date(e.time * 1000).toLocaleTimeString()}</span>
              <span title={e.direction === 'in' ? 'входящий запрос к API' : 'исходящее соединение кода'}>
                {e.direction === 'in' ? '⇢ API' : 'код ⇢'}
              </span>
              <code>
                {e.direction === 'in' ? `${e.ip} ${e.method} ${e.path}` : `${e.method === 'CONNECT' ? '' : e.method + ' '}${e.host}:${e.port}`}
              </code>
              <b>{e.allowed ? '✓' : '✗'}</b>
              <span className="fw-reason">{e.reason}</span>
              {e.source && <span className="muted">· {e.source}</span>}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

export default function FirewallTab({ projectId, firewall, setFirewall }) {
  const fw = firewall
  const setEgress = (patch) => setFirewall({ ...fw, egress: { ...fw.egress, ...patch } })
  const setIngress = (patch) => setFirewall({ ...fw, ingress: { ...fw.ingress, ...patch } })
  const [myIp, setMyIp] = useState(null)
  useEffect(() => {
    api.whoami().then((r) => setMyIp(r.ip)).catch(() => {})
  }, [])
  const eg = fw.egress
  const ing = fw.ingress
  const hasMyIp = myIp && ing.rules.some((r) => r.target === myIp)

  return (
    <div className="firewall-tab">
      <section>
        <div className="section-title">Исходящие: код блоков → сеть</div>
        <div className="radio-cards">
          {EGRESS_MODES.map(([value, title, desc]) => (
            <label key={value} className={`radio-card ${eg.mode === value ? 'checked' : ''}`}>
              <input type="radio" name="egress-mode" checked={eg.mode === value} onChange={() => setEgress({ mode: value })} />
              <div>
                <b>{title}</b>
                <div className="muted">{desc}</div>
              </div>
            </label>
          ))}
        </div>

        {eg.mode === 'filtered' && (
          <>
            <div className="fw-defaults">
              <span className="field-label">Если ни одно правило не подошло:</span>
              <label>
                <input type="radio" checked={eg.default === 'deny'} onChange={() => setEgress({ default: 'deny' })} /> запретить
                (белый список)
              </label>
              <label>
                <input type="radio" checked={eg.default === 'allow'} onChange={() => setEgress({ default: 'allow' })} /> разрешить
                (чёрный список)
              </label>
            </div>
            <label className="toggle">
              <input
                type="checkbox"
                checked={eg.block_private}
                onChange={(e) => setEgress({ block_private: e.target.checked })}
              />
              <div>
                <b>Запретить локальную сеть и метаданные облака</b>
                <div className="muted">
                  127.0.0.0/8, 10/8, 172.16/12, 192.168/16, 169.254.169.254 и т.п. Защита от SSRF. Общие правила (
                  <code>*</code>, <code>0.0.0.0/0</code>) этот запрет не снимают — только явное правило для адреса или имени.
                </div>
              </div>
            </label>
            <div className="field">
              <div className="field-label">Правила — применяется первое совпавшее</div>
              <ListEditor
                items={eg.rules}
                onChange={(rules) => setEgress({ rules })}
                reorder
                numbered
                empty={eg.default === 'deny' ? 'Правил нет — все соединения запрещены' : 'Правил нет — разрешено всё, кроме локальной сети'}
                addLabel="правило"
                newItem={() => ({ action: 'allow', target: '', ports: '443', comment: '' })}
                columns={[
                  { key: 'action', label: 'действие', options: ['allow', 'deny'], flex: 0.9 },
                  { key: 'target', label: 'api.example.com, *.example.com, 1.2.3.4, 10.0.0.0/8', mono: true, flex: 2.6 },
                  { key: 'ports', label: 'порты: 443, 80,443, 8000-8100 (пусто — любые)', mono: true, flex: 1.3 },
                  { key: 'comment', label: 'комментарий', flex: 1.4 },
                ]}
              />
            </div>
            <div className="field">
              <div className="field-label">Проверить адрес</div>
              <Checker direction="out" firewall={fw} placeholder="api.github.com:443 или https://api.github.com/users" />
            </div>
          </>
        )}
      </section>

      <section>
        <div className="section-title">Входящие: клиенты → API проекта</div>
        <label className="toggle">
          <input type="checkbox" checked={ing.enabled} onChange={(e) => setIngress({ enabled: e.target.checked })} />
          <div>
            <b>Фильтровать запросы к API по IP</b>
            <div className="muted">
              Работает в превью (<code>/run/…</code>, включая Swagger) и попадает в экспортированный проект как middleware.
              Запрещённые запросы получают 403.
            </div>
          </div>
        </label>
        {ing.enabled && (
          <>
            <div className="fw-defaults">
              <span className="field-label">Если ни одно правило не подошло:</span>
              <label>
                <input type="radio" checked={ing.default === 'allow'} onChange={() => setIngress({ default: 'allow' })} />{' '}
                разрешить
              </label>
              <label>
                <input type="radio" checked={ing.default === 'deny'} onChange={() => setIngress({ default: 'deny' })} /> запретить
              </label>
            </div>
            <div className="field">
              <div className="field-label">Правила — применяется первое совпавшее</div>
              <ListEditor
                items={ing.rules}
                onChange={(rules) => setIngress({ rules })}
                reorder
                numbered
                empty="Правил нет"
                addLabel="правило"
                newItem={() => ({ action: 'allow', target: '', comment: '' })}
                columns={[
                  { key: 'action', label: 'действие', options: ['allow', 'deny'], flex: 0.9 },
                  { key: 'target', label: 'IP или подсеть: 203.0.113.5, 10.0.0.0/8, 2001:db8::/32', mono: true, flex: 2.6 },
                  { key: 'comment', label: 'комментарий', flex: 1.6 },
                ]}
              />
              {myIp && (
                <div className="hint">
                  Ваш IP для платформы: <code>{myIp}</code>{' '}
                  {!hasMyIp && (
                    <button
                      className="linklike"
                      onClick={() =>
                        setIngress({ rules: [{ action: 'allow', target: myIp, comment: 'я (для Swagger и тестов)' }, ...ing.rules] })
                      }
                    >
                      разрешить себе
                    </button>
                  )}
                  {ing.default === 'deny' && !hasMyIp && (
                    <span className="weak-text"> — иначе превью и Swagger будут отвечать вам 403</span>
                  )}
                </div>
              )}
            </div>
            <label className="toggle">
              <input type="checkbox" checked={ing.trust_proxy} onChange={(e) => setIngress({ trust_proxy: e.target.checked })} />
              <div>
                <b>Брать IP из X-Forwarded-For</b>
                <div className="muted">
                  Включайте, только если API стоит за вашим обратным прокси (nginx, балансировщик): иначе клиент сможет
                  подставить любой IP в заголовок.
                </div>
              </div>
            </label>
            <div className="field">
              <div className="field-label">Проверить IP</div>
              <Checker direction="in" firewall={fw} placeholder="203.0.113.5" />
            </div>
          </>
        )}
      </section>

      <Journal projectId={projectId} />
    </div>
  )
}
