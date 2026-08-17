import React, { useState } from "react";
import { login } from "../api.js";

export default function Login({ onDone }) {
  const [username, setUsername] = useState("manager");
  const [password, setPassword] = useState("manager");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
      onDone();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="wrap" style={{ maxWidth: 380, paddingTop: 80 }}>
      <h1>Sign in</h1>
      <p className="sub">
        Seeded logins: manager/manager, admin/admin, or any userNNNN/demo
      </p>
      <form className="panel" onSubmit={submit}>
        <label>Username</label>
        <input value={username} onChange={(e) => setUsername(e.target.value)} />
        <label>Password</label>
        <input type="password" value={password}
               onChange={(e) => setPassword(e.target.value)} />
        <button className="go" disabled={busy}>
          {busy ? "Signing in..." : "Sign in"}
        </button>
        {error && <div className="err">{error}</div>}
      </form>
    </div>
  );
}
