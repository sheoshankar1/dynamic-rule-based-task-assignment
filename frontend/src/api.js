// Thin API client. Tokens live in localStorage: adequate for an internal admin
// surface, and called out in README section 13 as a deliberate simplification
// rather than an oversight -- httpOnly cookies would be the production choice.

const ACCESS = "access_token";
const REFRESH = "refresh_token";

export const getToken = () => localStorage.getItem(ACCESS);
export const isAuthed = () => Boolean(getToken());

// Read the role from the access token so the UI can hide actions the caller
// cannot perform. Presentation only -- the server enforces every rule anyway,
// so a forged claim buys a visible button and a 403.
export function currentUser() {
  const token = getToken();
  if (!token) return null;
  try {
    const [, payload] = token.split(".");
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    const { role, username } = JSON.parse(json);
    return { role, username };
  } catch {
    return null;   // malformed token: treat as the least-privileged case
  }
}

export function clearTokens() {
  localStorage.removeItem(ACCESS);
  localStorage.removeItem(REFRESH);
}

async function request(path, { method = "GET", body, retry = true } = {}) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  // One transparent refresh attempt, then give up rather than loop.
  if (res.status === 401 && retry && localStorage.getItem(REFRESH)) {
    const ok = await refresh();
    if (ok) return request(path, { method, body, retry: false });
    clearTokens();
  }

  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const err = new Error(data?.detail || `${res.status} ${res.statusText}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

async function refresh() {
  const res = await fetch("/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh: localStorage.getItem(REFRESH) }),
  });
  if (!res.ok) return false;
  const { access } = await res.json();
  localStorage.setItem(ACCESS, access);
  return true;
}

// Assignment runs in a worker, so a freshly created task reports `pending`
// until the worker gets to it. Poll the task until the outcome settles rather
// than telling the user to do it themselves.
export async function pollAssignment(id, { attempts = 10, gap = 600 } = {}) {
  for (let i = 0; i < attempts; i += 1) {
    await new Promise((r) => setTimeout(r, gap));
    const task = await api.task(id);
    if (task.assignee || !task.assignment.startsWith("pending")) return task;
  }
  return null;   // worker is down or backed up -- say so, do not invent a reason
}

export async function login(username, password) {
  const res = await fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) throw new Error("Login failed");
  const { access, refresh: r } = await res.json();
  localStorage.setItem(ACCESS, access);
  localStorage.setItem(REFRESH, r);
}

export const api = {
  createTask: (payload) => request("/tasks/", { method: "POST", body: payload }),
  task: (id) => request(`/tasks/${id}`),
  updateRules: (id, rules) =>
    request(`/tasks/${id}`, { method: "PATCH", body: { rules } }),
  myTasks: () => request("/my-eligible-tasks"),
  complete: (id, cancelled) =>
    request(`/tasks/${id}/complete${cancelled ? "?cancelled=1" : ""}`, {
      method: "POST",
    }),
};
