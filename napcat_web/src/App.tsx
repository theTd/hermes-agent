import { Routes, Route, Navigate } from "react-router-dom";
import MonitorPage from "@/pages/MonitorPage";
import TraceDetailPage from "@/pages/TraceDetailPage";

export default function App() {
  return (
    <div className="min-h-screen bg-background text-foreground overflow-x-hidden">
      <div className="noise-overlay" />
      <div className="warm-glow" />
      <Routes>
        <Route path="/" element={<MonitorPage />} />
        <Route path="/traces/:traceId" element={<TraceDetailPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}
