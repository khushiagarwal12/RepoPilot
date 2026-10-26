import { useCallback, useEffect, useState } from "react";
import {
  ApiError, AskResponse, Job, RepoSummary, ask, getJobs, listRepos, submitRepo, validateGithubUrl,
} from "./api";

const ACTIVE = ["pending", "running"];
const errText = (e: unknown) => (e instanceof ApiError ? e.message : "Unexpected error.");

/** Render the answer as plain text; [n] becomes a link only if citation n exists. */
export function AnswerText({ text, known }: { text: string; known: Set<number> }) {
  return (
    <p className="answer">
      {text.split(/(\[\d+\])/).map((part, i) => {
        const m = /^\[(\d+)\]$/.exec(part);
        return m && known.has(Number(m[1])) ? <a key={i} href={`#cite-${m[1]}`}>{part}</a> : <span key={i}>{part}</span>;
      })}
    </p>
  );
}

export default function App() {
  const [repos, setRepos] = useState<RepoSummary[] | null>(null);
  const [listError, setListError] = useState("");
  const [url, setUrl] = useState("");
  const [urlError, setUrlError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState("");
  const [result, setResult] = useState<AskResponse | null>(null);

  const refresh = useCallback(async () => {
    try {
      setRepos(await listRepos());
      setListError("");
    } catch (e) {
      setListError(errText(e));
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const repo = repos?.find((r) => r.id === selected) ?? null;
  const active = !!repo?.latest_job && ACTIVE.includes(repo.latest_job.status);

  // Poll the real job status while a job is pending/running. No progress is invented.
  useEffect(() => {
    if (selected === null) { setJob(null); return; }
    let stop = false;
    const tick = async () => {
      try {
        const jobs = await getJobs(selected);
        if (stop) return;
        setJob(jobs[0] ?? null);
        if (jobs[0] && !ACTIVE.includes(jobs[0].status)) void refresh();
      } catch (e) {
        if (!stop) setListError(errText(e));
      }
    };
    void tick();
    if (!active) return () => { stop = true; };
    const id = setInterval(tick, 2000);
    return () => { stop = true; clearInterval(id); };
  }, [selected, active, refresh]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const err = validateGithubUrl(url);
    setUrlError(err ?? "");
    if (err) return;
    setSubmitting(true);
    try {
      const r = await submitRepo(url.trim());
      setUrl("");
      await refresh();
      setSelected(r.repository_id);
    } catch (e2) {
      setUrlError(errText(e2));
    } finally {
      setSubmitting(false);
    }
  }

  async function onAsk(e: React.FormEvent) {
    e.preventDefault();
    if (!repo || !question.trim()) return;
    setAsking(true); setAskError(""); setResult(null);
    try {
      setResult(await ask(repo.id, question.trim()));
    } catch (e2) {
      setAskError(errText(e2));
    } finally {
      setAsking(false);
    }
  }

  return (
    <main>
      <h1>RepoPilot</h1>
      <p className="muted">Index a public Python repository, then ask questions answered only from its code.</p>

      <form onSubmit={onSubmit} aria-label="Add repository">
        <label htmlFor="url">GitHub repository URL</label>
        <div className="row">
          <input id="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/psf/requests" />
          <button disabled={submitting}>{submitting ? "Submitting…" : "Index"}</button>
        </div>
        {urlError && <p role="alert" className="error">{urlError}</p>}
      </form>

      <section aria-label="Repositories">
        <h2>Repositories</h2>
        {listError && <p role="alert" className="error">{listError}</p>}
        {repos === null && !listError && <p className="muted">Loading…</p>}
        {repos?.length === 0 && <p className="muted">No repositories yet. Submit a GitHub URL above.</p>}
        <ul className="repos">
          {repos?.map((r) => (
            <li key={r.id}>
              <button className={r.id === selected ? "repo selected" : "repo"} onClick={() => { setSelected(r.id); setResult(null); setAskError(""); }}>
                <span>{r.name}</span>
                <span className={`badge ${r.latest_job?.status ?? "none"}`}>{r.latest_job?.status ?? "no job"}</span>
              </button>
            </li>
          ))}
        </ul>
      </section>

      {repo && (
        <section aria-label="Selected repository">
          <h2>{repo.name}</h2>
          {active && <p role="status">Indexing in progress ({repo.latest_job?.status}). The backend reports no percentage; this page updates every 2 seconds.</p>}
          {job?.status === "failed" && <p role="alert" className="error">Indexing failed: {job.log ?? "no details"}</p>}
          {job?.status === "succeeded" && <p className="muted">{job.log}</p>}
          {!repo.indexed && !active && <p className="muted">Not indexed yet, so questions are disabled.</p>}
          {repo.indexed && job?.status === "failed" && <p className="muted">The previous successful index is still used for answers.</p>}

          <form onSubmit={onAsk} aria-label="Ask a question">
            <label htmlFor="q">Question</label>
            <textarea id="q" rows={3} value={question} maxLength={2000} disabled={!repo.indexed}
              onChange={(e) => setQuestion(e.target.value)} placeholder="How are redirects handled?" />
            <button disabled={!repo.indexed || asking || !question.trim()}>{asking ? "Thinking…" : "Ask"}</button>
          </form>
          {askError && <p role="alert" className="error">{askError}</p>}

          {result?.status === "insufficient_evidence" && (
            <p role="status" className="notice">Not enough evidence in the indexed code to answer this question.</p>
          )}
          {result?.status === "answered" && result.answer && (
            <div>
              <AnswerText text={result.answer} known={new Set(result.citations.map((c) => c.n))} />
              <h3>Sources</h3>
              <ol className="citations">
                {result.citations.map((c) => (
                  <li key={c.n} id={`cite-${c.n}`} value={c.n}>
                    <code>{c.file_path}:{c.start_line}-{c.end_line}</code> <span className="muted">similarity {c.score.toFixed(2)}</span>
                    <pre>{c.snippet}</pre>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </section>
      )}
    </main>
  );
}
