const BASE = '/api'

export function token() {
  return localStorage.getItem('token') || ''
}

async function req(method, path, body) {
  const resp = await fetch(BASE + path, {
    method,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token()}`,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (resp.status === 401) {
    localStorage.removeItem('token')
    location.href = '/login'
    throw new Error('未登录')
  }
  if (!resp.ok) {
    const text = await resp.text()
    throw new Error(`${resp.status}: ${text}`)
  }
  return resp.json()
}

export const api = {
  get: (p) => req('GET', p),
  post: (p, b) => req('POST', p, b ?? {}),
  login: (username, password) =>
    fetch(BASE + '/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }).then(async (r) => {
      if (!r.ok) throw new Error('账号或密码错误')
      return r.json()
    }),
}
