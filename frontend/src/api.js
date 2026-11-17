async function request(method, url, body) {
  const headers = { 'X-DragAPI': '1' } // защита от CSRF: чужой сайт не может выставить этот заголовок
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const res = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (res.status === 401 && !url.startsWith('/api/auth/')) {
    window.dispatchEvent(new Event('dragapi:unauthorized'))
  }
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
  me: () => request('GET', '/api/auth/me'),
  setup: (username, password) => request('POST', '/api/auth/setup', { username, password }),
  login: (username, password) => request('POST', '/api/auth/login', { username, password }),
  logout: () => request('POST', '/api/auth/logout'),
  changePassword: (old_password, password) => request('POST', '/api/auth/password', { old_password, password }),
  users: () => request('GET', '/api/users'),
  usersBrief: () => request('GET', '/api/users/brief'),
  createUser: (u) => request('POST', '/api/users', u),
  deleteUser: (id) => request('DELETE', `/api/users/${id}`),
  resetPassword: (id, password) => request('PUT', `/api/users/${id}/password`, { password }),
  members: (pid) => request('GET', `/api/projects/${pid}/members`),
  setMembers: (pid, members) => request('PUT', `/api/projects/${pid}/members`, { members }),
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
  testRequest: (pid, body) => request('POST', `/api/projects/${pid}/test-request`, body),
  runTests: (pid, body) => request('POST', `/api/projects/${pid}/tests/run`, body),
  schemaPlan: (pid, models) => request('POST', `/api/projects/${pid}/schema/plan`, { models }),
  schemaDropExtra: (pid, models) => request('POST', `/api/projects/${pid}/schema/drop-extra`, { models }),
  deployStatus: (pid) => request('GET', `/api/projects/${pid}/deploy`),
  deploy: (pid, body) => request('POST', `/api/projects/${pid}/deploy`, body),
  deployStop: (pid, removeData) => request('DELETE', `/api/projects/${pid}/deploy?remove_data=${removeData ? 'true' : 'false'}`),
  deployLogs: (pid) => request('GET', `/api/projects/${pid}/deploy/logs`),
  exportUrl: (id) => `/api/projects/${id}/export`,
}
