// Talks to the existing FastAPI + Groq service — unchanged from the
// original ProfileWatch build. This app is the "API integration" half of
// the stack: Supabase/RLS decides *which* account you're allowed to know
// about, and this service computes and explains that account's health.
const API_BASE = import.meta.env.VITE_API_BASE as string;

function accessKey(): string | null {
  return localStorage.getItem("profilewatch_access_key");
}

async function request<T>(path: string, opts?: RequestInit): Promise<T> {
  const key = accessKey();
  const headers = { ...(opts?.headers ?? {}), ...(key ? { "X-Access-Key": key } : {}) };
  const res = await fetch(API_BASE + path, { ...opts, headers });
  if (res.status === 401) {
    const entered = window.prompt("Enter the ProfileWatch access key for AI actions:");
    if (entered) {
      localStorage.setItem("profilewatch_access_key", entered.trim());
      return request<T>(path, opts);
    }
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new Error(body?.detail ?? `Request failed (${res.status})`);
  return body as T;
}

export interface AccountView {
  id: string;
  business_name: string;
  health_score: number;
  score_components: Record<string, number>;
  incidents: { type: string; severity: string; summary: string }[];
  churn_risk: { churn_risk: boolean; churn_risk_level: string };
}

export const getAccountView = (id: string) => request<AccountView>(`/api/accounts/${id}`);

export const explainIncidents = (id: string) =>
  request(`/api/accounts/${id}/explain`, { method: "POST" });

export const generateSavePlay = (id: string) =>
  request(`/api/accounts/${id}/save-play`, { method: "POST" });

export const generateSuccessPlan = (id: string) =>
  request(`/api/accounts/${id}/success-plan`, { method: "POST" });
