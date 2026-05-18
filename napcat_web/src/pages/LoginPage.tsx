import { useState } from "react";
import type { FormEvent } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface LoginPageProps {
  loading: boolean;
  error: string | null;
  onSubmitToken: (token: string) => Promise<void>;
}

export default function LoginPage({ loading, error, onSubmitToken }: LoginPageProps) {
  const [token, setToken] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [showToken, setShowToken] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedToken = token.trim();
    if (!trimmedToken) {
      setLocalError("Please enter an access token.");
      return;
    }
    setLocalError(null);
    await onSubmitToken(trimmedToken);
  }

  return (
    <div className="min-h-screen grid place-items-center px-4">
      <Card className="max-w-md border-border/60 bg-card/85 backdrop-blur w-full">
        <CardHeader>
          <CardTitle>NapCat Observatory Access</CardTitle>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={onSubmit}>
            <p className="text-sm text-muted-foreground leading-relaxed">
              Enter the preconfigured access token to open the Observatory dashboard.
            </p>

            <div className="relative">
              <input
                type={showToken ? "text" : "password"}
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="Access token"
                className="w-full rounded-none border border-border bg-background/70 px-3 py-2 pr-16 text-sm font-mono-ui focus:outline-none focus:ring-1 focus:ring-ring"
                autoComplete="current-password"
                autoFocus
                disabled={loading}
              />
              <button
                type="button"
                onClick={() => setShowToken((v) => !v)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-xs font-mono-ui opacity-60 hover:opacity-100"
              >
                {showToken ? "hide" : "show"}
              </button>
            </div>

            {(localError || error) && (
              <p className="text-sm text-destructive">{localError || error}</p>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full rounded-none border border-border px-3 py-2 text-sm font-sans font-semibold uppercase tracking-wide bg-foreground text-background disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {loading ? "Checking..." : "Login"}
            </button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
