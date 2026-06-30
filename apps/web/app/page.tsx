"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type FileInfo = {
  file_id: string;
  filename: string;
  source_ext: string;
  size_bytes: number;
  allowed_targets: string[];
};

type Job = {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "expired";
  progress: number;
  message: string | null;
  target_ext: string;
  result: {
    download_url: string;
    filename: string;
    size_bytes: number;
    quality_notice?: string | null;
  } | null;
  error: { code: string; message: string } | null;
};

const STATUS_LABEL: Record<Job["status"], string> = {
  queued: "排队中",
  running: "转换中",
  succeeded: "转换成功",
  failed: "转换失败",
  expired: "已过期",
};

export default function Page() {
  const [file, setFile] = useState<FileInfo | null>(null);
  const [uploading, setUploading] = useState(false);
  const [target, setTarget] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [recent, setRecent] = useState<Job[]>([]);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    try {
      setRecent(JSON.parse(localStorage.getItem("recent_jobs") || "[]"));
    } catch {
      /* ignore */
    }
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, []);

  const upload = useCallback(async (f: File) => {
    setUploading(true);
    setError(null);
    setFile(null);
    setJob(null);
    try {
      const fd = new FormData();
      fd.append("file", f);
      const r = await fetch(`${API}/api/files`, { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok) throw new Error(d?.error?.message || "上传失败");
      setFile(d);
      setTarget(d.allowed_targets[0] || "");
    } catch (e) {
      setError(e instanceof Error ? e.message : "上传失败");
    } finally {
      setUploading(false);
    }
  }, []);

  const poll = useCallback((jobId: string) => {
    if (timer.current) clearInterval(timer.current);
    timer.current = setInterval(async () => {
      try {
        const r = await fetch(`${API}/api/jobs/${jobId}`);
        const d: Job = await r.json();
        setJob(d);
        if (d.status === "succeeded" || d.status === "failed" || d.status === "expired") {
          if (timer.current) clearInterval(timer.current);
          setRecent((prev) => {
            const next = [d, ...prev.filter((x) => x.job_id !== d.job_id)].slice(0, 10);
            localStorage.setItem("recent_jobs", JSON.stringify(next));
            return next;
          });
        }
      } catch {
        /* keep polling */
      }
    }, 2000);
  }, []);

  const createJob = useCallback(async () => {
    if (!file || !target) return;
    setError(null);
    setJob(null);
    try {
      const r = await fetch(`${API}/api/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_id: file.file_id, target_ext: target }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d?.error?.message || "创建任务失败");
      poll(d.job_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "创建任务失败");
    }
  }, [file, target, poll]);

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header className="mb-8">
        <h1 className="text-2xl font-bold">多模态文件转换</h1>
        <p className="text-slate-500">支持文档、图片、音频、视频的格式转换与智能内容提取</p>
      </header>

      <section
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const f = e.dataTransfer.files[0];
          if (f) upload(f);
        }}
        className={`rounded-xl border-2 border-dashed p-8 text-center transition ${
          dragging ? "border-blue-500 bg-blue-50" : "border-slate-300 bg-white"
        }`}
      >
        <p className="mb-3 text-slate-600">拖拽文件到此处，或</p>
        <label className="inline-block cursor-pointer rounded-lg bg-slate-900 px-4 py-2 text-white hover:bg-slate-700">
          选择文件
          <input
            type="file"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) upload(f);
            }}
          />
        </label>
        {uploading && <p className="mt-3 text-slate-500">上传中…</p>}
      </section>

      {error && (
        <div className="mt-4 rounded-lg border border-red-300 bg-red-50 p-3 text-red-700">{error}</div>
      )}

      {file && (
        <section className="mt-6 rounded-xl border border-slate-200 bg-white p-5">
          <h2 className="mb-2 font-semibold">文件信息</h2>
          <ul className="mb-4 text-sm text-slate-600">
            <li>文件名：{file.filename}</li>
            <li>源格式：{file.source_ext}</li>
            <li>大小：{(file.size_bytes / 1024).toFixed(1)} KB</li>
          </ul>
          <h3 className="mb-2 text-sm font-semibold">选择目标格式</h3>
          <div className="mb-4 flex flex-wrap gap-2">
            {file.allowed_targets.map((t) => (
              <button
                key={t}
                onClick={() => setTarget(t)}
                className={`rounded-full px-3 py-1 text-sm ${
                  target === t ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-700"
                }`}
              >
                {t}
              </button>
            ))}
            {file.allowed_targets.length === 0 && (
              <span className="text-sm text-slate-400">无可转换目标</span>
            )}
          </div>
          <button
            onClick={createJob}
            disabled={!target}
            className="rounded-lg bg-emerald-600 px-4 py-2 text-white disabled:opacity-40"
          >
            开始转换
          </button>
        </section>
      )}

      {job && (
        <section className="mt-6 rounded-xl border border-slate-200 bg-white p-5">
          <h2 className="mb-2 font-semibold">任务状态</h2>
          <p className="text-sm">
            {STATUS_LABEL[job.status]}
            {job.message ? ` · ${job.message}` : ""}
            {job.status === "running" ? ` · ${job.progress}%` : ""}
          </p>
          {job.result && (
            <div className="mt-3">
              {job.result.quality_notice && (
                <p className="mb-2 rounded bg-amber-50 p-2 text-xs text-amber-700">
                  {job.result.quality_notice}
                </p>
              )}
              <a
                href={`${API}${job.result.download_url}`}
                className="inline-block rounded-lg bg-slate-900 px-4 py-2 text-white"
              >
                下载 {job.result.filename}
              </a>
            </div>
          )}
          {job.error && (
            <p className="mt-2 text-sm text-red-600">
              {job.error.code}：{job.error.message}
            </p>
          )}
        </section>
      )}

      {recent.length > 0 && (
        <section className="mt-6 rounded-xl border border-slate-200 bg-white p-5">
          <h2 className="mb-2 font-semibold">最近任务</h2>
          <ul className="space-y-1 text-sm">
            {recent.map((j) => (
              <li key={j.job_id} className="flex justify-between text-slate-600">
                <span>
                  {j.target_ext} · {STATUS_LABEL[j.status]}
                </span>
                {j.result && (
                  <a href={`${API}${j.result.download_url}`} className="text-blue-600">
                    下载
                  </a>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
