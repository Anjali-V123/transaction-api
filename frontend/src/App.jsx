import { useEffect, useState, useCallback } from 'react'
import './App.css'

function uuid() {
  // Idempotency keys need to be unique per logical order attempt, not
  // per HTTP request -- generated once client-side so a retried request
  // (e.g. the browser resending on a flaky connection) reuses the same
  // key and the API treats it as the same order rather than a new one.
  return crypto.randomUUID()
}

// ---- Auth screen: one form that toggles between login and signup ----
function AuthScreen({ onAuthenticated }) {
  const [mode, setMode] = useState('login') // 'login' | 'signup'
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      if (mode === 'signup') {
        const signupRes = await fetch('/auth/signup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password }),
        })
        if (!signupRes.ok) {
          const body = await signupRes.json().catch(() => ({}))
          throw new Error(body.detail || 'Could not create account')
        }
      }
      const loginRes = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      })
      if (!loginRes.ok) {
        const body = await loginRes.json().catch(() => ({}))
        throw new Error(body.detail || 'Login failed')
      }
      const { access_token } = await loginRes.json()
      localStorage.setItem('txn_token', access_token)
      onAuthenticated(access_token)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app">
      <header>
        <h1>Transaction Dashboard</h1>
      </header>
      <section className="auth-card">
        <h2>{mode === 'login' ? 'Log in' : 'Create an account'}</h2>
        <form onSubmit={submit} className="auth-form">
          <label>
            Email
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              required
            />
          </label>
          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={mode === 'signup' ? 'At least 8 characters' : ''}
              minLength={mode === 'signup' ? 8 : undefined}
              required
            />
          </label>
          {error && <div className="error">{error}</div>}
          <button type="submit" disabled={busy}>
            {busy ? 'Please wait...' : mode === 'login' ? 'Log in' : 'Sign up'}
          </button>
        </form>
        <button
          type="button"
          className="link-button"
          onClick={() => { setMode(mode === 'login' ? 'signup' : 'login'); setError('') }}
        >
          {mode === 'login' ? "Don't have an account? Sign up" : 'Already have an account? Log in'}
        </button>
      </section>
    </div>
  )
}

