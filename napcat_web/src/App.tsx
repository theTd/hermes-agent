import { useCallback, useEffect, useState } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import MonitorPage from "@/pages/MonitorPage";
import TraceDetailPage from "@/pages/TraceDetailPage";
import DashboardPage from "@/pages/DashboardPage";
import LoginPage from "@/pages/LoginPage";
import {
  OBS_AUTH_REQUIRED_EVENT,
  getObsAuthStatus,
  loginObsAccessToken,
  logoutObsAccess,
} from "@/lib/napcat-api";

export default function App() {
  const [authEnabled, setAuthEnabled] = useState(false);
  const [authenticated, setAuthenticated] = useState(true);
  const [checkingAuth, setCheckingAuth] = useState(true);
  const [authLoading, setAuthLoading] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);

  const refreshAuthStatus = useCallback(async () => {
    try {
      const status = await getObsAuthStatus();
      setAuthEnabled(status.enabled);
      setAuthenticated(status.authenticated);
      setAuthError(null);
    } catch (error) {
      setAuthEnabled(true);
      setAuthenticated(false);
      const message = error instanceof Error ? error.message : "Unable to check access status";
      setAuthError(message);
    } finally {
      setCheckingAuth(false);
    }
  }, []);

  const handleLogin = useCallback(async (token: string) => {
    setAuthLoading(true);
    setAuthError(null);
    try {
      await loginObsAccessToken(token);
      setAuthenticated(true);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Login failed";
      setAuthError(message);
      setAuthenticated(false);
    } finally {
      setAuthLoading(false);
    }
  }, []);

  const handleLogout = useCallback(async () => {
    setAuthLoading(true);
    try {
      await logoutObsAccess();
    } catch {
      // Keep behavior simple: always return to login screen.
    } finally {
      setAuthenticated(false);
      setAuthLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshAuthStatus();
  }, [refreshAuthStatus]);

  useEffect(() => {
    const onAuthRequired = () => {
      setAuthEnabled(true);
      setAuthenticated(false);
      setAuthError("Session expired. Please log in again.");
    };
    window.addEventListener(OBS_AUTH_REQUIRED_EVENT, onAuthRequired);
    return () => window.removeEventListener(OBS_AUTH_REQUIRED_EVENT, onAuthRequired);
  }, []);

  if (checkingAuth) {
    return (
      <div className="min-h-screen bg-background text-foreground grid place-items-center">
        <p className="font-mono-ui text-sm text-muted-foreground">Checking observatory access...</p>
      </div>
    );
  }

  if (authEnabled && !authenticated) {
    return (
      <div className="min-h-screen bg-background text-foreground overflow-x-hidden">
        <div className="noise-overlay" />
        <div className="warm-glow" />
        <LoginPage loading={authLoading} error={authError} onSubmitToken={handleLogin} />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background text-foreground overflow-x-hidden">
      <div className="noise-overlay" />
      <div className="warm-glow" />
      <Routes>
        <Route
          path="/"
          element={(
            <MonitorPage
              showLogout={authEnabled}
              logoutLoading={authLoading}
              onLogout={handleLogout}
            />
          )}
        />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/traces/:traceId" element={<TraceDetailPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}
