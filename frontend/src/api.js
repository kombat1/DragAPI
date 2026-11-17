async function request(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const text = await res.text()
  let data
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!res.ok) {
    const err = new Error(data?.detail?.message || data?.detail || res.statusText)
    err.status = res.status
    err.data = data
    throw err
  }
  return data
}

export const api = {
  blocks: () => request('GET', '/api/blocks'),
  projects: () => request('GET', '/api/projects'),
  createProject: (name) => request('POST', '/api/projects', { name }),
  project: (id) => request('GET', `/api/projects/${id}`),
  save: (id, graph) => request('PUT', `/api/projects/${id}`, graph),
  remove: (id) => request('DELETE', `/api/projects/${id}`),
  validate: (graph, pid) => request('POST', `/api/validate${pid ? `?project_id=${encodeURIComponent(pid)}` : ''}`, graph),
  run: (id) => request('POST', `/api/projects/${id}/run`),
  stop: (id) => request('POST', `/api/projects/${id}/stop`),
  logs: (id) => request('GET', `/api/projects/${id}/logs`),
  clearLogs: (id) => request('DELETE', `/api/projects/${id}/logs`),
  versions: (id) => request('GET', `/api/projects/${id}/versions`),
  restore: (id, v) => request('POST', `/api/projects/${id}/versions/${v}/restore`),
  dataOverview: (id) => request('GET', `/api/projects/${id}/data`),
  dataRows: (id, model) => request('GET', `/api/projects/${id}/data/${model}`),
  dataClear: (id, model) => request('DELETE', `/api/projects/${id}/data/${model}`),
  sandbox: () => request('GET', '/api/sandbox'),
  sandboxDescribe: (sandbox, firewall) => request('POST', '/api/sandbox/describe', { sandbox: sandbox || {}, firewall: firewall || {} }),
  firewallCheck: (body) => request('POST', '/api/firewall/check', body),
  firewallLog: (pid) => request('GET', `/api/projects/${pid}/firewall/log`),
  firewallLogClear: (pid) => request('DELETE', `/api/projects/${pid}/firewall/log`),
  whoami: () => request('GET', '/api/whoami'),
  sandboxRun: (pid, body) => request('POST', `/api/projects/${pid}/sandbox/run`, body),
  librariesStatus: (pid, body) => request('POST', `/api/projects/${pid}/libraries/status`, body),
  librariesInstall: (pid, body) => request('POST', `/api/projects/${pid}/libraries/install`, body),
  librariesRemove: (pid) => request('DELETE', `/api/projects/${pid}/libraries`),
  exportUrl: (id) => `/api/projects/${id}/export`,
}
