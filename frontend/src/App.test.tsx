import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import App, { AnswerText } from "./App";

type Route = (init?: RequestInit) => unknown | Promise<unknown>;
function mockApi(routes: Record<string, Route>) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${new URL(url).pathname}`;
    const r = routes[key];
    if (!r) return new Response(JSON.stringify({ detail: `no mock for ${key}` }), { status: 500 });
    const out = await r(init);
    return out instanceof Response ? out : new Response(JSON.stringify(out), { status: 200 });
  }));
}
const repo = (over = {}) => ({ id: 1, name: "psf/requests", github_url: "https://github.com/psf/requests",
  latest_job: { id: 5, status: "succeeded" }, indexed: true, ...over });

beforeEach(() => vi.useRealTimers());
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("App", () => {
  it("shows the empty state", async () => {
    mockApi({ "GET /repositories/": () => [] });
    render(<App />);
    expect(await screen.findByText(/No repositories yet/)).toBeTruthy();
  });

  it("shows a network error when the backend is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("down")));
    render(<App />);
    expect((await screen.findByRole("alert")).textContent).toContain("Cannot reach the backend");
  });

  it("rejects an invalid URL without calling the API", async () => {
    mockApi({ "GET /repositories/": () => [] });
    render(<App />);
    await screen.findByText(/No repositories yet/);
    fireEvent.change(screen.getByLabelText("GitHub repository URL"), { target: { value: "https://evil.com/a/b" } });
    fireEvent.click(screen.getByText("Index"));
    expect((await screen.findByRole("alert")).textContent).toContain("https://github.com/<owner>/<repo>");
    expect((fetch as ReturnType<typeof vi.fn>).mock.calls.filter(([, i]) => i?.method === "POST")).toHaveLength(0);
  });

  it("shows the real failure log for a failed job", async () => {
    mockApi({
      "GET /repositories/": () => [repo({ indexed: false, latest_job: { id: 5, status: "failed" } })],
      "GET /repositories/1/jobs": () => [{ id: 5, status: "failed", log: "Ingestion failed during vector indexing: boom" }],
    });
    render(<App />);
    fireEvent.click(await screen.findByText("psf/requests"));
    expect((await screen.findAllByRole("alert")).some((a) => a.textContent?.includes("vector indexing: boom"))).toBe(true);
    expect((screen.getByLabelText("Question") as HTMLTextAreaElement).disabled).toBe(true);
  });

  it("renders a grounded answer with citations as plain text", async () => {
    mockApi({
      "GET /repositories/": () => [repo()],
      "GET /repositories/1/jobs": () => [{ id: 5, status: "succeeded", log: "Successfully ingested." }],
      "POST /repositories/1/ask": () => ({
        status: "answered", answer: "Redirects are resolved in sessions [1]. <img src=x onerror=alert(1)>", index_job_id: 5, model: "m",
        citations: [{ n: 1, chunk_id: 9, file_path: "src/requests/sessions.py", start_line: 10, end_line: 40, snippet: "def resolve_redirects(): <b>x</b>", score: 0.61 }],
      }),
    });
    const { container } = render(<App />);
    fireEvent.click(await screen.findByText("psf/requests"));
    fireEvent.change(await screen.findByLabelText("Question"), { target: { value: "How are redirects handled?" } });
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByText("src/requests/sessions.py:10-40")).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();      // markup is shown as text, never injected
    expect(container.querySelector("pre")?.textContent).toContain("<b>x</b>");
    expect(container.querySelector('a[href="#cite-1"]')).toBeTruthy();
  });

  it("shows insufficient evidence and API errors distinctly", async () => {
    let n = 0;
    mockApi({
      "GET /repositories/": () => [repo()],
      "GET /repositories/1/jobs": () => [{ id: 5, status: "succeeded", log: "ok" }],
      "POST /repositories/1/ask": () => (n++ === 0
        ? { status: "insufficient_evidence", answer: null, citations: [], index_job_id: 5, model: null }
        : new Response(JSON.stringify({ detail: "LLM unavailable: not configured" }), { status: 503 })),
    });
    render(<App />);
    fireEvent.click(await screen.findByText("psf/requests"));
    const box = await screen.findByLabelText("Question");
    fireEvent.change(box, { target: { value: "capital of France?" } });
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByText(/Not enough evidence/)).toBeTruthy();
    fireEvent.click(screen.getByText("Ask"));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("LLM unavailable"));
  });

  it("does not fabricate progress while indexing", async () => {
    mockApi({
      "GET /repositories/": () => [repo({ indexed: false, latest_job: { id: 6, status: "running" } })],
      "GET /repositories/1/jobs": () => [{ id: 6, status: "running", log: null }],
    });
    const { container } = render(<App />);
    fireEvent.click(await screen.findByText("psf/requests"));
    expect((await screen.findByRole("status")).textContent).toContain("no percentage");
    expect(container.querySelector("progress")).toBeNull();
  });
});

describe("AnswerText", () => {
  it("links only known citation numbers", () => {
    const { container } = render(<AnswerText text="a [1] b [7]" known={new Set([1])} />);
    expect(container.querySelectorAll("a")).toHaveLength(1);
  });
});