export default function App() {
  const [token, setToken] = useState(() => localStorage.getItem('txn_token') || '')
  const [customerEmail, setCustomerEmail] = useState('')
  const [customerId, setCustomerId] = useState('')
  const [isAdmin, setIsAdmin] = useState(false)
  const [checkingAuth, setCheckingAuth] = useState(true)

  const [inventory, setInventory] = useState([])
  const [orders, setOrders] = useState([])
  const [instance, setInstance] = useState(null)
  const [error, setError] = useState(null)

  const [newItem, setNewItem] = useState({ sku: '', name: '', price: '', quantity_available: '' })
  const [newOrder, setNewOrder] = useState({ sku: '', quantity: 1 })

  const authHeaders = useCallback(
    () => (token ? { Authorization: `Bearer ${token}` } : {}),
    [token]
  )

  function logout() {
    localStorage.removeItem('txn_token')
    setToken('')
    setCustomerEmail('')
  }

  // Validate whatever token is in localStorage on first load -- if it's
  // expired or invalid, drop back to the auth screen instead of showing a
  // dashboard that will just fail on every request.
  useEffect(() => {
    if (!token) { setCheckingAuth(false); return }
    fetch('/auth/me', { headers: authHeaders() })
      .then((res) => {
        if (!res.ok) throw new Error('invalid token')
        return res.json()
      })
      .then((me) => { setCustomerEmail(me.email); setCustomerId(me.id); setIsAdmin(!!me.is_admin) })
      .catch(() => logout())
      .finally(() => setCheckingAuth(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const refreshAll = useCallback(async () => {
    try {
      const [invRes, ordRes, healthRes] = await Promise.all([
        fetch('/inventory'),
        fetch('/orders', { headers: authHeaders() }),
        fetch('/health'),
      ])
      setInventory(await invRes.json())
      setOrders(ordRes.ok ? await ordRes.json() : [])
      const health = await healthRes.json()
      setInstance(health.instance)
    } catch {
      setError('Could not reach the API. Is the backend running?')
    }
  }, [authHeaders])

  useEffect(() => {
    if (!token) return
    refreshAll()
    // Poll periodically -- when this is behind the nginx load balancer,
    // watch the "served by" badge below alternate between api1 / api2
    // as different requests land on different replicas.
    const id = setInterval(refreshAll, 4000)
    return () => clearInterval(id)
  }, [token, refreshAll])

  // Every write goes through this: on a non-2xx response, surface the
  // server's actual error detail (e.g. "Insufficient inventory") instead
  // of silently doing nothing, which is what a bare fetch() does by
  // default -- fetch only rejects on a network failure, never on a 4xx/5xx.
  async function apiCall(url, body) {
    setError(null)
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const problem = await res.json().catch(() => ({}))
        if (res.status === 401) {
          setError('Your session expired -- please log in again.')
          logout()
          return false
        }
        setError(problem.detail || `Request failed (${res.status})`)
        return false
      }
      return true
    } catch {
      setError('Could not reach the API. Is the backend running?')
      return false
    }
  }

  async function addInventory(e) {
    e.preventDefault()
    const ok = await apiCall('/inventory', {
      sku: newItem.sku,
      name: newItem.name,
      price: parseFloat(newItem.price),
      quantity_available: parseInt(newItem.quantity_available, 10),
    })
    if (ok) {
      setNewItem({ sku: '', name: '', price: '', quantity_available: '' })
      refreshAll()
    }
  }

  async function createOrder(e) {
    e.preventDefault()
    const ok = await apiCall('/orders', {
      idempotency_key: uuid(),
      sku: newOrder.sku,
      quantity: parseInt(newOrder.quantity, 10),
    })
    if (ok) {
      setNewOrder({ sku: '', quantity: 1 })
      refreshAll()
    }
  }

  async function pay(orderId, simulateFailure) {
    const ok = await apiCall(`/orders/${orderId}/pay`, { simulate_failure: simulateFailure })
    if (ok) refreshAll()
  }

  if (checkingAuth) return null // avoid a login-screen flash while we validate a stored token

  if (!token) {
    return (
      <AuthScreen
        onAuthenticated={(newToken) => {
          setToken(newToken)
          fetch('/auth/me', { headers: { Authorization: `Bearer ${newToken}` } })
            .then((r) => r.json())
            .then((me) => { setCustomerEmail(me.email); setCustomerId(me.id); setIsAdmin(!!me.is_admin) })
        }}
      />
    )
  }

  return (
    <div className="app">
      <header>
        <h1>Transaction Dashboard</h1>
        {instance && <span className="badge">served by: {instance}</span>}
        <span className="spacer" />
        <span className="who">{customerEmail}</span>
        <button className="logout-button" onClick={logout}>Log out</button>
      </header>

      {error && <div className="error">{error}</div>}

      <section>
        <h2>Inventory</h2>
        <table>
          <thead>
            <tr><th>SKU</th><th>Name</th><th>Price</th><th>Available</th></tr>
          </thead>
          <tbody>
            {inventory.map((i) => (
              <tr key={i.sku}>
                <td>{i.sku}</td><td>{i.name}</td><td>${i.price.toFixed(2)}</td>
                <td>
                  {i.quantity_available > 0
                    ? i.quantity_available
                    : <span className="status status-failed">Out of stock</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {isAdmin ? (
          <form onSubmit={addInventory} className="row">
            <input placeholder="SKU" value={newItem.sku} onChange={(e) => setNewItem({ ...newItem, sku: e.target.value })} required />
            <input placeholder="Name" value={newItem.name} onChange={(e) => setNewItem({ ...newItem, name: e.target.value })} required />
            <input placeholder="Price" type="number" step="0.01" value={newItem.price} onChange={(e) => setNewItem({ ...newItem, price: e.target.value })} required />
            <input placeholder="Qty" type="number" value={newItem.quantity_available} onChange={(e) => setNewItem({ ...newItem, quantity_available: e.target.value })} required />
            <button type="submit">Add item</button>
          </form>
        ) : (
          <p className="who">Only admin accounts can add inventory.</p>
        )}
      </section>

      <section>
        <h2>Orders</h2>
        <table>
          <thead>
            <tr><th>Customer</th><th>SKU</th><th>Qty</th><th>Total</th><th>Status</th><th>Actions</th></tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id}>
                <td>{o.customer_id === customerId ? 'You' : o.customer_id}</td><td>{o.sku}</td><td>{o.quantity}</td>
                <td>${o.total_amount.toFixed(2)}</td>
                <td><span className={`status status-${o.status.toLowerCase()}`}>{o.status}</span></td>
                <td>
                  {o.status === 'PENDING' && (
                    <>
                      <button onClick={() => pay(o.id, false)}>Pay</button>
                      <button onClick={() => pay(o.id, true)} className="danger">Simulate failure</button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <form onSubmit={createOrder} className="row">
          <input placeholder="SKU" value={newOrder.sku} onChange={(e) => setNewOrder({ ...newOrder, sku: e.target.value })} required />
          <input placeholder="Qty" type="number" min="1" value={newOrder.quantity} onChange={(e) => setNewOrder({ ...newOrder, quantity: e.target.value })} required />
          <button type="submit">Create order</button>
        </form>
      </section>
    </div>
  )
}
