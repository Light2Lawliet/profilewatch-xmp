import { useEffect, useState } from "react";
import { supabase, fetchMyAccount, type OwnedAccount } from "@/lib/supabase";
import {
  getAccountView,
  explainIncidents,
  generateSavePlay,
  generateSuccessPlan,
  type AccountView,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

function statusVariant(score: number): "healthy" | "warn" | "critical" {
  if (score >= 80) return "healthy";
  if (score >= 60) return "warn";
  return "critical";
}

export function Dashboard() {
  const [account, setAccount] = useState<OwnedAccount | null>(null);
  const [view, setView] = useState<AccountView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [panel, setPanel] = useState<{ label: string; body: unknown } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    fetchMyAccount()
      .then((a) => {
        setAccount(a);
        if (a) return getAccountView(a.id).then(setView);
      })
      .catch((e) => setError(e.message));
  }, []);

  async function runAction(label: string, fn: (id: string) => Promise<unknown>) {
    if (!account) return;
    setBusy(label);
    setPanel(null);
    try {
      const body = await fn(account.id);
      setPanel({ label, body });
    } catch (e) {
      setPanel({ label, body: { error: (e as Error).message } });
    } finally {
      setBusy(null);
    }
  }

  if (error) {
    return <div className="p-6 text-[--bad]">{error}</div>;
  }
  if (!account) {
    return <div className="p-6 text-[--ink-2]">Loading your profile…</div>;
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">{account.business_name}</h1>
          <p className="text-sm text-[--ink-2]">{account.category ?? "—"} · {account.account_type}</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => supabase.auth.signOut()}>
          Sign out
        </Button>
      </div>

      {view && (
        <Card>
          <CardHeader>
            <CardTitle>Health score</CardTitle>
            <Badge variant={statusVariant(view.health_score)}>{view.health_score}</Badge>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              {Object.entries(view.score_components).map(([k, v]) => (
                <div key={k} className="rounded-md bg-white/5 px-3 py-2">
                  <div className="text-xs text-[--ink-2]">{k.replace(/_/g, " ")}</div>
                  <div className="text-sm font-medium text-[--ink]">{v}</div>
                </div>
              ))}
            </div>
            {view.churn_risk.churn_risk && (
              <p className="text-sm text-[--warn]">Churn risk: {view.churn_risk.churn_risk_level}</p>
            )}
          </CardContent>
        </Card>
      )}

      {view && view.incidents.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Open incidents</CardTitle>
          </CardHeader>
          <CardContent>
            {view.incidents.map((i, idx) => (
              <div key={idx} className="rounded-md bg-white/5 px-3 py-2">
                <Badge variant={i.severity === "high" ? "critical" : "warn"}>{i.type.replace(/_/g, " ")}</Badge>
                <p className="mt-1 text-sm">{i.summary}</p>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Reviews &amp; listings</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs uppercase tracking-wide text-[--ink-2]">Reviews ({account.reviews.length})</p>
          {account.reviews.map((r) => (
            <div key={r.id} className="rounded-md bg-white/5 px-3 py-2">
              <div className="flex justify-between text-xs text-[--ink-2]">
                <span>{r.rating}★ · {r.date}</span>
                <span>{r.responded ? "Responded" : "Unanswered"}</span>
              </div>
              <p className="text-sm">{r.text}</p>
            </div>
          ))}
          <p className="pt-2 text-xs uppercase tracking-wide text-[--ink-2]">Listings ({account.listings.length})</p>
          {account.listings.map((l, idx) => (
            <div key={idx} className="rounded-md bg-white/5 px-3 py-2 text-sm">
              <span className="text-[--ink-2]">{l.directory}:</span> {l.name}, {l.address}, {l.phone}
            </div>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>AI actions</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={busy !== null} onClick={() => runAction("Explain", explainIncidents)}>
              {busy === "Explain" ? "Working…" : "Explain & draft response"}
            </Button>
            <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => runAction("Save play", generateSavePlay)}>
              {busy === "Save play" ? "Working…" : "Generate save play"}
            </Button>
            <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => runAction("Success plan", generateSuccessPlan)}>
              {busy === "Success plan" ? "Working…" : "Get success plan"}
            </Button>
          </div>
          {panel && (
            <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap rounded-md bg-black/30 p-3 text-xs">
              {JSON.stringify(panel.body, null, 2)}
            </pre>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
