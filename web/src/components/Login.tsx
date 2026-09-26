import { useState } from "react";
import { supabase } from "@/lib/supabase";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function Login() {
  const [email, setEmail] = useState("demo@profilewatch.dev");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    setBusy(false);
    if (error) setError(error.message);
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>Sign in to your profile</CardTitle>
        </CardHeader>
        <CardContent>
          <form className="space-y-3" onSubmit={onSubmit}>
            <input
              className="w-full rounded-md border border-[--line] bg-black/20 px-3 py-2 text-sm text-[--ink]"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@company.com"
            />
            <input
              className="w-full rounded-md border border-[--line] bg-black/20 px-3 py-2 text-sm text-[--ink]"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Password"
            />
            {error && <p className="text-sm text-[--bad]">{error}</p>}
            <Button className="w-full" type="submit" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
          <p className="mt-4 text-xs text-[--ink-2]">
            Demo login: <span className="text-[--ink]">demo@profilewatch.dev</span> — password set by{" "}
            <code>api/scripts/migrate_to_relational.py</code>. Row-level security means this account can only ever
            see its own profile, never anyone else's.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
