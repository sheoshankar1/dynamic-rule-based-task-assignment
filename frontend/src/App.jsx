import React, { useState } from "react";
import { isAuthed, clearTokens } from "./api.js";
import Login from "./views/Login.jsx";
import CreateTask from "./views/CreateTask.jsx";
import MyTasks from "./views/MyTasks.jsx";

const TABS = [
  ["create", "Create task", CreateTask],
  ["mine", "My tasks", MyTasks],
];

export default function App() {
  const [authed, setAuthed] = useState(isAuthed());
  const [tab, setTab] = useState("create");

  if (!authed) return <Login onDone={() => setAuthed(true)} />;

  const View = TABS.find(([key]) => key === tab)[2];
  return (
    <div className="wrap">
      <h1>Dynamic Rule-Based Task Assignment</h1>
      <p className="sub">
        Tasks are never assigned by hand. Each task carries a rule; the system
        computes eligible users and assigns in the background, in priority order.
        {" "}<a href="/docs" style={{ color: "var(--accent)" }}>API docs</a>
      </p>
      <nav>
        {TABS.map(([key, label]) => (
          <button key={key} className={tab === key ? "active" : ""}
                  onClick={() => setTab(key)}>{label}</button>
        ))}
        <button onClick={() => { clearTokens(); setAuthed(false); }}>Sign out</button>
      </nav>
      <View />
    </div>
  );
}
