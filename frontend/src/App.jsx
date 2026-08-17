import React, { useEffect, useState } from "react";
import { isAuthed, logout, currentUser } from "./api.js";
import Login from "./views/Login.jsx";
import CreateTask from "./views/CreateTask.jsx";
import MyTasks from "./views/MyTasks.jsx";

const ALL_TABS = [
  ["create", "Create task", CreateTask, ["manager", "admin"]],
  ["mine", "My tasks", MyTasks, null],   // null = every role
];

export default function App() {
  const [authed, setAuthed] = useState(isAuthed());
  const user = authed ? currentUser() : null;
  // Authoring is restricted to Managers and Admins, so a User is not shown a
  // form the API would reject after they had filled it in.
  const tabs = ALL_TABS.filter(([, , , roles]) => !roles || roles.includes(user?.role));
  const [tab, setTab] = useState(tabs[0][0]);

  // The tab list depends on the role, which is unknown on the first render --
  // so the initial state above is computed while logged out and would strand a
  // Manager on the wrong tab after signing in. Re-seat it whenever auth changes.
  useEffect(() => {
    setTab(tabs[0][0]);
  }, [authed]);   // eslint-disable-line react-hooks/exhaustive-deps

  if (!authed) return <Login onDone={() => setAuthed(true)} />;

  const active = tabs.find(([key]) => key === tab) || tabs[0];
  const View = active[2];
  return (
    <div className="wrap">
      <h1>Dynamic Rule-Based Task Assignment</h1>
      <p className="sub">
        Tasks are never assigned by hand. Each task carries a rule; the system
        computes eligible users and assigns in the background, in priority order.
        {" "}<a href="/docs" style={{ color: "var(--accent)" }}>API docs</a>
      </p>
      <nav>
        {tabs.map(([key, label]) => (
          <button key={key} className={active[0] === key ? "active" : ""}
                  onClick={() => setTab(key)}>{label}</button>
        ))}
        {user && <span className="tag" style={{ alignSelf: "center", marginLeft: 4 }}>
          {user.username} · {user.role}
        </span>}
        <button onClick={() => { logout(); setAuthed(false); }}>Sign out</button>
      </nav>
      <View />
    </div>
  );
}
