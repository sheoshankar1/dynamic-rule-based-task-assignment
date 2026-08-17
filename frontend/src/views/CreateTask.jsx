import React, { useState } from "react";
import { api, pollAssignment } from "../api.js";

const DEPARTMENTS = ["Finance", "HR", "IT", "Operations"];
const LOCATIONS = ["Bangalore", "Pune", "Delhi", "Remote"];

// The rule builder mirrors the closed predicate set exactly (README D3). There
// is no free-text rule input because there is no rule DSL -- the shape of this
// form IS the shape of the rule.
export default function CreateTask() {
  const [form, setForm] = useState({
    title: "", description: "", due_date: "", priority: 2, effort_hours: "2.0",
  });
  const [department, setDepartment] = useState("Finance");
  const [location, setLocation] = useState("");
  const [minYears, setMinYears] = useState("");
  const [cap, setCap] = useState("5");
  const [result, setResult] = useState(null);
  const [settled, setSettled] = useState(null);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  // Single-valued on purpose: a rule names one department and at most one
  // location. The API rejects more than one rather than silently truncating.
  const pick = (current, set, value) => set(current === value ? "" : value);

  function buildRule() {
    const rule = {};
    if (department) rule.department = department;
    if (location) rule.location = location;
    if (minYears !== "") rule.experience_years = { gte: Number(minYears) };
    if (cap !== "") rule.max_active_tasks = Number(cap);
    return rule;
  }

  async function submit(event) {
    event.preventDefault();
    setBusy(true); setError(null); setResult(null);
    try {
      const payload = { ...form, rules: buildRule() };
      if (!payload.due_date) delete payload.due_date;   // DRF rejects ""
      if (!payload.description) delete payload.description;
      const created = await api.createTask(payload);
      setResult(created);
      setSettled(null);

      if (!created.assignee) {
        setWaiting(true);
        try {
          setSettled(await pollAssignment(created.id));
        } finally {
          setWaiting(false);
        }
      }
    } catch (err) {
      setError(err.data ? JSON.stringify(err.data) : err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <div className="row">
        <div className="panel">
          <h2>Task</h2>
          <label>Title</label>
          <input required value={form.title}
                 onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <label>Description</label>
          <textarea rows="4" value={form.description}
                    placeholder="What actually needs doing"
                    onChange={(e) => setForm({ ...form, description: e.target.value })} />
          <label>Due date</label>
          <input type="date" value={form.due_date}
                 onChange={(e) => setForm({ ...form, due_date: e.target.value })} />
          <label>Priority (0 = P0, highest)</label>
          <select value={form.priority}
                  onChange={(e) => setForm({ ...form, priority: Number(e.target.value) })}>
            {[0, 1, 2, 3].map((p) => <option key={p} value={p}>P{p}</option>)}
          </select>
          <label>Effort (hours) — orders selection, does not cap</label>
          <input required type="number" step="0.5" min="0.5" value={form.effort_hours}
                 onChange={(e) => setForm({ ...form, effort_hours: e.target.value })} />
        </div>

        <div className="panel">
          <h2>Assignment rule</h2>
          <label>Department</label>
          <div>
            {DEPARTMENTS.map((d) => (
              <button type="button" key={d} className="mini"
                      style={{ marginRight: 4, marginBottom: 4,
                               borderColor: department === d ? "var(--accent)" : undefined,
                               color: department === d ? "var(--text)" : undefined }}
                      onClick={() => pick(department, setDepartment, d)}>{d}</button>
            ))}
          </div>
          <label>Location (leave unselected for anywhere)</label>
          <div>
            {LOCATIONS.map((l) => (
              <button type="button" key={l} className="mini"
                      style={{ marginRight: 4, marginBottom: 4,
                               borderColor: location === l ? "var(--accent)" : undefined,
                               color: location === l ? "var(--text)" : undefined }}
                      onClick={() => pick(location, setLocation, l)}>{l}</button>
            ))}
          </div>
          <label>Minimum experience (years)</label>
          <input type="number" min="0" value={minYears} placeholder="any"
                 onChange={(e) => setMinYears(e.target.value)} />
          <label>Max active tasks per user (the capacity cap)</label>
          <input type="number" min="1" value={cap} placeholder="uncapped"
                 onChange={(e) => setCap(e.target.value)} />
        </div>
      </div>

      <div className="panel">
        <h2>Rule as sent</h2>
        <pre style={{ margin: 0, color: "var(--muted)" }}>
          {JSON.stringify(buildRule(), null, 2)}
        </pre>
        {/* Clicking the selected department clears it, which is easy to do by
            accident and produces a rule matching everyone. Say so rather than
            forbidding it -- "anyone may do this" is a legitimate rule. */}
        {!department && (
          <div style={{ color: "var(--warn)", marginTop: 10 }}>
            No department selected — this rule matches <strong>every user</strong>.
            Click a department to narrow it.
          </div>
        )}
        <button className="go" disabled={busy}>
          {busy ? "Creating..." : "Create task"}
        </button>
        {error && <div className="err">{error}</div>}
        {result && (
          <div className="ok">
            Task <code>#{result.id}</code> —{" "}
            {settled
              ? settled.assignee
                ? <>assigned to <code>{settled.assignee}</code></>
                : settled.assignment
              : waiting
                ? <span style={{ color: "var(--muted)" }}>
                    placement queued, waiting for the worker…
                  </span>
                : result.assignee
                  ? <>assigned to user <code>{result.assignee}</code></>
                  : <span style={{ color: "var(--warn)" }}>
                      still unassigned after 6s — is the Celery worker running?
                    </span>}
            <div style={{ color: "var(--muted)", marginTop: 6 }}>
              fingerprint <code>{result.rule_fingerprint.slice(0, 16)}</code>
              {result.rule_reused
                ? " — reused, no recompute needed"
                : " — new rule, materialised on demand"}
            </div>
          </div>
        )}
      </div>
    </form>
  );
}
