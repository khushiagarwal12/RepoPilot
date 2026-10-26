export type JobStatus = "pending" | "running" | "succeeded" | "failed";
export interface Job { id: number; status: JobStatus; log?: string | null }
export interface RepoSummary {
  id: number; name: string; github_url: string;
  latest_job: { id: number; status: JobStatus } | null; indexed: boolean;
}
export interface Citation {
  n: number; chunk_id: number; file_path: string; start_line: number; end_line: number;
  snippet: string; score: number;
}
export interface AskResponse {
  status: "answered" | "insufficient_evidence"; answer: string | null;
  citations: Citation[]; index_job_id: number; model: string | null;
}

export const API_URL: string = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

const GITHUB_URL = /^https:\/\/(www\.)?github\.com\/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}\/?$/;

/** Mirrors the backend rule (https://github.com/<owner>/<repo>); the server stays the authority. */
export function validateGithubUrl(url: string): string | null {
  const u = url.trim();
  if (!u) return "Enter a GitHub repository URL.";
  if (!GITHUB_URL.test(u)) return "Use the form https://github.com/<owner>/<repo>.";
  return null;
}

/** FastAPI errors: {detail: string} or {detail: [{msg, loc}]}. */
export function formatDetail(body: unknown, status: number): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e) => (e as { msg?: string }).msg ?? "Invalid input").join("; ");
  return `Request failed (HTTP ${status}).`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, `Cannot reach the backend at ${API_URL}. Is it running?`);
  }
  let body: unknown = null;
  try { body = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) throw new ApiError(res.status, formatDetail(body, res.status));
  return body as T;
}

export const listRepos = () => request<RepoSummary[]>("/repositories/");
export const submitRepo = (github_url: string) =>
  request<{ repository_id: number; job_id: number; message: string }>("/repositories/", {
    method: "POST", body: JSON.stringify({ github_url }),
  });
export const getJobs = (repoId: number) => request<Job[]>(`/repositories/${repoId}/jobs`);
export const ask = (repoId: number, question: string, top_k = 8) =>
  request<AskResponse>(`/repositories/${repoId}/ask`, { method: "POST", body: JSON.stringify({ question, top_k }) });
