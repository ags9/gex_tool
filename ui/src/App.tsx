import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { todayEt } from "./components/minute";
import Live from "./pages/Live";
import Research from "./pages/Research";
import Session from "./pages/Session";

export default function App() {
  return (
    <div className="min-h-screen">
      <nav className="flex items-center gap-1 border-b border-neutral-800 bg-neutral-950 px-4 py-1.5 text-xs">
        <Tab to="/">Live</Tab>
        <Tab to={`/session/${todayEt()}`}>Session</Tab>
        <Tab to="/research">Research</Tab>
        <span className="ml-auto text-neutral-700">
          read-only · localhost
        </span>
      </nav>

      <Routes>
        <Route path="/" element={<Live />} />
        <Route path="/session" element={<Navigate to={`/session/${todayEt()}`} replace />} />
        <Route path="/session/:date" element={<Session />} />
        <Route path="/research" element={<Research />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}

function Tab({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        `rounded px-3 py-1.5 transition-colors ${
          isActive
            ? "bg-neutral-800 text-neutral-100"
            : "text-neutral-500 hover:bg-neutral-900 hover:text-neutral-300"
        }`
      }
    >
      {children}
    </NavLink>
  );
}
