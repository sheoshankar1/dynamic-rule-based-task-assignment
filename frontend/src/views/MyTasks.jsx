import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function MyTasks() {
  const [tasks, setTasks] = useState(null);
  const [error, setError] = useState(null);

  const load = () =>
    api.myTasks().then(setTasks).catch((e) => setError(e.message));

  useEffect(() => { load(); }, []);

  async function finish(id, cancelled) {
    await api.complete(id, cancelled);
    load();
  }

  // Todo -> In Progress -> Done is the status flow the brief specifies. The
  // middle state was reachable through the API but had no control here, so a
  // task could only jump straight to done.
  async function move(id, status) {
    await api.setStatus(id, status);
    load();
  }

  if (error) return <div className="panel err">{error}</div>;
  if (!tasks) return <div className="panel">Loading...</div>;

  return (
    <div className="panel">
      <h2>Assigned to me</h2>
      <p className="sub" style={{ marginBottom: 16 }}>
        Only tasks assigned to you are visible — there is no self-service pool.
      </p>
      {tasks.length === 0 ? (
        <p style={{ color: "var(--muted)" }}>Nothing assigned.</p>
      ) : (
        <table>
          <thead>
            <tr><th>#</th><th>Priority</th><th>Task</th><th>Effort</th>
                <th>Due</th><th>Status</th><th /></tr>
          </thead>
          <tbody>
            {tasks.map((t) => (
              <tr key={t.id}>
                <td>{t.id}</td>
                <td><span className={`tag p${t.priority}`}>P{t.priority}</span></td>
                <td>
                  {t.title}
                  {t.description && (
                    <div style={{ color: "var(--muted)", fontSize: 12,
                                  marginTop: 2, whiteSpace: "pre-wrap" }}>
                      {t.description}
                    </div>
                  )}
                </td>
                <td>{t.effort_hours}h</td>
                <td style={{ color: "var(--muted)" }}>{t.due_date || "—"}</td>
                <td>{t.status}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  {t.status === "todo" ? (
                    <button className="mini" onClick={() => move(t.id, "in_progress")}>
                      Start
                    </button>
                  ) : (
                    <button className="mini" onClick={() => move(t.id, "todo")}>
                      Stop
                    </button>
                  )}{" "}
                  <button className="mini" onClick={() => finish(t.id, false)}>
                    Complete
                  </button>{" "}
                  <button className="mini" onClick={() => finish(t.id, true)}>
                    Cancel
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
